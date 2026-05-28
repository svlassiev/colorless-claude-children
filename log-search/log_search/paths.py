from pathlib import Path

from search_common.settings import settings

CORPUS_ROOT = Path.home() / "projects" / "log"
CACHE_ROOT = Path.home() / ".cache" / "log-search"

CHUNKS_PATH = CACHE_ROOT / "chunks.jsonl"
INDEX_PATH = CACHE_ROOT / "index.npz"
META_PATH = CACHE_ROOT / "chunks_meta.jsonl"
# sha (image bytes prefix) → caption text. Persisted across runs so we
# don't re-bill Gemini Pro for unchanged images.
IMAGE_CAPTION_CACHE = CACHE_ROOT / "image_caption_cache.jsonl"

PROJECT = settings.project
LOCATION = settings.location  # regional endpoint for embeddings + generation
# Model selection centralized in search_common.settings (EXPLORE_* env vars).
# Defaults match the previous hardcoded values. Note: log-search uses one
# client for both embed_content and generate_content, so generation stays on
# the regional LOCATION; a "global"-only generate model would need that client
# split (see settings.gemini_location).
EMBED_MODEL = settings.log_embed_model
GENERATE_MODEL = settings.generate_model
# Image captions go through Pro (not Flash) so they're verbose enough to
# act as load-bearing chunk content for retrieval — see captioner.py.
CAPTION_MODEL = settings.log_caption_model
EMBED_DIM = 768

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
