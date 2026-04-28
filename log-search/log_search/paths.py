from pathlib import Path

CORPUS_ROOT = Path.home() / "projects" / "log"
CACHE_ROOT = Path.home() / ".cache" / "log-search"

CHUNKS_PATH = CACHE_ROOT / "chunks.jsonl"
INDEX_PATH = CACHE_ROOT / "index.npz"
META_PATH = CACHE_ROOT / "chunks_meta.jsonl"

PROJECT = "thematic-acumen-225120"
LOCATION = "europe-west4"
EMBED_MODEL = "text-embedding-005"
GENERATE_MODEL = "gemini-2.5-pro"
EMBED_DIM = 768

MAX_K = 20  # hard cap on retrieval depth — enforced in server / CLI / retriever

# Cloud cache (Phase 5b). Private bucket shared with photo-search via prefixes.
GCS_CACHE_BUCKET = "cdc-search-cache"
GCS_CACHE_PREFIX = "log-search/"


def ensure_cache_dir() -> Path:
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    return CACHE_ROOT
