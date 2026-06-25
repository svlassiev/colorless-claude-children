"""Pre-retrieval routing: one Gemini 2.5 Flash call decides which filters
apply to the user's query, before any photo retrieval runs.

Single-turn function calling. The model sees the user query and the list
of declared tools (currently just `filter_by_location` — date will join
when its tool is added). In AUTO mode the model can call zero, one, or
multiple tools in parallel. Each call's args are validated against the
tool's Pydantic model; failures are logged and skipped, never raised.

This module is photo-specific only insofar as it imports the photo tools
package and the photo `Filters` shape. The Gemini call mechanics live in
`search_common.generation.tool_call` so log-search can adopt the same
pattern without duplication.

Soft-fail contract: route_query never raises. On timeout / transport
error / SDK shape change, it returns `Filters()` and logs to stderr.
The server then runs retrieval without filters — the same behavior the
user got before this layer existed.

Cost / latency: one Flash call per request, deterministic (temperature=0).
Roughly 200-500 ms p50 on a warm Cloud Run instance; ~$0.00003 per query.
"""

from __future__ import annotations

import sys
from dataclasses import replace

from google import genai
from google.genai import types
from pydantic import ValidationError

from photo_search.paths import ROUTING_MODEL
from photo_search.tools import (
    ALL_DECLARATIONS,
    TOOL_REGISTRY,
    DateFilter,
    Filters,
    LocationFilter,
    PersonFilter,
    ProximityFilter,
    filter_by_person,
)
from search_common.generation import tool_call
# 8 s wasn't enough for the cold first call after server startup — the
# initial Vertex auth handshake + first-token latency can land in the
# 8-10 s range. 15 s keeps the warm-case latency invisible (Flash p50
# returns in ~500 ms) while tolerating cold start without timing out.
ROUTING_TIMEOUT_S = 15.0

# System instruction is a small, durable contract with the model: what
# the job is, what "good" looks like, and what to avoid. Iterate here
# after watching real routing decisions — first-draft prompts always
# need tightening.
_SYSTEM_INSTRUCTION = """\
You route natural-language photo-search queries to structured filters.

Read the user query. For each declared tool, the function declaration tells
you EXACTLY when to call it and with what arguments. Be conservative: call
a tool only when the query clearly matches its description. Vague or purely
topical queries should produce zero tool calls — the retrieval system handles
those without filters.

Multiple tools can apply to one query (e.g., a place AND a date range, or
two different places). In that case emit parallel function calls in one
response — do not chain them across turns.

Do not produce free-text output. Function calls only. If no tool applies,
return no function calls.
"""


def _build_tool_and_config(
    declarations: list[types.FunctionDeclaration],
) -> tuple[list[types.Tool], types.ToolConfig]:
    """Bundle declarations into the SDK's `Tool` wrapper and pick AUTO mode.

    AUTO is the right mode for routing: the model decides whether to call
    a tool. ANY forces a call even on vague queries, which manufactures
    nonsense filters. NONE disables tools entirely (useful for testing
    that the model would otherwise produce free text — not what we want
    in production).
    """
    tool = types.Tool(function_declarations=declarations)
    tool_config = types.ToolConfig(
        function_calling_config=types.FunctionCallingConfig(
            mode=types.FunctionCallingConfigMode.AUTO,
        )
    )
    return [tool], tool_config


def _combine_people(parts: list[PersonFilter]) -> PersonFilter:
    """Fold N per-name person filters into one with AND (intersection) semantics.

    A query naming several people arrives as several parallel filter_by_person
    calls — one per name. The user means photos that contain EVERYONE named, so
    we intersect the per-name photo sets. A single call passes through unchanged
    (intersection of one set).
    """
    shas = parts[0].matched_shas
    for p in parts[1:]:
        shas = shas & p.matched_shas
    names = tuple(dict.fromkeys(n for p in parts for n in p.names))
    query = " + ".join(p.query for p in parts)
    # Per-term breakdown so generation can map a tagged person back to the search
    # term it matched (so a tag that is a given name is recognised as the surname
    # the user typed, rather than being mismatched to a similar-looking name).
    groups = tuple((p.query, p.names) for p in parts)
    return PersonFilter(query=query, names=names, matched_shas=frozenset(shas), groups=groups)


def _roster_block() -> str:
    """Append the known-people roster to the system instruction (allow-listed
    callers only). Empty when no aliases are loaded, so routing is unchanged then.
    """
    names = filter_by_person.roster()
    if not names:
        return ""
    return (
        "\n\nPERSON SEARCH. A filter_by_person tool is available. Call it whenever the "
        "query contains ANY personal name — a given name, nickname, patronymic, or "
        "SURNAME, including a plural family surname (a Russian surname ending -овы/-евы/"
        "-ины names that whole family). Emit the name EXACTLY as written. This is "
        "INDEPENDENT of place and date: when the query has BOTH a person and a place or "
        "year (e.g. a surname followed by a place name, or a name with a year), emit "
        "filter_by_person AND the place/date tools TOGETHER — never drop the person just "
        "because a place or date is also present. "
        "Calling filter_by_person for a word that is not a known person is harmless (it "
        "matches nobody), so PREFER to call it whenever a token plausibly names a person. "
        "Known people in this collection include:\n" + ", ".join(names)
    )


async def route_query(
    query: str,
    metas: list[dict],
    gen_client: genai.Client,
    *,
    allow_person: bool = False,
    timeout_s: float = ROUTING_TIMEOUT_S,
) -> Filters:
    """Ask Flash which filters apply, validate, dispatch, return Filters.

    `metas` is the in-memory photo metadata list (one row per photo) —
    each tool's executor needs it to compute its match set against the
    actual index. The router doesn't peek inside; it just passes it
    through to whichever executors the model invoked.

    Always returns a `Filters` instance, never raises. Empty `Filters()`
    on timeout, on zero tool calls, or on every call failing validation.
    """
    # Gate the person tool: only declare it — and inject the roster of known
    # people — for allow-listed callers, so anonymous users never learn that
    # person search exists.
    if allow_person:
        declarations = ALL_DECLARATIONS
        system_instruction = _SYSTEM_INSTRUCTION + _roster_block()
    else:
        declarations = [
            decl
            for name, (decl, *_rest) in TOOL_REGISTRY.items()
            if name != "filter_by_person"
        ]
        system_instruction = _SYSTEM_INSTRUCTION
    tools, tool_config = _build_tool_and_config(declarations)

    outcome = await tool_call(
        gen_client,
        model=ROUTING_MODEL,
        contents=query,
        tools=tools,
        tool_config=tool_config,
        timeout_s=timeout_s,
        system_instruction=system_instruction,
    )

    if outcome.error:
        print(
            f"routing: {outcome.error}; proceeding with no filters",
            file=sys.stderr,
        )
        return Filters()

    filters = Filters()
    person_parts: list[PersonFilter] = []

    for raw in outcome.calls:
        if raw.name not in TOOL_REGISTRY:
            print(f"routing: unknown tool '{raw.name}' — skipping", file=sys.stderr)
            continue
        # Belt-and-suspenders: never honour a person call from a non-allowed
        # caller, even if the declaration somehow reached the model.
        if raw.name == "filter_by_person" and not allow_person:
            continue
        _decl, ArgsModel, executor = TOOL_REGISTRY[raw.name]
        try:
            args = ArgsModel.model_validate(raw.args)
        except ValidationError as e:
            print(
                f"routing: bad args for {raw.name}: {e!s} — skipping",
                file=sys.stderr,
            )
            continue

        result = executor(args, metas)

        # Place the typed result into the right Filters slot. New tools
        # extend this dispatch with their own branch — keeping it explicit
        # (rather than a generic setattr) makes the type relationship
        # between tool name and Filters field visible at a glance.
        if raw.name == "filter_by_location":
            if isinstance(result, LocationFilter):
                filters = replace(filters, location=result)
        elif raw.name == "filter_by_date_range":
            if isinstance(result, DateFilter):
                filters = replace(filters, date=result)
        elif raw.name == "filter_by_proximity":
            if isinstance(result, ProximityFilter):
                filters = replace(filters, proximity=result)
        elif raw.name == "filter_by_person":
            if isinstance(result, PersonFilter):
                person_parts.append(result)

    # Several names in one query arrive as parallel filter_by_person calls; AND
    # them so only photos containing EVERYONE named survive. A single call is an
    # intersection of one set — identical to the old single-person behavior.
    if person_parts:
        filters = replace(filters, person=_combine_people(person_parts))

    return filters
