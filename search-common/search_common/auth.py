"""Auth gate: identify the caller as anonymous or allow-listed-authenticated.

No Authorization header           → AnonSubject (allowed for photo corpus only)
Bad / expired Bearer token        → 401
Valid token, email not in list    → 401
Valid token, email in list        → AuthedSubject

Decision (PLAN, 2026-04-29): rely on allow-list match alone — do NOT also
require email_verified. Trade-off accepted because:
- Day-0 allow-list is one email (svlassiev@gmail.com) which only signs in via
  Google Sign-In, where email_verified is implicit.
- If the list grows and includes an email reachable via password sign-up,
  add the email_verified check then.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

import firebase_admin
from fastapi import Header, HTTPException
from firebase_admin import auth as firebase_auth

from search_common.settings import settings


@dataclass(frozen=True)
class AnonSubject:
    """Anonymous request — no token presented."""

    @property
    def label(self) -> str:
        return "anon"


@dataclass(frozen=True)
class AuthedSubject:
    """Allow-listed authenticated request."""

    email: str
    uid: str

    @property
    def label(self) -> str:
        return f"email:{self.email}"


Subject = Union[AnonSubject, AuthedSubject]


_initialized = False


def _ensure_firebase_initialized() -> None:
    """Initialise Firebase Admin once per process. ADC + project ID only — no key file."""
    global _initialized
    if _initialized or firebase_admin._apps:
        _initialized = True
        return
    firebase_admin.initialize_app(options={"projectId": settings.firebase_project_id})
    _initialized = True


async def get_subject(
    authorization: Optional[str] = Header(default=None),
) -> Subject:
    """FastAPI dependency."""
    if not authorization:
        return AnonSubject()

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return AnonSubject()

    _ensure_firebase_initialized()
    try:
        decoded = firebase_auth.verify_id_token(token)
    except (
        firebase_auth.InvalidIdTokenError,
        firebase_auth.ExpiredIdTokenError,
        firebase_auth.RevokedIdTokenError,
        ValueError,
    ) as e:
        raise HTTPException(status_code=401, detail=f"invalid token: {type(e).__name__}")

    email = (decoded.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="token has no email claim")

    if email not in settings.allowed_emails:
        raise HTTPException(status_code=401, detail="email not allow-listed")

    return AuthedSubject(email=email, uid=decoded.get("uid", ""))
