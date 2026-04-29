# Explore

FastAPI service that backs `serg.vlassiev.info/explore` — the unified entry point for [`photo-search`](../photo-search/README.md) (public, multimodal RAG over the photo bucket) and [`log-search`](../log-search/README.md) (private, text RAG over a working journal).

## What it does

- Serves a single static HTML page at `/explore/` with a search box and tabs.
- Exposes `POST /explore/api/ask` that dispatches to the right corpus (`photo` or `log`), runs retrieval + Gemini generation, and returns citations + (optionally) a generated answer.
- Enforces auth + rate limiting via the shared [`search-common`](../search-common/README.md) library:
  - **Anonymous** users can query the public photo corpus, capped at 20 queries/day globally.
  - **Allow-listed** users (`EXPLORE_ALLOWED_EMAILS` env var) can also query the private log corpus, capped at 50 queries/day per user.
- Backend trust boundary: every request that claims to be authed gets its `Authorization: Bearer <id_token>` cryptographically verified via Firebase Admin SDK (`firebase_admin.auth.verify_id_token`) — checks signature against Google's public JWKs, audience matches our project ID, then matches against the allow-list. Client-side flow can be entirely broken without compromising this.

## Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/explore/` | The HTML page. |
| GET | `/explore` | 307 redirect to `/explore/`. |
| GET | `/explore/healthz` | Liveness; returns `{"status":"ok","vectors_loaded":<n>}`. |
| GET | `/explore/api/auth/status` | Read-only; returns auth state + remaining quota for the UI. |
| POST | `/explore/api/ask` | The cost-causing endpoint: retrieval (+ optional generation). |

FastAPI's auto `/docs`, `/redoc`, `/openapi.json` are explicitly disabled — there's no public API; the JSON shapes only need to match what `static/explore.html` consumes.

## Local dev

Requires `uv` and gcloud ADC pointing at `thematic-acumen-225120` (svlassiev account). The albums.json + albums-files.json at the repo root are read at request time by `photo_search.site.site_url_for()` to map photo paths → share URLs.

```bash
cd explore
uv sync --frozen
uv run --no-sync python -m explore.server
# → uvicorn on http://127.0.0.1:8080 (override via $PORT, $HOST)
```

Smoke:

```bash
curl -s http://127.0.0.1:8080/explore/healthz
curl -s -X POST http://127.0.0.1:8080/explore/api/ask \
  -H 'Content-Type: application/json' \
  -d '{"query":"snake in the forest","corpus":"photo","retrieve_only":true}'
```

## Deployment

Built via `Dockerfile.explore` at the repo root (multi-package build that copies all four siblings + `albums*.json`). Pushed to Artifact Registry (`europe-west4-docker.pkg.dev/thematic-acumen-225120/explore/explore`), deployed to Cloud Run service `explore` in `europe-west4`.

Reached on the public domain via a small nginx-proxy pod on the existing GKE cluster (`k8s/explore-proxy.yml` + the `/explore` paths in `k8s/ingress.yml`) — Cloud Run's URL is hidden behind the Ingress, no DNS change required.

Idle = $0 (Cloud Run scale-to-zero). See `photo-search/PLAN.md` (private; build narrative) for the full architecture log.
