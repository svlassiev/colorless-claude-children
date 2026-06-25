"""Offline (Slice 2): bake person_names onto manifest.jsonl from named clusters.

Reads:
  - cluster_labels.json : {cluster_id: name}            (the Slice 1 naming)
  - clusters.json       : {cluster_id: {count, shas}}   (full membership)

For each photo (sha), `person_names` = the set of names of every NAMED cluster
whose faces appear in it (a photo with two people gets both names). Owns
`person_names` end-to-end: strip-then-reapply so re-runs are clean, atomic write
— exactly like place_baker owns the place_* fields.

Naming policy: two clusters with the SAME name merge into one identity (e.g. the
same kid split across ages) — allowed, and logged. Distinct people who share a
first name get DISTINCT person_names here; a query for the shared first name
unifies them later via shared aliases in person_aliases.json, not here.

Next: re-run embedder ($0 cache hits) to propagate person_names into the served
manifest_meta.jsonl, then cloud_cache push.

Run:
    uv run --directory photo-search python -m photo_search.face_promote --dry-run
    uv run --directory photo-search python -m photo_search.face_promote
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

from photo_search.paths import FACES_PATH, MANIFEST_PATH

CLUSTER_LABELS_PATH = FACES_PATH.parent / "cluster_labels.json"
CLUSTERS_PATH = FACES_PATH.parent / "clusters.json"
PERSON_FIELD = "person_names"


def _write_atomic(rows: list[dict], dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, dest)


def main() -> int:
    ap = argparse.ArgumentParser(prog="photo-search-face-promote")
    ap.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    ap.add_argument("--labels", type=Path, default=CLUSTER_LABELS_PATH)
    ap.add_argument("--clusters", type=Path, default=CLUSTERS_PATH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for p in (args.manifest, args.labels, args.clusters):
        if not p.exists():
            print(f"missing: {p}", file=sys.stderr)
            return 1

    labels = json.loads(args.labels.read_text(encoding="utf-8"))
    clusters = json.loads(args.clusters.read_text(encoding="utf-8"))

    # Same-name-on-purpose merges are fine; surface them so they're not silent.
    name_to_cids: dict[str, list[str]] = defaultdict(list)
    for cid, name in labels.items():
        name_to_cids[name].append(cid)
    for name, cids in name_to_cids.items():
        if len(cids) > 1:
            print(f"merge: '{name}' spans clusters {cids}", file=sys.stderr)

    # sha -> set of names
    sha_names: dict[str, set[str]] = defaultdict(set)
    for cid, name in labels.items():
        entry = clusters.get(str(cid))
        if not entry:
            print(f"warn: cluster {cid} ('{name}') not in clusters.json — skipped", file=sys.stderr)
            continue
        for sha in entry["shas"]:
            sha_names[sha].add(name)

    rows = [json.loads(ln) for ln in args.manifest.open()]
    tagged = 0
    per_name: Counter = Counter()
    for r in rows:
        r.pop(PERSON_FIELD, None)  # strip-then-reapply (idempotent)
        names = sha_names.get(r["sha"])
        if names:
            r[PERSON_FIELD] = sorted(names)
            tagged += 1
            per_name.update(names)

    print(f"\nmanifest: {len(rows)} photos | tagged with people: {tagged}", file=sys.stderr)
    print("per name (photos):", file=sys.stderr)
    for name, n in per_name.most_common():
        print(f"  {name:<18} {n}", file=sys.stderr)

    if args.dry_run:
        print("\ndry-run: not writing", file=sys.stderr)
        return 0

    _write_atomic(rows, args.manifest)
    print(
        f"\nwrote {args.manifest}\n"
        "next: re-run embedder (cache hits, $0) to propagate person_names "
        "into manifest_meta.jsonl, then cloud_cache push.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
