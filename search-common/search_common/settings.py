"""Shared settings — read from env vars at import time.

Defaults match the personal-project setup; production Cloud Run will
override via env vars or Secret Manager.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    project: str
    location: str
    firebase_project_id: str
    allowed_emails: frozenset[str]
    log_tab_enabled: bool
    # Face/person search — a SEPARATE, owner-only allow-list, kept independent of
    # `allowed_emails` so log access and people-search can be toggled separately.
    # `face_search_enabled` is the master kill-switch (mirrors `log_tab_enabled`).
    face_allowed_emails: frozenset[str]
    face_search_enabled: bool
    # Model selection — every Gemini/embedding model is chosen here so a new
    # model available on its endpoint is a one-env-var swap (EXPLORE_*_MODEL).
    # Each generative model env var accepts "model" or "model@location": the
    # 3.x Gemini family serves only from specific endpoints (verified 2026-08:
    # everything from "global", gemini-3.5-flash also europe-west3), while the
    # 2.5 family and both embedding models are regional. The parsed *_location
    # fields drive per-purpose clients; embedding clients ALWAYS stay on
    # `location` (multimodalembedding@001 / text-embedding-005 are regional).
    # `gemini_location` is the default endpoint for a bare model name.
    gemini_location: str
    generate_model: str
    generate_location: str
    routing_model: str
    routing_location: str
    rerank_model: str
    rerank_location: str
    photo_caption_model: str
    photo_caption_location: str
    log_caption_model: str
    log_caption_location: str
    photo_embed_model: str
    log_embed_model: str
    # Google Geocoding API key — used at request time by filter_by_proximity to
    # turn a "near <place>" query into a coordinate when no labeled photo
    # anchors the place. Empty disables the fallback (proximity then only works
    # for places that have coordinated photos). Restrict the key to the
    # Geocoding API; results are cached in-memory per server instance.
    geocoding_api_key: str


def _model_spec(env: str, default_model: str, default_location: str) -> tuple[str, str]:
    """Parse an EXPLORE_*_MODEL env var into (model, endpoint location).

    Accepts "gemini-2.5-pro" (endpoint = default_location) or an explicit
    "gemini-3.5-flash@europe-west3". Keeping the endpoint next to the model
    name in one env var means a model swap can never silently pair a model
    with an endpoint that doesn't serve it.
    """
    raw = os.environ.get(env, default_model)
    model, sep, loc = raw.partition("@")
    return model, (loc if sep else default_location)


def _load() -> Settings:
    project = os.environ.get("EXPLORE_PROJECT", "thematic-acumen-225120")
    location = os.environ.get("EXPLORE_LOCATION", "europe-west4")
    firebase_project_id = os.environ.get("EXPLORE_FIREBASE_PROJECT_ID", project)
    raw_emails = os.environ.get("EXPLORE_ALLOWED_EMAILS", "")
    allowed = frozenset(e.strip().lower() for e in raw_emails.split(",") if e.strip())
    log_tab = os.environ.get("EXPLORE_LOG_TAB_ENABLED", "false").lower() == "true"
    raw_face_emails = os.environ.get("EXPLORE_FACE_ALLOWED_EMAILS", "")
    face_allowed = frozenset(e.strip().lower() for e in raw_face_emails.split(",") if e.strip())
    face_enabled = os.environ.get("EXPLORE_FACE_SEARCH_ENABLED", "false").lower() == "true"
    gemini_location = os.environ.get("EXPLORE_GEMINI_LOCATION", location)
    generate_model, generate_location = _model_spec(
        "EXPLORE_GENERATE_MODEL", "gemini-2.5-pro", gemini_location
    )
    routing_model, routing_location = _model_spec(
        "EXPLORE_ROUTING_MODEL", "gemini-2.5-flash", gemini_location
    )
    rerank_model, rerank_location = _model_spec(
        "EXPLORE_RERANK_MODEL", "gemini-2.5-flash", gemini_location
    )
    photo_caption_model, photo_caption_location = _model_spec(
        "EXPLORE_PHOTO_CAPTION_MODEL", "gemini-2.5-flash", gemini_location
    )
    log_caption_model, log_caption_location = _model_spec(
        "EXPLORE_LOG_CAPTION_MODEL", "gemini-2.5-pro", gemini_location
    )
    return Settings(
        project=project,
        location=location,
        firebase_project_id=firebase_project_id,
        allowed_emails=allowed,
        log_tab_enabled=log_tab,
        face_allowed_emails=face_allowed,
        face_search_enabled=face_enabled,
        gemini_location=gemini_location,
        generate_model=generate_model,
        generate_location=generate_location,
        routing_model=routing_model,
        routing_location=routing_location,
        rerank_model=rerank_model,
        rerank_location=rerank_location,
        photo_caption_model=photo_caption_model,
        photo_caption_location=photo_caption_location,
        log_caption_model=log_caption_model,
        log_caption_location=log_caption_location,
        photo_embed_model=os.environ.get(
            "EXPLORE_PHOTO_EMBED_MODEL", "multimodalembedding@001"
        ),
        log_embed_model=os.environ.get("EXPLORE_LOG_EMBED_MODEL", "text-embedding-005"),
        geocoding_api_key=os.environ.get("GEOCODING_API_KEY", ""),
    )


settings = _load()
