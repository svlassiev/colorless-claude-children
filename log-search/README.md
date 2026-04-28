# Log Search

A private text-RAG tool over my `~/projects/log` working journal — date-headed markdown notes spanning years of Epidemic Sound work, brag-book entries, team observations, and post-layoff planning.

Type queries like *"records about incidents"*, *"who are the main stakeholders"*, *"results of last performance review"*, *"what I worked on in Q4 2024"* — the app finds matching journal entries and has Gemini answer the question grounded in those entries with citations.

## Why this exists

Two reasons that align:

1. **Genuinely useful for me.** ~10 k lines of markdown across 15+ files is too much to grep through by name. A working memory of my own career history is the actual value here.
2. **Hands-on Vertex AI + Gemini.** Sibling to [`../photo-search/`](../photo-search/README.md). Where photo-search demonstrates *multimodal* RAG, log-search demonstrates the canonical *text* RAG with proper document chunking.

## Privacy model — read this first

The corpus is **private personal content**. It must never be exposed publicly.

| Layer | What's there | Where it lives |
|---|---|---|
| Source corpus | Raw markdown journals | `~/projects/log` — separate private repo, never copied here |
| Cached index | Chunks + embeddings + metadata | `~/.cache/log-search/` — outside this repo, never committed |
| This module | RAG implementation code only | This public repo (`colorless-claude-children`) |
| API calls | Embedding queries + chunk text → Vertex AI / Gemini | Vertex doesn't train on customer data by default |

The code in this module is public. The data this module operates on never enters the repo. The `.gitignore` and default cache path enforce this — but the discipline matters more than the mechanism.

This module is **never published as a public demo**. It is for personal use and for me.

## Example queries

- *"records about incidents"* — entity lookup
- *"who are the main stakeholders mentioned"* — aggregation across the corpus
- *"what's the result of the last performance review"* — date-aware retrieval
- *"summarise everything about the layoff"* — multi-document synthesis
- *"what I worked on in 2024"* — temporal filter (date-prefixed chunks make this trivial)

## Architecture

```
~/projects/log/**/*.md
        │
        ├── chunk    ── split by `# YYYYMMDD` headers + sliding window for long entries
        │             ── attach {file, date, heading-path} metadata
        │
        └── embed    ── text-embedding-005 (Vertex AI)
                                    │
                                    ▼
                  gs://cdc-search-cache/log-search/   (authoritative store)
                                    │
                                    ▼ (server pulls on startup)
                       in-memory NumPy + cosine similarity
                                    │
query ── embed ── top-k ── Gemini 2.5 Pro ── answer + citations
```

**No Vertex AI Vector Search.** The corpus is small enough (~1 k chunks, ~1 MB of vectors) to fit in memory; running the managed service for it would cost ~$360/mo for zero benefit. This is the right engineering call: "I evaluated Vector Search and rejected it for this corpus size; the same code path scales to it at 100 k+ chunks."

## Stack

| Layer | Choice                                   | Why |
|---|------------------------------------------|---|
| Chunking | Date-header split + sliding window       | Date headers are natural semantic boundaries in this corpus |
| Embeddings | `text-embedding-005` (768-dim)           | Cheaper than `gemini-embedding-001`, more than enough for 1 k chunks |
| Index | In-memory NumPy + cosine similarity      | 1 k chunks × 768-dim ≈ 6 MB — trivially fits |
| Retrieval | Top-k (k=5) + optional date-range filter | Date metadata enables temporal queries without re-embedding |
| Generation | Gemini 2.5 Pro                           | Long context, strong synthesis over multiple chunks |
| UI (MVP) | FastAPI + vanilla HTML, `127.0.0.1:8080` only | Visually consistent with the rest of `serg.vlassiev.info` (Verdana, retro palette). Localhost-only, single-user, no auth |
| UI (stretch) | Same FastAPI app, authed at `serg.vlassiev.info/log` | Phase 5 — three deployment options ranked, decision deferred |

## Running locally

### Prerequisites

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).
- Use `svlassiev` accounts, verify before the first run:
    ```bash
    gcloud config configurations activate personal
    curl -s "https://www.googleapis.com/oauth2/v3/tokeninfo?access_token=$(gcloud auth application-default print-access-token)" | grep email
    ```
    If not, `gcloud auth application-default login` and re-pick the right account.
- Vertex AI API enabled: `gcloud services enable aiplatform.googleapis.com` (one-time, free).
- The corpus exists at `~/projects/log` (override by editing `CORPUS_ROOT` in `log_search/paths.py`).

### Install

```bash
cd log-search
uv sync
```

### Build the index (one-time, plus after corpus updates)

```bash
# 1. Chunk the corpus → ~/.cache/log-search/chunks.jsonl  (free, local-only)
uv run python -m log_search.chunker

# 2. Embed chunks → ~/.cache/log-search/index.npz  (~$0.025 for the full corpus)
uv run python -m log_search.embedder
```

The embedder is **idempotent** — re-running on unchanged chunks costs $0 (SHA-keyed cache). Adding or editing journal entries only re-bills the touched chunks.

### Ask a question

```bash
# Full grounded answer with citations (~$0.005–$0.01 per query)
uv run python -m log_search.ask "what was discussed about layoffs"

# Date-filtered query — recognises bare years and `Q[1-4] 20XX`
uv run python -m log_search.ask "performance review 2024"

# Tune top-k retrieval (default 5)
uv run python -m log_search.ask -k 8 "who are the main stakeholders"

# Free debugging — retrieval only, no Gemini call (~$0.0001 per query)
uv run python -m log_search.ask --retrieve-only "anything about hiring"
```

The CLI prints the grounded answer to stdout, citations + per-query token cost to stderr. Pipe stdout to a file if you want only the answer.

### Web UI (localhost only)

```bash
uv run python -m log_search.server
# open http://127.0.0.1:8080
```

The server binds to `127.0.0.1:8080` only — never reachable from the network. Same retro visual style as the rest of `serg.vlassiev.info`.

The query bar has two controls:

- **`retrieve only (free)`** — checkbox; skips Gemini and prints just the matching journal entries with their cosine scores. ~$0.0001 per query. Ideal for tuning prompts and exploring retrieval quality.
- **`depth: 8 / 12 / 20`** — how many journal entries Gemini sees and reasons over. Higher depth = more synthesis across entries, more cost. Default 8 covers most queries; 20 is for broad "summarise everything about X" queries. Cost scales linearly — see *How costs are built* below.

Per-query token cost and cumulative session cost are shown in the page footer for transparency. Stop the server with Ctrl-C; idle = $0.

### How costs are built

Vertex AI is pay-per-use; this project has no subscription, reservation, or hourly endpoint. There are three distinct cost surfaces:

**1. Chunking** — pure local file processing, $0. Walks markdown, splits on `# YYYYMMDD` headers, no API calls.

**2. One-off embed** (already paid: ~$0.025) — `text-embedding-005` at $0.000025 per 1k input characters. ~1 k chunks of ~600 chars each ≈ **~$0.025 for the full corpus**. SHA-keyed cache; editing one journal entry only re-bills that chunk.

**3. Per-query** — only the moment you click `ask`. The query is embedded (negligible) and Gemini 2.5 Pro reads `depth` chunks and writes the answer. **Cost scales linearly with the `depth` preset:**

| depth | full Gemini per query | `retrieve only` per query |
|---|---|---|
| **8 (default)** | ~$0.004–$0.007 | ~$0.0001 |
| 12 | ~$0.005–$0.009 | ~$0.0001 |
| 20 | ~$0.008–$0.014 | ~$0.0001 |

Gemini 2.5 Pro: $1.25/1M input, $10/1M output (≤200k context). Each extra chunk adds ~200 input tokens. The `retrieve only` mode skips Gemini entirely and bills only the query embedding — much cheaper, ideal for iterating on prompts or debugging retrieval quality.

**Idle = $0.** No Vertex AI Vector Search endpoint, no Cloud Run, no recurring infrastructure. The FastAPI server is a Python process holding ~6 MB of vectors in RAM — it bills only when you query.

### Per-operation cheat sheet

| Operation | Approx cost | Notes |
|---|---|---|
| `chunker` | $0 | Local file processing only |
| `embedder` (full corpus, ~1 k chunks) | ~$0.025 | One-off; cached by SHA |
| `embedder` (incremental, ~10 changed chunks) | <$0.001 | Cache hits skip the API call |
| `ask` / server, depth=8 | ~$0.004–$0.007 per query | Default; cost printed in stderr / page footer |
| `ask` / server, depth=12 | ~$0.005–$0.009 per query | +50% chunks |
| `ask` / server, depth=20 | ~$0.008–$0.014 per query | Useful for broad "summarise everything …" queries |
| `ask --retrieve-only` | ~$0.0001 per query | One embedding call for the query; no Gemini |
| Idle | $0 | No deployed services, no recurring infra |

### Cache (authoritative store: GCS)

The index lives in a private GCS bucket. `~/.cache/log-search/` is just a transient local working copy — the server pulls from GCS on startup, the embedder pushes back to GCS at end of run.

```bash
# Manual sync (auto-push at end of embedder run already covers most cases):
uv run python -m log_search.cloud_cache push   # local → bucket
uv run python -m log_search.cloud_cache pull   # bucket → local (only newer remote)
```

The server's `lifespan()` calls `pull_from_gcs()` at startup automatically — no-op if local is already fresh. Class A/B op costs are effectively $0; storage is <$0.001/month.

Bucket: `gs://cdc-search-cache/log-search/` — UBLA + `publicAccessPrevention=enforced` + versioning enabled + 7-day soft-delete. Never public; overwrites are recoverable.

### Rebuilding the index

If you've edited the journal and want the index to reflect the new content:

```bash
uv run python -m log_search.chunker     # free; re-chunks ~/projects/log
uv run python -m log_search.embedder    # ~$0.025 full corpus, sha-cached on incremental
# embedder auto-pushes the updated index to GCS at the end
```

