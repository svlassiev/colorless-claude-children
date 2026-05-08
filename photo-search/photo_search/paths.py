from pathlib import Path

PROJECT = "thematic-acumen-225120"
LOCATION = "europe-west4"
BUCKET = "colorless-days-children"

CAPTION_MODEL = "gemini-2.5-flash"
EMBED_MODEL = "multimodalembedding@001"
GENERATE_MODEL = "gemini-2.5-pro"
RERANK_MODEL = "gemini-2.5-flash"
EMBED_DIM = 1408

MAX_K = 20  # hard cap on retrieval depth — enforced in server / CLI / retriever

# Reranker activates only when the user requests at least this much depth.
# Below the threshold, all hits go to Pro directly and the frontend falls
# back to similarity-score fading.
RERANK_THRESHOLD_K = 20
# Number of top reranked hits passed to Pro for the answer step. Hits
# beyond this are still shown to the user (faded) but not summarised.
RERANK_KEEP = 10
# Per-image batch size for parallel Flash calls. RERANK_THRESHOLD_K must be
# divisible by this for clean batching at the threshold.
RERANK_BATCH_SIZE = 5
# Soft timeout for the entire rerank step (downloads + Flash). On timeout
# we fall back to similarity ordering and still send top-RERANK_KEEP to
# Pro — guarantees a fast response when Flash is slow.
RERANK_TIMEOUT_S = 15.0

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
