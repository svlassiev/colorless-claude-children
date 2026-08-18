"""Photo embedding backends behind one small interface.

Two families serve photo embeddings:

- multimodalembedding@001 (legacy, 1408-dim, regional) — only reachable
  through the deprecated `vertexai.vision_models` SDK; google-genai never
  exposed it. Kept as the default until the gemini-embedding-2 index passes
  its retrieval A/B.
- gemini-embedding-* (3072-dim; gemini-embedding-2 adds images, global-only)
  — served through google-genai `embed_content`, which also retires the dead
  SDK dependency for good once the flip lands.

`make_photo_embedder()` picks the backend from EMBED_MODEL. Both backends
expose `embed_image(bytes)` and `embed_text(str)` returning float32 arrays,
which is all the indexer and the query path ever need. Construction is where
any SDK init happens — importing this module stays side-effect free.
"""

from __future__ import annotations

import numpy as np

from photo_search.paths import EMBED_LOCATION, EMBED_MODEL, LOCATION, PROJECT


class _LegacyEmbedder:
    """multimodalembedding@001 via the (deprecated) vertexai SDK."""

    def __init__(self) -> None:
        import vertexai
        from vertexai.vision_models import MultiModalEmbeddingModel

        vertexai.init(project=PROJECT, location=LOCATION)
        self._model = MultiModalEmbeddingModel.from_pretrained(EMBED_MODEL)

    def embed_image(self, img_bytes: bytes) -> np.ndarray:
        from vertexai.vision_models import Image

        embs = self._model.get_embeddings(image=Image(image_bytes=img_bytes))
        return np.array(embs.image_embedding, dtype=np.float32)

    def embed_text(self, text: str) -> np.ndarray:
        embs = self._model.get_embeddings(contextual_text=text[:1024])
        return np.array(embs.text_embedding, dtype=np.float32)


class _GenaiEmbedder:
    """gemini-embedding family via google-genai embed_content.

    Images are embedded plain (no task type — cross-modal docs); text queries
    declare RETRIEVAL_QUERY, which the family supports and which measured
    fine against image vectors in the migration probes.
    """

    def __init__(self) -> None:
        from google import genai

        self._client = genai.Client(
            vertexai=True, project=PROJECT, location=EMBED_LOCATION
        )

    def embed_image(self, img_bytes: bytes) -> np.ndarray:
        from google.genai import types

        r = self._client.models.embed_content(
            model=EMBED_MODEL,
            contents=[types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")],
        )
        return np.array(r.embeddings[0].values, dtype=np.float32)

    def embed_text(self, text: str) -> np.ndarray:
        from google.genai import types

        r = self._client.models.embed_content(
            model=EMBED_MODEL,
            contents=text,
            config=types.EmbedContentConfig(task_type="RETRIEVAL_QUERY"),
        )
        return np.array(r.embeddings[0].values, dtype=np.float32)


PhotoEmbedder = _LegacyEmbedder | _GenaiEmbedder


def make_photo_embedder() -> PhotoEmbedder:
    if EMBED_MODEL.startswith("gemini-embedding"):
        return _GenaiEmbedder()
    return _LegacyEmbedder()
