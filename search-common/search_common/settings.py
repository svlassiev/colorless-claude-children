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
    )


settings = _load()
