"""Offline: build the GCS-blob-path -> hiking imageId map for /share links.

The hiking gallery (serg.vlassiev.info/hiking) is backed by hiking-api, which
stores each photo as a MongoDB document with its own `imageId` (the UUID used in
`/share/hiking/image/<imageId>`) plus the GCS `location`s of its variants. The
GCS path alone does NOT contain the imageId — a UUID folder holds many images,
each a separate document — so `site.py` cannot derive the share id from the blob
path. This script exports the mapping once, at build time, from hiking-api's
PUBLIC read API (`GET /hiking-api/folders` + `POST /hiking-api/images`). No
runtime coupling: serving reads only the baked file.

Output: HIKING_IMAGE_IDS_PATH = {blob_path: imageId} for every image variant.
PRIVATE (it exposes the bucket layout) — gitignored, synced to the private GCS
bucket, NEVER committed. `site.py` loads it; `cloud_cache` syncs it.

Run:
    uv run --directory photo-search python -m photo_search.hiking_links
    uv run --directory photo-search python -m photo_search.hiking_links --base https://serg.vlassiev.info
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request

from photo_search.paths import BUCKET, HIKING_IMAGE_IDS_PATH, ensure_cache_dir

DEFAULT_BASE = "https://serg.vlassiev.info"
_TIMEOUT = 120


def _get(url: str) -> object:
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as r:
        return json.load(r)


def _post(url: str, payload: dict) -> object:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.load(r)


def build_map(base: str) -> dict[str, str]:
    """{blob_path: imageId} for every variant of every hiking image."""
    folders = _get(f"{base}/hiking-api/folders")
    image_ids = sorted({i for lst in folders for i in lst.get("images", [])})
    print(f"hiking-api: {len(folders)} lists, {len(image_ids)} images", file=sys.stderr)
    images = _post(
        f"{base}/hiking-api/images",
        {"imageIds": image_ids, "skip": 0, "limit": len(image_ids)},
    )
    # Variant locations look like https://storage.googleapis.com/<bucket>/<blob>.
    loc_re = re.compile(rf"/{re.escape(BUCKET)}/(.+)$")
    out: dict[str, str] = {}
    for img in images:
        iid = img.get("imageId")
        if not iid:
            continue
        locs = [img.get("location", "")] + [
            v.get("location", "") for v in img.get("variants", [])
        ]
        for loc in locs:
            m = loc_re.search(loc or "")
            if m:
                out[m.group(1)] = iid
    return out


def main() -> int:
    ap = argparse.ArgumentParser(prog="photo-search-hiking-links")
    ap.add_argument("--base", default=DEFAULT_BASE, help="hiking site base URL")
    ap.add_argument("--out", default=str(HIKING_IMAGE_IDS_PATH))
    args = ap.parse_args()

    mapping = build_map(args.base)
    print(f"mapped {len(mapping)} blob paths -> imageId", file=sys.stderr)
    if not mapping:
        print("empty map — refusing to overwrite", file=sys.stderr)
        return 1

    ensure_cache_dir()
    from pathlib import Path

    Path(args.out).write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {args.out} (private, gitignored)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
