"""Pull/push the photo-search cache between local disk and a private GCS bucket.

Mirrors `log_search/cloud_cache.py` — same shared bucket (`cdc-search-cache`),
sibling prefix (`photo-search/` vs `log-search/`), same staleness logic.

Used by:
- `server.py` lifespan — `pull_from_gcs()` at startup
- `indexer.py`, `embedder.py`, `dedup.py` `main()` — `push_to_gcs()` at end

Files synced explicitly so `*.bak` (local-only dedup safety nets) never leak.

A push replaces what production serves, and every tool pushes by itself at the
end of its run — so `push_to_gcs()` first compares what it is about to upload
with what is LIVE and refuses, uploading nothing, when that would wipe data
(`photo_search.safety`). Override, for an intended loss only:
`python -m photo_search.cloud_cache push --allow-data-loss`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from google.api_core.exceptions import NotFound
from google.cloud import storage

from photo_search.safety import PushRefused, check_meta, check_sizes

from photo_search.paths import (
    ANSWER_VOICE_PATH,
    CAPTION_CACHE,
    GCS_CACHE_BUCKET,
    GCS_CACHE_PREFIX,
    HIKING_IMAGE_IDS_PATH,
    INDEX_PATH,
    MANIFEST_PATH,
    META_PATH,
    PERSON_ALIASES_PATH,
    PROJECT,
    ensure_cache_dir,
    STYLED_CAPTION_CACHE,
)

# person_aliases.json (family names) and hiking_image_ids.json (bucket layout /
# image IDs) are private but live in the same private bucket — synced so the
# serving instance can pull them at startup.
SYNC_FILES: list[Path] = [
    INDEX_PATH, META_PATH, MANIFEST_PATH, CAPTION_CACHE, STYLED_CAPTION_CACHE,
    ANSWER_VOICE_PATH,
    PERSON_ALIASES_PATH, HIKING_IMAGE_IDS_PATH,
]


def _bucket() -> storage.Bucket:
    return storage.Client(project=PROJECT).bucket(GCS_CACHE_BUCKET)


def _blob_for(path: Path) -> storage.Blob:
    return _bucket().blob(f"{GCS_CACHE_PREFIX}{path.name}")


def _remote_updated_ts(blob: storage.Blob) -> float:
    """When the live copy was last written; 0.0 only when there is none.

    Any other error propagates. For a push, "could not look" must never be read
    as "nothing live to lose" — that would switch the guard off for that file.
    """
    try:
        blob.reload()
    except NotFound:  # no live copy yet
        return 0.0
    return blob.updated.timestamp() if blob.updated else 0.0


def _integrity() -> list[str]:
    """Vectors and rows must pair up, or serving pairs photos with the wrong vectors.

    Checked whenever either half is about to be uploaded — also on a first push,
    also when only one of the two is newer than the live copy — and never waived:
    no intended loss looks like this.
    """
    if not (INDEX_PATH.exists() and META_PATH.exists()):
        return []
    import numpy as np

    n_vectors = int(np.load(INDEX_PATH)["vectors"].shape[0])
    n_rows = sum(1 for line in META_PATH.open(encoding="utf-8") if line.strip())
    if n_vectors == n_rows:
        return []
    return [
        f"the index holds {n_vectors} vectors but the meta has {n_rows} rows — "
        "serving would pair photos with the wrong vectors; re-run the embedder"
    ]


def _losses(pending: list[tuple[Path, storage.Blob]]) -> list[str]:
    """What live data these uploads would wipe. Empty = none."""
    problems = check_sizes(
        (path.name, blob.size or 0, path.stat().st_size) for path, blob in pending
    )
    meta_blob = dict(pending).get(META_PATH)
    if meta_blob is not None and meta_blob.size:
        # The served index: compare it, photo by photo, with the live one (a few MB).
        live = [json.loads(line) for line in meta_blob.download_as_text().splitlines() if line.strip()]
        new = [json.loads(line) for line in META_PATH.open(encoding="utf-8") if line.strip()]
        problems += check_meta(live, new, n_vectors=None)  # pairing is _integrity()'s
    return problems


def push_to_gcs(allow_data_loss: bool = False) -> int:
    """Upload SYNC_FILES whose local mtime is newer than remote. Returns count.

    All or nothing: if any pending file would wipe live data (see
    `photo_search.safety`), NOTHING is uploaded and `PushRefused` is raised —
    the tools that push at the end of a run report it and leave production as
    it was. `allow_data_loss` is for the CLI's `--allow-data-loss` only: a
    per-command decision, deliberately not an environment variable that could
    stay exported and switch the guard off for every later run. It says what it
    overrides, and it does not waive the vectors/rows pairing.
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
            + "\nIf the loss is intended: python -m photo_search.cloud_cache push --allow-data-loss"
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
    """Download SYNC_FILES from GCS where remote is newer (or local missing)."""
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
            continue
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
    """Manual CLI: `python -m photo_search.cloud_cache push|pull [--force]`."""
    import argparse

    parser = argparse.ArgumentParser(prog="photo-search-cloud-cache")
    parser.add_argument("action", choices=["push", "pull"])
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--allow-data-loss", action="store_true",
        help="push even though photos would lose baked fields or the index would shrink",
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
