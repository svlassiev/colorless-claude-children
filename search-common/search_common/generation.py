"""Soft-failure wrappers around sync Gemini calls.

Two primitives live here:

- `safe_generate` — wraps the text-generation `generate(...)` calls in
  photo_search.qa and log_search.qa. Returns `GenerationOutcome`.
- `tool_call` — wraps a single-turn Gemini function-calling request used
  by the pre-retrieval routing layer. Returns `ToolCallOutcome` carrying
  the raw `(name, args_dict)` for each function_call the model emitted.

Both share the same shape: sync SDK call moved off the event loop via
`asyncio.to_thread`, bounded by `asyncio.wait_for`, exceptions captured
into a typed outcome rather than raised. Callers stay responsive when
the upstream is slow or flaky.

Why both functions live in one module: they're the same pattern applied
to two endpoints of the Gemini API. Keeping them together makes the
"how we talk to Gemini safely from FastAPI" contract one file to read.

Note on cancelation: Python can't cancel threads. On timeout the worker
keeps running until the SDK call returns, then its result is discarded.
Acceptable for a personal site — we trade a stranded thread for a
responsive UI.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any, Callable

DEFAULT_TIMEOUT_S = 60.0
TOOL_CALL_TIMEOUT_S = 8.0

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


@dataclass(frozen=True)
class RawToolCall:
    """One function_call emitted by the model, before per-tool validation.

    `args` is the raw dict the SDK parsed from the model's response — keys
    and value shapes are up to the schema we sent, but a misbehaving model
    can still emit malformed values. Per-tool Pydantic validation happens
    in the dispatcher, not here.
    """

    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolCallOutcome:
    """Result of one routing call.

    `calls` is always a list — empty on timeout/exception, empty when the
    model declined to call any tool in AUTO mode (a legitimate outcome),
    and one-or-more when it did.

    `error` is None on success AND on the legitimate zero-call case; it
    is populated only for timeout / transport failures so the caller can
    distinguish "model said nothing applies" from "we never heard back."
    """

    calls: list[RawToolCall]
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


def _parse_function_calls(resp: Any) -> list[RawToolCall]:
    """Walk response.candidates[0].content.parts; collect function_call parts.

    The SDK shape: `resp.candidates` is a list; we take candidate[0] (Gemini
    returns one unless you explicitly request more). `content.parts` may mix
    text parts, function_call parts, and others. We pick only the ones that
    have a `.function_call.name` set.
    """
    candidates = getattr(resp, "candidates", None) or []
    if not candidates:
        return []
    content = getattr(candidates[0], "content", None)
    parts = getattr(content, "parts", None) or []
    out: list[RawToolCall] = []
    for p in parts:
        fc = getattr(p, "function_call", None)
        name = getattr(fc, "name", None) if fc is not None else None
        if not name:
            continue
        # `fc.args` is a proto Map — dict() materializes it as plain dict[str, Any].
        out.append(RawToolCall(name=name, args=dict(getattr(fc, "args", None) or {})))
    return out


def _tool_call_sync(
    client: Any,
    model: str,
    contents: Any,
    tools: list[Any],
    tool_config: Any,
    system_instruction: str | None,
    temperature: float,
) -> Any:
    """Sync wrapper around generate_content with tools/tool_config wired in.

    Lives here (not inline) so `asyncio.to_thread` can submit it directly.
    All keyword names match google-genai's expected `GenerateContentConfig`
    fields — if the SDK reshapes these in a future version, this is the
    single place to fix.
    """
    from google.genai import types

    cfg = types.GenerateContentConfig(
        tools=tools,
        tool_config=tool_config,
        temperature=temperature,
        system_instruction=system_instruction,
    )
    return client.models.generate_content(model=model, contents=contents, config=cfg)


async def tool_call(
    client: Any,
    model: str,
    contents: Any,
    tools: list[Any],
    *,
    tool_config: Any | None = None,
    timeout_s: float = TOOL_CALL_TIMEOUT_S,
    system_instruction: str | None = None,
    temperature: float = 0.0,
) -> ToolCallOutcome:
    """Single-turn Gemini function-calling. Never raises.

    `client` is a `google.genai.Client` (typed as Any to keep this module
    SDK-version-tolerant). `tools` is a list of `types.Tool`; `tool_config`
    is a `types.ToolConfig` (AUTO/ANY/NONE). Pass `temperature=0` for
    routing — we want deterministic dispatch.

    On timeout or unexpected exception, returns `calls=[]` plus a short
    error string. The caller decides whether to fall back to a regex
    parser, run unfiltered retrieval, or surface the error.
    """
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                _tool_call_sync,
                client,
                model,
                contents,
                tools,
                tool_config,
                system_instruction,
                temperature,
            ),
            timeout=timeout_s,
        )
        return ToolCallOutcome(calls=_parse_function_calls(resp), error=None)
    except asyncio.TimeoutError:
        return ToolCallOutcome(
            calls=[],
            error=f"tool_call timed out (>{timeout_s:g}s)",
        )
    except Exception as e:
        print(
            f"tool_call: model={model} raised {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return ToolCallOutcome(calls=[], error="tool_call failed")
