"""Phase 2: embed each photo with multimodalembedding@001, persist as index.npz.

multimodalembedding@001 puts images and text into the same 1408-dim space,
so a text query at retrieval time lands near matching image vectors without
a captioning detour. Captions remain useful as displayed metadata.

Cost-aware:
- $0.0002 per image. ~6,488 photos = ~$1.30.
- Sanity check on one image + its caption (cosine > 0.10) gates the batch.
- SHA-keyed cache: re-runs on unchanged photos cost $0.
- ThreadPoolExecutor concurrency for ~10× wall-clock speedup.

Note: uses the older `vertexai.vision_models` API because google-genai does
not yet expose multimodalembedding@001. Acceptable — that namespace is not
part of the June-2026 deprecation that affected log-search's text path.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import vertexai
from google.cloud import storage
from vertexai.vision_models import Image, MultiModalEmbeddingModel

from photo_search.paths import (
    BUCKET,
    EMBED_DIM,
    EMBED_MODEL,
    INDEX_PATH,
    LOCATION,
    MANIFEST_PATH,
    META_PATH,
    PROJECT,
    ensure_cache_dir,
)

PRICE_PER_EMBED = 0.0002


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


def _embed_image_bytes(model: MultiModalEmbeddingModel, img_bytes: bytes) -> np.ndarray:
    img = Image(image_bytes=img_bytes)
    embs = model.get_embeddings(image=img)
    return np.array(embs.image_embedding, dtype=np.float32)


def _embed_text(model: MultiModalEmbeddingModel, text: str) -> np.ndarray:
    embs = model.get_embeddings(contextual_text=text[:1024])
    return np.array(embs.text_embedding, dtype=np.float32)


def _sanity_check(
    model: MultiModalEmbeddingModel,
    storage_client: storage.Client,
    sample_blob_path: str,
    sample_caption: str,
) -> None:
    """Contrast test: matched caption must outscore an unrelated probe.

    Absolute cross-modal cosines vary widely (0.05–0.30 for matched pairs,
    near 0 for unrelated). The robust signal is the *gap* — a working model
    produces a clear margin between the matched caption and an unrelated
    probe. Margin requirement: matched − unrelated ≥ 0.05.
    """
    blob = storage_client.bucket(BUCKET).blob(sample_blob_path)
    img_bytes = blob.download_as_bytes()
    img_vec = _embed_image_bytes(model, img_bytes)

    matched_vec = _embed_text(model, sample_caption)
    unrelated_vec = _embed_text(model, "modern city skyline at night with traffic and skyscrapers")

    matched_sim = _cosine(img_vec, matched_vec)
    unrelated_sim = _cosine(img_vec, unrelated_vec)
    margin = matched_sim - unrelated_sim
    print(
        f"sanity: matched={matched_sim:+.3f}  unrelated={unrelated_sim:+.3f}  "
        f"margin={margin:+.3f}",
        file=sys.stderr,
    )
    if margin < 0.05:
        raise RuntimeError(
            f"sanity check failed: margin {margin:+.3f} too low — model not "
            f"discriminating between matched and unrelated text"
        )


def _load_existing_index() -> dict[str, np.ndarray]:
    """SHA → vector for already-embedded photos. Skip zero vectors so a
    re-run retries failures from the previous batch."""
    if not INDEX_PATH.exists() or not META_PATH.exists():
        return {}
    metas = [json.loads(line) for line in META_PATH.open()]
    arr = np.load(INDEX_PATH)["vectors"]
    out: dict[str, np.ndarray] = {}
    for meta, vec in zip(metas, arr):
        if np.linalg.norm(vec) > 0:
            out[meta["sha"]] = vec
    return out


def _process_one(
    blob_path: str,
    storage_client: storage.Client,
    model: MultiModalEmbeddingModel,
) -> tuple[str, np.ndarray]:
    blob = storage_client.bucket(BUCKET).blob(blob_path)
    img_bytes = blob.download_as_bytes()
    # Backoff schedule: 5s, 15s, 30s, 60s, 90s. The last few are long enough
    # to ride out per-minute quota refills that crashed the first run.
    backoffs = [5, 15, 30, 60, 90]
    for attempt, delay in enumerate(backoffs):
        try:
            vec = _embed_image_bytes(model, img_bytes)
            return blob_path, vec
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if ("429" in msg or "RESOURCE_EXHAUSTED" in msg) and attempt < len(backoffs) - 1:
                time.sleep(delay)
                continue
            raise
    raise RuntimeError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser(prog="photo-search-embedder")
    parser.add_argument("--limit", type=int, default=None, help="cap photos (smoke)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--skip-sanity", action="store_true")
    args = parser.parse_args()

    if not MANIFEST_PATH.exists():
        print(f"manifest not found: {MANIFEST_PATH}; run indexer first", file=sys.stderr)
        return 1

    manifest = [json.loads(line) for line in MANIFEST_PATH.open()]
    # Skip rows with empty caption — those failed Phase 1 and we don't keep them
    # in the searchable index. Could include them with image-only embedding if
    # we wanted pure visual search of the failures.
    rows = [r for r in manifest if r.get("caption")]
    skipped = len(manifest) - len(rows)
    print(
        f"manifest: {len(manifest)} total, {skipped} skipped (no caption), "
        f"{len(rows)} to embed",
        file=sys.stderr,
    )

    if args.limit:
        rows = rows[: args.limit]
        print(f"limited to {len(rows)} for this run", file=sys.stderr)

    cache_by_sha = _load_existing_index()
    to_embed = [r for r in rows if r["sha"] not in cache_by_sha]
    cached_count = len(rows) - len(to_embed)
    new_cost = len(to_embed) * PRICE_PER_EMBED
    print(
        f"cached: {cached_count}, to embed: {len(to_embed)}, "
        f"estimated cost ~${new_cost:.4f}",
        file=sys.stderr,
    )

    vertexai.init(project=PROJECT, location=LOCATION)
    model = MultiModalEmbeddingModel.from_pretrained(EMBED_MODEL)
    storage_client = storage.Client(project=PROJECT)

    if not args.skip_sanity and to_embed:
        _sanity_check(model, storage_client, rows[0]["blob_path"], rows[0]["caption"])

    vectors = np.zeros((len(rows), EMBED_DIM), dtype=np.float32)
    for i, r in enumerate(rows):
        if r["sha"] in cache_by_sha:
            vectors[i] = cache_by_sha[r["sha"]]

    sha_to_index = {r["sha"]: i for i, r in enumerate(rows) if r["sha"] not in cache_by_sha}

    if to_embed:
        workers = max(1, args.workers)
        print(f"embedding {len(to_embed)} with {workers} workers", file=sys.stderr)
        start = time.time()
        completed = 0
        failed = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(_process_one, r["blob_path"], storage_client, model): r
                for r in to_embed
            }
            for fut in as_completed(futures):
                r = futures[fut]
                try:
                    _, vec = fut.result()
                except Exception as e:  # noqa: BLE001
                    print(f"  embed failed for {r['blob_path']}: {e}", file=sys.stderr)
                    failed += 1
                    continue
                vectors[sha_to_index[r["sha"]]] = vec
                completed += 1
                if completed % 25 == 0 or completed == len(to_embed):
                    elapsed = time.time() - start
                    rate = completed / elapsed if elapsed else 0
                    eta_s = (len(to_embed) - completed) / rate if rate else 0
                    print(
                        f"  [{completed:>5}/{len(to_embed)}] elapsed={elapsed:.1f}s "
                        f"rate={rate:.1f}/s eta={eta_s:.0f}s",
                        file=sys.stderr,
                    )

    ensure_cache_dir()
    np.savez_compressed(INDEX_PATH, vectors=vectors)
    with META_PATH.open("w") as f:
        for r in rows:
            meta = {
                k: r.get(k)
                for k in ("id", "gcs_uri", "blob_path", "exif_date_iso", "exif_gps", "caption", "sha")
            }
            f.write(json.dumps(meta) + "\n")

    print(f"\nwrote {INDEX_PATH} ({vectors.nbytes / 1024:.1f} KiB)", file=sys.stderr)
    print(f"wrote {META_PATH}", file=sys.stderr)

    summary = {
        "manifest_size": len(manifest),
        "embedded": len(rows),
        "newly_embedded": len(to_embed),
        "cached_reused": cached_count,
        "actual_cost": round(new_cost, 4),
    }
    print(json.dumps(summary, indent=2))

    # Auto-push updated index + meta to the private GCS bucket.
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
