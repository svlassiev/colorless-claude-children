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
                       in-memory NumPy + cosine similarity
                       (cached to ~/.cache/log-search/index.npz)
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
| UI (MVP) | Streamlit, `localhost:8501` only         | Private, single-user, no auth needed |
| UI (stretch) | Authed `serg.vlassiev.info/log`          | See PLAN Phase 5 — three deployment options ranked |

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

### Cost summary

| Operation | Approx cost | Notes |
|---|---|---|
| `chunker` | $0 | Local file processing only |
| `embedder` (full corpus, ~1 k chunks) | ~$0.025 | One-off; cached by SHA |
| `embedder` (incremental, ~10 changed chunks) | <$0.001 | Cache hits skip the API call |
| `ask` (full Gemini) | ~$0.005–$0.01 per query | Bounded by `usage_metadata`, printed to stderr |
| `ask --retrieve-only` | ~$0.0001 per query | One embed call for the query, nothing else |
| Idle | $0 | No deployed services, no recurring infra |

### Resetting / clearing the index

```bash
rm -rf ~/.cache/log-search/   # forces a clean re-chunk + re-embed on the next run
```

