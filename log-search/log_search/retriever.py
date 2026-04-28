"""Cosine top-k over the in-memory index, with optional date-range filter."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import numpy as np

from log_search.paths import CHUNKS_PATH, INDEX_PATH, MAX_K, META_PATH

_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_QUARTER_RE = re.compile(r"\bQ([1-4])\s*(20\d{2})\b", re.IGNORECASE)
_QUARTER_MONTHS = {"1": (1, 3), "2": (4, 6), "3": (7, 9), "4": (10, 12)}


@dataclass
class Hit:
    rank: int
    score: float
    file: str
    date_iso: str | None
    heading_path: str
    text: str


def parse_date_filter(query: str) -> tuple[str | None, str | None]:
    """Extract a (lo, hi) ISO-date range from the query, or (None, None).

    Recognises:
      - 'Q3 2024' → 2024-07-01 .. 2024-09-30
      - bare 4-digit year → 2024-01-01 .. 2024-12-31
    """
    qm = _QUARTER_RE.search(query)
    if qm:
        q, year = qm.group(1), qm.group(2)
        m_lo, m_hi = _QUARTER_MONTHS[q]
        last_day = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}[m_hi]
        return f"{year}-{m_lo:02d}-01", f"{year}-{m_hi:02d}-{last_day:02d}"
    ym = _YEAR_RE.search(query)
    if ym:
        y = ym.group(1)
        return f"{y}-01-01", f"{y}-12-31"
    return None, None


def load_index() -> tuple[np.ndarray, list[dict], dict[str, str]]:
    """Returns (vectors, metas, sha→full_text map)."""
    vectors = np.load(INDEX_PATH)["vectors"]
    metas = [json.loads(l) for l in META_PATH.open()]
    chunks = {c["sha"]: c["text"] for c in (json.loads(l) for l in CHUNKS_PATH.open())}
    return vectors, metas, chunks


def search(
    query_vec: np.ndarray,
    vectors: np.ndarray,
    metas: list[dict],
    texts: dict[str, str],
    *,
    k: int = 5,
    date_lo: str | None = None,
    date_hi: str | None = None,
) -> list[Hit]:
    # Defensive cap (last line of defense; server + CLI also clamp).
    k = min(max(k, 1), MAX_K)
    norms = np.linalg.norm(vectors, axis=1) * np.linalg.norm(query_vec)
    sims = (vectors @ query_vec) / np.maximum(norms, 1e-9)

    if date_lo or date_hi:
        # Mask out chunks outside the range. Chunks with no date_iso are kept
        # (they may be year-agnostic background like brag-book sections).
        mask = np.ones(len(metas), dtype=bool)
        for i, m in enumerate(metas):
            d = m.get("date_iso")
            if d is None:
                continue
            if date_lo and d < date_lo:
                mask[i] = False
            if date_hi and d > date_hi:
                mask[i] = False
        sims = np.where(mask, sims, -np.inf)

    top_idx = np.argsort(-sims)[:k]
    hits = []
    for rank, i in enumerate(top_idx):
        if not np.isfinite(sims[i]):
            break
        m = metas[i]
        hits.append(
            Hit(
                rank=rank + 1,
                score=float(sims[i]),
                file=m["file"],
                date_iso=m.get("date_iso"),
                heading_path=m["heading_path"],
                text=texts[m["sha"]],
            )
        )
    return hits
