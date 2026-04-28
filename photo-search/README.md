# Photo Search

A multimodal RAG demo over this site's photo archive, built on **Vertex AI + Gemini**.

Type queries like *"girl playing cards"*, *"snake in the forest"*, *"people gathered around a campfire"*, *"children playing in snow"* — the app finds matching photos from the **6,040-photo personal collection** from https://serg.vlassiev.info and has Gemini write a short natural-language summary of what it found.

## Why this exists

**Genuinely useful.** https://serg.vlassiev.info gallery has ~20 years of photos organised by folder but with no tags, captions, or searchable metadata. Finding *that one photo* of a specific hike in 2012 means scrolling through folders.

## Example queries

- *"girl with red hair"* — attribute-based
- *"snake in the forest"* — semantic, fine-grained subject
- *"people from 2009"* — temporal (EXIF date filter; folder-name fallback for older photos without EXIF)
- *"girl playing cards"* — visual context with no obvious caption keyword
- *"hiking trips in the Alps in 2017"* — hybrid (semantic + date filter)

## Architecture

```
photos in GCS
     │
     ├── ingest  ── Gemini 2.5 Flash Vision ──▶ caption
     │           ── EXIF (Pillow)            ──▶ metadata (date, gps)
     │
     └── embed   ── multimodalembedding@001  ──▶ 1408-dim vector
                                                       │
                                                       ▼
                                        in-memory NumPy + cosine similarity
                                        (cached to ~/.cache/photo-search/index.npz)
                                                       │
query ── embed ── top-k + sha-dedup + date filter ── Gemini 2.5 Pro (reads images) ── narrative + thumbnails
```

**No Vertex AI Vector Search.** at this corpus size (~6 k images × 1408-dim × 4 bytes ≈ 35 MB), in-memory NumPy `argsort` outperforms any RPC-based vector store, and the managed service costs ~$360/mo for zero benefit. The same code path scales to it at 100 k+ images.

## Stack

| Layer | Choice | Why |
|---|--|---|
| Embeddings | `multimodalembedding@001` (1408-dim) | Shared text+image space — a text query lands near matching images without a caption bottleneck |
| Ingest captioning | Gemini 2.5 Flash Vision | Cheap, fast; captions are displayed metadata + extra context for the generator, not the primary retrieval signal |
| Query-time generation | Gemini 2.5 Pro | Reads images directly via `Part.from_bytes`; rich visual reasoning over retrieved images |
| Vector store | In-memory NumPy + cosine similarity | 6 k × 1408-dim ≈ 35 MB; trivially fits, faster than any RPC |
| Date filter | EXIF `DateTimeOriginal` + folder-name heuristic fallback | ~91% of photos have EXIF; remainder fall back to folder-name year inference |
| Dedup | Content-SHA cleanup + retrieval-side backfill | Bucket has 448 byte-identical pairs across case + zero-padding variants |
| UI (planned, Phase 4) | FastAPI + vanilla HTML, retro `serg.vlassiev.info` palette | Visual consistency with the rest of the site; click-through citations to existing preview pages |
| Photos |  | Existing public bucket, no new storage |

## Running locally

### Prerequisites

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).
- Use svlassiev accounts. Verify with:
    ```bash
    gcloud config configurations activate personal
    curl -s "https://www.googleapis.com/oauth2/v3/tokeninfo?access_token=$(gcloud auth application-default print-access-token)" | grep email
    ```
    If not, `gcloud auth application-default login` and re-pick the right account.
- Vertex AI API enabled on the project: `gcloud services enable aiplatform.googleapis.com` (one-time, free).

### Install

```bash
cd photo-search
uv sync
```

### Build the index (one-time, plus after corpus updates)

```bash
# 1. Free counts only — verify the bucket and filter rules.
uv run python -m photo_search.indexer --count-only

# 2. Free dry-run on 10 photos — exercise the EXIF path, no captioning calls.
uv run python -m photo_search.indexer --dry-run --limit 10

# 3. Smoke test: caption 10 photos (~$0.0025) to validate prompt quality.
uv run python -m photo_search.indexer --limit 10

# 4. Full ingest: enumerate + EXIF + caption (~$1.62 for ~6.5k photos, ~36 min @ 10 workers).
uv run python -m photo_search.indexer --workers 10

# 5. Embed: multimodalembedding@001 (~$1.30 for ~6.5k photos, ~30-45 min @ 4 workers).
#    Sanity-checks before billing — aborts on $0 if the model isn't discriminating.
uv run python -m photo_search.embedder --workers 4

# 6. Dedup cleanup (one-time, ~$0): collapse byte-identical photos uploaded under
#    multiple paths (case differences + zero-padding differences). Backups saved.
uv run python -m photo_search.dedup
```

The indexer and embedder are **idempotent** — re-running on unchanged inputs costs $0 (path-keyed caption cache, sha-keyed embedding cache).

### Ask a question

```bash
# Full grounded answer with citations (~$0.005–$0.010 per query).
uv run python -m photo_search.ask "girl playing cards"

# Date-filtered query — recognises bare years and seasons (winter spans year boundary).
uv run python -m photo_search.ask "people from 2009"
uv run python -m photo_search.ask "summer 2017 hiking"

# Tune top-k retrieval (default 5; backfill dedup pulls 4× internally then trims).
uv run python -m photo_search.ask -k 8 "snake in the forest"

# Free debugging — retrieval only, no Gemini call (~$0.0002 per query).
uv run python -m photo_search.ask --retrieve-only "girl with red hair"
```

The CLI prints the grounded answer to stdout, citations + per-query token cost to stderr. Each citation includes cosine score, EXIF date (when present), a clickable `serg.vlassiev.info/share/...` URL (rich OG-preview page that resolves to the actual photo), the GCS URI, and the auto-generated caption preview.

Example citation block:

```
  [1] score=0.208  2007-10-15
      site:    https://serg.vlassiev.info/share/pohod1497/85
      gcs:     gs://colorless-days-children/pohod1497/Picture085.JPG
      caption: A young person is intently holding a hand of cards indoors...
  [5] score=0.168  2005-06-15
      site:    https://serg.vlassiev.info/share/summer2005/28
      gcs:     gs://colorless-days-children/summer2005/summer028.jpg
      caption: A young woman with red hair sits at a wooden table indoors...
```

Hiking-api photos (UUID-named blobs from the shared bucket) get `/share/hiking/image/{imageId}` URLs; unknown folders get a GCS URI only and no site link.

### Web UI (localhost only)

```bash
uv run python -m photo_search.server
# open http://127.0.0.1:8081
```

Same retro palette as log-search and the parent site (Verdana 10pt body, monospace `> photo search` heading in `#0000CC`, white background). 4-column photo grid (2 cols on mobile) with `5px inset #FFFFAA` borders matching `../styles.css`. Click any thumbnail → opens the corresponding `serg.vlassiev.info/share/...` page.

The query bar has two controls:

- **`retrieve only (free)`** — checkbox; skips the Gemini generation step and prints just the matching photos with their cosine scores. ~$0.0002 per query. Ideal for tuning prompts and exploring retrieval quality.
- **`depth: 8 / 12 / 20`** — how many photos Gemini sees and reasons over. Higher depth = richer narrative, more cost. Default 8 covers most queries; 20 is for "show me everything related to X." Cost scales linearly — see *How costs are built* below.

Per-query token cost and cumulative session cost are shown in the page footer for transparency.

Bound to `127.0.0.1:8081` only — never reachable from the network. Stop with Ctrl-C; idle = $0. Port 8081 (vs log-search's 8080) so both servers can run alongside.

### How costs are built

Vertex AI is pay-per-use; this project has no subscription, reservation, or hourly endpoint. There are three distinct cost surfaces:

**1. One-off ingest** (already paid: ~$1.62) — Gemini 2.5 Flash Vision generates a caption for each photo. Per image: 258 image-tokens + ~30 prompt tokens at $0.30/1M input, ~50 output tokens at $2.50/1M output ≈ **~$0.00025 per image**. Path-keyed cache makes re-runs idempotent — only newly-uploaded photos re-bill.

**2. One-off embed** (already paid: ~$1.30) — `multimodalembedding@001` produces one 1408-dim vector per photo at **~$0.0002 per image**. SHA-keyed cache; failed embeddings retry for free on rerun.

**3. Per-query** — only the moment you click `explore`. The query is embedded (negligible) and Gemini 2.5 Pro reads `depth` photos and writes a narrative. **Cost scales linearly with the `depth` preset:**

| depth | full Gemini per query | `retrieve only` per query |
|---|---|---|
| **8 (default)** | ~$0.005–$0.008 | ~$0.0002 |
| 12 | ~$0.008–$0.012 | ~$0.0002 |
| 20 | ~$0.013–$0.020 | ~$0.0002 |

Each extra image adds 258 input tokens to Gemini Pro ($1.25/1M input, $10/1M output, ≤200k context). The `retrieve only` mode skips Gemini entirely and bills only the query embedding — three orders of magnitude cheaper, ideal for iterating on prompts or debugging retrieval quality.

**Idle = $0.** No Vertex AI Vector Search endpoint (the alternative would be ~$0.50/hr while deployed → ~$360/mo). No Cloud Run. The FastAPI server is a Python process holding ~35 MB of vectors in RAM — it bills only when you query.

### Per-operation cheat sheet

| Operation | Approx cost | Notes |
|---|---|---|
| `indexer --count-only` / `--dry-run` | $0 | No API calls; bucket enumeration only |
| `indexer` smoke (10 photos) | ~$0.003 | Validates prompt quality before the full batch |
| `indexer` full (~6.5k photos) | ~$1.62 | One-off; path-cached so re-runs cost $0 |
| `embedder` full (~6.5k photos) | ~$1.30 | One-off; sha-cached |
| `dedup` | $0 | Local file processing only |
| `ask` / server, depth=8 | ~$0.005–$0.008 per query | Default; cost printed in stderr / page footer |
| `ask` / server, depth=12 | ~$0.008–$0.012 per query | +50% photos, +50% input tokens |
| `ask` / server, depth=20 | ~$0.013–$0.020 per query | Useful for broad "show me everything …" queries |
| `ask --retrieve-only` | ~$0.0002 per query | One text-side embedding call; no Gemini |
| Idle | $0 | No deployed services, no Vector Search endpoint, no Cloud Run |

### Resetting / clearing the index

```bash
rm -rf ~/.cache/photo-search/   # forces a clean rebuild (re-bills indexer + embedder)
```

Keep the `*.bak` files alone — they're the dedup script's safety net for the existing index.

## Known follow-ups

### `vertexai` SDK deprecation (sunset 2026-06-24)

`embedder.py` and `qa.py` import `vertexai.vision_models.MultiModalEmbeddingModel` for `multimodalembedding@001`. That namespace is deprecated as of 2025-06-24 and **removed 2026-06-24**. Captioning and generation already run on the unified `google-genai` SDK; only the embedding path is still on the legacy one because `google-genai` doesn't expose `multimodalembedding@001` yet.

Two viable migration paths (when convenient — not blocking):

1. **Wait** for `google-genai` to expose `multimodalembedding@001`. Swap the imports, no re-embedding.
2. **Pivot** to a Gemini-based multimodal embedding model via `google-genai`. Re-bills the embedding pass (~$1.30 for ~6.5 k photos) but produces the cleanest end state — one SDK, one client per process. The retriever stays the same; cosine similarity is dimension-agnostic.

