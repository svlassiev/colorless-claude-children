"""A re-index must not wipe what is live.

2026-09-20: following the README's rebuild order would have replaced the served index
with one that had no place labels and no people tags for ~6,000 photos — the indexer
rebuilt the manifest with base fields only, and every tool pushed by itself. It was
caught by reading the code before running it. These tests replay that, and the other
ways a push could shrink production, against the guards in photo_search.safety.

No cloud, no files: plain rows in, reasons out.
"""

from __future__ import annotations

import copy

import pytest

from photo_search import cloud_cache, dedup, place_baker
from photo_search.safety import (
    ENRICHMENT_FIELDS,
    PushRefused,
    carry_over,
    check_meta,
    check_sizes,
    drop_relisted_copies,
    field_loss,
)


def row(path: str, sha: str, **baked) -> dict:
    return {
        "id": sha, "gcs_uri": f"gs://bucket/{path}", "blob_path": path, "exif_date_iso": None,
        "exif_gps": None, "caption": "a photo", "sha": sha, **baked,
    }


def live_index(n: int = 200) -> list[dict]:
    """Every second photo has a place, every third has people — like production, smaller."""
    rows = []
    for i in range(n):
        baked = {}
        if i % 2 == 0:
            baked |= {"place_names": ["Хибины"], "place_detail": "Хибины, перевал", "place_context": "Кольский"}
        if i % 3 == 0:
            baked |= {"person_names": ["Person A"]}
        rows.append(row(f"album{i // 20}/IMG_{i:04d}.jpg", f"{i:016x}", **baked))
    return rows


def stripped(rows: list[dict]) -> list[dict]:
    """What the indexer used to write: the same photos, base fields only."""
    return [{k: v for k, v in r.items() if k not in ENRICHMENT_FIELDS} for r in rows]


# ---- the cause: the indexer keeps what was baked -------------------------------------------


def test_an_indexer_run_keeps_places_and_people_on_the_photos_that_had_them():
    before = live_index()
    rebuilt = stripped(before) + [row("new-album/IMG_0001.jpg", "f" * 16)]

    kept = carry_over(before, rebuilt)

    assert rebuilt[:-1] == before  # every old photo exactly as it was
    assert kept == sum(1 for r in before if any(r.get(f) for f in ENRICHMENT_FIELDS))
    assert not any(f in rebuilt[-1] for f in ENRICHMENT_FIELDS)  # a new photo is not labelled by accident


def test_a_file_replaced_under_the_same_name_keeps_its_place_but_not_the_old_faces():
    before = [row("album/IMG_1.jpg", "a" * 16, place_names=["Ловозеро"], person_names=["Person B"])]
    rebuilt = [row("album/IMG_1.jpg", "b" * 16)]  # different bytes now

    carry_over(before, rebuilt)

    assert rebuilt[0]["place_names"] == ["Ловозеро"]  # the place follows the album position
    assert "person_names" not in rebuilt[0]  # people follow the content, and the content changed


def test_the_duplicates_dedup_had_removed_come_back_bare_and_wait_for_the_next_dedup():
    before = [row("pohod/pohod057.jpg", "c" * 16, place_names=["Вуокса"])]
    rebuilt = [row("pohod/pohod057.jpg", "c" * 16), row("pohod/pohod57.JPG", "c" * 16)]

    carry_over(before, rebuilt)

    assert rebuilt[0]["place_names"] == ["Вуокса"]
    assert "place_names" not in rebuilt[1]


# ---- once deduplicated, stay deduplicated (found by the review of this change) ---------------
#
# A copy of an old photo published in a NEW album can sort before the old path. dedup used to
# pick "the smallest path" afresh on every rebuild: the old album lost a row, and every place
# label after it moved by one — 38 photos in the reviewer's replay on the real index, no guard firing.


def test_a_copy_of_an_old_photo_in_a_new_album_does_not_enter_the_manifest():
    before = [row("UniversityIVV/Picture005.jpg", "a" * 16, place_names=["СПбГУ"]), row("UniversityIVV/Picture006.jpg", "b" * 16)]
    listed = [row("0a1b-new-album/DSC_0001.jpg", "a" * 16)] + stripped(before) + [row("0a1b-new-album/DSC_0002.jpg", "c" * 16)]

    rows = drop_relisted_copies(before, listed)

    assert [r["blob_path"] for r in rows] == [
        "UniversityIVV/Picture005.jpg", "UniversityIVV/Picture006.jpg", "0a1b-new-album/DSC_0002.jpg",
    ]


def test_a_copy_takes_over_only_when_the_old_path_is_gone_from_the_bucket():
    before = [row("old/IMG_1.jpg", "a" * 16)]
    assert drop_relisted_copies(before, [row("new/IMG_1.jpg", "a" * 16)]) == [row("new/IMG_1.jpg", "a" * 16)]


def test_a_manifest_that_was_never_deduplicated_settles_nothing__dedup_decides():
    before = [row("pohod/pohod057.jpg", "c" * 16), row("pohod/pohod57.JPG", "c" * 16)]
    listed = copy.deepcopy(before)
    assert drop_relisted_copies(before, listed) == listed
    assert drop_relisted_copies([], listed) == listed  # a first build


def test_dedup_keeps_the_copy_that_was_kept_before__not_simply_the_smallest_path():
    labelled = row("UniversityIVV/Picture005.jpg", "a" * 16, place_names=["СПбГУ"])
    copy_in_new_album = row("0a1b-new-album/DSC_0001.jpg", "a" * 16)

    assert min([copy_in_new_album, labelled], key=dedup.survivor_key) is labelled
    # with nothing baked on either, the smallest path — as before
    bare = [row("pohod/pohod57.JPG", "c" * 16), row("pohod/pohod057.jpg", "c" * 16)]
    assert min(bare, key=dedup.survivor_key)["blob_path"] == "pohod/pohod057.jpg"


# ---- the guard: what may not replace the live index ---------------------------------------


def test_the_2026_09_20_scenario_is_refused__every_photo_would_lose_its_place_and_people():
    live = live_index()
    new = stripped(live) + [row("new-album/IMG_0001.jpg", "f" * 16)]

    problems = check_meta(live, new, n_vectors=len(new))

    assert any("`place_names`" in p for p in problems)
    assert any("`person_names`" in p for p in problems)
    lost, had = field_loss(live, new)["place_names"]
    assert lost == had == 100


def test_a_normal_run_passes__new_photos_added_nothing_lost():
    live = live_index()
    new = copy.deepcopy(live) + [row(f"new-album/IMG_{i}.jpg", f"{i:015x}f") for i in range(146)]

    assert check_meta(live, new, n_vectors=len(new)) == []


def test_relabelling_a_few_photos_and_removing_an_album_passes():
    live = live_index()
    new = [r for r in copy.deepcopy(live) if not r["blob_path"].startswith("album9/")][:]  # an album deleted: 10 %… too many
    assert check_meta(live, new, n_vectors=len(new))  # 20 of 200 is over the 5 % line: needs the flag

    new = copy.deepcopy(live)[:-6]  # 3 % gone: fine
    new[0].pop("place_names")  # and a label withdrawn from one photo
    assert check_meta(live, new, n_vectors=len(new)) == []


def test_a_limit_smoke_run_cannot_become_the_live_index():
    live = live_index()
    problems = check_meta(live, copy.deepcopy(live)[:10], n_vectors=10)
    assert any("190 of the 200 photos that are live would disappear" in p for p in problems)


def test_dedup_may_push_right_after_the_embedder__rows_go_but_no_photo_does():
    # The embedder's push (before dedup) made the byte-identical copies live: 7 % more rows.
    photos = live_index()
    copies = [row(f"album0/img_{i}.JPG", r["sha"]) for i, r in enumerate(photos[:14])]
    live, deduplicated = photos + copies, copy.deepcopy(photos)

    assert check_meta(live, deduplicated, n_vectors=len(deduplicated)) == []


def test_vectors_and_rows_that_disagree_are_refused__even_on_a_first_push():
    new = live_index(50)
    assert any("50 rows" in p for p in check_meta([], new, n_vectors=49))
    assert check_meta([], new, n_vectors=50) == []


def test_a_photo_that_leaves_the_index_is_not_counted_as_a_lost_label():
    live = live_index()
    new = copy.deepcopy(live)[2:]  # two photos gone, one of them labelled
    assert all(lost == 0 for lost, _ in field_loss(live, new).values())


def test_a_synced_file_that_shrinks_a_lot_is_refused_and_one_that_grows_is_not():
    assert check_sizes([("caption_cache_styled.jsonl", 1_000_000, 40_000)])
    assert check_sizes([("index.npz", 70_000_000, 70_100_000), ("new-file.json", 0, 5)]) == []
    assert check_sizes([("manifest.jsonl", 4_700_000, 4_400_000)]) == []  # dedup took its 7 %


# ---- the push itself: all or nothing -------------------------------------------------------


class FakeBlob:
    def __init__(self, size: int, text: str = ""):
        self.size, self._text, self.uploaded = size, text, []

    def download_as_text(self) -> str:
        return self._text

    def upload_from_filename(self, name: str) -> None:
        self.uploaded.append(name)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """A local cache with a meta file and a caption cache, and their live counterparts."""
    import json

    import numpy as np

    live = live_index()
    meta, index, captions = tmp_path / "meta.jsonl", tmp_path / "index.npz", tmp_path / "captions.jsonl"
    live_text = "\n".join(json.dumps(r, ensure_ascii=False) for r in live)
    blobs = {  # live sizes no larger than what the tests write: size is not what these tests are about
        meta: FakeBlob(len(live_text.encode()) // 2, live_text),
        index: FakeBlob(100),
        captions: FakeBlob(100),
    }

    def write(rows: list[dict]) -> None:
        meta.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
        np.savez_compressed(index, vectors=np.zeros((len(rows), 4), dtype=np.float32))
        captions.write_text("x" * 200)

    monkeypatch.setattr(cloud_cache, "SYNC_FILES", [index, meta, captions])
    monkeypatch.setattr(cloud_cache, "META_PATH", meta)
    monkeypatch.setattr(cloud_cache, "INDEX_PATH", index)
    monkeypatch.setattr(cloud_cache, "ensure_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(cloud_cache, "_blob_for", lambda path: blobs[path])
    monkeypatch.setattr(cloud_cache, "_remote_updated_ts", lambda blob: 0.0)  # local is always newer
    return live, write, blobs


def test_a_refused_push_uploads_NOTHING__not_even_the_files_that_were_fine(cache):
    live, write, blobs = cache
    write(stripped(live))

    with pytest.raises(PushRefused, match="nothing was uploaded"):
        cloud_cache.push_to_gcs()

    assert all(blob.uploaded == [] for blob in blobs.values())


def test_a_healthy_push_uploads_everything(cache):
    live, write, blobs = cache
    write(copy.deepcopy(live) + [row("new/IMG_1.jpg", "e" * 16)])

    assert cloud_cache.push_to_gcs() == 3
    assert all(len(blob.uploaded) == 1 for blob in blobs.values())


def test_an_intended_loss_goes_through_only_with_the_flag__and_says_what_it_overrides(cache, capsys):
    live, write, blobs = cache
    write(stripped(live))

    assert cloud_cache.push_to_gcs(allow_data_loss=True) == 3
    assert "knowingly" in capsys.readouterr().err


def test_an_exported_environment_variable_cannot_switch_the_guard_off(cache, monkeypatch):
    live, write, blobs = cache
    write(stripped(live))
    monkeypatch.setenv("PHOTO_SEARCH_ALLOW_DATA_LOSS", "1")  # 0.1 of this change honoured it; it would have stayed exported

    with pytest.raises(PushRefused):
        cloud_cache.push_to_gcs()


def test_vectors_that_do_not_match_the_rows_are_refused_even_with_the_flag_and_even_when_only_the_index_is_new(cache, monkeypatch):
    import numpy as np

    live, write, blobs = cache
    write(copy.deepcopy(live))
    index = cloud_cache.INDEX_PATH
    np.savez_compressed(index, vectors=np.zeros((len(live) - 1, 4), dtype=np.float32))  # one vector short
    monkeypatch.setattr(cloud_cache, "SYNC_FILES", [index])  # the meta is not pending at all

    with pytest.raises(PushRefused, match="vectors"):
        cloud_cache.push_to_gcs(allow_data_loss=True)
    assert blobs[index].uploaded == []


def test_a_storage_error_is_not_mistaken_for_nothing_live(cache, monkeypatch):
    live, write, blobs = cache
    write(stripped(live))

    class Unreachable:
        size = None

        def reload(self):
            raise ConnectionError("GCS is unreachable")

    monkeypatch.undo()  # back to the real _remote_updated_ts...
    monkeypatch.setattr(cloud_cache, "SYNC_FILES", [cloud_cache.META_PATH])
    monkeypatch.setattr(cloud_cache, "ensure_cache_dir", lambda: None)
    monkeypatch.setattr(cloud_cache, "_blob_for", lambda path: Unreachable())

    with pytest.raises(ConnectionError):  # ...which no longer answers "no live copy" to every error
        cloud_cache.push_to_gcs()


# ---- place labels are counted on the deduplicated manifest ----------------------------------


def test_place_baker_sees_the_duplicates_an_indexer_run_brought_back():
    clean = [row("pohod/pohod057.jpg", "c" * 16), row("pohod/pohod058.jpg", "d" * 16)]
    assert place_baker.duplicate_photos(clean) == 0
    assert place_baker.duplicate_photos(clean + [row("pohod/pohod57.JPG", "c" * 16)]) == 1


# ---- every image is searchable unless it is excluded explicitly (owner's rule, 2026-09-21) -------


def test_a_photo_without_a_caption_gets_a_vector_like_any_other():
    from photo_search.embedder import searchable_rows

    manifest = [row("album/IMG_1.jpg", "a" * 16), {**row("album/IMG_2.jpg", "b" * 16), "caption": ""}]
    assert searchable_rows(manifest) == manifest  # the model declining to describe a photo must not drop it


def test_dedup_treats_caption_less_photos_as_ordinary_rows__and_clears_copies_outside_the_index_too():
    silent = {**row("pohod/pohod057.jpg", "c" * 16), "caption": ""}  # no caption, but embedded now
    copy_of_it = row("pohod/pohod57.JPG", "c" * 16)
    failed = row("pohod/broken.jpg", "e" * 16)  # its embedding failed: in the manifest only
    failed_copy = row("pohod/broken-copy.jpg", "e" * 16)
    manifest = [silent, copy_of_it, failed, failed_copy]
    meta = [silent, copy_of_it]

    kept = [min(meta, key=dedup.survivor_key)]
    assert kept == [copy_of_it]  # of two bare copies, the one that has a caption to show

    after = dedup.deduplicated_manifest(manifest, meta, kept)
    assert [r["blob_path"] for r in after] == ["pohod/pohod57.JPG", "pohod/broken.jpg"]
    assert place_baker.duplicate_photos(after) == 0  # so the precondition can always be cleared
    assert place_baker.duplicate_photos(manifest) == 2
