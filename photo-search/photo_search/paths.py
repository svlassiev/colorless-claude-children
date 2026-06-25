from pathlib import Path

from search_common.settings import settings

PROJECT = settings.project
LOCATION = settings.location  # regional endpoint for embeddings + project init
BUCKET = "colorless-days-children"

# Model selection is centralized in search_common.settings, driven by
# EXPLORE_*_MODEL env vars. Defaults match the previous hardcoded values, so
# behavior is unchanged until an env var overrides one. (Endpoint stays
# LOCATION; moving generation to a "global"-only model also needs settings.
# gemini_location + a regional embed client — see that field's note.)
CAPTION_MODEL = settings.photo_caption_model
EMBED_MODEL = settings.photo_embed_model
GENERATE_MODEL = settings.generate_model
RERANK_MODEL = settings.rerank_model
ROUTING_MODEL = settings.routing_model
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

# ─── Face search (offline pipeline) ─────────────────────────────────────
# Face data is biometric / personally identifying — ALL of it stays private.
# faces.jsonl AND person_aliases.json (which carries the people's names) live
# under CACHE_ROOT (outside the repo, gitignored). Unlike place_aliases.json
# (committed, public place names), person_aliases.json is NOT committed — it is
# synced to the private GCS bucket and pulled at serving startup.
FACES_PATH = CACHE_ROOT / "faces.jsonl"          # detected faces + embeddings
FACE_REVIEW_DIR = CACHE_ROOT / "face_review"     # per-cluster montages for naming
PERSON_ALIASES_PATH = CACHE_ROOT / "person_aliases.json"  # name → forms (private)
PERSON_EXTRAS_PATH = CACHE_ROOT / "person_extras.json"    # owner extras/exclude (private)

# Thresholds — starting guesses, tuned during the review rounds.
FACE_DET_MIN = 0.6        # min RetinaFace detection score to keep a face
FACE_MIN_PX = 40         # min face bounding-box side in pixels (smaller is noisy)
HDBSCAN_MIN_CLUSTER = 4  # min faces to form a cluster (smaller is treated as noise)
FACE_ASSIGN_COS = 0.55   # (family phase) min cosine to attach a face to an identity


def ensure_cache_dir() -> Path:
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    return CACHE_ROOT
