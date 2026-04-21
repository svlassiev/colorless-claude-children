# Photo Search

A multimodal RAG demo over this site's photo archive, built on **Vertex AI + Gemini**.

Type queries like *"hiking trip"*, *"forest in autumn"*, *"girl with red hair"*, *"people from 2009"* — the app finds matching photos from the ~1 k-image personal collection in `gs://colorless-days-children/` and has Gemini write a short natural-language summary of what it found.

## Why this exists

Two reasons that align:

1. **Genuinely useful.** The colorless-days gallery has ~20 years of photos organised by folder but with no tags, captions, or searchable metadata. Finding *that one photo* of a specific hike in 2012 means scrolling through folders.
2. **Gap-closure artefact.** An end-to-end, hands-on Vertex AI + Gemini + multimodal-embedding piece of work, for the Google Cloud GenAI Blackbelt application — see `../cv/Google/gap-closure.html`, Week 1 item 01.

## Example queries

- *"kids playing in snow"* — semantic
- *"girl with red hair"* — attribute-based
- *"people from 2009"* — temporal (EXIF date filter, not embeddings)
- *"hiking trips in the Alps in 2017"* — hybrid (semantic + date filter)

## Architecture

```
photos in GCS
     │
     ├── ingest ── Gemini 2.5 Flash Vision ──▶ caption
     │          ── EXIF (Pillow)           ──▶ metadata (date, gps)
     │
     └── embed  ── multimodalembedding@001 ──▶ 1408-dim vector
                                                     │
                                                     ▼
                                   Vertex AI Vector Search index
                                                     │
query ── embed ── ANN + metadata filter ── top-k ── Gemini 2.5 Pro ── narrative + thumbnails
```

## Stack

| Layer | Choice | Why |
|---|---|---|
| Embeddings | `multimodalembedding@001` (1408-dim) | Shared text+image space — a text query lands near matching images without a caption bottleneck |
| Ingest captioning | Gemini 2.5 Flash | Cheap, fast; captions are debuggable metadata, not the primary retrieval signal |
| Query-time generation | Gemini 2.5 Pro | Writes the narrative over retrieved images (the "G" in RAG) |
| Vector store | Vertex AI Vector Search | Native Google-cloud stack, the canonical choice for Blackbelt-team signal |
| Metadata filter | Vector Search filter expression | Temporal queries via EXIF date |
| UI | Streamlit | Single-file, fast to iterate; screenshot-able for the CV |
| Photos | `gs://colorless-days-children/` | Existing bucket, no new storage |

## Identity guardrail

This module follows the repo-level strict rule: all operations run as `svlassiev` on GCP project `thematic-acumen-225120`. Never `vlassieves` or any Epidemic Sound identity. See the root `CLAUDE.md` for the full identity table.

## Running locally

_Filled in during Phase 5._

## Status

See [`PLAN.md`](./PLAN.md) for the current phase and progress log.
