"""Pull/push the log-search cache between local disk and a private GCS bucket.

Used by:
- `server.py` lifespan — `pull_from_gcs()` at startup, no-op if local copy is fresh
- `embedder.py` `main()` — `push_to_gcs()` at end of run, no-op if remote up to date

Staleness: compare local file `mtime` against blob `updated`. Local equal-or-newer
short-circuits the network call, so typical dev iterations cost nothing.

The list of synced files is explicit (`SYNC_FILES`) so we never accidentally
push backups (`*.bak`), test artefacts, or ephemeral state. Order doesn't matter
for correctness.
"""

from __future__ import annotations

import sys
from pathlib import Path

from google.cloud import storage

from log_search.paths import (
    CHUNKS_PATH,
    GCS_CACHE_BUCKET,
    GCS_CACHE_PREFIX,
    INDEX_PATH,
    META_PATH,
    PROJECT,
    ensure_cache_dir,
)

SYNC_FILES: list[Path] = [INDEX_PATH, META_PATH, CHUNKS_PATH]


def _bucket() -> storage.Bucket:
    return storage.Client(project=PROJECT).bucket(GCS_CACHE_BUCKET)


def _blob_for(path: Path) -> storage.Blob:
    return _bucket().blob(f"{GCS_CACHE_PREFIX}{path.name}")


def _remote_updated_ts(blob: storage.Blob) -> float:
    try:
        blob.reload()
    except Exception:  # noqa: BLE001 — blob doesn't exist yet
        return 0.0
    return blob.updated.timestamp() if blob.updated else 0.0


def push_to_gcs() -> int:
    """Upload SYNC_FILES whose local mtime is newer than the remote.

    Returns the number of objects uploaded. Idempotent — re-running with
    no local changes uploads nothing and costs nothing.
    """
    ensure_cache_dir()
    n = 0
    for path in SYNC_FILES:
        if not path.exists():
            continue
        blob = _blob_for(path)
        local_ts = path.stat().st_mtime
        if local_ts <= _remote_updated_ts(blob):
            continue
        blob.upload_from_filename(str(path))
        size_kb = path.stat().st_size / 1024
        print(
            f"  push {path.name} ({size_kb:.1f} KiB) → "
            f"gs://{GCS_CACHE_BUCKET}/{GCS_CACHE_PREFIX}{path.name}",
            file=sys.stderr,
        )
        n += 1
    return n


def pull_from_gcs(force: bool = False) -> int:
    """Download SYNC_FILES from GCS where remote is newer (or local missing).

    With `force=True`, downloads regardless of local state. Returns the number
    of objects downloaded.
    """
    ensure_cache_dir()
    n = 0
    for path in SYNC_FILES:
        blob = _blob_for(path)
        remote_ts = _remote_updated_ts(blob)
        if remote_ts == 0.0:
            continue  # blob doesn't exist on the remote — nothing to pull
        local_ts = path.stat().st_mtime if path.exists() else 0.0
        if not force and path.exists() and local_ts >= remote_ts:
            continue
        blob.download_to_filename(str(path))
        size_kb = path.stat().st_size / 1024
        print(
            f"  pull gs://{GCS_CACHE_BUCKET}/{GCS_CACHE_PREFIX}{path.name} "
            f"({size_kb:.1f} KiB) → {path.name}",
            file=sys.stderr,
        )
        n += 1
    return n


def _cli() -> int:
    """Manual CLI: `python -m log_search.cloud_cache push|pull|pull --force`."""
    import argparse

    parser = argparse.ArgumentParser(prog="log-search-cloud-cache")
    parser.add_argument("action", choices=["push", "pull"])
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.action == "push":
        n = push_to_gcs()
    else:
        n = pull_from_gcs(force=args.force)
    print(f"{args.action}: {n} object(s) {('uploaded' if args.action == 'push' else 'downloaded')}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
