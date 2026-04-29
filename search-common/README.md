# search-common

Shared library for [`explore`](../explore/README.md) — auth, rate limiting, and env-driven settings for the photo/log RAG service.

Lives as a sibling Python package so [`photo-search`](../photo-search/README.md), [`log-search`](../log-search/README.md), and `explore` can all depend on it via uv path-installs without publishing to PyPI.

## What's inside

| Module | Purpose |
|---|---|
| `settings.py` | `EXPLORE_*` env-var loading. Reads `EXPLORE_PROJECT`, `EXPLORE_LOCATION`, `EXPLORE_FIREBASE_PROJECT_ID`, `EXPLORE_ALLOWED_EMAILS` (comma → `frozenset[str]`), `EXPLORE_LOG_TAB_ENABLED` (bool). All env-driven; nothing in code. |
| `auth.py` | FastAPI dependency `get_subject(authorization: str \| None)` that returns `AnonSubject` or `AuthedSubject`. Cryptographically verifies Firebase ID tokens via `firebase_admin.auth.verify_id_token` (checks signature, audience, expiry), requires `email_verified=True`, then matches `email ∈ EXPLORE_ALLOWED_EMAILS`. Returns 401 on any failure. |
| `rate_limit.py` | `enforce_rate_limit(subject, db)` and `get_remaining(subject, db)` against per-user (authed) or global (anon) Firestore counters. Daily reset via document ID = `YYYY-MM-DD`. Atomic `Increment(1)` — no read-modify-write race even under concurrent requests. |

## Trust model

The auth gate here is the **only** server-side trust boundary in the explore service. The whole point: even if the client-side auth flow is fully compromised (forged tokens, MITM'd Firebase config, broken sign-in UI), the backend will not authorize a request without a valid Firebase ID token cryptographically tied to our project AND an email on the allow-list.

Decision: rely on allow-list match alone — no separate domain whitelist on the email. Day-0 allow-list is one email (`svlassiev@gmail.com`), which itself is also the project owner; not worth a second layer.

## Local dev

```bash
cd search-common
uv sync --frozen
uv run --no-sync pytest  # if/when tests land
```

Or installed transparently by [`explore`](../explore/README.md)'s `uv sync` via path-deps.
