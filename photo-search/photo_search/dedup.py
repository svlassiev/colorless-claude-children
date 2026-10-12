"""One-time post-index dedup: collapse content-SHA duplicates.

Groups manifest_meta rows by content sha, keeps per group the copy a previous
dedup kept (the one carrying baked fields), else the lexicographically-smallest
blob_path, rewrites manifest_meta.jsonl + index.npz with the
duplicates removed. Also rewrites manifest.jsonl so a later indexer rerun
sees the deduplicated state.

Idempotent — running twice on a clean index is a no-op.

Backups (`*.bak`) are written next to each artefact on the first run.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict

import numpy as np

from photo_search.paths import INDEX_PATH, MANIFEST_PATH, META_PATH
from photo_search.safety import ENRICHMENT_FIELDS


def _backup_once(path) -> None:
    backup = path.with_suffix(path.suffix + ".bak")
    if not backup.exists() and path.exists():
        backup.write_bytes(path.read_bytes())


def survivor_key(row: dict) -> tuple[bool, bool, str]:
    """Sort key: which of several byte-identical copies stays.

    Not simply the smallest path: a copy of an old photo published in a new album
    can sort first, and the old album would lose a row — every place label after
    it shifts by one. The copy that already carries baked fields is the one a
    previous dedup kept; it wins. (The indexer also keeps such copies out of the
    manifest, `safety.drop_relisted_copies`; this covers the rest.)
    """
    return (
        not any(row.get(f) is not None for f in ENRICHMENT_FIELDS),
        not row.get("caption"),  # of two bare copies, the one that has a caption to show
        row["blob_path"],
    )


def deduplicated_manifest(manifest_rows: list[dict], meta_rows: list[dict], new_meta: list[dict]) -> list[dict]:
    """The manifest after the same choice as the meta (first-row-per-sha could keep the other copy).

    Rows that are in the meta follow it. Rows that are not — a photo whose
    embedding failed — are kept, one per photo, so the next embedder run retries
    them; whether a row has a caption plays no part (a caption-less photo is
    embedded like any other since 2026-09-21).
    """
    keep_paths = {m["blob_path"] for m in new_meta}
    in_meta = {m["blob_path"] for m in meta_rows}
    kept_shas = {m["sha"] for m in new_meta}
    out: list[dict] = []
    for r in manifest_rows:
        if r["blob_path"] in in_meta:
            if r["blob_path"] in keep_paths:
                out.append(r)
        elif r["sha"] not in kept_shas:
            kept_shas.add(r["sha"])
            out.append(r)
    return out


def main() -> int:
    if not (MANIFEST_PATH.exists() and INDEX_PATH.exists() and META_PATH.exists()):
        print("missing artefact(s); run indexer + embedder first", file=sys.stderr)
        return 1

    manifest_rows = [json.loads(line) for line in MANIFEST_PATH.open(encoding="utf-8")]
    meta_rows = [json.loads(line) for line in META_PATH.open(encoding="utf-8")]
    vectors = np.load(INDEX_PATH)["vectors"]

    print(f"manifest: {len(manifest_rows)} rows", file=sys.stderr)
    print(f"meta: {len(meta_rows)} rows", file=sys.stderr)
    print(f"index: {vectors.shape}", file=sys.stderr)

    # Group meta by sha. The meta + index pair is what retrieval reads.
    by_sha: dict[str, list[int]] = defaultdict(list)
    for i, m in enumerate(meta_rows):
        by_sha[m["sha"]].append(i)

    duplicate_groups = {s: idxs for s, idxs in by_sha.items() if len(idxs) > 1}
    print(
        f"unique shas: {len(by_sha)}, duplicate groups: {len(duplicate_groups)}",
        file=sys.stderr,
    )

    # Copies can also sit among the rows that are not in the index (an embedding that failed).
    manifest_is_clean = len(deduplicated_manifest(manifest_rows, meta_rows, meta_rows)) == len(manifest_rows)
    if not duplicate_groups and manifest_is_clean:
        print("no duplicates — index is already clean.", file=sys.stderr)
        return 0

    # Pick winners: the copy that carries baked fields (the indexer carries them over
    # only onto the path a previous dedup kept), then the smallest blob_path.
    keep_indices: list[int] = []
    dropped: list[tuple[str, str]] = []  # (kept_path, dropped_path)
    for sha, idxs in by_sha.items():
        sorted_idxs = sorted(idxs, key=lambda i: survivor_key(meta_rows[i]))
        keep_indices.append(sorted_idxs[0])
        for loser in sorted_idxs[1:]:
            dropped.append((meta_rows[sorted_idxs[0]]["blob_path"], meta_rows[loser]["blob_path"]))

    print(f"\n{len(dropped)} duplicate(s) being removed:", file=sys.stderr)
    for kept, gone in dropped:
        print(f"  keep: {kept}", file=sys.stderr)
        print(f"  drop: {gone}", file=sys.stderr)

    # Preserve original order so vectors and manifest stay aligned positionally.
    keep_indices.sort()

    new_meta = [meta_rows[i] for i in keep_indices]
    new_vectors = vectors[keep_indices]

    # Rewrite manifest.jsonl too — keep one row per sha so a later embedder
    # rerun doesn't reintroduce the duplicate. manifest_rows is a superset
    # of meta_rows (it includes caption-less failures); preserve those.
    new_manifest = deduplicated_manifest(manifest_rows, meta_rows, new_meta)

    # Backup before overwriting.
    for path in (MANIFEST_PATH, META_PATH, INDEX_PATH):
        _backup_once(path)

    # ensure_ascii=False like every other writer of these files: escaped Cyrillic
    # doubles their size, and the push guard compares sizes with what is live.
    with MANIFEST_PATH.open("w", encoding="utf-8") as f:
        for r in new_manifest:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with META_PATH.open("w", encoding="utf-8") as f:
        for r in new_meta:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    np.savez_compressed(INDEX_PATH, vectors=new_vectors)

    print(
        f"\nbefore: meta={len(meta_rows)} index={len(vectors)} manifest={len(manifest_rows)}",
        file=sys.stderr,
    )
    print(
        f"after:  meta={len(new_meta)} index={len(new_vectors)} manifest={len(new_manifest)}",
        file=sys.stderr,
    )
    print(f"removed: {len(dropped)}", file=sys.stderr)
    print(f"backups: *.bak in {INDEX_PATH.parent}", file=sys.stderr)

    # Auto-push the post-dedup state to GCS. With versioning on, the prior
    # (pre-dedup) version remains recoverable from the bucket if anything
    # goes wrong.
    try:
        from photo_search.cloud_cache import push_to_gcs

        pushed = push_to_gcs()
        if pushed:
            print(f"pushed {pushed} cache file(s) to GCS", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"warning: cloud-cache push failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
