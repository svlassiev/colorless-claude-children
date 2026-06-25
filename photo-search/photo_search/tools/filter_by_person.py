"""filter_by_person — narrow the photo search to photos of a named person.

Serving-side mirror of filter_by_location, but for people. Differences:

- The aliases are PRIVATE (person_aliases.json carries family names), so they are
  loaded at SERVER STARTUP via `load()` (after the cache is pulled) — NOT at import
  like place_aliases.json. The executor stays pure: `execute(args, metas)` reads a
  module-global reverse index.
- Matching is EXACT on the normalized query (not substring) — person_aliases.json
  already contains every declension/transliteration (baked offline by alias_expand),
  so substring matching would only cross-leak short names.
- Resolution is one-to-MANY: a query form may belong to several identities (a
  shared first name, or a shared family surname, matches more than one person).
  `execute` unions the matched_shas of every identity whose alias set contains
  the query.

Auth: this tool is only DECLARED to the router for allow-listed callers (gated in
routing.py), so anonymous users never even see that person search exists.
"""

from __future__ import annotations

import json
import unicodedata
from collections import defaultdict
from pathlib import Path

from google.genai import types
from pydantic import BaseModel, Field

from photo_search.tools.base import PersonFilter

# Populated at startup by load(); empty until then. The serving code never reads
# person_aliases.json at import time — it is private and pulled at runtime.
_ALIASES: dict[str, list[str]] = {}
_FORM_TO_NAMES: dict[str, list[str]] = {}


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s).strip().lower()


def load(path: str | Path) -> int:
    """Load person_aliases.json and build the form → identities reverse index.

    Called from the server lifespan after the cache pull. A missing file leaves
    the index empty (person search resolves nothing → queries fall back to
    ordinary visual search). Returns the identity count.
    """
    global _ALIASES, _FORM_TO_NAMES
    _ALIASES, _FORM_TO_NAMES = {}, {}
    p = Path(path)
    if not p.exists():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("person_aliases.json is not a JSON object")
        rev: dict[str, set[str]] = defaultdict(set)
        for name, forms in data.items():
            if not isinstance(forms, list):
                continue
            for f in forms:
                if isinstance(f, str):
                    rev[_norm(f)].add(name)
            rev[_norm(name)].add(name)
        _ALIASES = data
        _FORM_TO_NAMES = {k: sorted(v) for k, v in rev.items()}
        return len(_ALIASES)
    except Exception as e:  # noqa: BLE001
        # A corrupt private aliases file must DISABLE person search, never take
        # down the public service. Fail closed to an empty index.
        import sys

        print(
            f"filter_by_person: bad aliases file ({type(e).__name__}: {e}) — "
            "person search disabled",
            file=sys.stderr,
        )
        _ALIASES, _FORM_TO_NAMES = {}, {}
        return 0


def roster() -> list[str]:
    """Canonical identity names — injected into the routing prompt for allowed callers."""
    return sorted(_ALIASES.keys())


DECLARATION = types.FunctionDeclaration(
    name="filter_by_person",
    description=(
        "Narrow the photo search to photos that include a specific named person. "
        "Call this when the user names a person they want photos of — a given name, "
        "nickname, full name, or surname (e.g. 'photos of Anna', 'Ivan on the beach', "
        "'show me <a person>', 'фото Анны'). Emit the name exactly as the user "
        "wrote it; spelling, case and declension variants are handled internally. Do "
        "NOT call this for generic people words with no name ('photos of kids', 'a "
        "man', 'people'), nor for places or objects."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "person_name": types.Schema(
                type=types.Type.STRING,
                description="The person's name exactly as the user wrote it (one person per call).",
            ),
        },
        required=["person_name"],
    ),
)


class Args(BaseModel):
    """Validated arguments for filter_by_person."""

    person_name: str = Field(min_length=1, max_length=80)


def _resolve(raw: str) -> list[str] | None:
    """Resolve a written name to canonical identity name(s), or None.

    First an exact normalized match — covers first names, nicknames, surnames
    and full canonical names (every form alias_expand baked). Failing that, a
    multi-word query is read as "<given> <surname>" and the identity sets of its
    words are INTERSECTED: a "FirstName Surname" resolves to whoever is both that
    first name AND that surname. This resolves combined forms that were never
    baked as a single alias, and disambiguates a shared first name (two people
    with the same given name are separated by the surname). None when nothing
    resolves (caller falls back to visual search).
    """
    direct = _FORM_TO_NAMES.get(_norm(raw))
    if direct:
        return direct
    toks = [t for t in _norm(raw).split() if t]
    if len(toks) < 2:
        return None
    per_token = [_FORM_TO_NAMES.get(t) for t in toks]
    if not all(per_token):
        return None
    inter = set(per_token[0])
    for s in per_token[1:]:
        inter &= set(s)
    return sorted(inter) if inter else None


def execute(args: Args, metas: list[dict]) -> PersonFilter | None:
    """Resolve the query name to identities and mask metas by person_names.

    Returns None when the name resolves to no known identity — the router then
    leaves the person slot empty and vector search runs unfiltered (same UX as
    before person search existed). Otherwise returns a PersonFilter masking to
    the union of photos that carry any of the resolved identities.
    """
    names = _resolve(args.person_name)
    if not names:
        return None
    nameset = set(names)
    matched = frozenset(
        m["sha"] for m in metas if nameset & set(m.get("person_names") or [])
    )
    return PersonFilter(
        query=args.person_name, names=tuple(sorted(nameset)), matched_shas=matched
    )
