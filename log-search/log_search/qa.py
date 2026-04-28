"""Retrieve + generate primitives shared by the CLI and the FastAPI server."""

from __future__ import annotations

import numpy as np
from google.genai import Client

from log_search.paths import EMBED_MODEL, GENERATE_MODEL
from log_search.retriever import Hit, parse_date_filter, search

PROMPT_TEMPLATE = """\
You are answering a question about Sergey's working journal.

Use ONLY the journal excerpts below. Cite each fact as [n] where n is the excerpt
number. If the excerpts do not contain the answer, say so plainly — do not make
up details. Do not invent dates, names, or numbers that aren't in the excerpts.

QUESTION:
{query}

EXCERPTS:
{excerpts}

ANSWER (1-3 short paragraphs, with [n] citations):
"""


def format_excerpts(hits: list[Hit]) -> str:
    parts = []
    for h in hits:
        meta_line = f"[{h.rank}] {h.file} :: {h.heading_path}"
        if h.date_iso:
            meta_line += f" :: {h.date_iso}"
        parts.append(f"{meta_line}\n{h.text}")
    return "\n\n---\n\n".join(parts)


def embed_query(text: str, client: Client) -> np.ndarray:
    result = client.models.embed_content(model=EMBED_MODEL, contents=text)
    return np.array(result.embeddings[0].values, dtype=np.float32)


def retrieve(
    query: str,
    client: Client,
    vectors: np.ndarray,
    metas: list[dict],
    texts: dict[str, str],
    *,
    k: int = 5,
) -> tuple[list[Hit], tuple[str | None, str | None]]:
    """Embed the query, run cosine top-k, return (hits, (date_lo, date_hi))."""
    q_emb = embed_query(query, client)
    date_lo, date_hi = parse_date_filter(query)
    hits = search(q_emb, vectors, metas, texts, k=k, date_lo=date_lo, date_hi=date_hi)
    return hits, (date_lo, date_hi)


def generate(query: str, hits: list[Hit], client: Client) -> tuple[str, dict]:
    """Run Gemini generation over the hits. Returns (answer_text, usage_dict)."""
    excerpts = format_excerpts(hits)
    prompt = PROMPT_TEMPLATE.format(query=query, excerpts=excerpts)
    resp = client.models.generate_content(model=GENERATE_MODEL, contents=prompt)

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
