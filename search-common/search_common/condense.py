"""Condense-question step for conversational follow-ups.

A follow-up like "а зимой?" is useless to routing and retrieval on its own.
One cheap Flash call rewrites it into a standalone query using the visible
conversation history — everything downstream (filters, retrieval, deep
classification, the language rule) then runs on the rewrite, which is why
chat composes with the rest of the pipeline without touching it.

Soft-fail contract: any error, timeout, or empty result returns the original
query unchanged — the request then behaves exactly like a fresh single-shot
question.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from google.genai import types

CONDENSE_TIMEOUT_S = 8.0

_PROMPT = """\
Rewrite the user's follow-up into ONE standalone search query over a personal
archive, resolving pronouns and references using the conversation below.

Rules:
- Output ONLY the rewritten query text — no quotes, no explanations.
- Write it in the SAME language the follow-up is written in.
- Keep every concrete term (places, names, dates) the follow-up or its
  antecedents mention; invent nothing.
- If the follow-up is already self-contained, return it unchanged.

Conversation:
{history}

Follow-up: {query}

Standalone query:"""


def _sync_condense(client: Any, model: str, prompt: str) -> str:
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            max_output_tokens=1200,
            temperature=0.0,
            # A rewrite needs no reasoning; keep it fast and cheap.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    return (resp.text or "").strip()


async def condense_query(
    client: Any,
    model: str,
    query: str,
    history: list[dict],
    *,
    timeout_s: float = CONDENSE_TIMEOUT_S,
) -> str:
    """History rows: {"role": "user"|"assistant", "text": str}. Never raises."""
    if not history:
        return query
    lines = [f"{h['role'].upper()}: {h['text']}" for h in history]
    prompt = _PROMPT.format(history="\n".join(lines), query=query)
    try:
        rewritten = await asyncio.wait_for(
            asyncio.to_thread(_sync_condense, client, model, prompt),
            timeout=timeout_s,
        )
        # A rewrite that balloons or vanishes is a failed rewrite.
        if rewritten and len(rewritten) <= 4 * max(len(query), 80):
            return rewritten
    except Exception as e:  # noqa: BLE001
        print(f"condense: {type(e).__name__}: {e} — using raw query", file=sys.stderr)
    return query
