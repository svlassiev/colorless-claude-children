"""Deep-answer classification, shared by both corpora.

One function declaration decides the answer mode: `request_deep_answer` is
called by the routing model when the query asks for analysis/synthesis over
the retrieved material rather than a lookup. On the photo side the
declaration rides the existing filter-routing call (zero added latency); the
log side runs `classify_deep` concurrently with retrieval, so its latency is
hidden behind the embedding + top-k work.

Soft-fail contract: any failure or timeout classifies as NOT deep — the
query then takes exactly the pre-feature path.
"""

from __future__ import annotations

from typing import Any

from google.genai import types

from search_common.generation import tool_call

DECLARATION = types.FunctionDeclaration(
    name="request_deep_answer",
    description=(
        "Call this when the query asks for analysis or synthesis ACROSS the "
        "retrieved material: aggregation, comparison, patterns or trends, an "
        "overview or summary, counting or ranking, reasons, feelings, or "
        "'what was typical/popular/most X'. Signals include: 'what was "
        "popular', 'how did X change', 'summarise everything about', 'why', "
        "'how do I usually', 'compare', 'сколько раз', 'как менялось', "
        "'подведи итог'. Do NOT call it for simple lookups — finding photos "
        "or entries about a subject, place, person or date ('photos of the "
        "dacha', 'закат на море', 'записи про Lovable')."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "reason": types.Schema(
                type=types.Type.STRING,
                description="One short phrase: what kind of analysis the query asks for.",
            )
        },
    ),
)

_CLASSIFIER_INSTRUCTION = """\
You decide whether a search query over a personal archive needs a DEEP
analytical answer or a simple lookup. Call request_deep_answer only for
queries that require reasoning across many items; emit no call for lookups.
Do not produce free-text output."""


async def classify_deep(
    client: Any, model: str, query: str, *, timeout_s: float = 8.0
) -> bool:
    """Standalone classifier call (log corpus). Never raises; False on failure."""
    tool = types.Tool(function_declarations=[DECLARATION])
    tool_config = types.ToolConfig(
        function_calling_config=types.FunctionCallingConfig(
            mode=types.FunctionCallingConfigMode.AUTO,
        )
    )
    outcome = await tool_call(
        client,
        model=model,
        contents=query,
        tools=[tool],
        tool_config=tool_config,
        timeout_s=timeout_s,
        system_instruction=_CLASSIFIER_INSTRUCTION,
    )
    return any(c.name == "request_deep_answer" for c in outcome.calls)
