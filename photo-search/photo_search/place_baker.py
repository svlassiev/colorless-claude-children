"""Bake place labels into manifest.jsonl from data/place_labels.json.

For each photo row in manifest.jsonl, resolve (album, n) — where n is the
1-indexed position in the album's alphabetically-sorted file list, matching
preview.html's numbering — against the labels file. First matching range
wins; the album's default applies as fallback. Albums absent from the
labels file, or photos whose n falls outside any range with no default,
are left with no place_* fields (strict policy).

Writes augmented rows back to manifest.jsonl atomically (temp file + rename).
Idempotent: existing place_* fields are stripped first, so re-running with
an updated labels file produces clean output.

Run:  uv run --directory photo-search python -m photo_search.place_baker
      uv run --directory photo-search python -m photo_search.place_baker --dry-run

Workflow:
  1. python -m photo_search.place_baker         # augments manifest.jsonl
  2. python -m photo_search.embedder            # rewrites manifest_meta.jsonl
                                                # (cache hits all SHAs — no
                                                #  new embeddings, no cost)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from photo_search.paths import MANIFEST_PATH

# Sibling of this module: photo_search/data/place_labels.json
LABELS_PATH: Path = Path(__file__).parent / "data" / "place_labels.json"
# Gazetteer produced by coord_resolver: short-link → {lat, lng, source, name}.
COORDS_PATH: Path = Path(__file__).parent / "data" / "place_coords.json"

_SHORTLINK_RE = re.compile(r"https?://maps\.app\.goo\.gl/\S+")

# Fields the baker owns end-to-end. Stripped on each run before re-applying,
# so the manifest never accumulates stale place data from earlier labels.
PLACE_FIELDS = (
    "place_names",
    "place_detail",
    "place_approximate",
    "place_context",
    "place_coord",
    "place_coord_source",
)


def _expand_n(spec: list) -> set[int]:
    """Expand a mixed n-spec into a set of ints.

    Accepts entries that are either:
      - int            → single photo number
      - [lo, hi]       → inclusive range
    Order within the spec does not matter; duplicates are fine.
    """
    out: set[int] = set()
    for item in spec:
        if isinstance(item, int):
            out.add(item)
        elif isinstance(item, list) and len(item) == 2 and all(isinstance(x, int) for x in item):
            lo, hi = item
            out.update(range(lo, hi + 1))
        else:
            raise ValueError(f"bad n-spec entry: {item!r}")
    return out


def _build_files_map(rows: list[dict]) -> dict[str, list[str]]:
    """folder → alphabetically-sorted list of filenames in that folder.

    The bake-time sort must match the sort used to produce the n in
    place_labels.json (which came from preview.html, which uses
    Array.sort()'s default lexicographic order on filenames). Python's
    list.sort() on strings is the same lexicographic ASCII compare — good.
    """
    files_by_folder: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        folder, _, fname = r["blob_path"].partition("/")
        if not fname:
            continue
        files_by_folder[folder].append(fname)
    for folder in files_by_folder:
        files_by_folder[folder].sort()
    return files_by_folder


def _resolve_for_photo(
    album_entry: dict, n: int
) -> dict | None:
    """Pick the matching range or default for photo number n.

    First range whose expanded n-set contains n wins. If none matches, the
    album-level default applies. Returns None to signal 'no label' (strict).
    """
    for r in album_entry.get("ranges", []):
        if n in _expand_n(r["n"]):
            return r
    return album_entry.get("default")


def _pin_coords(detail: str, coords_map: dict) -> list[list[float]]:
    """All resolved [lat, lng] for the maps.app.goo.gl links in `detail`."""
    out: list[list[float]] = []
    for raw in _SHORTLINK_RE.findall(detail or ""):
        entry = coords_map.get(raw.rstrip(".,);"))
        if entry and "lat" in entry and "lng" in entry:
            out.append([entry["lat"], entry["lng"]])
    return out


def _pin_coord(match: dict, coords_map: dict) -> list[float] | None:
    """The [lat, lng] of the first resolved pin in a range/default's detail.

    Ranges with multiple links are rare; the first is the representative spot.
    """
    pins = _pin_coords(match.get("place_detail") or "", coords_map)
    return pins[0] if pins else None


def _build_name_coords(labels: dict, coords_map: dict) -> dict[str, list[list[float]]]:
    """place_name → every resolved pin coordinate tagged with that name,
    gathered across all albums' ranges and defaults.

    Used for the canonical fallback: a range labeled with a name but lacking
    its own inline pin borrows the centroid of that name's pins from wherever
    they were dropped. Tight clusters (Хибины) give a precise centroid; a
    scattered city (Санкт-Петербург) gives its center — coarse but honest.
    """
    out: dict[str, list[list[float]]] = defaultdict(list)
    for album in labels.get("albums", {}).values():
        for entry in [album.get("default", {}), *album.get("ranges", [])]:
            pins = _pin_coords(entry.get("place_detail") or "", coords_map)
            for name in entry.get("place_names") or []:
                out[name].extend(pins)
    return out


def _centroid(points: list[list[float]]) -> list[float] | None:
    if not points:
        return None
    n = len(points)
    return [sum(p[0] for p in points) / n, sum(p[1] for p in points) / n]


def _apply_match(
    row: dict,
    match: dict,
    context: str | None,
    coords_map: dict,
    name_coords: dict,
) -> None:
    """Stamp place_* fields onto the row from a resolved range/default entry.

    Coordinate precedence: a photo's own EXIF GPS wins (it's where the shot
    was actually taken); otherwise the labeled range's hand-dropped pin is
    the fallback. Photos with neither get no place_coord — they stay
    findable by name via filter_by_location, just not by distance.
    """
    place_names = list(match.get("place_names", []))
    # Empty list means 'transit / unknown' — still a deliberate label, write it.
    row["place_names"] = place_names
    detail = match.get("place_detail")
    if detail:
        row["place_detail"] = detail
    if match.get("approximate"):
        row["place_approximate"] = True
    if context:
        row["place_context"] = context

    # Coordinate precedence (EXIF is stamped earlier in the bake loop):
    #   2. the range's own inline pin
    #   3. canonical fallback — centroid of the first place_name's pins from
    #      anywhere (covers ranges that reuse a place without re-dropping the
    #      pin, e.g. 'Санкт-Петербург'-only ranges → city centroid).
    if "place_coord" not in row:
        pin = _pin_coord(match, coords_map)
        if pin is not None:
            row["place_coord"] = pin
            row["place_coord_source"] = "pin"
        else:
            names = match.get("place_names") or []
            cen = _centroid(name_coords.get(names[0], [])) if names else None
            if cen is not None:
                row["place_coord"] = cen
                row["place_coord_source"] = "canonical"


def bake(rows: list[dict], labels: dict, coords_map: dict | None = None) -> dict[str, Any]:
    """Mutate rows in place; return a coverage summary."""
    files_map = _build_files_map(rows)
    albums = labels.get("albums", {})
    coords_map = coords_map or {}
    name_coords = _build_name_coords(labels, coords_map)

    per_album_total: dict[str, int] = defaultdict(int)
    per_album_labeled: dict[str, int] = defaultdict(int)
    unknown_albums: set[str] = set()
    coord_exif = 0
    coord_pin = 0
    coord_canonical = 0

    for r in rows:
        for f in PLACE_FIELDS:
            r.pop(f, None)

        folder, _, fname = r["blob_path"].partition("/")
        per_album_total[folder] += 1

        # EXIF GPS first, independent of labeling: a photo with real GPS is
        # distance-matchable even if its album was never hand-labeled.
        exif = r.get("exif_gps")
        if isinstance(exif, list) and len(exif) == 2:
            r["place_coord"] = [float(exif[0]), float(exif[1])]
            r["place_coord_source"] = "exif"

        album_entry = albums.get(folder)
        if not album_entry:
            unknown_albums.add(folder)
            if r.get("place_coord_source") == "exif":
                coord_exif += 1
            continue

        try:
            n = files_map[folder].index(fname) + 1
        except ValueError:
            print(
                f"warn: {r['blob_path']} not found in its own folder's file list",
                file=sys.stderr,
            )
            continue

        match = _resolve_for_photo(album_entry, n)
        if match is None:
            if r.get("place_coord_source") == "exif":
                coord_exif += 1
            continue

        _apply_match(r, match, album_entry.get("context"), coords_map, name_coords)
        per_album_labeled[folder] += 1
        src = r.get("place_coord_source")
        if src == "exif":
            coord_exif += 1
        elif src == "pin":
            coord_pin += 1
        elif src == "canonical":
            coord_canonical += 1

    return {
        "rows_total": len(rows),
        "rows_labeled": sum(per_album_labeled.values()),
        "rows_coord_exif": coord_exif,
        "rows_coord_pin": coord_pin,
        "rows_coord_canonical": coord_canonical,
        "per_album_total": dict(per_album_total),
        "per_album_labeled": dict(per_album_labeled),
        "albums_without_entry": sorted(unknown_albums),
    }


def _write_atomic(rows: list[dict], dest: Path) -> None:
    """Write JSONL atomically: temp file in same dir, then os.replace.

    `ensure_ascii=False` preserves Cyrillic readable in the file (still valid
    JSON; loads back to the same strings)."""
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, dest)


def main() -> int:
    parser = argparse.ArgumentParser(prog="photo-search-place-baker")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="resolve and report coverage, but do not write back",
    )
    parser.add_argument(
        "--manifest", type=Path, default=MANIFEST_PATH,
        help=f"manifest.jsonl path (default: {MANIFEST_PATH})",
    )
    parser.add_argument(
        "--labels", type=Path, default=LABELS_PATH,
        help=f"place_labels.json path (default: {LABELS_PATH})",
    )
    parser.add_argument(
        "--coords", type=Path, default=COORDS_PATH,
        help=f"place_coords.json gazetteer path (default: {COORDS_PATH})",
    )
    args = parser.parse_args()

    if not args.manifest.exists():
        print(f"manifest not found: {args.manifest}", file=sys.stderr)
        return 1
    if not args.labels.exists():
        print(f"labels not found: {args.labels}", file=sys.stderr)
        return 1

    rows = [json.loads(line) for line in args.manifest.open()]
    labels = json.loads(args.labels.read_text())
    coords_map = (
        json.loads(args.coords.read_text(encoding="utf-8"))
        if args.coords.exists()
        else {}
    )
    if not coords_map:
        print(f"warn: no gazetteer at {args.coords} — pin coords skipped", file=sys.stderr)

    summary = bake(rows, labels, coords_map)

    # Per-album coverage table, sorted by labeled-count descending.
    labeled_by_album = summary["per_album_labeled"]
    total_by_album = summary["per_album_total"]
    labelled_albums = sorted(
        labeled_by_album.keys(),
        key=lambda a: (-labeled_by_album[a], a),
    )

    print(f"manifest: {summary['rows_total']} rows", file=sys.stderr)
    print(f"labeled : {summary['rows_labeled']} rows "
          f"({100 * summary['rows_labeled'] / max(1, summary['rows_total']):.1f}%)",
          file=sys.stderr)
    coord_total = (
        summary["rows_coord_exif"]
        + summary["rows_coord_pin"]
        + summary["rows_coord_canonical"]
    )
    print(f"coords  : {coord_total} rows "
          f"({100 * coord_total / max(1, summary['rows_total']):.1f}%) "
          f"— {summary['rows_coord_exif']} exif + {summary['rows_coord_pin']} pin "
          f"+ {summary['rows_coord_canonical']} canonical",
          file=sys.stderr)
    print(file=sys.stderr)
    print(f"{'album':30}  {'total':>7}  {'labeled':>7}  coverage", file=sys.stderr)
    print("-" * 60, file=sys.stderr)
    for album in labelled_albums:
        tot = total_by_album.get(album, 0)
        lab = labeled_by_album[album]
        pct = 100 * lab / tot if tot else 0
        print(f"{album:30}  {tot:>7}  {lab:>7}  {pct:5.1f}%", file=sys.stderr)
    print(file=sys.stderr)

    if args.dry_run:
        print("dry-run: not writing", file=sys.stderr)
        return 0

    _write_atomic(rows, args.manifest)
    print(f"wrote {args.manifest}", file=sys.stderr)
    print(
        "next: run `uv run --directory photo-search python -m photo_search.embedder` "
        "to propagate place_* fields into manifest_meta.jsonl",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
