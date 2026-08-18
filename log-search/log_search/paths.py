import os
from pathlib import Path

from search_common.settings import settings

# The corpus to index. Default is the live checkout; the committed-only
# indexing flow (the standing policy) exports HEAD via
# `python -m log_search.committed_corpus` and points this env var at the
# export, so untracked drafts never enter the index.
CORPUS_ROOT = Path(os.environ.get("LOG_CORPUS_ROOT", str(Path.home() / "projects" / "log")))
CACHE_ROOT = Path.home() / ".cache" / "log-search"

EMBED_MODEL = settings.log_embed_model
EMBED_LOCATION = settings.log_embed_location

# Index artefacts are tagged with the embedding model (legacy 005 keeps the
# untagged names it always had). A revision configured for model X pulls only
# X's files, so a query embedder and corpus vectors can never mismatch during
# a migration — the atomic-flip guarantee lives in the filenames.
_TAG = "" if EMBED_MODEL == "text-embedding-005" else f"-{EMBED_MODEL.replace('@', '-')}"
CHUNKS_PATH = CACHE_ROOT / f"chunks{_TAG}.jsonl"
INDEX_PATH = CACHE_ROOT / f"index{_TAG}.npz"
META_PATH = CACHE_ROOT / f"chunks_meta{_TAG}.jsonl"
# sha (image bytes prefix) → caption text. Persisted across runs so we
# don't re-bill the caption model for unchanged images.
IMAGE_CAPTION_CACHE = CACHE_ROOT / "image_caption_cache.jsonl"

PROJECT = settings.project
LOCATION = settings.location
# Model selection centralized in search_common.settings (EXPLORE_* env vars,
# "model" or "model@location"; defaults there). Every role — embedding,
# generation, captioning — carries its own endpoint; the server and CLI
# build separate clients per role.
GENERATE_MODEL = settings.generate_model
GENERATE_LOCATION = settings.generate_location
# Image captions go through a strong tier (CAPTION_MODEL) so they're verbose
# enough to act as load-bearing chunk content for retrieval — see captioner.py.
CAPTION_MODEL = settings.log_caption_model
CAPTION_LOCATION = settings.log_caption_location
EMBED_DIM = {"text-embedding-005": 768}.get(EMBED_MODEL, 3072)

MAX_K = 20  # hard cap on retrieval depth — enforced in server / CLI / retriever

# Chunker: minimum chunk size (in chars). Chunks below this threshold are
# merged into the preceding emitted chunk so we don't embed near-empty
# fragments and dilute the index. The chunk's heading_path still records
# the deeper-level provenance, so search can still surface it.
MIN_CHUNK_CHARS = 250

# Cloud cache (Phase 5b). Private bucket shared with photo-search via prefixes.
GCS_CACHE_BUCKET = "cdc-search-cache"
GCS_CACHE_PREFIX = "log-search/"


def ensure_cache_dir() -> Path:
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    return CACHE_ROOT
