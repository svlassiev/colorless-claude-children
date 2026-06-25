"""Offline (Slice 2): expand named clusters into a private alias list.

Three sources combine into person_aliases.json (canonical name -> [normalized forms]):
  1. Gemini (one roster-aware call) — the BASE name variants in Russian (given name,
     diminutives, formal name) and transliterations / English equivalents in Latin.
  2. pymorphy3 — declines every Russian (Cyrillic) base through ALL 6 grammatical
     cases, singular AND plural, plus a colloquial-instrumental augmentation. Runs
     OFFLINE only (the serving side never imports pymorphy3 — it exact-matches this
     pre-baked list).
  3. person_extras.json (PRIVATE, gitignored, in the cache) — owner-supplied surnames,
     transliterations and handles a model can't know, plus explicit removals.

PRIVACY: person names are private data, NOT code. The canonical roster, surnames and
handles live only in private, gitignored, cache-resident files (person_extras.json +
person_aliases.json) — NEVER in tracked source. This module hard-codes no real name.
The runtime roster goes to Gemini (Vertex, which does not train on it) at bake time;
the output is private. Review with --print before relying on it.

Run:
    uv run --directory photo-search python -m photo_search.alias_expand --print
    uv run --directory photo-search python -m photo_search.alias_expand
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import pymorphy3
from google import genai
from google.genai import types

from photo_search.paths import (
    FACES_PATH,
    GENERATE_MODEL,
    LOCATION,
    PERSON_ALIASES_PATH,
    PERSON_EXTRAS_PATH,
    PROJECT,
)

CLUSTER_LABELS_PATH = FACES_PATH.parent / "cluster_labels.json"
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_morph = pymorphy3.MorphAnalyzer()

_PROMPT = """\
These are people in a personal photo collection (mixed Russian given names, some
with a surname):

{roster}

For EACH person give the distinct BASE name forms (nominative singular — do NOT
inflect; grammatical cases are generated separately by a morphology tool):
- "ru": Russian (Cyrillic) variants — the given name, common diminutives, and the
  formal full first name. Include BOTH the ё and е spelling of any name with ё.
- "lat": transliterations / English equivalents in Latin letters.

RULES:
1. Shared first name: if two listed people share a first name (e.g. a full-name
   entry and a diminutive of that same first name, or two entries with the same
   given name), put that shared first-name base in BOTH people's "ru".
2. A surnamed person keeps their surname (Cyrillic) in "ru".
3. Do NOT give a person a diminutive that is literally another listed person's label.

Output ONLY JSON mapping each name EXACTLY as written to {{"ru": [...], "lat": [...]}}.
"""


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s).strip().lower()


def _load_extras() -> tuple[dict, dict]:
    """Owner-supplied extras/exclusions from the PRIVATE person_extras.json.

    Shape: {"extras": {name: {"ru": [...], "lat": [...]}}, "exclude": {name: [...]}}.
    Absent or malformed → empty (Gemini + pymorphy only). Never tracked in git.
    """
    p = PERSON_EXTRAS_PATH
    if not p.exists():
        print(f"note: no {p.name} — proceeding without owner extras", file=sys.stderr)
        return {}, {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return dict(d.get("extras") or {}), dict(d.get("exclude") or {})
    except Exception as e:  # noqa: BLE001
        print(f"warn: bad {p.name} ({type(e).__name__}: {e}) — ignoring extras", file=sys.stderr)
        return {}, {}


def _decline(word: str) -> set[str]:
    """All inflected forms of a single Russian word (every case, both numbers).

    Prefers parses tagged as a name/surname/patronymic; falls back to the most
    probable parse. Multi-word strings and non-Cyrillic words are kept verbatim.
    """
    out = {_norm(word)}
    w = word.strip()
    if not w or " " in w or not _CYRILLIC.search(w):
        return out
    parses = _morph.parse(w)
    named = [p for p in parses if ("Name" in p.tag or "Surn" in p.tag or "Patr" in p.tag)]
    for p in named or parses[:1]:
        for f in p.lexeme:
            out.add(_norm(f.word))
    return out


def _colloquial(forms: set[str]) -> set[str]:
    """Colloquial instrumental forms pymorphy3 doesn't emit. Hushing-stem names have
    a spoken instrumental -ой beside the standard -ей (e.g. ...шей -> ...шой).
    Over-generation is harmless — an unused alias just sits there."""
    extra: set[str] = set()
    for f in forms:
        for end, alt in (("жей", "жой"), ("шей", "шой"), ("чей", "чой"), ("щей", "щой")):
            if f.endswith(end):
                extra.add(f[: -len(end)] + alt)
    return extra


def main() -> int:
    ap = argparse.ArgumentParser(prog="photo-search-alias-expand")
    ap.add_argument("--print", action="store_true", help="print result + overlaps, do not write")
    ap.add_argument("--out", default=str(PERSON_ALIASES_PATH))
    args = ap.parse_args()

    if not CLUSTER_LABELS_PATH.exists():
        print(f"missing {CLUSTER_LABELS_PATH} — run face naming first", file=sys.stderr)
        return 1
    labels = json.loads(CLUSTER_LABELS_PATH.read_text(encoding="utf-8"))
    names = sorted(set(labels.values()))
    extras, exclude = _load_extras()

    print(f"expanding {len(names)} names via {GENERATE_MODEL} + pymorphy3...", file=sys.stderr)
    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    resp = client.models.generate_content(
        model=GENERATE_MODEL,
        contents=_PROMPT.format(roster="\n".join(f"- {n}" for n in names)),
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2),
    )
    raw = json.loads(resp.text)

    out: dict[str, list[str]] = {}
    for name in names:
        g = raw.get(name, {})
        ru_bases = set(g.get("ru", [])) | set(extras.get(name, {}).get("ru", []))
        ru_bases |= {tok for tok in name.split() if _CYRILLIC.search(tok)}
        excl = {_norm(w) for w in exclude.get(name, [])}
        ru_bases = {w for w in ru_bases if _norm(w) not in excl}

        forms: set[str] = set()
        for w in ru_bases:
            forms |= _decline(w)
        forms |= _colloquial(forms)
        forms |= {_norm(w) for w in g.get("lat", [])}
        forms |= {_norm(w) for w in extras.get(name, {}).get("lat", [])}
        forms.add(_norm(name))

        for w in exclude.get(name, []):
            forms -= _decline(w)

        out[name] = sorted(forms)

    alias_to_names: dict[str, list[str]] = defaultdict(list)
    for name, forms in out.items():
        for f in forms:
            alias_to_names[f].append(name)
    overlaps = {a: ns for a, ns in alias_to_names.items() if len(ns) > 1}

    print(f"\n=== {len(out)} identities, {sum(len(v) for v in out.values())} alias forms ===", file=sys.stderr)
    grouped: dict[str, list[str]] = defaultdict(list)
    for a, ns in overlaps.items():
        grouped[" + ".join(sorted(ns))].append(a)
    print("shared-alias groups (one query -> several people):", file=sys.stderr)
    for who, forms in sorted(grouped.items()):
        print(f"  {who}: {len(forms)} shared forms", file=sys.stderr)

    if args.print:
        for name, forms in out.items():
            print(f"  {name}  ({len(forms)}): {', '.join(forms)}", file=sys.stderr)
        print("\n--print: not writing", file=sys.stderr)
        return 0

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out} (private, gitignored)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
