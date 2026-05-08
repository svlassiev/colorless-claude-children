"""Soft-failure wrapper around the sync `generate(...)` calls in
photo_search.qa and log_search.qa.

Why: both route handlers call `generate()` synchronously inside an async
endpoint. Two problems with the bare call site:

1. A slow Gemini response blocks the event loop until completion.
2. Any exception (timeout, quota, transport) bubbles up as a 500 — the
   client error handler then wipes the citations the retriever already
   produced. We want the photos/excerpts to render even when the LLM
   summary fails.

`safe_generate` runs the sync function in a worker thread and applies an
asyncio timeout. On success: the original (answer, usage). On timeout or
exception: a structured outcome with a short, user-safe error string and
a zeroed usage dict. Callers keep their citations and surface the error
inline.

Note on cancelation: Python can't cancel threads. On timeout the worker
keeps running until the SDK call returns, then its result is discarded.
This is acceptable for a personal site — we trade a stranded thread for
a responsive UI.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any, Callable

DEFAULT_TIMEOUT_S = 60.0

_EMPTY_USAGE: dict[str, Any] = {
    "tokens_in": 0,
    "tokens_out": 0,
    "tokens_thoughts": 0,
    "cost": None,
}


@dataclass(frozen=True)
class GenerationOutcome:
    answer: str | None
    usage: dict[str, Any]
    error: str | None


async def safe_generate(
    fn: Callable[..., tuple[str, dict[str, Any]]],
    /,
    *args: Any,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    **kwargs: Any,
) -> GenerationOutcome:
    try:
        answer, usage = await asyncio.wait_for(
            asyncio.to_thread(fn, *args, **kwargs),
            timeout=timeout_s,
        )
        return GenerationOutcome(answer=answer, usage=usage, error=None)
    except asyncio.TimeoutError:
        return GenerationOutcome(
            answer=None,
            usage=dict(_EMPTY_USAGE),
            error=f"generation timed out (>{timeout_s:g}s) — showing matches only",
        )
    except Exception as e:
        # Log the full exception server-side; surface only the type name
        # to the client. Leaking SDK error bodies on a public endpoint is
        # a poor default.
        print(
            f"safe_generate: {fn.__name__} raised {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return GenerationOutcome(
            answer=None,
            usage=dict(_EMPTY_USAGE),
            error="generation failed — showing matches only",
        )
