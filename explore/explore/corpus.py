"""Corpus dispatch.

Photo corpus: open to anonymous (subject to global anon rate-limit).
Log corpus:   gated behind both `settings.log_tab_enabled` and authentication.

The two-layer auth check (route handler + dispatch) is defense-in-depth:
even if a future route handler forgets to validate, this dispatcher won't
serve log content to an anonymous caller.
"""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException

from search_common.auth import AnonSubject, AuthedSubject, Subject
from search_common.settings import settings

CorpusName = Literal["photo", "log"]


def authorize_corpus(corpus: CorpusName, subject: Subject) -> None:
    """Raises HTTPException if the caller can't access this corpus."""
    if corpus == "photo":
        return
    if corpus == "log":
        if not settings.log_tab_enabled:
            raise HTTPException(status_code=503, detail="log corpus not enabled")
        if isinstance(subject, AnonSubject):
            raise HTTPException(status_code=401, detail="log corpus requires authentication")
        return
    raise HTTPException(status_code=400, detail=f"unknown corpus: {corpus}")
