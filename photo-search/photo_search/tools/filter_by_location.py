"""filter_by_location — narrow the photo search to a named place.

Three things live in this file:

1. `DECLARATION` — the FunctionDeclaration Gemini reads to decide when
   to call this tool. The docstring + type names in the declaration are
   the *only* signal the model has about when this tool applies, so they
   need to be specific (when to call, when NOT to call, what kinds of
   arguments to emit).

2. `Args` — the Pydantic model that validates what the model emitted.
   Two-sided typing: the JSON Schema constrains the model on the way in;
   Pydantic catches anything that slipped through on the way out.

3. `execute` — the pure executor. Reads the alias map at import time,
   builds a sha-set against the index metadata, returns a `LocationFilter`.
   No Gemini calls, no I/O beyond the alias-map load.

Matching tiers (executed as a union — be generous, the retriever still
ranks by visual similarity inside the surviving set):

  Tier 1: hand-labeled `place_names` on the row (strongest signal —
          baked by place_baker.py from the Stage -1 labeling session).
  Tier 2: folder-name fallback when Tier 1 didn't fire — covers albums
          whose folder names *are* places ('Petergof', 'Lovozero10')
          even without hand labels.

Photos with no place_names AND a non-matching folder name produce no
match. They are NOT excluded from retrieval — the filter just doesn't
contribute a vote either way. (Compare: an active mask that excluded
them would penalize the entire un-labeled tail of the corpus.)
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from google.genai import types
from pydantic import BaseModel, Field

from photo_search.tools.base import LocationFilter

_ALIASES_PATH: Path = Path(__file__).parent.parent / "data" / "place_aliases.json"


def _norm(s: str) -> str:
    """Lowercase + NFKC-normalize. Cyrillic preserved; whitespace trimmed."""
    return unicodedata.normalize("NFKC", s).strip().lower()


def _load_aliases() -> dict[str, set[str]]:
    """Read place_aliases.json once at import time. Drops `_meta` block;
    lowercases every variant and also adds the canonical name itself."""
    raw = json.loads(_ALIASES_PATH.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for canonical, variants in raw.items():
        if canonical.startswith("_"):
            continue
        if not isinstance(variants, list):
            continue
        bag = {_norm(v) for v in variants}
        bag.add(_norm(canonical))
        out[canonical] = bag
    return out


ALIASES: dict[str, set[str]] = _load_aliases()


# What Gemini reads. The description and the parameter description are
# the model's only context for when/how to call this tool. Iterate here
# after watching real routing decisions, not before.
DECLARATION = types.FunctionDeclaration(
    name="filter_by_location",
    description=(
        "Narrow the photo search to photos taken at a specific named place. "
        "Call this when the user mentions a place, region, city, district, "
        "named landmark, or a known personal location (e.g. 'home', 'school', "
        "'the dacha') in their query. Examples that should trigger this tool: "
        "'photos from Хибины', 'Tokyo trip', 'at the dacha', 'around школа 239', "
        "'Petergof in spring'. Do NOT call this for purely topical or visual "
        "queries with no place mentioned ('photos of snow', 'the fish dinner', "
        "'me holding a cat'). Do NOT call this for vague non-locations "
        "('somewhere warm', 'a forest', 'a quiet place')."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "place_name": types.Schema(
                type=types.Type.STRING,
                description=(
                    "The place exactly as the user wrote it, or the closest "
                    "single noun phrase identifying the location. Russian "
                    "(Cyrillic), Latin transliteration, English names, and "
                    "personal nicknames ('home', 'the dacha', 'school') are "
                    "all acceptable — the filter handles normalization and "
                    "aliases internally. Keep it short: one place per call. "
                    "If the user mentions multiple places, call this tool "
                    "multiple times in parallel."
                ),
            ),
        },
        required=["place_name"],
    ),
)


class Args(BaseModel):
    """Validated arguments for filter_by_location.

    Pydantic catches:
      - missing place_name (required, but the schema also enforces it)
      - empty string or whitespace-only string (min_length=1)
      - pathological long input (max_length=80 — no real place needs more)
    """

    place_name: str = Field(min_length=1, max_length=80)


def _canonicals_matching_query(query_place: str) -> set[str]:
    """For each canonical place, decide whether the query touches it.

    Bidirectional substring match: alias appears in query, OR query
    appears in alias. Bidirectional is deliberate — 'Хиб' should still
    find 'Хибины'; 'photos near Купчино' should still find 'Купчино'.
    """
    qn = _norm(query_place)
    if not qn:
        return set()
    out: set[str] = set()
    for canonical, variants in ALIASES.items():
        for v in variants:
            if not v:
                continue
            if v in qn or qn in v:
                out.add(canonical)
                break
    return out


def _whole_word_in(needle: str, haystack: str) -> bool:
    """True iff `needle` appears in `haystack` flanked by non-letter chars.

    'hibiny' in 'hibiny9' → True (flanked by start-of-string and digit).
    'dacha'  in 'rodacha' → False (preceded by 'o' — same word).
    'kola'   in 'kolaholic' → False (followed by 'h' — same word).

    Used for Tier 2 folder-name matching where naive substring would
    produce false positives (the original implementation matched
    'dacha' inside 'rodacha', a different album).
    """
    if not needle or not haystack:
        return False
    n = len(needle)
    i = haystack.find(needle)
    while i != -1:
        before_ok = i == 0 or not haystack[i - 1].isalpha()
        end = i + n
        after_ok = end == len(haystack) or not haystack[end].isalpha()
        if before_ok and after_ok:
            return True
        i = haystack.find(needle, i + 1)
    return False


def execute(args: Args, metas: list[dict]) -> LocationFilter | None:
    """Return a LocationFilter masking metas to photos at args.place_name,
    or `None` when the query names a place we don't recognize.

    `None` is a deliberately distinct signal from "recognized but matched
    zero photos": the router treats `None` as 'no location filter' (let
    vector retrieval proceed unfiltered), and treats a `LocationFilter`
    with empty `matched_shas` as 'recognized place but corpus has no
    matches' (retriever returns no hits — honest empty result).

    Tiers (executed as a union):
      (1) Hand-labeled `place_names` on the row — exact normalized match.
      (2) Folder-name fallback when Tier 1 didn't fire — covers albums
          whose folder names *are* places ('Petergof', 'Lovozero10')
          even without hand labels.
    """
    canonicals = _canonicals_matching_query(args.place_name)
    if not canonicals:
        # Unknown place — caller will leave the location slot empty so
        # vector similarity runs unfiltered. A user typing 'Tokyo' (no
        # photos, no alias) gets the same UX as before the routing layer.
        return None

    canonicals_n = {_norm(c) for c in canonicals}
    matched: set[str] = set()

    for m in metas:
        # Tier 1: hand-labeled place_names — exact normalized equality.
        place_names = m.get("place_names") or []
        if any(_norm(pn) in canonicals_n for pn in place_names):
            matched.add(m["sha"])
            continue

        # Tier 2: folder-name fallback. Only consulted when Tier 1
        # didn't fire, so a hand-labeled photo never gets a weaker
        # match overriding its strong one. Whole-word match against
        # folder name — naive substring would catch e.g. 'dacha' inside
        # 'rodacha', conflating different albums.
        folder_n = _norm(m["blob_path"].split("/", 1)[0])
        for cs in canonicals:
            if any(_whole_word_in(v, folder_n) for v in ALIASES[cs]):
                matched.add(m["sha"])
                break

    return LocationFilter(place_name=args.place_name, matched_shas=frozenset(matched))
