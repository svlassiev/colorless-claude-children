"""filter_by_proximity — narrow the search to photos near a named place.

The distance counterpart to filter_by_location. Where location matches by
*label* (photo tagged Хибины), proximity matches by *geographic distance*
(photo's coordinate within N km of any of the place's pins).

How the anchor is found: the same alias machinery as filter_by_location
resolves the query place to canonical names; the anchor pin-set is the
coordinates of every photo labeled with one of those canonicals. Because a
canonical legitimately has many pins (Санкт-Петербург has ~48), proximity
is min-distance to *any* anchor — a photo near any one of the place's pins
counts. A place whose photos carry no coordinate yields no anchors, so the
tool returns None (unlocatable → no filter, vector search proceeds).

Photo coordinates come from `place_coord` baked by place_baker (EXIF GPS,
else the photo's range pin). Photos without `place_coord` can never match —
proximity is strictly for the located subset of the corpus.
"""

from __future__ import annotations

import math
import sys

from google.genai import types
from pydantic import BaseModel, Field

from photo_search.tools.base import ProximityFilter
from photo_search.tools.filter_by_location import _canonicals_matching_query, _norm
from search_common.settings import settings

# Default radius when the user says "near X" without a distance. A
# compromise: tight enough to be meaningful in a city, loose enough that a
# single mountain/lake pin still gathers its surroundings. The model
# overrides this whenever the query names a distance.
DEFAULT_RADIUS_KM = 10.0
_EARTH_RADIUS_KM = 6371.0088


DECLARATION = types.FunctionDeclaration(
    name="filter_by_proximity",
    description=(
        "Narrow the photo search to photos taken NEAR a place, by geographic "
        "distance. Call this only when the query expresses nearness or a "
        "radius — 'near Купчино', 'close to the dacha', 'within 5 km of "
        "Petergof', 'around Хибины'. Prefer the plain filter_by_location tool "
        "for 'at'/'in'/'from' a place ('photos from Хибины', 'at school'); use "
        "this one when the user clearly wants a surrounding area rather than "
        "the place itself. Do NOT call for queries with no place, or purely "
        "topical ones ('photos of snow')."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "place_name": types.Schema(
                type=types.Type.STRING,
                description=(
                    "The anchor place the search should be near, exactly as the "
                    "user wrote it or the closest single noun phrase. Cyrillic, "
                    "transliteration, English, and nicknames ('the dacha') are "
                    "all fine — normalization and aliases are handled internally."
                ),
            ),
            "radius_km": types.Schema(
                type=types.Type.NUMBER,
                description=(
                    "Search radius in kilometers, if the user stated one "
                    "('within 5 km' → 5). Omit when the user only says 'near' / "
                    "'around' with no distance — a sensible default is applied."
                ),
            ),
        },
        required=["place_name"],
    ),
)


class Args(BaseModel):
    """Validated arguments for filter_by_proximity."""

    place_name: str = Field(min_length=1, max_length=80)
    # Cap at 500 km: beyond that 'near' is meaningless for this corpus and a
    # huge radius would just return everything located.
    radius_km: float = Field(default=DEFAULT_RADIUS_KM, gt=0, le=500)


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km between two (lat, lng) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def _coord(m: dict) -> tuple[float, float] | None:
    c = m.get("place_coord")
    if isinstance(c, list) and len(c) == 2:
        return (float(c[0]), float(c[1]))
    return None


# Cache geocoded query places per server instance: a place the user types but
# never tagged (e.g. "Красное Село") is geocoded once, then reused. None means
# "geocode returned nothing" — cached too, so we don't re-call for misses.
_GEOCODE_CACHE: dict[str, tuple[float, float] | None] = {}


def _geocode_anchor(place_name: str) -> tuple[float, float] | None:
    """Last-resort anchor: geocode the query place to a point when no labeled
    photo anchors it. Returns None when geocoding is disabled (no API key) or
    the place can't be resolved — caller then yields no proximity filter.

    Network I/O lives here rather than in the pure executor's main path; it's
    only reached for otherwise-unanchorable places and is cached aggressively.
    """
    key = settings.geocoding_api_key
    if not key:
        return None
    qn = _norm(place_name)
    if qn in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[qn]
    # Imported lazily: keeps the batch-only resolver out of the import path for
    # the common (labeled-anchor) case.
    from photo_search.coord_resolver import geocode

    point: tuple[float, float] | None = None
    try:
        g = geocode(place_name, key)
        if g:
            point = (float(g["lat"]), float(g["lng"]))
    except Exception as e:  # noqa: BLE001 — never break retrieval on geocode failure
        print(f"filter_by_proximity: geocode failed for {place_name!r}: {e}", file=sys.stderr)
    _GEOCODE_CACHE[qn] = point
    return point


def execute(args: Args, metas: list[dict]) -> ProximityFilter | None:
    """Return a ProximityFilter for photos within radius of args.place_name's
    pins, or None when the place is unrecognized or has no located photos.

    None vs empty matched_shas mirrors filter_by_location: None means 'no
    proximity constraint' (vector search runs unfiltered); a filter with an
    empty set means 'recognized, located place, but nothing within radius.'
    """
    canonicals = _canonicals_matching_query(args.place_name)
    canonicals_n = {_norm(c) for c in canonicals}

    # All photos that have a coordinate (the searchable-by-distance subset),
    # plus the anchors: coordinates of photos labeled with one of the
    # canonicals the query resolved to.
    anchors: list[tuple[float, float]] = []
    located: list[tuple[str, tuple[float, float]]] = []
    for m in metas:
        coord = _coord(m)
        if coord is None:
            continue
        located.append((m["sha"], coord))
        names = m.get("place_names") or []
        if canonicals_n and any(_norm(pn) in canonicals_n for pn in names):
            anchors.append(coord)

    if not anchors:
        # No labeled photo anchors the place (unrecognized name, or a known
        # place whose photos lack coordinates). Fall back to geocoding the
        # query place to a single point so "near <somewhere I never tagged>"
        # still works. None → unlocatable → no proximity filter.
        point = _geocode_anchor(args.place_name)
        if point is None:
            return None
        anchors = [point]

    matched = {
        sha
        for sha, coord in located
        if min(_haversine_km(coord, a) for a in anchors) <= args.radius_km
    }
    return ProximityFilter(
        place_name=args.place_name,
        radius_km=args.radius_km,
        matched_shas=frozenset(matched),
    )
