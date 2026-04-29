"""Daily-cap rate limiting via Firestore counters.

Anonymous: a single global counter `anon:YYYY-MM-DD` capped at GLOBAL_ANON_DAILY_CAP.
Authed:    per-email counter `email:<email>:YYYY-MM-DD` capped at PER_AUTHED_DAILY_CAP.

Document key includes the date so daily reset is automatic — no cron, no TTL.

Atomicity: each request reads the current count, then writes via FieldValue.Increment.
The check-then-increment has a TOCTOU race but the worst-case overshoot is bounded by
concurrent in-flight request count (≤ small number for our load). Acceptable.
"""

from __future__ import annotations

from datetime import date

from fastapi import HTTPException
from google.cloud import firestore

from search_common.auth import AnonSubject, AuthedSubject, Subject

GLOBAL_ANON_DAILY_CAP = 20
PER_AUTHED_DAILY_CAP = 50

COLLECTION = "explore-rate-limit"


def _key_and_cap(subject: Subject) -> tuple[str, int]:
    today = date.today().isoformat()
    if isinstance(subject, AuthedSubject):
        return f"email:{subject.email}:{today}", PER_AUTHED_DAILY_CAP
    return f"anon:{today}", GLOBAL_ANON_DAILY_CAP


def get_remaining(
    subject: Subject,
    db: firestore.Client,
    *,
    collection: str = COLLECTION,
) -> tuple[int, int]:
    """Return (remaining_today, daily_cap) without incrementing. UI display.

    Read-only — does NOT count toward the rate-limit decision.
    """
    key, cap = _key_and_cap(subject)
    snapshot = db.collection(collection).document(key).get()
    current = (snapshot.get("count") if snapshot.exists else 0) or 0
    return max(0, cap - current), cap


def enforce_rate_limit(
    subject: Subject,
    db: firestore.Client,
    *,
    collection: str = COLLECTION,
) -> int:
    """Read current count; raise 429 if at cap; else atomic increment.

    Returns the post-increment count for inclusion in response envelopes.
    """
    key, cap = _key_and_cap(subject)
    doc_ref = db.collection(collection).document(key)
    snapshot = doc_ref.get()
    current = (snapshot.get("count") if snapshot.exists else 0) or 0
    if current >= cap:
        raise HTTPException(
            status_code=429,
            detail=f"daily limit ({cap}) reached for {subject.label}",
        )
    doc_ref.set(
        {"count": firestore.Increment(1), "day": date.today().isoformat()},
        merge=True,
    )
    return current + 1
