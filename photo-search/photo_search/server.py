"""FastAPI server: serves the UI + a /ask JSON endpoint.

Bind to 127.0.0.1 only — Phase 4 is local-only by design. Phase 6 (deploy)
will add auth before binding to anything reachable from the network.

Port 8081 (not 8080) so log-search's server can run alongside.

Run: uv run python -m photo_search.server
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
import vertexai
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from google import genai
from google.cloud import storage
from pydantic import BaseModel, field_validator
from vertexai.vision_models import MultiModalEmbeddingModel

from photo_search.cloud_cache import pull_from_gcs
from photo_search.paths import EMBED_MODEL, LOCATION, MAX_K, PROJECT
from photo_search.qa import generate, retrieve
from photo_search.retriever import load_index
from photo_search.site import site_url_for

INDEX_HTML = (Path(__file__).parent / "static" / "index.html").read_text()

_state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Pull cache from the private GCS bucket if remote is newer than local
    # (or local is missing). No-op for warm-laptop case.
    n = pull_from_gcs()
    if n:
        print(f"pulled {n} cache file(s) from GCS", file=sys.stderr)

    vertexai.init(project=PROJECT, location=LOCATION)
    _state["embed_model"] = MultiModalEmbeddingModel.from_pretrained(EMBED_MODEL)
    _state["gen_client"] = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    _state["storage_client"] = storage.Client(project=PROJECT)
    vectors, metas = load_index()
    _state["vectors"] = vectors
    _state["metas"] = metas
    _state["session_cost"] = 0.0
    _state["session_queries"] = 0
    print(f"loaded index: {len(metas)} vectors", file=sys.stderr)
    yield


app = FastAPI(lifespan=lifespan)


class AskRequest(BaseModel):
    query: str
    k: int = 5
    retrieve_only: bool = False

    @field_validator("k")
    @classmethod
    def _clamp_k(cls, v: int) -> int:
        """Silently clamp to [1, MAX_K]. Belt-and-suspenders with the
        UI's preset buttons (8/12/20) — guards direct API callers."""
        return min(max(v, 1), MAX_K)


class CitationOut(BaseModel):
    rank: int
    score: float
    blob_path: str
    gcs_uri: str
    site_url: str | None = None
    date_iso: str | None = None
    caption: str = ""
    sha: str


class AskResponse(BaseModel):
    answer: str | None = None
    citations: list[CitationOut] = []
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float | None = None
    date_filter: str | None = None
    session_cost: float = 0.0
    session_queries: int = 0


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


@app.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest) -> AskResponse:
    q = req.query.strip()
    if not q:
        raise HTTPException(status_code=400, detail="empty query")

    hits, (date_lo, date_hi) = retrieve(
        q,
        _state["embed_model"],
        _state["vectors"],
        _state["metas"],
        k=req.k,
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
    _state["session_queries"] += 1

    def _envelope(**fields) -> AskResponse:
        return AskResponse(
            session_cost=_state["session_cost"],
            session_queries=_state["session_queries"],
            date_filter=date_filter,
            **fields,
        )

    if not hits:
        return _envelope(answer="no matching photos found.")

    if req.retrieve_only:
        return _envelope(citations=citations)

    answer, usage = generate(q, hits, _state["gen_client"], _state["storage_client"])
    if usage["cost"] is not None:
        _state["session_cost"] += usage["cost"]
    return _envelope(
        answer=answer,
        citations=citations,
        tokens_in=usage["tokens_in"],
        tokens_out=usage["tokens_out"],
        cost=usage["cost"],
    )


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8081, log_level="info")


if __name__ == "__main__":
    main()
