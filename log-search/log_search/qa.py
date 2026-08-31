"""Retrieve + generate primitives shared by the CLI and the FastAPI server."""

from __future__ import annotations

import numpy as np
from google.genai import Client
from google.genai import types

from log_search.paths import EMBED_MODEL, GENERATE_MODEL
from log_search.retriever import Hit, parse_date_filter, search
from search_common.pricing import generation_cost

PROMPT_TEMPLATE = """\
You are answering a question about Sergey's working journal.

Use ONLY the journal excerpts below. Cite each fact as [n] where n is the excerpt
number. If the excerpts do not contain the answer, say so plainly — do not make
up details. Do not invent dates, names, or numbers that aren't in the excerpts.
{history_block}{analysis_block}{voice_block}
Answer in the language the user WROTE the question in. Judge the question's
language by its grammar and function words, not by proper names: a question
written in English that merely contains Russian place or person names is an
English question — answer in English. A question written in Russian gets an answer
written ENTIRELY in Russian.

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


_HISTORY_BLOCK = (
    "\nThis is a follow-up in a conversation. Previous exchange, for context"
    " only — answer the CURRENT question:\n{history}\n"
)

# Deep mode: synthesis across excerpts instead of a stitched summary.
_ANALYSIS_BLOCK = (
    "\nThis question asks for ANALYSIS. Treat the excerpts as one body of"
    " evidence: synthesize the answer FIRST (the pattern, the comparison, the"
    " overall picture), then support it, citing [n] for every claim and"
    " quantifying where the excerpts allow. Name what the excerpts do NOT"
    " cover instead of stretching them. Up to 4-5 paragraphs.\n"
)

_VOICE_BLOCK = (
    "\nWrite in the journal owner's register — these tone rules override any"
    " default assistant style:\n{voice}\n"
)

DEEP_THINKING_BUDGET = 8192
DEEP_PER_HIT_TOKENS = 400
DEEP_MIN_VISIBLE_TOKENS = 1500

# Query-side task type — pairs with the embedder's RETRIEVAL_DOCUMENT.
_QUERY_EMBED_CONFIG = (
    types.EmbedContentConfig(task_type="RETRIEVAL_QUERY")
    if EMBED_MODEL.startswith("gemini-embedding")
    else None
)


def embed_query(text: str, client: Client) -> np.ndarray:
    result = client.models.embed_content(
        model=EMBED_MODEL, contents=text, config=_QUERY_EMBED_CONFIG
    )
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


def generate(
    query: str,
    hits: list[Hit],
    client: Client,
    *,
    max_output_tokens: int | None = None,
    deep: bool = False,
    voice: str | None = None,
    history: str | None = None,
) -> tuple[str, dict]:
    """Run Gemini generation over the hits. Returns (answer_text, usage_dict).

    `max_output_tokens` caps Gemini's TOTAL output (thinking + visible).
    When None, scales with retrieval depth: 300 * len(hits). At k=8 → 2400;
    at k=20 → 6000, so the visible answer isn't starved when more chunks are
    summarised. The Gemini generate models think by default — thinking tokens
    count toward this budget and toward billing. (Raised from 250/hit during
    the 3.x migration: both 2.5-pro and 3.6-flash truncated long syntheses at
    the old budget; 2.5-pro worse, since it thinks more.)
    """
    if max_output_tokens is None:
        if deep:
            max_output_tokens = DEEP_THINKING_BUDGET + max(
                DEEP_MIN_VISIBLE_TOKENS, DEEP_PER_HIT_TOKENS * len(hits)
            )
        else:
            max_output_tokens = 300 * max(1, len(hits))
    excerpts = format_excerpts(hits)
    prompt = PROMPT_TEMPLATE.format(
        query=query,
        excerpts=excerpts,
        analysis_block=_ANALYSIS_BLOCK if deep else "",
        voice_block=_VOICE_BLOCK.format(voice=voice) if voice else "",
        history_block=_HISTORY_BLOCK.format(history=history) if history else "",
    )
    config = types.GenerateContentConfig(max_output_tokens=max_output_tokens)
    if deep:
        # Same reasoning ceiling as the photo side; lookup mode keeps the
        # model's default thinking behavior it has always had here.
        config = types.GenerateContentConfig(
            max_output_tokens=max_output_tokens,
            thinking_config=types.ThinkingConfig(thinking_budget=DEEP_THINKING_BUDGET),
        )
    resp = client.models.generate_content(
        model=GENERATE_MODEL,
        contents=prompt,
        config=config,
    )

    usage: dict = {"tokens_in": 0, "tokens_out": 0, "tokens_thoughts": 0, "cost": None}
    meta = getattr(resp, "usage_metadata", None)
    if meta is not None:
        in_tok = getattr(meta, "prompt_token_count", 0) or 0
        visible_out = getattr(meta, "candidates_token_count", 0) or 0
        thoughts = getattr(meta, "thoughts_token_count", 0) or 0
        # Output billing covers thinking + visible — bill on the sum.
        billable_out = visible_out + thoughts
        usage["tokens_in"] = in_tok
        usage["tokens_out"] = visible_out
        usage["tokens_thoughts"] = thoughts
        usage["cost"] = generation_cost(GENERATE_MODEL, in_tok, billable_out)

    return (resp.text or "").strip(), usage
