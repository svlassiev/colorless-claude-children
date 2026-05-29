"""FastAPI app for serg.vlassiev.info/explore.

Single combined service serving photo + (later) log corpora via tabs.
Auth + rate-limit + CORS + CSP at the application layer.

Run locally: uv run --directory explore python -m explore.server
"""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

import uvicorn
import vertexai
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from google import genai
from google.cloud import firestore, storage
from pydantic import BaseModel, field_validator
from vertexai.vision_models import MultiModalEmbeddingModel

from dataclasses import replace

from log_search import qa as log_qa
from log_search.cloud_cache import pull_from_gcs as log_pull
from log_search.retriever import load_index as load_log_index
from photo_search import qa as photo_qa
from photo_search import routing as photo_routing
from photo_search.cloud_cache import pull_from_gcs as photo_pull
from photo_search.paths import EMBED_MODEL as PHOTO_EMBED_MODEL
from photo_search.paths import MAX_K, RERANK_KEEP, RERANK_THRESHOLD_K
from photo_search.rerank import rerank_hits as photo_rerank_hits
from photo_search.retriever import load_index as load_photo_index, parse_date_filter
from photo_search.site import site_url_for
from photo_search.tools.base import DateFilter, Filters
from search_common.auth import AnonSubject, AuthedSubject, Subject, get_subject
from search_common.generation import safe_generate
from search_common.rate_limit import enforce_rate_limit, get_remaining
from search_common.settings import settings

from explore.corpus import CorpusName, authorize_corpus

INDEX_HTML = (Path(__file__).parent / "static" / "explore.html").read_text()
_state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    n = photo_pull()
    if n:
        print(f"explore: pulled {n} photo cache file(s) from GCS", file=sys.stderr)

    vertexai.init(project=settings.project, location=settings.location)
    _state["embed_model"] = MultiModalEmbeddingModel.from_pretrained(PHOTO_EMBED_MODEL)
    _state["gen_client"] = genai.Client(
        vertexai=True, project=settings.project, location=settings.location
    )
    _state["storage_client"] = storage.Client(project=settings.project)
    _state["firestore"] = firestore.Client(project=settings.project)

    vectors, metas = load_photo_index()
    _state["photo_vectors"] = vectors
    _state["photo_metas"] = metas
    print(f"explore: loaded photo index — {len(metas)} vectors", file=sys.stderr)

    if settings.log_tab_enabled:
        n = log_pull()
        if n:
            print(f"explore: pulled {n} log cache file(s) from GCS", file=sys.stderr)
        lvecs, lmetas, ltexts = load_log_index()
        _state["log_vectors"] = lvecs
        _state["log_metas"] = lmetas
        _state["log_texts"] = ltexts
        print(f"explore: loaded log index — {len(lmetas)} chunks", file=sys.stderr)

    yield


app = FastAPI(
    lifespan=lifespan,
    # Disable auto-generated docs/spec — we don't want /openapi.json, /docs, /redoc
    # available in production. Re-enable explicitly under /explore/ if ever needed.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://serg.vlassiev.info",
        "http://127.0.0.1:8082",  # local dev only
        "http://localhost:8082",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def csp_and_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        # apis.google.com needed for Firebase Auth gapi helper (loaded by the
        # auth iframe to drive the OAuth popup handshake). Missing this is
        # the most common cause of immediate auth/internal-error on init.
        "script-src 'self' https://www.gstatic.com https://apis.google.com 'unsafe-inline'; "
        "connect-src 'self' https://*.googleapis.com https://*.firebaseio.com https://*.firebaseapp.com https://securetoken.googleapis.com https://apis.google.com; "
        "img-src 'self' data: https://storage.googleapis.com https://*.googleusercontent.com; "
        "style-src 'self' 'unsafe-inline'; "
        "frame-src 'self' https://*.firebaseapp.com https://accounts.google.com https://apis.google.com; "
        "frame-ancestors 'none';"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


class AskRequest(BaseModel):
    query: str
    corpus: CorpusName = "photo"
    k: int = 8
    retrieve_only: bool = False

    @field_validator("k")
    @classmethod
    def _clamp_k(cls, v: int) -> int:
        return min(max(v, 1), MAX_K)


class CitationOut(BaseModel):
    rank: int
    score: float
    date_iso: Optional[str] = None
    # Photo-citation fields
    blob_path: Optional[str] = None
    gcs_uri: Optional[str] = None
    site_url: Optional[str] = None
    caption: str = ""
    sha: Optional[str] = None
    # Log-citation fields
    file: Optional[str] = None
    heading_path: Optional[str] = None
    text: Optional[str] = None
    # True when this citation was passed to the generator. The frontend
    # uses this (with rerank_used) to fade citations that didn't inform
    # the answer.
    in_generation: bool = True


# Note: there's no monolithic AskResponse model anymore. /explore/api/ask
# streams Server-Sent Events (`citations` → `answer`|`answer_error` → `done`)
# whose union of payload fields covers what AskResponse used to carry. The
# event payloads are constructed inline in the handler so the JSON shape
# travels with the code that produces it. CitationOut above remains the
# authoritative per-citation schema.


# All routes live under /explore/. The nginx proxy on GKE forwards as-is
# (no path rewrite needed). Hitting / on the Cloud Run URL directly returns
# 404 — by design; serg.vlassiev.info/explore is the only public surface.


@app.get("/explore/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


@app.get("/explore", include_in_schema=False)
async def _explore_no_slash_redirect() -> RedirectResponse:
    """Browsers occasionally drop trailing slashes — keep one canonical URL."""
    return RedirectResponse(url="/explore/", status_code=307)


@app.get("/explore/healthz")
async def healthz():
    return {"status": "ok", "vectors_loaded": len(_state.get("photo_metas", []))}


@app.get("/explore/api/auth/status")
async def auth_status(subject: Subject = Depends(get_subject)):
    """Returns auth state + quota remaining (no rate-limit increment)."""
    db = _state["firestore"]
    remaining, cap = get_remaining(subject, db)
    if isinstance(subject, AuthedSubject):
        return {
            "authed": True,
            "email": subject.email,
            "log_tab_enabled": settings.log_tab_enabled,
            "quota_remaining": remaining,
            "quota_cap": cap,
        }
    return {
        "authed": False,
        "log_tab_enabled": False,
        "quota_remaining": remaining,
        "quota_cap": cap,
    }


def _sse(event: str, data: dict) -> str:
    """Format one Server-Sent Events frame.

    SSE is plain text: `event: <name>\\ndata: <payload>\\n\\n`. Two newlines
    terminate the frame. We JSON-encode the payload so the client can parse
    it uniformly across event types.
    """
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# `Cache-Control: no-cache` keeps proxies from coalescing events.
# `X-Accel-Buffering: no` is the standard hint to nginx (and to Cloud Run's
# front-end proxy) to flush each chunk as it's written instead of buffering.
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


@app.post("/explore/api/ask")
async def ask(req: AskRequest, subject: Subject = Depends(get_subject)):
    """Stream the retrieval pipeline as SSE so the client can render
    citations as soon as they're known and append the answer when Pro
    finishes — instead of waiting ~15-20 s for everything at once.

    Event sequence:
      - `citations` — emitted after retrieve (+ rerank). Always first when
        we have anything to retrieve. Carries the full citation list,
        rerank_used, corpus, date_filter, current quota.
      - `answer` *or* `answer_error` — emitted after Pro succeeds or
        soft-fails. Skipped when `retrieve_only=true`.
      - `done` — last event. Carries refreshed quota numbers (post Pro
        increment).

    Auth + corpus + rate-limit failures happen *before* the stream begins
    and still surface as regular 4xx JSON responses — never as a partial
    stream. That keeps client error handling simple.
    """
    # Layer 1: route-handler corpus authorisation
    authorize_corpus(req.corpus, subject)

    db = _state["firestore"]
    auth_label = (
        f"authed:{subject.email}" if isinstance(subject, AuthedSubject) else "anon"
    )

    # Retrieve — corpus-specific. Sync; runs before streaming starts so
    # any embed/index failure is a regular 5xx, not a torn stream.
    photo_filters: Filters = Filters()
    if req.corpus == "photo":
        # Step A: Flash routing. AUTO mode — the model may emit zero,
        # one, or several parallel filter calls. Soft-fails to empty
        # Filters() on timeout; retrieval proceeds unfiltered in that case.
        photo_filters = await photo_routing.route_query(
            req.query,
            _state["photo_metas"],
            _state["gen_client"],
        )
        # Step B: cheap deterministic date parser as a fast-path for
        # patterns the regex handles ('summer 2017', '2014'). Only fills
        # in when routing didn't already produce a date filter — once a
        # `filter_by_date_range` tool lands, this becomes a fallback
        # rather than the primary path.
        if photo_filters.date is None:
            date_lo, date_hi = parse_date_filter(req.query)
            if date_lo or date_hi:
                photo_filters = replace(
                    photo_filters,
                    date=DateFilter(start_iso=date_lo, end_iso=date_hi),
                )
        hits = photo_qa.retrieve(
            req.query,
            _state["embed_model"],
            _state["photo_vectors"],
            _state["photo_metas"],
            k=req.k,
            filters=photo_filters,
        )
        date_lo = photo_filters.date.start_iso if photo_filters.date else None
        date_hi = photo_filters.date.end_iso if photo_filters.date else None
        empty_msg = "no matching photos found."
    else:  # corpus == "log"
        hits, (date_lo, date_hi) = log_qa.retrieve(
            req.query,
            _state["gen_client"],
            _state["log_vectors"],
            _state["log_metas"],
            _state["log_texts"],
            k=req.k,
        )
        empty_msg = "no matching journal entries found."

    # SSE display strings for the citations event. Both filters get a
    # human-readable label so the client can render 'Filtered to Хибины,
    # summer 2009' without re-parsing JSON shapes.
    if date_lo or date_hi:
        date_filter = f"{date_lo or '…'} .. {date_hi or '…'}"
    else:
        date_filter = None
    location_filter = (
        photo_filters.location.place_name if photo_filters.location else None
    )
    proximity_filter = (
        f"{photo_filters.proximity.place_name} "
        f"(within {photo_filters.proximity.radius_km:g} km)"
        if photo_filters.proximity
        else None
    )

    def _build_citations(ordered_hits, gen_shas: set[str]) -> list[dict]:
        """Return list[dict] (already JSON-serialisable) so we can drop the
        results straight into an SSE frame without an extra model_dump pass.
        """
        if req.corpus == "photo":
            return [
                CitationOut(
                    rank=h.rank,
                    score=h.score,
                    blob_path=h.blob_path,
                    gcs_uri=h.gcs_uri,
                    site_url=site_url_for(h.blob_path),
                    date_iso=h.date_iso,
                    caption=h.caption,
                    sha=h.sha,
                    in_generation=h.sha in gen_shas,
                ).model_dump()
                for h in ordered_hits
            ]
        return [
            CitationOut(
                rank=h.rank,
                score=h.score,
                date_iso=h.date_iso,
                file=h.file,
                heading_path=h.heading_path,
                text=h.text,
                # Log corpus has no rerank yet — every retrieved chunk goes
                # to Pro, so in_generation stays at its default True.
            ).model_dump()
            for h in ordered_hits
        ]

    # Cost-free paths charge no rate-limit; quota is read-only.
    if not hits:

        async def gen_empty() -> AsyncGenerator[str, None]:
            remaining, cap = get_remaining(subject, db)
            yield _sse(
                "citations",
                {
                    "citations": [],
                    "rerank_used": False,
                    "corpus": req.corpus,
                    "date_filter": date_filter,
                    "location_filter": location_filter,
                    "proximity_filter": proximity_filter,
                    "auth_state": auth_label,
                    "quota_used": cap - remaining,
                    "quota_remaining": remaining,
                    "quota_cap": cap,
                },
            )
            yield _sse("answer", {"answer": empty_msg})
            yield _sse("done", {})

        return StreamingResponse(
            gen_empty(), media_type="text/event-stream", headers=_SSE_HEADERS
        )

    if req.retrieve_only:

        async def gen_retrieve_only() -> AsyncGenerator[str, None]:
            remaining, cap = get_remaining(subject, db)
            yield _sse(
                "citations",
                {
                    "citations": _build_citations(
                        hits, {getattr(h, "sha", "") for h in hits}
                    ),
                    "rerank_used": False,
                    "corpus": req.corpus,
                    "date_filter": date_filter,
                    "location_filter": location_filter,
                    "proximity_filter": proximity_filter,
                    "auth_state": auth_label,
                    "quota_used": cap - remaining,
                    "quota_remaining": remaining,
                    "quota_cap": cap,
                },
            )
            yield _sse("done", {})

        return StreamingResponse(
            gen_retrieve_only(),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    # Cost-causing path: rate-limit BEFORE the stream starts. Raises 429 at
    # cap, surfaces as a normal HTTP error rather than a partial stream.
    quota_used = enforce_rate_limit(subject, db)

    # Layer 2: dispatch-time corpus re-check (defense-in-depth)
    authorize_corpus(req.corpus, subject)

    async def gen_full() -> AsyncGenerator[str, None]:
        # Step 1: rerank (photo corpus, depth ≥ threshold).
        rerank_used = False
        photo_bytes_by_sha: dict[str, bytes] = {}
        local_hits = hits
        if req.corpus == "photo" and req.k >= RERANK_THRESHOLD_K:
            outcome_rr = await photo_rerank_hits(
                req.query,
                local_hits,
                _state["gen_client"],
                _state["storage_client"],
            )
            local_hits = outcome_rr.hits
            photo_bytes_by_sha = outcome_rr.bytes_by_sha
            rerank_used = outcome_rr.used

        # At/above the photo rerank threshold we always trim Pro's input —
        # even when rerank fell back to similarity order — so wall time
        # stays bounded.
        if req.corpus == "photo" and req.k >= RERANK_THRESHOLD_K:
            gen_hits = local_hits[:RERANK_KEEP]
        else:
            gen_hits = local_hits

        # Event 1: citations — render now, even though Pro hasn't started.
        yield _sse(
            "citations",
            {
                "citations": _build_citations(
                    local_hits, {getattr(h, "sha", "") for h in gen_hits}
                ),
                "rerank_used": rerank_used,
                "corpus": req.corpus,
                "date_filter": date_filter,
                "location_filter": location_filter,
                "proximity_filter": proximity_filter,
                "auth_state": auth_label,
                "quota_used": quota_used,
            },
        )

        # Step 2: generate.
        if req.corpus == "photo":
            # Tell the generator what metadata filters already matched these
            # photos, so it treats place/date as established and doesn't
            # disclaim photos it can't visually tie to the location.
            _notes = []
            if location_filter:
                _notes.append(f"location = {location_filter}")
            if proximity_filter:
                _notes.append(f"near {proximity_filter}")
            if date_filter:
                _notes.append(f"dates {date_filter}")
            outcome = await safe_generate(
                photo_qa.generate,
                req.query,
                gen_hits,
                _state["gen_client"],
                _state["storage_client"],
                prefetched_bytes=photo_bytes_by_sha or None,
                filters_note="; ".join(_notes) or None,
            )
        else:
            outcome = await safe_generate(
                log_qa.generate, req.query, gen_hits, _state["gen_client"]
            )

        # Event 2: answer or answer_error. Citations are already on the
        # client at this point; on failure they stay rendered.
        if outcome.error:
            yield _sse(
                "answer_error",
                {
                    "error": outcome.error,
                    "tokens_in": outcome.usage["tokens_in"],
                    "tokens_out": outcome.usage["tokens_out"],
                    "cost": outcome.usage["cost"],
                },
            )
        else:
            yield _sse(
                "answer",
                {
                    "answer": outcome.answer,
                    "tokens_in": outcome.usage["tokens_in"],
                    "tokens_out": outcome.usage["tokens_out"],
                    "cost": outcome.usage["cost"],
                },
            )

        # Event 3: done — refreshed quota after Pro's increment.
        remaining, cap = get_remaining(subject, db)
        yield _sse(
            "done",
            {
                "quota_used": quota_used,
                "quota_remaining": remaining,
                "quota_cap": cap,
            },
        )

    return StreamingResponse(
        gen_full(), media_type="text/event-stream", headers=_SSE_HEADERS
    )


def main() -> None:
    """Run uvicorn. In Cloud Run, $PORT and $HOST are injected; locally we
    default to 127.0.0.1:8082 for safe dev binding."""
    import os

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8082"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
