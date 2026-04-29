"""FastAPI app for serg.vlassiev.info/explore.

Single combined service serving photo + (later) log corpora via tabs.
Auth + rate-limit + CORS + CSP at the application layer.

Run locally: uv run --directory explore python -m explore.server
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import uvicorn
import vertexai
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from google import genai
from google.cloud import firestore, storage
from pydantic import BaseModel, Field, field_validator
from vertexai.vision_models import MultiModalEmbeddingModel

from photo_search import qa as photo_qa
from photo_search.cloud_cache import pull_from_gcs as photo_pull
from photo_search.paths import EMBED_MODEL as PHOTO_EMBED_MODEL
from photo_search.paths import MAX_K
from photo_search.retriever import load_index as load_photo_index
from photo_search.site import site_url_for
from search_common.auth import AnonSubject, AuthedSubject, Subject, get_subject
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
        # Wired in a follow-up phase. For now this branch is unreachable.
        print("explore: log_tab_enabled=true but log dispatch not yet wired", file=sys.stderr)

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
    blob_path: Optional[str] = None
    gcs_uri: Optional[str] = None
    site_url: Optional[str] = None
    date_iso: Optional[str] = None
    caption: str = ""
    sha: Optional[str] = None


class AskResponse(BaseModel):
    answer: Optional[str] = None
    citations: list[CitationOut] = Field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost: Optional[float] = None
    date_filter: Optional[str] = None
    corpus: CorpusName = "photo"
    auth_state: str = "anon"
    quota_used: int = 0
    quota_remaining: int = 0
    quota_cap: int = 0


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


@app.post("/explore/api/ask", response_model=AskResponse)
async def ask(req: AskRequest, subject: Subject = Depends(get_subject)):
    # Layer 1: route-handler corpus authorisation
    authorize_corpus(req.corpus, subject)

    db = _state["firestore"]
    auth_label = (
        f"authed:{subject.email}" if isinstance(subject, AuthedSubject) else "anon"
    )

    if req.corpus != "photo":
        # log corpus is gated upstream; this branch only runs after the feature flag flips
        raise HTTPException(status_code=503, detail="log corpus dispatch not yet wired")

    embed_model = _state["embed_model"]
    vectors = _state["photo_vectors"]
    metas = _state["photo_metas"]

    hits, (date_lo, date_hi) = photo_qa.retrieve(
        req.query, embed_model, vectors, metas, k=req.k
    )
    date_filter = f"{date_lo} .. {date_hi}" if date_lo else None

    citations = [
        CitationOut(
            rank=h.rank,
            score=h.score,
            blob_path=h.blob_path,
            gcs_uri=h.gcs_uri,
            site_url=site_url_for(h.blob_path),
            date_iso=h.date_iso,
            caption=h.caption,
            sha=h.sha,
        )
        for h in hits
    ]

    if not hits:
        remaining, cap = get_remaining(subject, db)
        return AskResponse(
            answer="no matching photos found.",
            corpus=req.corpus,
            date_filter=date_filter,
            auth_state=auth_label,
            quota_used=cap - remaining,
            quota_remaining=remaining,
            quota_cap=cap,
        )

    if req.retrieve_only:
        # No Gemini call → no rate-limit increment (cost is just the query embed)
        remaining, cap = get_remaining(subject, db)
        return AskResponse(
            citations=citations,
            corpus=req.corpus,
            date_filter=date_filter,
            auth_state=auth_label,
            quota_used=cap - remaining,
            quota_remaining=remaining,
            quota_cap=cap,
        )

    # Cost-causing path: rate-limit BEFORE Gemini. Raises 429 at cap.
    quota_used = enforce_rate_limit(subject, db)

    # Layer 2: dispatch-time corpus re-check (defense-in-depth)
    authorize_corpus(req.corpus, subject)

    answer, usage = photo_qa.generate(
        req.query, hits, _state["gen_client"], _state["storage_client"]
    )

    remaining, cap = get_remaining(subject, db)
    return AskResponse(
        answer=answer,
        citations=citations,
        corpus=req.corpus,
        date_filter=date_filter,
        tokens_in=usage["tokens_in"],
        tokens_out=usage["tokens_out"],
        cost=usage["cost"],
        auth_state=auth_label,
        quota_used=quota_used,
        quota_remaining=remaining,
        quota_cap=cap,
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
