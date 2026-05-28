"""Resolve the maps.app.goo.gl pins in place_labels.json to coordinates.

Each labeled range carries a Google Maps short-link in its `place_detail`.
This module expands every unique short-link to a (lat, lng) and writes the
result to data/place_coords.json, keyed by the short-link. That file is the
gazetteer the baker reads to stamp a coordinate onto each photo.

Two resolution paths (a short-link lands in one or the other):
  - URL coords: the short-link redirects to a Maps URL that already carries
    the point — '/maps/search/LAT,LNG', '/@LAT,LNG,zoom', '!3dLAT!4dLNG', or
    '?q=LAT,LNG'. ~75% of pins. Free, exact, no API.
  - Geocode: the short-link redirects to a named place ('/maps/place/<name>')
    with no coords in the URL. We geocode <name> via the Google Geocoding API
    (centroid of that named POI — which is what the pin pointed at anyway).
    Needs GEOCODING_API_KEY in the env; skipped (left pending) when absent.

Idempotent: links already resolved in place_coords.json are skipped, so
re-running after adding pins only touches the new ones (and only the new
ones cost a geocode call). Pass --refresh to re-resolve everything.

The consent interstitial Google serves to EU/unauthenticated clients is
skipped with a `CONSENT=YES+1` cookie on the redirect fetch.

Run:  uv run --directory photo-search python -m photo_search.coord_resolver
      GEOCODING_API_KEY=… uv run … -m photo_search.coord_resolver   # +geocode
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LABELS_PATH: Path = Path(__file__).parent / "data" / "place_labels.json"
COORDS_PATH: Path = Path(__file__).parent / "data" / "place_coords.json"

_SHORTLINK_RE = re.compile(r"https?://maps\.app\.goo\.gl/\S+")

# Coordinate shapes seen in resolved effective URLs, tried in order. Each
# captures (lat, lng). Latitudes/longitudes are 3+ decimal places to avoid
# matching zoom levels or unrelated short decimals.
_COORD_PATTERNS = [
    re.compile(r"/maps/search/(-?\d{1,3}\.\d{3,}),\+?(-?\d{1,3}\.\d{3,})"),
    re.compile(r"/@(-?\d{1,3}\.\d{3,}),(-?\d{1,3}\.\d{3,}),"),
    re.compile(r"!3d(-?\d{1,3}\.\d{3,})!4d(-?\d{1,3}\.\d{3,})"),
    re.compile(r"[?&]q=(-?\d{1,3}\.\d{3,}),(-?\d{1,3}\.\d{3,})"),
]
# Capture the whole place segment up to the next '/' (the address can be
# long and contains URL-encoded Cyrillic — truncating mid-%XX corrupts it).
_PLACE_NAME_RE = re.compile(r"/maps/place/([^/]+)")

_GEOCODE_ENDPOINT = "https://maps.googleapis.com/maps/api/geocode/json"


def unique_shortlinks(labels: dict) -> list[str]:
    """Every distinct maps.app.goo.gl link across all albums' ranges+defaults."""
    out: set[str] = set()
    for album in labels.get("albums", {}).values():
        entries = [album.get("default", {}), *album.get("ranges", [])]
        for e in entries:
            for u in _SHORTLINK_RE.findall(e.get("place_detail") or ""):
                out.add(u.rstrip(".,);"))
    return sorted(out)


def _follow(url: str, timeout: float = 25.0) -> str:
    """Return the effective URL after following redirects, consent skipped."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0", "Cookie": "CONSENT=YES+1"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.geturl()


def resolve_shortlink(url: str) -> dict:
    """Resolve one short-link to {lat,lng,source,name} or a pending/error stub.

    `source`: 'url' (coords from the redirect target) or 'place' (named place,
    coords not in URL — needs a later geocode pass). Errors return
    {'error': msg} so a transient failure doesn't poison the cache.
    """
    try:
        eff = _follow(url)
    except Exception as e:  # noqa: BLE001 — record, don't crash the batch
        return {"error": f"{type(e).__name__}: {e}"[:80]}
    for pat in _COORD_PATTERNS:
        m = pat.search(eff)
        if m:
            return {"lat": float(m.group(1)), "lng": float(m.group(2)), "source": "url"}
    m = _PLACE_NAME_RE.search(eff)
    if m:
        return {"source": "place", "name": urllib.parse.unquote_plus(m.group(1))}
    return {"source": "place", "name": "", "effective": eff[:120]}


def geocode(name: str, api_key: str, timeout: float = 25.0) -> dict | None:
    """Geocode a place name → {lat,lng} via the Google Geocoding API, or None."""
    q = urllib.parse.urlencode({"address": name, "key": api_key})
    req = urllib.request.Request(f"{_GEOCODE_ENDPOINT}?{q}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    if data.get("status") != "OK" or not data.get("results"):
        return None
    loc = data["results"][0]["geometry"]["location"]
    return {"lat": loc["lat"], "lng": loc["lng"]}


def _load_coords() -> dict:
    if COORDS_PATH.exists():
        return json.loads(COORDS_PATH.read_text(encoding="utf-8"))
    return {}


def _write_coords(coords: dict) -> None:
    COORDS_PATH.write_text(
        json.dumps(coords, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _is_resolved(entry: dict | None) -> bool:
    return bool(entry) and "lat" in entry and "lng" in entry


def main() -> int:
    ap = argparse.ArgumentParser(prog="photo-search-coord-resolver")
    ap.add_argument("--refresh", action="store_true", help="re-resolve all links")
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()

    labels = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    links = unique_shortlinks(labels)
    coords = _load_coords()
    api_key = os.environ.get("GEOCODING_API_KEY")

    todo = [u for u in links if args.refresh or not _is_resolved(coords.get(u))]
    print(f"{len(links)} unique links; {len(todo)} to resolve", file=sys.stderr)

    # Phase 1: redirect resolution (free). Fills coords for 'url' links and
    # records names for 'place' links.
    resolved = list(
        ThreadPoolExecutor(max_workers=args.workers).map(resolve_shortlink, todo)
    )
    for u, r in zip(todo, resolved):
        merged = {**coords.get(u, {}), **r}
        if "error" not in r:
            merged.pop("error", None)
        coords[u] = merged

    # Phase 2: geocode the 'place' links if a key is available.
    pending = [
        u for u in links
        if coords.get(u, {}).get("source") == "place" and not _is_resolved(coords[u])
    ]
    geocoded = 0
    if pending and api_key:
        for u in pending:
            name = coords[u].get("name")
            if not name:
                continue
            try:
                g = geocode(name, api_key)
            except Exception as e:  # noqa: BLE001
                coords[u]["error"] = f"{type(e).__name__}: {e}"[:80]
                continue
            if g:
                coords[u].update(g)
                coords[u]["source"] = "geocode"
                coords[u].pop("error", None)
                geocoded += 1
    elif pending:
        print(
            f"{len(pending)} named-place links need geocoding — set GEOCODING_API_KEY "
            "to resolve them",
            file=sys.stderr,
        )

    _write_coords(coords)

    n_url = sum(1 for v in coords.values() if v.get("source") == "url")
    n_geo = sum(1 for v in coords.values() if v.get("source") == "geocode")
    n_pending = sum(
        1 for v in coords.values() if v.get("source") == "place" and not _is_resolved(v)
    )
    n_err = sum(1 for v in coords.values() if "error" in v)
    print(
        f"resolved: {n_url} url + {n_geo} geocode | pending: {n_pending} | "
        f"errors: {n_err} | geocoded this run: {geocoded}",
        file=sys.stderr,
    )
    print(f"wrote {COORDS_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())