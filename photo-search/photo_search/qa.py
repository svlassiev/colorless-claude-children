"""Retrieve + generate primitives shared by the CLI and the FastAPI server.

Multimodal embedding side uses `vertexai.vision_models.MultiModalEmbeddingModel`
(same client used in Phase 2). Generation side uses google-genai's Gemini 2.5
Pro, which can read GCS URIs directly via Part.from_uri.
"""

from __future__ import annotations

import numpy as np
from google import genai
from google.cloud import storage
from google.genai import types
from vertexai.vision_models import MultiModalEmbeddingModel

from photo_search.paths import BUCKET, GENERATE_MODEL
from photo_search.retriever import Hit, parse_date_filter, search

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
) -> tuple[list[Hit], tuple[str | None, str | None]]:
    q_emb = embed_query(query, embed_model)
    date_lo, date_hi = parse_date_filter(query)
    hits = search(q_emb, vectors, metas, k=k, date_lo=date_lo, date_hi=date_hi)
    return hits, (date_lo, date_hi)


def generate(
    query: str,
    hits: list[Hit],
    gen_client: genai.Client,
    storage_client: storage.Client,
) -> tuple[str, dict]:
    """Pass query + retrieved images (with date+caption metadata) to Gemini.

    Downloads image bytes locally and sends inline, rather than passing GCS
    URIs. This bypasses Vertex's service-agent → GCS access path, which
    requires one-time provisioning that can fail on first use. Cost: local
    bandwidth (~1-2 MB per image; 5-10 MB per 5-image query).

    Returns (answer_text, usage_dict).
    """
    contents: list = []
    bucket = storage_client.bucket(BUCKET)
    for h in hits:
        header = f"\n\n--- Photo [{h.rank}] (date: {h.date_iso or 'unknown'}, score: {h.score:.3f}) ---"
        contents.append(header)
        # h.blob_path is the path within the bucket (set by indexer)
        # h.gcs_uri is "gs://BUCKET/blob_path" — derive blob name back.
        blob_name = h.gcs_uri.removeprefix(f"gs://{BUCKET}/")
        img_bytes = bucket.blob(blob_name).download_as_bytes()
        contents.append(types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))
        if h.caption:
            contents.append(f"Auto-generated caption: {h.caption}")
    contents.append("\n\n" + GENERATION_PROMPT.format(n=len(hits), query=query))

    resp = gen_client.models.generate_content(model=GENERATE_MODEL, contents=contents)

    usage: dict = {"tokens_in": 0, "tokens_out": 0, "cost": None}
    meta = getattr(resp, "usage_metadata", None)
    if meta is not None:
        in_tok = getattr(meta, "prompt_token_count", 0) or 0
        out_tok = getattr(meta, "candidates_token_count", 0) or 0
        # Gemini 2.5 Pro: $1.25/1M input (≤200k context), $10/1M output
        usage["tokens_in"] = in_tok
        usage["tokens_out"] = out_tok
        usage["cost"] = in_tok * 1.25 / 1_000_000 + out_tok * 10 / 1_000_000

    return (resp.text or "").strip(), usage
