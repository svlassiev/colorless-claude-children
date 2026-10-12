"""Guards that keep a re-index from wiping what is live.

The served index is more than vectors. Place labels (`place_baker`) and people
tags (`face_promote`) are baked onto manifest rows AFTER indexing, and the
embedder copies them into the served `manifest_meta`. Two facts made that
fragile (found the hard way on 2026-09-20, caught before anything went live):

1. `indexer` rebuilds `manifest.jsonl` from the bucket listing + caption cache —
   base fields only. Every baked field was gone after an indexer run, and the
   embedder then wrote (and auto-pushed) a served index without place labels
   or people tags for the whole corpus.
2. Every tool pushes to the live bucket by itself at the end of its run. A
   smoke run (`--limit 10`) or a half-finished sequence went live as it was.

So, two layers:

- `carry_over()` — the indexer keeps what earlier passes baked onto a photo.
- `check_meta()` / `check_sizes()` — `cloud_cache.push_to_gcs()` compares what it
  is about to upload with what is LIVE and refuses (uploading nothing) when
  photos would lose baked fields, photos would disappear, vectors and rows
  disagree, or a synced file would be replaced by a much smaller one.

An intentional loss (an album deleted, labels withdrawn) is pushed with
`python -m photo_search.cloud_cache push --allow-data-loss`.

Pure functions over plain rows: no cloud, no files — see tests/test_safety.py.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from photo_search.place_baker import PLACE_FIELDS

# Owned by face_promote, keyed by the photo's content sha.
PERSON_FIELDS: tuple[str, ...] = ("person_names",)
# Everything a later pass bakes onto a manifest row (what the embedder copies
# into the served meta as optional fields).
ENRICHMENT_FIELDS: tuple[str, ...] = tuple(PLACE_FIELDS) + PERSON_FIELDS

# How many of the live PHOTOS may disappear in one push without a flag. Removing
# an album is ~1 %; a `--limit` run or a broken listing is ~100 %. Counted by
# content sha, not by row: `dedup` drops ~7 % of the rows an indexer run brings
# back (byte-identical copies) without removing a single photo, and its push —
# right after the embedder's — has to pass.
MAX_PHOTO_LOSS = 0.05
# Photos that may lose a baked field in one push: re-labelling moves a few.
MAX_FIELD_LOSS = 0.02
FIELD_LOSS_FLOOR = 5
# A synced file replaced by one under this share of its live size is suspect. A
# coarse net for every file (a truncated write, a cache rebuilt from nothing);
# `dedup` alone shrinks manifest, meta and index by ~7 %, so not tighter than this.
MIN_SIZE_KEPT = 0.80


class PushRefused(RuntimeError):
    """The push would wipe live data. Nothing was uploaded."""


def carry_over(previous: Iterable[dict], rows: list[dict]) -> int:
    """Copy baked fields from the previous manifest onto freshly built rows.

    In place; returns how many rows received something. Place fields follow the
    path (they are resolved from the album and the photo's position in it);
    people tags follow the content, so they are kept only while the sha is the
    same. (Today that condition always holds: the indexer's caption cache is
    keyed by path, so a file REPLACED under the same name is not noticed at all —
    it keeps its old sha, caption and vector until its cache row is removed.
    The check is here for the day the indexer learns to notice.)

    Rows that did not exist before (new photos, and the byte-identical
    duplicates `dedup` had removed) get nothing, which is right: the first are
    not labelled yet, the second are removed again by the next `dedup`.
    """
    before = {r["blob_path"]: r for r in previous}
    touched = 0
    for row in rows:
        old = before.get(row["blob_path"])
        if old is None:
            continue
        kept = False
        for field in PLACE_FIELDS:
            if old.get(field) is not None and row.get(field) is None:
                row[field] = old[field]
                kept = True
        if old.get("sha") == row.get("sha"):
            for field in PERSON_FIELDS:
                if old.get(field) is not None and row.get(field) is None:
                    row[field] = old[field]
                    kept = True
        touched += kept
    return touched


def drop_relisted_copies(previous: Iterable[dict], rows: list[dict]) -> list[dict]:
    """Rows without the copies of photos the previous manifest already holds elsewhere.

    The bucket listing brings every byte-identical copy back on each indexer run,
    and `dedup` would choose among them afresh — by path. A copy of an old photo
    published in a NEW album can sort first, become the survivor, and the old
    album silently loses a row: every place label after it shifts by one
    (labels address a photo by its position in the album). So the choice, once
    made, is kept: a photo the previous manifest holds exactly once stays under
    that path for as long as that path is still listed.

    "Exactly once" matters: a previous manifest that was never deduplicated says
    nothing about which copy should survive, and is left to `dedup`.
    """
    previous = list(previous)
    times = Counter(r["sha"] for r in previous)
    settled = {r["sha"]: r["blob_path"] for r in previous if times[r["sha"]] == 1}
    listed = {r["blob_path"] for r in rows}
    return [
        r for r in rows
        if settled.get(r["sha"]) in (None, r["blob_path"]) or settled[r["sha"]] not in listed
    ]


def field_loss(live: Iterable[dict], new: Iterable[dict]) -> dict[str, tuple[int, int]]:
    """{field: (lost, had)} over the photos present in BOTH indexes.

    `had` = photos that carry the field live; `lost` = those of them that would
    no longer carry it. Photos leaving the index are `check_meta`'s row-count
    business, not counted here.
    """
    new_by_path = {r["blob_path"]: r for r in new}
    had: Counter[str] = Counter()
    lost: Counter[str] = Counter()
    for old in live:
        now = new_by_path.get(old["blob_path"])
        if now is None:
            continue
        for field in ENRICHMENT_FIELDS:
            if old.get(field):
                had[field] += 1
                if not now.get(field):
                    lost[field] += 1
    return {f: (lost[f], had[f]) for f in ENRICHMENT_FIELDS if had[f]}


def check_meta(live: list[dict], new: list[dict], n_vectors: int | None) -> list[str]:
    """Reasons not to replace the live served index with the new one. Empty = fine.

    `live` empty (first push, nothing to lose) checks only that rows and
    vectors agree.
    """
    problems: list[str] = []
    if n_vectors is not None and n_vectors != len(new):
        problems.append(
            f"the index holds {n_vectors} vectors but the meta has {len(new)} rows — "
            "serving would pair photos with the wrong vectors; re-run the embedder"
        )
    if not live:
        return problems
    live_photos = {r["sha"] for r in live}
    gone = len(live_photos - {r["sha"] for r in new})
    if gone > MAX_PHOTO_LOSS * len(live_photos):
        problems.append(
            f"{gone} of the {len(live_photos)} photos that are live would disappear from the index "
            "(a --limit run? a failed bucket listing?)"
        )
    for field, (lost, had) in field_loss(live, new).items():
        if lost > max(FIELD_LOSS_FLOOR, MAX_FIELD_LOSS * had):
            problems.append(
                f"{lost} of the {had} photos that carry `{field}` live would lose it — "
                "after an indexer run the order is: embedder, dedup, place_baker, "
                "face_promote, embedder (see README, Rebuilding the index)"
            )
    return problems


def check_sizes(sizes: Iterable[tuple[str, int, int]]) -> list[str]:
    """(name, live_bytes, local_bytes) → reasons. A much smaller file is suspect."""
    return [
        f"{name} would go from {live} bytes live to {local}"
        for name, live, local in sizes
        if live > 0 and local < MIN_SIZE_KEPT * live
    ]
