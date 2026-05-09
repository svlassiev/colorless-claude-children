# Explore

FastAPI service that backs `serg.vlassiev.info/explore` — the unified entry point for [`photo-search`](../photo-search/README.md) (public, multimodal RAG over the photo bucket) and [`log-search`](../log-search/README.md) (private, text RAG over a working journal).

## What it does

- Serves a single static HTML page at `/explore/` with a search box and a `photos` / `log` tab toggle.
- Exposes `POST /explore/api/ask`, which **streams Server-Sent Events**: a `citations` event lands as soon as retrieval (+ rerank) finishes, then an `answer` event when Pro is done, then a `done` event with refreshed quota numbers. The frontend renders photos under the search box ~5–10 s in and appends the answer ~10–15 s later — instead of staring at a spinner for the full pipeline.
- Enforces auth + rate-limiting via the shared [`search-common`](../search-common/README.md) library:
  - **Anonymous** users can query the public photo corpus, capped at **20 queries/day globally**.
  - **Allow-listed** users (`EXPLORE_ALLOWED_EMAILS` env var) can also query the private log corpus, capped at **50 queries/day per user**.
- Backend trust boundary: every request that claims to be authed gets its `Authorization: Bearer <id_token>` cryptographically verified via Firebase Admin (`firebase_admin.auth.verify_id_token`) — checks signature against Google's public JWKs, audience matches our project ID, then matches against the allow-list. The client-side flow can be entirely broken without compromising this.

## High-level architecture

```
              ┌─────────┐     ┌───────────┐     ┌──────────────────────┐
   browser ──→│  GKE    │────→│ explore-  │────→│  Cloud Run "explore" │
              │ Ingress │     │ proxy     │     │  (FastAPI, this svc) │
              │ + cert  │     │ (nginx)   │     │                      │
              └─────────┘     └───────────┘     └──┬───────────────┬───┘
                                                   │               │
       Firebase Auth ←──── id_token ────→ Firebase Admin verify    │
                                                                   ↓
       Vertex AI ─────────→ Gemini 2.5 (embed / rerank Flash / generate Pro)
       Firestore ─────────→ daily rate-limit counters
       GCS (private) ─────→ index cache + raw photo bytes
```

Cloud Run scales to zero when idle, so steady-state cost is dominated by Gemini calls (Pro is $1.25 / 1M input, $10 / 1M output). The L7 ingress + proxy hop adds ~30 ms p50 vs hitting Cloud Run directly — acceptable for the privacy of keeping `*.run.app` URLs out of the public surface.

## Repository layout

The `/explore` Cloud Run image bundles four sibling packages (declared as `tool.uv.sources` path deps in [`pyproject.toml`](pyproject.toml)):

| Package | Role |
|---|---|
| `explore` | FastAPI app (this dir): routing, CSP, CORS, lifespan, dispatch, frontend HTML. |
| `search-common` | Shared cross-corpus primitives: auth, rate limit, settings, `safe_generate` wrapper. |
| `photo-search` | Photo corpus: indexer, retriever, Flash reranker, Pro generator. |
| `log-search` | Log corpus: chunked-text indexer, retriever, Pro generator. |

The `explore` package itself is intentionally thin — its job is composition and policy enforcement; corpus-specific logic lives in `photo-search` / `log-search`.

## Request lifecycle: `POST /explore/api/ask`

`/explore/api/ask` returns `Content-Type: text/event-stream` (Server-Sent Events). Auth, corpus authorisation and rate-limit checks all happen **before** the stream begins — failures there surface as regular 4xx JSON responses, not torn streams. Once streaming starts, the client gets up to three frames over the same connection.

```
   client → ingress → proxy → Cloud Run
                                 │
   pre-stream                    ↓
   ┌──────────────────────────────────────────────────────────────────┐
   │ 1. get_subject              — identify caller (anon / authed)    │
   │ 2. authorize_corpus         — corpus access policy               │
   │ 3. retrieve                 — embed query, cosine top-k          │
   │ 4. enforce_rate_limit       — Firestore increment, may raise 429 │
   │ 5. authorize_corpus         — defense-in-depth re-check          │
   └──────────────────────────────────────────────────────────────────┘
                                 │
   streaming                     ↓
   ┌──────────────────────────────────────────────────────────────────┐
   │ rerank (photo, k≥20)        — 4× parallel Flash batches          │
   │ ─── event: citations ───►   client renders photos NOW            │
   │ safe_generate               — Pro with asyncio timeout           │
   │ ─── event: answer ───►      (or event: answer_error on failure)  │
   │ ─── event: done ───►        refreshed quota; client closes       │
   └──────────────────────────────────────────────────────────────────┘
```

Each event is a normal SSE frame:

```
event: citations
data: {"citations":[…],"rerank_used":true,"corpus":"photo", …}

event: answer
data: {"answer":"…","tokens_in":1234,"tokens_out":567,"cost":0.0042}

event: done
data: {"quota_used":3,"quota_remaining":17,"quota_cap":20}
```

`retrieve_only=true` skips the Pro call entirely — the stream emits `citations` then `done`, no rate-limit charge. Empty results emit `citations` (with empty list) + `answer` (with the empty-result message) + `done`.

A few load-bearing details:

**Lifespan startup.** [`server.py:lifespan`](explore/server.py) pulls the vector index from the private `cdc-search-cache` bucket if remote is newer than local, then loads the photo index (~6k vectors) and (when enabled) the log index into memory. The `MultiModalEmbeddingModel` and `genai.Client` are also constructed once and reused — embedding a query at request time is a single Vertex round-trip.

**Retrieval.** Cosine top-k over the in-memory index plus an optional EXIF / folder-name date filter parsed out of the query (`"summer 2017"`, `"2014"`). For the log corpus, embeddings come from `text-embedding-005`; for photos, from `multimodalembedding@001`.

**Rerank (photo, depth ≥ 20).** When the caller picks depth 20, [`photo_search.rerank.rerank_hits`](../photo-search/photo_search/rerank.py) downloads the 20 image bytes in parallel, fans them out to **four parallel Gemini 2.5 Flash calls of five images each** with a structured `response_schema`, sorts the hits by relevance score (cosine as tiebreaker), and trims to `RERANK_KEEP=10` for Pro. Bytes are kept in a sha-keyed map and reused — Pro never re-downloads what Flash already saw. The whole rerank step is bounded by `RERANK_TIMEOUT_S=15s`; on timeout or any exception we fall back to similarity order, **but Pro's input is still trimmed to 10** — wall time stays bounded even when Flash is unreachable.

**Generation.** Gemini 2.5 Pro receives the (possibly reranked, possibly trimmed) hits inline as JPEG bytes plus dates and captions. The output budget scales with hit count (`250 * len(hits)`) so the visible answer doesn't get starved by reasoning tokens at high depth.

**Soft-failure wrapper.** Every generation call goes through [`search_common.generation.safe_generate`](../search-common/search_common/generation.py), which dispatches the sync Gemini call to `asyncio.to_thread` under `asyncio.wait_for` (default 60 s). On timeout or exception we return `GenerationOutcome(answer=None, usage=zero, error=<safe string>)` instead of bubbling a 500 — citations still render, and the frontend surfaces the error in a small inline notice.

## Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/explore/` | The HTML page. |
| GET | `/explore` | 307 redirect to `/explore/`. |
| GET | `/explore/healthz` | Liveness; returns `{"status":"ok","vectors_loaded":<n>}`. |
| GET | `/explore/api/auth/status` | Read-only; returns auth state + remaining quota for the UI. |
| POST | `/explore/api/ask` | Cost-causing endpoint: retrieval (+ rerank + generation). |

FastAPI's auto `/docs`, `/redoc`, `/openapi.json` are explicitly disabled — the JSON shapes only need to match what `static/explore.html` consumes.

## Auth and rate limiting

Three layers, in order:

1. **Identification** — [`search_common.auth.get_subject`](../search-common/search_common/auth.py) is a FastAPI dependency. No `Authorization` header → `AnonSubject`. Bearer token → Firebase Admin verifies the JWT (signature, audience, expiry); email must be in `settings.allowed_emails`. Anything else → 401.
2. **Corpus policy** — [`explore.corpus.authorize_corpus`](explore/corpus.py) is called twice per `POST /api/ask`: once before retrieval, once at dispatch time (defense-in-depth). Photo is open. Log requires both `EXPLORE_LOG_TAB_ENABLED=true` and an `AuthedSubject`.
3. **Rate limit** — [`search_common.rate_limit.enforce_rate_limit`](../search-common/search_common/rate_limit.py) increments a per-day Firestore counter (`anon:YYYY-MM-DD` or `email:<addr>:YYYY-MM-DD`) and raises 429 at the cap. Read-only `get_remaining` powers the quota line in the UI.

The rate-limit increment fires **after** retrieval and **before** generation. Retrieval-only requests (`retrieve_only: true`) skip the increment — the embed call is cheap and not worth gating.

## Failure modes

| Failure | Handling |
|---|---|
| Gemini call slow / timed out | `safe_generate` returns citations + `answer_error="generation timed out (>60s) — showing matches only"`. Pro's rate-limit unit was already charged (we don't know if upstream billed). |
| Gemini call raised | Same as timeout, with a generic `"generation failed — showing matches only"`. Full exception logged server-side; not surfaced to the client. |
| Flash rerank batch failed | Logged to stderr. Other batches continue. If every batch fails, we fall back to similarity order; if a subset fails, missing hits sink to the bottom. **Pro still receives top-10** either way. |
| Byte download for rerank failed | Skip rerank entirely; Pro receives the cosine-top-10. |
| Firestore unreachable | 5xx. We treat rate-limit infra as required — failing open would leak quota. |
| Firebase Admin verification failed | 401, no token-introspection details surfaced. |
| Cloud Run cold start | First request takes ~3–5 s to load the vector index from local cache. Cache miss adds another 1–2 s of GCS reads. |

Front-end mirrors the backend: `answer_error` renders as a small amber `.notice`; citations render unconditionally when present; `rerank_used` toggles between "fade by `in_generation`" and "fade by score-vs-top" baselines.

## Configuration

All settings come from env vars at process start (see [`search_common.settings`](../search-common/search_common/settings.py)). Defaults match the personal-project setup; production overrides via Cloud Run `--set-env-vars` / Secret Manager.

| Env var | Default | Purpose |
|---|---|---|
| `EXPLORE_PROJECT` | `thematic-acumen-225120` | GCP project for Vertex / Firestore / GCS. |
| `EXPLORE_LOCATION` | `europe-west4` | Vertex AI region. Must match the indexer's region. |
| `EXPLORE_FIREBASE_PROJECT_ID` | same as `EXPLORE_PROJECT` | Audience claim for ID-token verification. |
| `EXPLORE_ALLOWED_EMAILS` | empty | Comma-separated allow-list. Lower-cased, trimmed. |
| `EXPLORE_LOG_TAB_ENABLED` | `false` | Master kill-switch for the log corpus tab + endpoint. |

## Local dev

Requires `uv` and gcloud ADC pointing at `thematic-acumen-225120` (svlassiev account). The `albums.json` + `albums-files.json` at the repo root are read at request time by `photo_search.site.site_url_for()` to map photo paths → share URLs.

```bash
cd explore
uv sync --frozen
uv run --no-sync python -m explore.server
# → uvicorn on http://127.0.0.1:8082 (override via $PORT, $HOST)
```

Smoke:

```bash
curl -s http://127.0.0.1:8082/explore/healthz

# /api/ask streams SSE — pass -N (or --no-buffer) so curl doesn't line-buffer
# and you see frames land in the order the server emits them.
curl -sN -X POST http://127.0.0.1:8082/explore/api/ask \
  -H 'Content-Type: application/json' \
  -d '{"query":"snake in the forest","corpus":"photo","retrieve_only":true}'
```

## Deployment

Built via [`Dockerfile.explore`](../Dockerfile.explore) at the repo root (multi-package build that copies all four siblings + `albums*.json`). Pushed to Artifact Registry (`europe-west4-docker.pkg.dev/thematic-acumen-225120/explore/explore`), deployed to Cloud Run service `explore` in `europe-west4`.

```bash
TAG=$(git rev-parse --short=8 HEAD)
IMAGE="europe-west4-docker.pkg.dev/thematic-acumen-225120/explore/explore:${TAG}"
docker build --platform linux/amd64 -f Dockerfile.explore -t "$IMAGE" .
docker push "$IMAGE"
gcloud run deploy explore --image="$IMAGE" --region=europe-west4 --project=thematic-acumen-225120
```

Reached on the public domain via the small nginx-proxy pod on the existing GKE cluster ([`k8s/explore-proxy.yml`](../k8s/explore-proxy.yml) + the `/explore` paths in [`k8s/ingress.yml`](../k8s/ingress.yml)). The proxy forwards path-as-is, sets the SNI/Host header for Cloud Run's `*.run.app` certificate, and bumps `proxy_read_timeout` to 130 s so we surface upstream 504s instead of pre-empting at nginx's default 90 s. Cloud Run's URL is hidden behind the Ingress — no DNS change required.

Idle = $0 (Cloud Run scale-to-zero). See `photo-search/PLAN.md` (private; build narrative) for the full architecture log.
