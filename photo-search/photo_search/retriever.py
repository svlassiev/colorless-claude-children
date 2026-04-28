"""Cosine top-k over the photo index, optional EXIF date filter.

Date filter recognises:
- bare 4-digit years: '2014'
- season + year: 'summer 2017', 'winter 2010', 'autumn 2008' (also 'fall')
For photos without EXIF date, falls back to a folder-name heuristic
(e.g. 'summer2005/' → 2005, '10tradfall/' → 2010).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import numpy as np

from photo_search.paths import INDEX_PATH, MAX_K, META_PATH

_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_SEASON_YEAR_RE = re.compile(
    r"\b(spring|summer|autumn|fall|winter)\s+(19\d{2}|20\d{2})\b",
    re.IGNORECASE,
)
SEASONS_MONTHS = {
    "spring": (3, 5),
    "summer": (6, 8),
    "autumn": (9, 11),
    "fall": (9, 11),
    "winter": (12, 2),  # spans year boundary
}

_FOLDER_FOUR_DIGIT_RE = re.compile(r"(20\d{2}|19\d{2})")
# 2-digit year embedded in folder names like 'kolvica8' or '10tradfall'.
# Match a 1-2 digit number adjacent to letters (or string boundary).
_FOLDER_TWO_DIGIT_RE = re.compile(r"(?:^|[a-zA-Z])(\d{1,2})(?:[a-zA-Z]|$)")


def _folder_year(blob_path: str) -> int | None:
    """Approximate year from the top-level folder name.

    First tries a 4-digit year anywhere in the folder; then a 1-2 digit
    integer adjacent to letters (kolvica8 → 2008, 10tradfall → 2010).
    """
    folder = blob_path.split("/")[0]
    m4 = _FOLDER_FOUR_DIGIT_RE.search(folder)
    if m4:
        return int(m4.group(1))
    m2 = _FOLDER_TWO_DIGIT_RE.search(folder)
    if m2:
        yy = int(m2.group(1))
        # Heuristic: 0-30 → 20XX, 31-99 → 19XX. Bucket spans 2003-2026.
        return 2000 + yy if yy <= 30 else 1900 + yy
    return None


def _infer_date_iso_from_path(blob_path: str) -> str | None:
    """Returns YYYY-06-15 (mid-year) so a year-only query catches the photo
    even when the folder convention only encodes a year."""
    year = _folder_year(blob_path)
    return f"{year}-06-15" if year else None


@dataclass
class Hit:
    rank: int
    score: float
    blob_path: str
    gcs_uri: str
    date_iso: str | None
    caption: str
    sha: str


def parse_date_filter(query: str) -> tuple[str | None, str | None]:
    """Extract (lo, hi) ISO-date range from query, or (None, None)."""
    sm = _SEASON_YEAR_RE.search(query)
    if sm:
        season = sm.group(1).lower()
        year = int(sm.group(2))
        lo_m, hi_m = SEASONS_MONTHS[season]
        if lo_m <= hi_m:
            return f"{year}-{lo_m:02d}-01", f"{year}-{hi_m:02d}-30"
        # winter spans year boundary: Dec YYYY .. Feb YYYY+1
        return f"{year}-12-01", f"{year + 1}-02-28"
    ym = _YEAR_RE.search(query)
    if ym:
        y = ym.group(1)
        return f"{y}-01-01", f"{y}-12-31"
    return None, None


def load_index() -> tuple[np.ndarray, list[dict]]:
    """Returns (vectors, metas), filtering out zero-vector failures."""
    arr = np.load(INDEX_PATH)["vectors"]
    metas = [json.loads(line) for line in META_PATH.open()]
    norms = np.linalg.norm(arr, axis=1)
    mask = norms > 0
    return arr[mask], [m for m, ok in zip(metas, mask) if ok]


def search(
    query_vec: np.ndarray,
    vectors: np.ndarray,
    metas: list[dict],
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
        # Use EXIF date when available; fall back to folder-name heuristic.
        # Photos with neither are kept (don't penalize undatable photos).
        mask = np.ones(len(metas), dtype=bool)
        for i, m in enumerate(metas):
            d = m.get("exif_date_iso") or _infer_date_iso_from_path(m["blob_path"])
            if d is None:
                continue
            if date_lo and d < date_lo:
                mask[i] = False
            if date_hi and d > date_hi:
                mask[i] = False
        sims = np.where(mask, sims, -np.inf)

    # Backfill dedup: pull more candidates than k, dedup by content sha,
    # trim to k. Defends against any future content-duplicates that slip
    # past the post-index cleanup. Overshoot factor of 4 covers worst-case
    # clusters of identical content uploaded under many paths.
    overshoot = max(4 * k, k + 10)
    top_idx = np.argsort(-sims)[:overshoot]

    seen_sha: set[str] = set()
    hits: list[Hit] = []
    for i in top_idx:
        if not np.isfinite(sims[i]):
            break
        m = metas[i]
        sha = m["sha"]
        if sha in seen_sha:
            continue
        seen_sha.add(sha)
        hits.append(
            Hit(
                rank=len(hits) + 1,
                score=float(sims[i]),
                blob_path=m["blob_path"],
                gcs_uri=m["gcs_uri"],
                date_iso=m.get("exif_date_iso") or _infer_date_iso_from_path(m["blob_path"]),
                caption=m.get("caption", ""),
                sha=sha,
            )
        )
        if len(hits) >= k:
            break
    return hits
