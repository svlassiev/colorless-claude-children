from pathlib import Path

PROJECT = "thematic-acumen-225120"
LOCATION = "europe-west4"
BUCKET = "colorless-days-children"

CAPTION_MODEL = "gemini-2.5-flash"
EMBED_MODEL = "multimodalembedding@001"
GENERATE_MODEL = "gemini-2.5-pro"
EMBED_DIM = 1408

MAX_K = 20  # hard cap on retrieval depth — enforced in server / CLI / retriever

# Cloud cache (Phase 5b). Same private bucket as log-search, sibling prefix.
GCS_CACHE_BUCKET = "cdc-search-cache"
GCS_CACHE_PREFIX = "photo-search/"

CACHE_ROOT = Path.home() / ".cache" / "photo-search"
CAPTION_CACHE = CACHE_ROOT / "caption_cache.jsonl"
MANIFEST_PATH = CACHE_ROOT / "manifest.jsonl"
INDEX_PATH = CACHE_ROOT / "index.npz"
META_PATH = CACHE_ROOT / "manifest_meta.jsonl"


def ensure_cache_dir() -> Path:
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    return CACHE_ROOT
