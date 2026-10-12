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
     ├── ingest  ── Gemini Flash captioning ──▶ caption
     │           ── EXIF (Pillow)            ──▶ metadata (date, gps)
     │
     └── embed   ── gemini-embedding-2      ──▶ 3072-dim vector (joint text+image space)
                                                       │
                                                       ▼
                            gs://cdc-search-cache/photo-search/   (authoritative store)
                                                       │
                                                       ▼ (server pulls on startup)
                                        in-memory NumPy + cosine similarity
                                                       │
query ── embed ── top-k + sha-dedup + date filter ── Gemini 3.8 Flash (reads images) ── narrative + thumbnails
```

**No Vertex AI Vector Search.** at this corpus size (~6 k images × 3072-dim × 4 bytes ≈ 74 MB), in-memory NumPy `argsort` outperforms any RPC-based vector store, and the managed service costs ~$360/mo for zero benefit. The same code path scales to it at 100 k+ images.

## Stack

| Layer | Choice | Why |
|---|--|---|
| Embeddings | `gemini-embedding-2` (3072-dim, `EXPLORE_PHOTO_EMBED_MODEL`) | Shared text+image space; migrated from `multimodalembedding@001` 2026-08 — fixed Russian-query retrieval (Озеро/байдарки/костёр) |
| Ingest captioning | Gemini 3.8 Flash (`EXPLORE_PHOTO_CAPTION_MODEL`) | Cheap, fast; captions are displayed metadata + extra context for the generator, not the primary retrieval signal |
| Query-time generation | Gemini 3.8 Flash (`EXPLORE_GENERATE_MODEL`) | Reads images directly via `Part.from_bytes`; on par with 3.6 Flash on guardrails/quality in the 2026-10 eval (deep answers better) at about the same cost |
| Vector store | In-memory NumPy + cosine similarity | 6 k × 3072-dim ≈ 74 MB; trivially fits, faster than any RPC |
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

# 5. Embed: gemini-embedding-2 (~$1.20 for ~6k photos, ~20 min @ 8 workers).
#    Sanity-checks before billing — aborts on $0 if the model isn't discriminating.
uv run python -m photo_search.embedder --workers 4

# 6. Dedup cleanup (one-time, ~$0): collapse byte-identical photos uploaded under
#    multiple paths (case differences + zero-padding differences). Backups saved.
uv run python -m photo_search.dedup
```

The indexer and embedder are **idempotent** — re-running on unchanged inputs costs $0 (path-keyed caption cache, sha-keyed embedding cache).

That is the first build. **After adding photos, follow [Rebuilding the index](#rebuilding-the-index)** — it has more steps (place labels, people tags, the owner-voice Russian captions), and their order matters.

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

**1. One-off ingest** (already paid: ~$1.62) — a Gemini Flash tier generates a caption for each photo. Per image: 258 image-tokens + ~30 prompt tokens, ~50 output tokens at Flash-tier rates ≈ **~$0.00025 per image**. That basis (also `indexer --count-only`'s `PRICE_PER_CAPTION`) predates Gemini 3.8 Flash, which bills ~1,120 tokens per image plus default thinking; measured on 3.8 in 2026-10: ~$0.002 per plain caption and ~$0.005–0.008 per owner-voice caption. Path-keyed cache makes re-runs idempotent — only newly-uploaded photos re-bill.

**2. One-off embed** (~$1.20) — `gemini-embedding-2` produces one 3072-dim vector per photo (**~$0.0002 per image**). SHA-keyed cache per model-tagged index; failed embeddings retry for free on rerun.

**3. Per-query** — only the moment you click `explore`. The query is embedded (negligible) and the generate model (Gemini 3.8 Flash) reads `depth` photos and writes a narrative. **Cost scales linearly with the `depth` preset:**

| depth | full Gemini per query | `retrieve only` per query |
|---|---|---|
| **8 (default)** | ~$0.004–$0.008 | ~$0.0002 |
| 12 | ~$0.006–$0.011 | ~$0.0002 |
| 20 | ~$0.009–$0.016 | ~$0.0002 |

Each extra image adds ~1,120 input tokens to the generate model (Gemini 3.8 Flash: $0.75 / $3.75 per 1M input / output through 2026-12-31, then $1.50 / $7.50). The depth table above is the 2026-08 3.6 Flash estimate; measured on 3.8 in 2026-10: ~$0.007–0.009 per answer at depth 5 and ~$0.015–0.018 per depth-12 deep answer. The `retrieve only` mode skips Gemini entirely and bills only the query embedding — three orders of magnitude cheaper, ideal for iterating on prompts or debugging retrieval quality.

**Idle = $0.** No Vertex AI Vector Search endpoint (the alternative would be ~$0.50/hr while deployed → ~$360/mo). No Cloud Run. The FastAPI server is a Python process holding ~35 MB of vectors in RAM — it bills only when you query.

### Per-operation cheat sheet

| Operation | Approx cost | Notes |
|---|---|---|
| `indexer --count-only` / `--dry-run` | $0 | No API calls; bucket enumeration only |
| `indexer` smoke (10 photos) | ~$0.003 | Validates prompt quality before the full batch |
| `indexer` full (~6.5k photos) | ~$1.62 | One-off; path-cached so re-runs cost $0 |
| `embedder` full (~6.5k photos) | ~$1.30 | One-off; sha-cached |
| `dedup`, `place_baker`, `face_promote` | $0 | Local file processing only |
| `style_captioner` (owner-voice ru/en) | ~$0.005 per photo | Part of every rebuild; the style card rides on every call |
| Egress while indexing | ~$0.12 per GB | `indexer`, `embedder`, `style_captioner` each download the originals |
| `ask` / server, depth=8 | ~$0.004–$0.008 per query | Default; cost printed in stderr / page footer |
| `ask` / server, depth=12 | ~$0.008–$0.012 per query | +50% photos, +50% input tokens |
| `ask` / server, depth=20 | ~$0.013–$0.020 per query | Useful for broad "show me everything …" queries |
| `ask --retrieve-only` | ~$0.0002 per query | One text-side embedding call; no Gemini |
| Idle | $0 | No deployed services, no Vector Search endpoint, no Cloud Run |

### Cache (authoritative store: GCS)

The index lives in a private GCS bucket. `~/.cache/photo-search/` is just a transient local working copy — the server pulls from GCS on startup, and the indexer/embedder/dedup push back to GCS at end of each run.

```bash
# Manual sync (auto-push at end of indexer/embedder/dedup already covers most cases):
uv run python -m photo_search.cloud_cache push   # local → bucket
uv run python -m photo_search.cloud_cache pull   # bucket → local (only newer remote)
```

The server's `lifespan()` calls `pull_from_gcs()` at startup automatically — no-op if local is already fresh.

Files synced: `index-<embed-model>.npz` and `manifest_meta-<embed-model>.jsonl` (today `index-gemini-embedding-2.npz` / `manifest_meta-gemini-embedding-2.jsonl`; the untagged `index.npz` / `manifest_meta.jsonl` in the bucket are the legacy `multimodalembedding@001` index and are not served), `manifest.jsonl`, `caption_cache.jsonl`, `caption_cache_styled.jsonl`, plus the private `answer_voice.md`, `person_aliases.json`, `hiking_image_ids.json`. A push is checked against the live copies first — see [A rebuild cannot wipe what is live](#a-rebuild-cannot-wipe-what-is-live).

Bucket: `gs://cdc-search-cache/photo-search/` — UBLA + `publicAccessPrevention=enforced` + versioning enabled + 7-day soft-delete. Never public; overwrites are recoverable.

Cost: ~$0.001/month for the ~37 MB cache; Class A/B ops effectively $0.

### Rebuilding the index

After photos were added to `gs://colorless-days-children/`. **This order, all of it** — the served
index is more than vectors: place labels and the owner-voice Russian captions are baked on by later
passes, and a rebuild that skips them serves new photos without them; people tags are re-applied
to the photos that already belong to a named face cluster.

```bash
uv run python -m photo_search.indexer --workers 10   # captions new photos        ~$0.00025 / new photo
uv run python -m photo_search.embedder --workers 4   # vectors for new photos     ~$0.0002  / new photo
uv run python -m photo_search.dedup                  # local: drops byte-identical copies
uv run python -m photo_search.place_baker            # local: place labels (refuses until dedup has run)
uv run python -m photo_search.face_promote           # local: re-applies the people tags of the named face clusters
uv run python -m photo_search.embedder --workers 4   # $0 (cache hits): carries the baked fields into the served meta
uv run python -m photo_search.style_captioner        # owner-voice {ru, en} captions  ~$0.005 / new photo
uv run python -m photo_search.cloud_cache push       # the styled cache is not pushed by its tool
# new albums or app uploads? also:  python -m photo_search.hiking_links  (free; share links), then push
```

- **The Russian captions are part of every rebuild, not an extra** — search shows `caption_ru` to a
  Russian query, and a photo without one falls back to the generic English caption. They use the
  place labels as context, so label new albums in `photo_search/data/place_labels.json` *before*
  this run if the captions should be able to say where.
- **Every photo gets a vector, caption or not.** The vector is made from the image alone, so a photo
  the caption model declined to describe is still searchable (until 2026-09-21 the embedder skipped
  such photos silently). What is *not* indexed is decided in one place, explicitly: the indexer's
  file-name rules (`_is_photo`).
- **People tags do not reach new photos.** `face_promote` only re-applies the named clusters in
  `~/.cache/photo-search/clusters.json` + `cluster_labels.json`. Photos added since the last
  `face_detect` → `face_cluster` → naming pass have no faces on record and get no `person_names`
  until that (local, separate) pass is run again.
- **What it really costs.** Per new photo ~$0.0005 for caption + vector, ~$0.005 for the styled
  captions (the style card rides on every call; about a third of the photos get a second,
  hedge-removing call) — and **egress**: `indexer`, `embedder` and `style_captioner` each download
  the full-size originals from the EU multi-region bucket (~$0.12/GB). For 149 phone photos
  (426 MB) on 2026-09-20: captions $0.04, vectors $0.03, styled captions ≈ $0.7–1.1, egress ≈ $0.15.
- New albums also need the **explore image rebuilt** (`albums.json` / `albums-files.json` are baked
  into it; see `explore/README.md`) for their share links. New *data* alone does not: the server
  pulls the caches from GCS on its next cold start.

#### A rebuild cannot wipe what is live

`indexer`, `embedder` and `dedup` push to the live bucket by themselves at the end of a run, and the
indexer rebuilds `manifest.jsonl` from the bucket listing. Until 2026-09-20 that combination meant:
run the three commands above as the old README listed them, and production served an index with
**no place labels and no people tags for the whole corpus**. Now ([`photo_search/safety.py`](photo_search/safety.py),
[`tests/test_safety.py`](tests/test_safety.py)):

- the **indexer keeps** the fields `place_baker` and `face_promote` baked onto a photo, and **keeps
  re-listed copies out**: a photo the previous manifest already holds stays under that path, so a
  byte-identical copy published in a new album cannot take its place (the old album would lose a
  row, and every place label after it would move by one — labels address a photo by its position).
  `dedup` makes the same choice for whatever is left: the copy that was kept before wins, then the
  smallest path. *Not covered:* a file **replaced under the same name** is not noticed at all — the
  caption cache is keyed by path, so it keeps its old sha, caption, vector and tags until its row is
  removed from `caption_cache.jsonl` by hand;
- an `indexer --limit N` smoke run **does not push and does not truncate** an existing, fuller manifest;
- `place_baker` **refuses to run** while the manifest still holds byte-identical (captioned) copies —
  its labels count positions on the deduplicated manifest;
- every **push is checked against what is live, and is all-or-nothing**: it is refused — nothing
  uploaded, a non-zero exit from the CLI — if photos would lose baked fields (more than 2 % of those
  that carry one), more than 5 % of the live photos would disappear (counted by content, so `dedup`
  dropping duplicate rows passes), or any synced file would shrink by more than 20 %. An intended
  loss (an album deleted, labels withdrawn) goes through with
  `python -m photo_search.cloud_cache push --allow-data-loss`, which prints what it overrides. It is
  a flag on that one command on purpose — there is no environment variable that could stay exported;
- **vectors and rows must pair up** whenever either file is pushed — first push and
  `--allow-data-loss` included: no intended loss looks like that;
- a storage error while looking at the live copy **fails the push** instead of being read as
  "nothing live to lose" (the server's start-up pull stays tolerant: it starts on its local copy).

Run the tests: `cd photo-search && uv run pytest -q` (24, no cloud).

The bucket keeps versions and a 7-day soft-delete as the last line; `dedup` leaves `*.bak` locally.

## Known follow-ups

### Legacy embedding backend removal

The 2026-08 embedding migration (option 2 of the old plan) moved the default
to `gemini-embedding-2` via `google-genai`. The deprecated
`vertexai.vision_models` path survives only as the `_LegacyEmbedder` backend
in `photo_search/embedding.py`, selected when `EXPLORE_PHOTO_EMBED_MODEL` is
set back to `multimodalembedding@001` (the rollback path). Once the new index
has proven itself in production, delete the legacy backend and the old
untagged index files in GCS.

