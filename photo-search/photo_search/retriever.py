"""Cosine top-k over the photo index, with optional structured filters.

Filters are passed in as a `Filters` bag from `photo_search.tools.base`
(date + location, either may be None). The cheap deterministic regex
date parser (`parse_date_filter`) is still exposed for the server to
use as a fast-path before calling the Flash router.

Date filter recognises:
- bare 4-digit years: '2014'
- season + year: 'summer 2017', 'winter 2010', 'autumn 2008' (also 'fall')
For photos without EXIF date, falls back to a folder-name heuristic
(e.g. 'summer2005/' → 2005, '10tradfall/' → 2010).

Location filter is a sha-set built by `tools.filter_by_location.execute`
against indexed `place_names` (Tier 1) + folder names (Tier 2). The
retriever just AND-masks the candidate pool with it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import numpy as np

from photo_search.paths import INDEX_PATH, MAX_K, META_PATH
from photo_search.tools.base import (
    DateFilter,
    Filters,
    LocationFilter,
    PersonFilter,
    ProximityFilter,
)

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
    """Extract (lo, hi) ISO-date range from query, or (None, None).

    Kept as a fast-path the server runs before / instead of asking the
    Flash router about dates. Cheap, deterministic, and covers the
    common pattern queries — Flash routing handles the rest when wired
    in (e.g., 'before the pandemic', 'around when the kids were small').
    """
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


def _date_mask(metas: list[dict], df: DateFilter) -> np.ndarray:
    """Boolean mask, True where the photo's date falls inside `df`.

    Uses EXIF date when present; falls back to a folder-name year
    heuristic. Photos with neither are kept — don't penalize undatable
    photos when the user asks for a date range.
    """
    out = np.ones(len(metas), dtype=bool)
    for i, m in enumerate(metas):
        d = m.get("exif_date_iso") or _infer_date_iso_from_path(m["blob_path"])
        if d is None:
            continue
        if df.start_iso and d < df.start_iso:
            out[i] = False
        if df.end_iso and d > df.end_iso:
            out[i] = False
    return out


def _location_mask(metas: list[dict], lf: LocationFilter) -> np.ndarray:
    """Boolean mask: True only for shas in `lf.matched_shas`.

    Caller invokes us only when the location was *recognized* (the
    executor returns None for unrecognized places, which keeps us out
    of this code path entirely). At that point matched_shas is the
    authoritative set — including the empty case, which legitimately
    means 'recognized place, but the corpus has no photos there.'
    """
    return np.array([m["sha"] in lf.matched_shas for m in metas], dtype=bool)


def _proximity_mask(metas: list[dict], pf: ProximityFilter) -> np.ndarray:
    """Boolean mask: True only for shas within the proximity filter's set.

    Distance is precomputed by the tool's executor (haversine against the
    place's pins); here we just AND-mask by the resulting sha membership,
    exactly like the location filter."""
    return np.array([m["sha"] in pf.matched_shas for m in metas], dtype=bool)


def _person_mask(metas: list[dict], pf: PersonFilter) -> np.ndarray:
    """Boolean mask: True only for shas in the person filter's set.

    The executor resolved the queried name to one or more identities and unioned
    their photos' shas; here we just AND-mask by membership, exactly like the
    location and proximity filters."""
    return np.array([m["sha"] in pf.matched_shas for m in metas], dtype=bool)


def search(
    query_vec: np.ndarray,
    vectors: np.ndarray,
    metas: list[dict],
    *,
    k: int = 5,
    filters: Filters | None = None,
) -> list[Hit]:
    # Defensive cap (last line of defense; server + CLI also clamp).
    k = min(max(k, 1), MAX_K)
    norms = np.linalg.norm(vectors, axis=1) * np.linalg.norm(query_vec)
    sims = (vectors @ query_vec) / np.maximum(norms, 1e-9)

    # Pre-filter by metadata before sort — filtered-out photos lose
    # eligibility to win the top-k regardless of their cosine score.
    # This is the core 'metadata gates retrieval, embedding ranks
    # survivors' design.
    if filters and not filters.is_empty:
        mask = np.ones(len(metas), dtype=bool)
        if filters.date is not None:
            mask &= _date_mask(metas, filters.date)
        if filters.location is not None:
            mask &= _location_mask(metas, filters.location)
        if filters.proximity is not None:
            mask &= _proximity_mask(metas, filters.proximity)
        if filters.person is not None:
            mask &= _person_mask(metas, filters.person)
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
