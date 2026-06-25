"""Offline (Slice 1): cluster faces and build montages for naming.

Loads faces.jsonl, runs ONE HDBSCAN pass on the L2-normalized embeddings
(`metric='euclidean'` — euclidean² = 2(1−cos), monotonic with cosine, version-
independent and tree-accelerated), and writes two things to ~/.cache:

  - cluster_proposals.json : cluster_id → {count, photos, sample shas/face_ids}
  - face_review/cluster_<id>_n<size>.jpg : a montage of sampled faces per cluster

There is no assign / exemplar / merge machinery here (Slice 1 is one pass).
You then open the montages, find the 2-3 clusters that are clearly your people,
and write a flat map in cluster_labels.json, e.g.:

    { "3": "Anna", "8": "Anna", "12": "Ivan" }

(assigning the same name to several cluster ids merges them). Slice 2 reads that.

Run (after face_detect.py has produced faces.jsonl):
    uv run --directory photo-search python -m photo_search.face_cluster
    uv run --directory photo-search python -m photo_search.face_cluster --min-cluster 6 --top 60
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from google.cloud import storage

from photo_search.paths import (
    BUCKET,
    FACE_REVIEW_DIR,
    FACES_PATH,
    HDBSCAN_MIN_CLUSTER,
    PROJECT,
    ensure_cache_dir,
)


def main() -> int:
    ap = argparse.ArgumentParser(prog="photo-search-face-cluster")
    ap.add_argument("--min-cluster", type=int, default=HDBSCAN_MIN_CLUSTER)
    ap.add_argument("--top", type=int, default=50, help="montages for the N largest clusters")
    ap.add_argument("--samples", type=int, default=25, help="faces per montage")
    ap.add_argument(
        "--no-montage",
        action="store_true",
        help="skip montage rebuild — just (re)write clusters.json + proposals.json",
    )
    args = ap.parse_args()

    if not FACES_PATH.exists():
        print(f"no faces file at {FACES_PATH} — run face_detect first", file=sys.stderr)
        return 1
    faces = [json.loads(ln) for ln in FACES_PATH.open()]
    if not faces:
        print("faces.jsonl is empty", file=sys.stderr)
        return 1

    embs = np.asarray([f["embedding"] for f in faces], dtype=np.float32)
    n_photos = len({f["sha"] for f in faces})
    print(f"loaded {len(faces)} faces over {n_photos} photos, {embs.shape[1]}-dim", file=sys.stderr)

    from sklearn.cluster import HDBSCAN

    labels = HDBSCAN(min_cluster_size=args.min_cluster, metric="euclidean").fit_predict(embs)

    clusters: dict[int, list[int]] = defaultdict(list)
    for idx, lab in enumerate(labels):
        if lab >= 0:
            clusters[int(lab)].append(idx)
    noise = int((labels == -1).sum())
    ordered = sorted(clusters.items(), key=lambda kv: -len(kv[1]))

    print(
        f"clusters: {len(clusters)} | noise faces: {noise} "
        f"({100 * noise / len(faces):.0f}%) | top sizes: "
        f"{[len(v) for _, v in ordered[:15]]}",
        file=sys.stderr,
    )

    ensure_cache_dir()
    proposals = {
        str(cid): {
            "count": len(idxs),
            "photos": len({faces[i]["sha"] for i in idxs}),
            "sample_shas": list(dict.fromkeys(faces[i]["sha"] for i in idxs))[:8],
            "sample_face_ids": [faces[i]["face_id"] for i in idxs[:8]],
        }
        for cid, idxs in ordered
    }
    prop_path = FACES_PATH.parent / "cluster_proposals.json"
    prop_path.write_text(json.dumps(proposals, indent=2, ensure_ascii=False))

    # Full membership (every cluster → every photo sha) — what face_promote
    # consumes to stamp person_names. HDBSCAN labelling is deterministic for a
    # fixed faces.jsonl + params, so re-running here reproduces the same ids the
    # montages were named under.
    clusters_full = {
        str(cid): {
            "count": len(idxs),
            "shas": list(dict.fromkeys(faces[i]["sha"] for i in idxs)),
        }
        for cid, idxs in ordered
    }
    clusters_path = FACES_PATH.parent / "clusters.json"
    clusters_path.write_text(json.dumps(clusters_full, ensure_ascii=False))
    print(f"wrote full membership → {clusters_path}", file=sys.stderr)

    if args.no_montage:
        print("--no-montage: skipping montage build.", file=sys.stderr)
        return 0

    # --- montages ---------------------------------------------------------
    from PIL import Image

    FACE_REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    bucket = storage.Client(project=PROJECT).bucket(BUCKET)

    THUMB, COLS, PAD = 110, 5, 4
    built = 0
    for cid, idxs in ordered[: args.top]:
        thumbs: list = []
        for i in idxs[: args.samples]:
            f = faces[i]
            try:
                data = bucket.blob(f["blob_path"]).download_as_bytes()
                im = Image.open(io.BytesIO(data)).convert("RGB")
                x1, y1, x2, y2 = f["bbox"]
                crop = im.crop((max(0, int(x1)), max(0, int(y1)), int(x2), int(y2)))
                crop.thumbnail((THUMB, THUMB))
                tile = Image.new("RGB", (THUMB, THUMB), (35, 35, 35))
                tile.paste(crop, ((THUMB - crop.width) // 2, (THUMB - crop.height) // 2))
                thumbs.append(tile)
            except Exception as e:  # noqa: BLE001
                print(f"  montage skip {f['face_id']}: {e}", file=sys.stderr)
        if not thumbs:
            continue
        nrows = (len(thumbs) + COLS - 1) // COLS
        canvas = Image.new(
            "RGB", (COLS * (THUMB + PAD) + PAD, nrows * (THUMB + PAD) + PAD), (20, 20, 20)
        )
        for k, tile in enumerate(thumbs):
            r, c = divmod(k, COLS)
            canvas.paste(tile, (PAD + c * (THUMB + PAD), PAD + r * (THUMB + PAD)))
        canvas.save(FACE_REVIEW_DIR / f"cluster_{cid:04d}_n{len(idxs)}.jpg", quality=85)
        built += 1
        if built % 10 == 0:
            print(f"  built {built} montages...", file=sys.stderr)

    print(
        f"\nwrote {built} montages → {FACE_REVIEW_DIR}\n"
        f"proposals → {prop_path}\n"
        f"next: open the montages, then write cluster_labels.json "
        f"(cluster_id → name) for your people.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
