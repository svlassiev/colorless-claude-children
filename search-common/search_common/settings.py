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
    # Model selection — every Gemini/embedding model is chosen here so a new
    # model available in `location` is a one-env-var swap (EXPLORE_*_MODEL).
    # `gemini_location` records the endpoint generation *should* use. Today all
    # clients run in `location` (europe-west4), which serves the 2.5 family.
    # The newest models (*-latest aliases, gemini-3-*) only serve from "global";
    # adopting one means EXPLORE_GEMINI_LOCATION=global AND splitting the shared
    # client so query-embedding stays regional (multimodalembedding@001 /
    # text-embedding-005 are not served from global). Until that split lands,
    # this field documents intent rather than rewiring clients.
    gemini_location: str
    generate_model: str
    routing_model: str
    rerank_model: str
    photo_caption_model: str
    log_caption_model: str
    photo_embed_model: str
    log_embed_model: str


def _load() -> Settings:
    project = os.environ.get("EXPLORE_PROJECT", "thematic-acumen-225120")
    location = os.environ.get("EXPLORE_LOCATION", "europe-west4")
    firebase_project_id = os.environ.get("EXPLORE_FIREBASE_PROJECT_ID", project)
    raw_emails = os.environ.get("EXPLORE_ALLOWED_EMAILS", "")
    allowed = frozenset(e.strip().lower() for e in raw_emails.split(",") if e.strip())
    log_tab = os.environ.get("EXPLORE_LOG_TAB_ENABLED", "false").lower() == "true"
    return Settings(
        project=project,
        location=location,
        firebase_project_id=firebase_project_id,
        allowed_emails=allowed,
        log_tab_enabled=log_tab,
        gemini_location=os.environ.get("EXPLORE_GEMINI_LOCATION", location),
        generate_model=os.environ.get("EXPLORE_GENERATE_MODEL", "gemini-2.5-pro"),
        routing_model=os.environ.get("EXPLORE_ROUTING_MODEL", "gemini-2.5-flash"),
        rerank_model=os.environ.get("EXPLORE_RERANK_MODEL", "gemini-2.5-flash"),
        photo_caption_model=os.environ.get(
            "EXPLORE_PHOTO_CAPTION_MODEL", "gemini-2.5-flash"
        ),
        log_caption_model=os.environ.get("EXPLORE_LOG_CAPTION_MODEL", "gemini-2.5-pro"),
        photo_embed_model=os.environ.get(
            "EXPLORE_PHOTO_EMBED_MODEL", "multimodalembedding@001"
        ),
        log_embed_model=os.environ.get("EXPLORE_LOG_EMBED_MODEL", "text-embedding-005"),
    )


settings = _load()
