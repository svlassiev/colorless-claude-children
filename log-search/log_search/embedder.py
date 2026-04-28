"""Embed chunks with text-embedding-005, persist to index.npz.

Cost-aware:
- Estimates spend up front, prints to stdout.
- Sanity-checks the model on two semantically close strings before the batch.
- Caches by chunk SHA — re-running on unchanged chunks does not re-bill.
"""

from __future__ import annotations

import json
import sys
import time

import numpy as np
from google import genai

from log_search.paths import (
    CHUNKS_PATH,
    EMBED_DIM,
    EMBED_MODEL,
    INDEX_PATH,
    LOCATION,
    META_PATH,
    PROJECT,
    ensure_cache_dir,
)

BATCH_SIZE = 25
PRICE_PER_1K_CHARS = 0.000025


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


def _sanity_check(client: genai.Client) -> None:
    pair = [
        "We deployed the new feature to production after the code review.",
        "The pull request was merged once it passed all tests in CI.",
    ]
    result = client.models.embed_content(model=EMBED_MODEL, contents=pair)
    a = np.array(result.embeddings[0].values, dtype=np.float32)
    b = np.array(result.embeddings[1].values, dtype=np.float32)
    sim = _cosine(a, b)
    print(f"sanity: cosine(close pair) = {sim:.3f}", file=sys.stderr)
    if sim < 0.5:
        raise RuntimeError(f"sanity check failed: cosine {sim:.3f} too low")


def _load_existing_cache() -> dict[str, np.ndarray]:
    """SHA → vector for already-embedded chunks. Empty on first run."""
    if not INDEX_PATH.exists() or not META_PATH.exists():
        return {}
    cache: dict[str, np.ndarray] = {}
    metas = [json.loads(l) for l in META_PATH.open()]
    arr = np.load(INDEX_PATH)["vectors"]
    for meta, vec in zip(metas, arr):
        cache[meta["sha"]] = vec
    return cache


def main() -> int:
    if not CHUNKS_PATH.exists():
        print(f"chunks not found: {CHUNKS_PATH}; run chunker first", file=sys.stderr)
        return 1

    chunks = [json.loads(l) for l in CHUNKS_PATH.open()]
    n = len(chunks)
    char_total = sum(c["char_count"] for c in chunks)
    est_cost = char_total * PRICE_PER_1K_CHARS / 1000

    cache = _load_existing_cache()
    to_embed_idx = [i for i, c in enumerate(chunks) if c["sha"] not in cache]
    cached_count = n - len(to_embed_idx)
    new_chars = sum(chunks[i]["char_count"] for i in to_embed_idx)
    new_cost = new_chars * PRICE_PER_1K_CHARS / 1000

    print(f"corpus: {n} chunks, {char_total:,} chars, full re-embed cost ${est_cost:.4f}")
    print(f"cached: {cached_count} chunks reused")
    print(f"to embed: {len(to_embed_idx)} chunks, {new_chars:,} new chars, cost ~${new_cost:.4f}")
    print(f"model: {EMBED_MODEL}, region: {LOCATION}")
    print()

    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)

    if to_embed_idx:
        _sanity_check(client)

    vectors = np.zeros((n, EMBED_DIM), dtype=np.float32)
    for i, c in enumerate(chunks):
        if c["sha"] in cache:
            vectors[i] = cache[c["sha"]]

    if not to_embed_idx:
        print("nothing new to embed — everything cached.")
    else:
        start = time.time()
        for batch_start in range(0, len(to_embed_idx), BATCH_SIZE):
            batch_idx = to_embed_idx[batch_start : batch_start + BATCH_SIZE]
            batch_texts = [chunks[i]["text"] for i in batch_idx]

            for attempt in range(5):
                try:
                    result = client.models.embed_content(model=EMBED_MODEL, contents=batch_texts)
                    break
                except Exception as e:  # noqa: BLE001
                    msg = str(e)
                    if ("429" in msg or "RESOURCE_EXHAUSTED" in msg) and attempt < 4:
                        backoff = 2**attempt
                        print(f"  rate-limited, sleeping {backoff}s", file=sys.stderr)
                        time.sleep(backoff)
                        continue
                    raise

            for k, emb in enumerate(result.embeddings):
                vectors[batch_idx[k]] = np.array(emb.values, dtype=np.float32)

            done = batch_start + len(batch_idx)
            elapsed = time.time() - start
            print(f"  [{done:>4}/{len(to_embed_idx)}]  elapsed={elapsed:.1f}s")

    ensure_cache_dir()
    np.savez_compressed(INDEX_PATH, vectors=vectors)
    with META_PATH.open("w") as f:
        for c in chunks:
            meta = {k: c[k] for k in ("id", "file", "date_iso", "heading_path", "char_count", "sha")}
            f.write(json.dumps(meta) + "\n")

    print()
    print(f"wrote {INDEX_PATH} ({vectors.nbytes / 1024:.1f} KiB)")
    print(f"wrote {META_PATH}")
    print(f"this-run cost: ~${new_cost:.4f}")

    # Auto-push updated artefacts to the private GCS bucket. Class A ops at
    # $0.005/1k → effectively $0 for our handful of files. Idempotent — no-op
    # if remote is already up to date.
    try:
        from log_search.cloud_cache import push_to_gcs

        pushed = push_to_gcs()
        if pushed:
            print(f"pushed {pushed} cache file(s) to GCS", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"warning: cloud-cache push failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
