"""Offline (Slice 1): detect + embed faces across the photo corpus.

For every photo in manifest.jsonl, run InsightFace `buffalo_l` (RetinaFace
detection + ArcFace recognition) and append one row per *kept* face to
faces.jsonl:

    {face_id, sha, blob_path, bbox, det_score, embedding}

`embedding` is the 512-dim ArcFace `normed_embedding` (already L2-normalized).
A face is kept only when its detection score ≥ FACE_DET_MIN and its smaller
bbox side ≥ FACE_MIN_PX (tiny/low-confidence faces cluster badly).

Privacy: faces.jsonl is biometric data — it stays laptop-local under
~/.cache/photo-search/ (never synced to GCS, gitignored). No crops are saved
here; montages in face_cluster.py re-crop on demand.

Idempotent + resumable: a sidecar `faces_done.txt` records every *processed*
sha (including photos with zero faces), so re-running skips finished work.
Download/decode failures are NOT marked done, so a later run retries them.
Rows are flushed+fsynced periodically, so an interrupt loses at most the last
batch.

Run (after `uv sync --extra face` in photo-search/):
    uv run --directory photo-search python -m photo_search.face_detect
    uv run --directory photo-search python -m photo_search.face_detect --limit 10
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
from google.cloud import storage

from photo_search.paths import (
    BUCKET,
    FACE_DET_MIN,
    FACE_MIN_PX,
    FACES_PATH,
    MANIFEST_PATH,
    PROJECT,
    ensure_cache_dir,
)

# Sidecar set of processed shas — separate from faces.jsonl so that "this photo
# was looked at" is distinct from "this photo produced faces".
FACES_DONE_PATH = FACES_PATH.parent / "faces_done.txt"


def _load_done() -> set[str]:
    if not FACES_DONE_PATH.exists():
        return set()
    return {ln.strip() for ln in FACES_DONE_PATH.read_text().splitlines() if ln.strip()}


def _decode_bgr(data: bytes) -> "np.ndarray | None":
    """JPEG bytes → BGR uint8 HxWx3 (the format InsightFace expects)."""
    import cv2

    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    return img


def main() -> int:
    ap = argparse.ArgumentParser(prog="photo-search-face-detect")
    ap.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    ap.add_argument(
        "--limit", type=int, default=0, help="process at most N new photos (smoke test)"
    )
    ap.add_argument("--det-size", type=int, default=640)
    args = ap.parse_args()

    if not args.manifest.exists():
        print(f"manifest not found: {args.manifest}", file=sys.stderr)
        return 1

    ensure_cache_dir()
    rows = [json.loads(ln) for ln in args.manifest.open()]
    done = _load_done()
    todo = [r for r in rows if r["sha"] not in done]
    if args.limit:
        todo = todo[: args.limit]

    print(
        f"manifest: {len(rows)} photos | already done: {len(done)} | "
        f"to process: {len(todo)}",
        file=sys.stderr,
    )
    if not todo:
        print("nothing to do.", file=sys.stderr)
        return 0

    # Heavy imports / model load only after the cheap exits.
    from insightface.app import FaceAnalysis

    print("loading buffalo_l (downloads ~300 MB on first run)...", file=sys.stderr)
    app = FaceAnalysis(
        name="buffalo_l",
        providers=["CPUExecutionProvider"],
        allowed_modules=["detection", "recognition"],  # skip landmark/genderage
    )
    app.prepare(ctx_id=-1, det_size=(args.det_size, args.det_size))

    bucket = storage.Client(project=PROJECT).bucket(BUCKET)

    faces_f = FACES_PATH.open("a", encoding="utf-8")
    done_f = FACES_DONE_PATH.open("a", encoding="utf-8")

    t0 = time.time()
    n_faces = n_err = 0
    try:
        for i, r in enumerate(todo, 1):
            sha, blob_path = r["sha"], r["blob_path"]
            try:
                data = bucket.blob(blob_path).download_as_bytes()
                img = _decode_bgr(data)
                if img is None:
                    raise ValueError("image decode failed")
                faces = app.get(img)
            except Exception as e:  # noqa: BLE001 — log + retry next run
                print(f"  skip {blob_path}: {type(e).__name__}: {e}", file=sys.stderr)
                n_err += 1
                continue  # NOT marked done → retried on a later run

            for j, f in enumerate(faces):
                x1, y1, x2, y2 = (float(v) for v in f.bbox)
                if float(f.det_score) < FACE_DET_MIN:
                    continue
                if min(x2 - x1, y2 - y1) < FACE_MIN_PX:
                    continue
                faces_f.write(
                    json.dumps(
                        {
                            "face_id": f"{sha}:{j}",
                            "sha": sha,
                            "blob_path": blob_path,
                            "bbox": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
                            "det_score": round(float(f.det_score), 4),
                            "embedding": f.normed_embedding.astype(float).round(6).tolist(),
                        }
                    )
                    + "\n"
                )
                n_faces += 1

            done_f.write(sha + "\n")
            if i % 50 == 0:
                for fh in (faces_f, done_f):
                    fh.flush()
                    os.fsync(fh.fileno())
                rate = i / (time.time() - t0)
                eta_m = (len(todo) - i) / rate / 60 if rate else 0
                print(
                    f"  [{i:>5}/{len(todo)}] faces={n_faces} err={n_err} "
                    f"rate={rate:.1f}/s eta={eta_m:.0f}m",
                    file=sys.stderr,
                )
    finally:
        for fh in (faces_f, done_f):
            fh.flush()
            os.fsync(fh.fileno())
            fh.close()

    print(
        f"done. processed {len(todo)} photos, kept {n_faces} faces, {n_err} errors.\n"
        f"faces → {FACES_PATH}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
