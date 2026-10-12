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

from google.api_core.exceptions import NotFound
from google.cloud import storage

from log_search.paths import (
    CHUNKS_PATH,
    GCS_CACHE_BUCKET,
    GCS_CACHE_PREFIX,
    IMAGE_CAPTION_CACHE,
    INDEX_PATH,
    META_PATH,
    PROJECT,
    ensure_cache_dir,
)

# IMAGE_CAPTION_CACHE rides along so a fresh checkout/clone doesn't have
# to re-pay the caption model for captions already generated. Mirrors
# the photo-search caption_cache.jsonl convention.
SYNC_FILES: list[Path] = [INDEX_PATH, META_PATH, CHUNKS_PATH, IMAGE_CAPTION_CACHE]


def _bucket() -> storage.Bucket:
    return storage.Client(project=PROJECT).bucket(GCS_CACHE_BUCKET)


def _blob_for(path: Path) -> storage.Blob:
    return _bucket().blob(f"{GCS_CACHE_PREFIX}{path.name}")


def _remote_updated_ts(blob: storage.Blob) -> float:
    """When the live copy was last written; 0.0 only when there is none.

    Any other error propagates: for a push, "could not look" must never be read
    as "nothing live to lose".
    """
    try:
        blob.reload()
    except NotFound:  # no live copy yet
        return 0.0
    return blob.updated.timestamp() if blob.updated else 0.0


class PushRefused(RuntimeError):
    """The push would wipe live data. Nothing was uploaded."""


# A synced file replaced by one under this share of its live size is suspect
# (a chunker run on the wrong corpus root, a truncated write): the journal only grows.
MIN_SIZE_KEPT = 0.90


def _integrity() -> list[str]:
    """Vectors and rows must pair up. Checked on every push of either, never waived."""
    if not (INDEX_PATH.exists() and META_PATH.exists()):
        return []
    import numpy as np

    n_vectors = int(np.load(INDEX_PATH)["vectors"].shape[0])
    n_rows = sum(1 for line in META_PATH.open(encoding="utf-8") if line.strip())
    if n_vectors == n_rows:
        return []
    return [f"the index holds {n_vectors} vectors but the meta has {n_rows} rows — re-run the embedder"]


def _losses(pending: list[tuple[Path, storage.Blob]]) -> list[str]:
    """What live data these uploads would wipe. Empty = none."""
    return [
        f"{path.name} would go from {blob.size} bytes live to {path.stat().st_size}"
        for path, blob in pending
        if blob.size and path.stat().st_size < MIN_SIZE_KEPT * blob.size
    ]


def push_to_gcs(allow_data_loss: bool = False) -> int:
    """Upload SYNC_FILES whose local mtime is newer than the remote.

    Returns the number of objects uploaded. Idempotent — re-running with
    no local changes uploads nothing and costs nothing.

    All or nothing: a push replaces what production serves, so if any pending
    file would wipe live data NOTHING is uploaded and `PushRefused` is raised.
    `allow_data_loss` is for the CLI's `--allow-data-loss` only — a per-command
    decision, not an environment variable that could stay exported. It says what
    it overrides, and it does not waive the vectors/rows pairing.
    """
    ensure_cache_dir()
    pending: list[tuple[Path, storage.Blob]] = []
    for path in SYNC_FILES:
        if not path.exists():
            continue
        blob = _blob_for(path)
        if path.stat().st_mtime <= _remote_updated_ts(blob):  # reloads the blob: size is known after this
            continue
        pending.append((path, blob))
    if not pending:
        return 0

    refusal = "PUSH REFUSED — nothing was uploaded, production is unchanged:\n  - "
    if {INDEX_PATH, META_PATH} & {path for path, _ in pending}:
        broken = _integrity()
        if broken:
            raise PushRefused(refusal + "\n  - ".join(broken))
    losses = _losses(pending)
    if losses and not allow_data_loss:
        raise PushRefused(
            refusal + "\n  - ".join(losses)
            + "\nIf the loss is intended: python -m log_search.cloud_cache push --allow-data-loss"
        )
    if losses:
        print("pushing with --allow-data-loss, knowingly:\n  - " + "\n  - ".join(losses), file=sys.stderr)

    for path, blob in pending:
        blob.upload_from_filename(str(path))
        size_kb = path.stat().st_size / 1024
        print(
            f"  push {path.name} ({size_kb:.1f} KiB) → "
            f"gs://{GCS_CACHE_BUCKET}/{GCS_CACHE_PREFIX}{path.name}",
            file=sys.stderr,
        )
    return len(pending)


def pull_from_gcs(force: bool = False) -> int:
    """Download SYNC_FILES from GCS where remote is newer (or local missing).

    With `force=True`, downloads regardless of local state. Returns the number
    of objects downloaded.
    """
    ensure_cache_dir()
    n = 0
    for path in SYNC_FILES:
        blob = _blob_for(path)
        try:
            remote_ts = _remote_updated_ts(blob)
        except Exception as e:  # noqa: BLE001 — a server must still start on its local copy when GCS is unreachable
            print(f"  pull: cannot look at {path.name} ({type(e).__name__}) — keeping the local copy", file=sys.stderr)
            continue
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
    parser.add_argument(
        "--allow-data-loss", action="store_true",
        help="push even though a synced file would shrink by more than 10 %",
    )
    args = parser.parse_args()

    if args.action == "push":
        try:
            n = push_to_gcs(allow_data_loss=args.allow_data_loss)
        except PushRefused as e:
            print(e, file=sys.stderr)
            return 2
    else:
        n = pull_from_gcs(force=args.force)
    print(f"{args.action}: {n} object(s) {('uploaded' if args.action == 'push' else 'downloaded')}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
