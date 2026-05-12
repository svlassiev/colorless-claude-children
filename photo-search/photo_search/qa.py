"""Retrieve + generate primitives shared by the CLI and the FastAPI server.

Multimodal embedding side uses `vertexai.vision_models.MultiModalEmbeddingModel`
(same client used in Phase 2). Generation side uses google-genai's Gemini 2.5
Pro, which can read GCS URIs directly via Part.from_uri.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import numpy as np
from google import genai
from google.cloud import storage
from google.genai import types
from vertexai.vision_models import MultiModalEmbeddingModel

from photo_search.paths import BUCKET, GENERATE_MODEL
from photo_search.retriever import Hit, search
from photo_search.tools.base import Filters

GENERATION_PROMPT = """\
You are answering a question about Sergey's personal photo collection.

Look at the {n} photos provided above and answer the user's query. For each
relevant photo, briefly describe what's visible. If a photo is not relevant
to the query, say so for that photo. Do not speculate about the identities
of the people in the photos. Stay concise — 2-3 short paragraphs total.

USER QUERY: {query}

ANSWER:"""


def embed_query(text: str, embed_model: MultiModalEmbeddingModel) -> np.ndarray:
    embs = embed_model.get_embeddings(contextual_text=text[:1024])
    return np.array(embs.text_embedding, dtype=np.float32)


def retrieve(
    query: str,
    embed_model: MultiModalEmbeddingModel,
    vectors: np.ndarray,
    metas: list[dict],
    *,
    k: int = 5,
    filters: Filters | None = None,
) -> list[Hit]:
    """Embed the query and run filtered cosine top-k.

    `filters` is composed upstream by the server from the Flash routing
    layer (`photo_search.routing.route_query`) and the regex date
    fast-path (`retriever.parse_date_filter`). Passing None is fine —
    the search becomes pure vector similarity, same as before this
    layer existed.
    """
    q_emb = embed_query(query, embed_model)
    return search(q_emb, vectors, metas, k=k, filters=filters)


def _download_blob_bytes(hit: Hit, storage_client: storage.Client) -> bytes:
    """Pull image bytes for a single hit. The bucket lookup is cheap and
    keeps this safely callable from a worker thread."""
    blob_name = hit.gcs_uri.removeprefix(f"gs://{BUCKET}/")
    return storage_client.bucket(BUCKET).blob(blob_name).download_as_bytes()


def fetch_image_bytes(
    hits: list[Hit],
    storage_client: storage.Client,
    *,
    max_workers: int = 8,
) -> dict[str, bytes]:
    """Download bytes for `hits` in parallel; key by sha for stable lookups
    across rerank → generate.

    Used by the rerank pipeline to fetch once and reuse for both Flash and
    Pro. `generate()` accepts the result via `prefetched_bytes`.
    """
    if not hits:
        return {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(hits))) as ex:
        bytes_list = list(ex.map(lambda h: _download_blob_bytes(h, storage_client), hits))
    return {h.sha: b for h, b in zip(hits, bytes_list)}


def generate(
    query: str,
    hits: list[Hit],
    gen_client: genai.Client,
    storage_client: storage.Client,
    *,
    max_output_tokens: int | None = None,
    prefetched_bytes: dict[str, bytes] | None = None,
) -> tuple[str, dict]:
    """Pass query + retrieved images (with date+caption metadata) to Gemini.

    Downloads image bytes locally and sends inline, rather than passing GCS
    URIs. This bypasses Vertex's service-agent → GCS access path, which
    requires one-time provisioning that can fail on first use. Cost: local
    bandwidth (~1-2 MB per image; 5-10 MB per 5-image query).

    When `prefetched_bytes` is provided (sha → bytes), reuse those instead
    of re-downloading — the rerank pipeline already fetched them.

    `max_output_tokens` caps Gemini's TOTAL output (thinking + visible).
    When None, scales with retrieval depth: 250 * len(hits). At k=8 → 2000
    (the prior fixed default); at k=20 → 5000, so the visible answer isn't
    starved when the model has more photos to summarize. Gemini 2.5 Pro is
    a reasoning model — thinking tokens count toward this budget and toward
    billing.

    Returns (answer_text, usage_dict).
    """
    if max_output_tokens is None:
        max_output_tokens = 250 * max(1, len(hits))

    # Resolve bytes for every hit: prefer the prefetched map, otherwise
    # download what's missing in parallel. After this block every hit has
    # an entry in `bytes_by_sha`.
    bytes_by_sha: dict[str, bytes] = dict(prefetched_bytes) if prefetched_bytes else {}
    missing = [h for h in hits if h.sha not in bytes_by_sha]
    if missing:
        bytes_by_sha.update(fetch_image_bytes(missing, storage_client))

    contents: list = []
    for h in hits:
        header = f"\n\n--- Photo [{h.rank}] (date: {h.date_iso or 'unknown'}, score: {h.score:.3f}) ---"
        contents.append(header)
        contents.append(
            types.Part.from_bytes(data=bytes_by_sha[h.sha], mime_type="image/jpeg")
        )
        if h.caption:
            contents.append(f"Auto-generated caption: {h.caption}")
    contents.append("\n\n" + GENERATION_PROMPT.format(n=len(hits), query=query))

    resp = gen_client.models.generate_content(
        model=GENERATE_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(max_output_tokens=max_output_tokens),
    )

    usage: dict = {"tokens_in": 0, "tokens_out": 0, "tokens_thoughts": 0, "cost": None}
    meta = getattr(resp, "usage_metadata", None)
    if meta is not None:
        in_tok = getattr(meta, "prompt_token_count", 0) or 0
        visible_out = getattr(meta, "candidates_token_count", 0) or 0
        thoughts = getattr(meta, "thoughts_token_count", 0) or 0
        # Gemini 2.5 Pro: $1.25/1M input (≤200k context), $10/1M output.
        # Output billing covers thinking + visible — bill on the sum.
        billable_out = visible_out + thoughts
        usage["tokens_in"] = in_tok
        usage["tokens_out"] = visible_out
        usage["tokens_thoughts"] = thoughts
        usage["cost"] = in_tok * 1.25 / 1_000_000 + billable_out * 10 / 1_000_000

    return (resp.text or "").strip(), usage
