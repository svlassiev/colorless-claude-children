"""Flash reranker — runs Gemini 2.5 Flash in parallel batches over the
retrieved photos and returns them sorted by LLM-judged relevance.

Why: at high retrieval depth (k=20) embedding similarity is too noisy —
the bottom of the list is often unrelated to the query. Sending all 20
images to Pro means Pro spends its input budget and its thinking on
photos it will end up calling out as irrelevant. That's the long wait
the user sees.

How: split the hits into batches of RERANK_BATCH_SIZE (default 5),
fan them out to Flash in parallel with structured output, and sort by
the resulting score. Pro then only sees the top RERANK_KEEP. Bytes are
downloaded once (in parallel) and shared with Pro via the prefetched
path — no double upload work.

Failure model: this is a soft-fast layer. Any timeout or exception in
download or rerank → return hits in their original similarity order.
The caller still trims to RERANK_KEEP for Pro, which preserves the
"don't make Pro chew through 20 images" promise even when Flash is
unreachable.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

from google import genai
from google.cloud import storage
from google.genai import types
from pydantic import BaseModel

from photo_search.paths import BUCKET, RERANK_BATCH_SIZE, RERANK_MODEL, RERANK_TIMEOUT_S
from photo_search.qa import _download_blob_bytes
from photo_search.retriever import Hit

RERANK_PROMPT = """\
You are filtering photos from a personal collection for a search query.

Query: {query}

Below are {n} photos labeled with their identifier in [brackets]. Rate each
photo's relevance to the query from 0.0 (not relevant) to 1.0 (highly
relevant). A photo is relevant only if it depicts the literal subject of
the query, not just loosely associated themes. Be strict.

Return one entry per photo, using the bracketed identifier as `rank`.
"""


class _Rating(BaseModel):
    rank: int
    relevance_score: float


class _RerankBatchResponse(BaseModel):
    ratings: list[_Rating]


@dataclass(frozen=True)
class RerankOutcome:
    """Result of the rerank step.

    `hits` is the input list, reordered by Flash relevance descending when
    `used` is True; otherwise unchanged (similarity order).

    `bytes_by_sha` always contains the downloaded image bytes for whichever
    hits we managed to download — callers pass this through to `generate()`
    to avoid a second round of GCS reads.
    """

    hits: list[Hit]
    bytes_by_sha: dict[str, bytes]
    used: bool


def _rerank_one_batch(
    query: str,
    batch: list[Hit],
    bytes_by_sha: dict[str, bytes],
    gen_client: genai.Client,
) -> dict[int, float]:
    """Sync helper: rate one batch of hits with Flash. Returns rank→score.

    Meant to be wrapped with `asyncio.to_thread` for parallelism. Raises
    on transport / parse errors so the caller can record a per-batch
    failure without aborting the whole rerank.
    """
    contents: list = [RERANK_PROMPT.format(query=query, n=len(batch))]
    for h in batch:
        date = h.date_iso or "unknown"
        contents.append(f"\n[{h.rank}] (date: {date})")
        contents.append(
            types.Part.from_bytes(data=bytes_by_sha[h.sha], mime_type="image/jpeg")
        )
        if h.caption:
            contents.append(f"Auto-generated caption: {h.caption}")

    resp = gen_client.models.generate_content(
        model=RERANK_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_RerankBatchResponse,
            # Small budget — we expect O(n) JSON entries, no prose.
            max_output_tokens=512,
        ),
    )

    # Prefer the SDK's auto-parsed pydantic instance; fall back to JSON if
    # the SDK couldn't parse (rare with response_schema, but defensive).
    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, _RerankBatchResponse):
        ratings = parsed.ratings
    else:
        import json

        raw = json.loads(resp.text or "{}")
        ratings = [_Rating(**r) for r in raw.get("ratings", [])]

    return {r.rank: float(r.relevance_score) for r in ratings}


async def rerank_hits(
    query: str,
    hits: list[Hit],
    gen_client: genai.Client,
    storage_client: storage.Client,
    *,
    timeout_s: float = RERANK_TIMEOUT_S,
    batch_size: int = RERANK_BATCH_SIZE,
) -> RerankOutcome:
    """Run the full rerank pipeline. Always returns — never raises.

    1. Fan out byte downloads in parallel.
    2. Split into batches of `batch_size`, fan out Flash in parallel.
    3. Sort hits by Flash score (similarity score is the tiebreaker).

    On any timeout or exception: log to stderr and return the input hits
    in their original order, alongside whatever bytes were downloaded.
    `used=False` lets the caller (and the frontend) know rerank didn't
    influence the ordering.
    """
    if not hits:
        return RerankOutcome(hits=hits, bytes_by_sha={}, used=False)

    # --- Step 1: parallel byte download ---------------------------------
    download_tasks = [
        asyncio.to_thread(_download_blob_bytes, h, storage_client) for h in hits
    ]
    try:
        bytes_list = await asyncio.wait_for(
            asyncio.gather(*download_tasks), timeout=timeout_s
        )
    except (asyncio.TimeoutError, Exception) as e:
        print(
            f"rerank: byte download failed ({type(e).__name__}: {e}); "
            "skipping rerank",
            file=sys.stderr,
        )
        return RerankOutcome(hits=hits, bytes_by_sha={}, used=False)

    bytes_by_sha = {h.sha: b for h, b in zip(hits, bytes_list)}

    # --- Step 2: split + fan out Flash ----------------------------------
    batches = [hits[i : i + batch_size] for i in range(0, len(hits), batch_size)]
    rerank_tasks = [
        asyncio.to_thread(_rerank_one_batch, query, b, bytes_by_sha, gen_client)
        for b in batches
    ]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*rerank_tasks, return_exceptions=True),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        print(
            f"rerank: Flash batches timed out (>{timeout_s:g}s); "
            "skipping rerank",
            file=sys.stderr,
        )
        return RerankOutcome(hits=hits, bytes_by_sha=bytes_by_sha, used=False)

    flash_scores: dict[int, float] = {}
    for r in results:
        if isinstance(r, BaseException):
            print(
                f"rerank: a Flash batch raised {type(r).__name__}: {r}",
                file=sys.stderr,
            )
            continue
        flash_scores.update(r)

    if not flash_scores:
        # Every batch failed — nothing useful to do. Stay in similarity
        # order; caller still trims for Pro.
        return RerankOutcome(hits=hits, bytes_by_sha=bytes_by_sha, used=False)

    # --- Step 3: sort by Flash, similarity as tiebreaker ----------------
    # Hits not scored by Flash (rare: only the batch-failed subset) sink
    # to the bottom but stay above unrelated hits in similarity order.
    def _key(h: Hit) -> tuple[float, float]:
        return (flash_scores.get(h.rank, float("-inf")), h.score)

    ranked = sorted(hits, key=_key, reverse=True)
    return RerankOutcome(hits=ranked, bytes_by_sha=bytes_by_sha, used=True)
