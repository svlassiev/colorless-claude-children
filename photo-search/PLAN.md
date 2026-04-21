# Photo Search — Build Plan

## Goal

Ship an end-to-end Vertex AI + Gemini multimodal RAG demo over `gs://colorless-days-children/`. Public repo, screenshot-able UI, linked from the Google Blackbelt application package (`../cv/Google/gap-closure.html`, Week 1 item 01).

## Non-goals

- Replace the existing static photo gallery at `serg.vlassiev.info`.
- Scale beyond the ~1–5 k photos currently in the bucket.
- 24×7 availability — the Vector Search endpoint is undeployed between demos to control cost.

## Scope estimate

One weekend of build, a handful of evenings of captioning/embedding runs (batch, unattended), approximately **$20–$50** in GCP spend if the Vector Search endpoint is undeployed overnight. The endpoint is the main cost driver (~$0.50/hr while deployed).

**Region:** `europe-west4` (decided 2026-04-19).

## Cost control

Strict rules for all work in this module (personal project on a tight budget — root `PLAN.md` baseline ~$36/mo):

1. **Ask first.** Any step that bills GCP — captioning batches, embedding batches, creating/deploying/serving a Vector Search endpoint, Cloud Run — requires explicit user confirmation before running. Never act on cost-generating commands autonomously even when they're listed as a step below.
2. **Huge disclaimer on every cost increase.** Before starting a recurring or sizeable charge, surface: the unit cost, what it adds per hour/day/month, the upper bound if left running, and how to stop it.
3. **Proactively offer pause/reduce at natural breaks.** End of phase, demo working, about to step away → remind what's currently billing and how to pause or delete it.

### Cost gates (ordered by risk)

| Gate | Phase | Cost shape | Pause / stop |
| --- | --- | --- | --- |
| **Vector Search index endpoint (deployed)** | 2 | **~$0.50/hr while deployed** → ~$12/day, ~$360/mo if left on | `gcloud ai index-endpoints undeploy-index ...` |
| Gemini 2.5 Flash captioning batch | 1 | ~$0.10–$0.50 for ~1 k images (one-off) | Kill the script |
| Multimodal embeddings batch | 2 | ~$0.10 for ~1 k images (one-off) | Kill the script |
| Gemini 2.5 Pro query-time | 4 | ~$0.01 per query | Close the app |
| Cloud Run service (if deployed) | 5 optional | Request-time only; ~free at idle with scale-to-zero | `gcloud run services delete ...` |

The **deployed index endpoint** is the single largest and most silent cost — it accrues while the endpoint is up, independent of query traffic. Always undeploy when walking away.

## Phases

### Phase 0 — Prerequisites

Goal: gcloud/identity/API state ready for Python SDK calls.

- [ ] Verify or configure a gcloud configuration using account `svlassiev@gmail.com` + project `thematic-acumen-225120`, activate it
- [ ] `gcloud auth application-default login` as `svlassiev@gmail.com` (Python SDK ADC)
- [ ] Enable APIs: `aiplatform.googleapis.com` (likely `storage.googleapis.com` already on)
- [ ] Confirm IAM for `svlassiev@gmail.com`: `roles/aiplatform.user`, read on `gs://colorless-days-children/` (project owner likely covers both)
- [ ] Pick a region for Vertex Vector Search + embedding calls — tentative: `europe-west4`
- [ ] `uv init` inside `photo-search/`, pin Python 3.11+, add `google-cloud-aiplatform`, `google-cloud-storage`, `google-genai`, `pillow`, `pydantic`, `streamlit`, `pytest`

#### Log

**2026-04-19**

- Scaffolded module: `README.md`, `PLAN.md`, Cost-control section with cost-gates table.
- Region decided: `europe-west4`.
- Cost-discipline rule saved as feedback memory (always ask before paid actions, disclaim increases, offer pause at natural breaks) and codified in this PLAN.
- Paused before any gcloud mutation. **No GCP spend yet.**

Went well: identity audit up front prevented wrong-account cloud calls; cost rule captured as durable memory before any billing surface was touched.

Surprises / deviations: (1) `development` gcloud config did not match CLAUDE.md's description — CLAUDE.md may need a correction after the new `personal` config is in place. (2) Naive `gh` calls would silently operate as `vlassieves` due to the env-var-set token — non-obvious. (3) IDE linter persistently flags the cost-gates table layout (valid GFM, info-level — ignored).

### Phase 1 — Ingest + caption

Goal: one `manifest.jsonl` row per photo — `{gcs_uri, caption, exif.date, exif.gps, checksum}`.

- [ ] Enumerate `gs://colorless-days-children/` (skip thumbnails — `_thumbnail` suffix or `1_` prefix; skip non-JPEG)
- [ ] Extract EXIF (`DateTimeOriginal`, GPS) with Pillow
- [ ] Batch call Gemini 2.5 Flash Vision for a ~2-sentence caption per image
- [ ] Cache captions by image checksum so re-runs don't re-bill
- [ ] Smoke-test on 10 images end-to-end before the full batch

### Phase 2 — Embed + index

Goal: queryable Vertex AI Vector Search index.

- [ ] Generate one `multimodalembedding@001` vector per image
- [ ] Create a Vector Search index — tree-AH, 1408-dim, cosine
- [ ] Create an index endpoint (public for demo — cheaper than private with PSC)
- [ ] Deploy the index, upsert vectors + metadata payload (date-iso, gcs_uri, caption-preview)
- [ ] Document the `undeploy` command in the runbook so the endpoint doesn't idle

### Phase 3 — Retrieve + filter

Goal: text → top-k GCS URIs.

- [ ] Embed the query with the text side of `multimodalembedding@001`
- [ ] ANN search with optional metadata filter
- [ ] Query parser for temporal hints (regex for `YYYY`, seasons, month names) → date-range filter
- [ ] Return top-k with scores

### Phase 4 — Generate

Goal: Gemini writes a short narrative over retrieved images — this is the "G" in RAG.

- [ ] Pass GCS URIs (Gemini can read GCS directly) + query to Gemini 2.5 Pro
- [ ] Prompt for a 2–3-sentence summary plus one-line per-image annotations
- [ ] Handle zero-result queries gracefully

### Phase 5 — UI

Goal: a clickable demo, screenshot-able for the CV.

- [ ] Streamlit — query input, Gemini narrative, grid of retrieved thumbnails
- [ ] Click a thumbnail → full-size via the existing public GCS URL
- [ ] Deployment decision: local-only vs. Cloud Run (cost vs. signal trade-off)

### Phase 6 — Evaluation harness

Goal: measurable "this works" signal — closes the *evaluation* item in the gap list.

- [ ] Curate 15–30 query / expected-photo pairs manually
- [ ] Compute Recall@5 and MRR on retrieval
- [ ] Gemini-as-judge grading of 10 query narratives (rubric: groundedness, relevance)
- [ ] Summarise in README

### Phase 7 — Polish + ship

- [ ] README screenshots
- [ ] Link the repo from the CV Blackbelt pitch (`../cv/Google/gap-closure.html`)
- [ ] Optional: short blog post reframing MCP-auth work for an enterprise GenAI audience (closes *public artefact* item in the gap list)

## Progress log

Per the root `CLAUDE.md` convention, each phase gets a `#### Log` subsection appended on completion: what was actually done, surprises, deviations from the plan above. Terse past-tense notes.
