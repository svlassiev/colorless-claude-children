"""Build serg.vlassiev.info /share/ URLs from a GCS blob path.

The `/share/*` routes (served by hiking-api) provide rich OG metadata previews
and friendly canonical URLs for both colorless and hiking content:

- Old-style colorless album (`pathName` + integer in filename): N parsed from
  the filename's digit suffix.  `pohod1497/Picture085.jpg` → `/share/pohod1497/85`.
- New-style colorless album (`useFiles` + files list in albums-files.json): N is
  the 1-indexed position.  `Lovozero2012/IMG_0038.jpg` → `/share/Lovozero2012/1`.
- Hiking photo: the share id is hiking-api's own `imageId` (a MongoDB UUID), which
  is NOT derivable from the GCS path — a UUID folder holds many images, each with
  its own imageId. So we look the blob path up in a PRIVATE map built offline from
  hiking-api (`photo_search.hiking_links`) and synced via the private GCS bucket.
  → `/share/hiking/image/<imageId>`.
- Anything else (incl. an orphaned hiking blob with no current imageId): returns
  None — better no link than a broken one; the caller falls back to the GCS URI.

PRECEDENCE: colorless album first (many folders exist in BOTH galleries and have
long-working colorless links), then the hiking map, then None.

PRIVACY: the hiking map exposes the bucket layout / image IDs, so it lives only in
the private cache (gitignored, GCS-synced) — never committed. albums.json /
albums-files.json are public colorless metadata and stay committed at the repo root.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from urllib.parse import quote

from photo_search.paths import HIKING_IMAGE_IDS_PATH

SITE_BASE = "https://serg.vlassiev.info"

# This module: photo-search/photo_search/site.py
# albums.json:  <repo-root>/albums.json   (i.e. parents[2])
_REPO_ROOT = Path(__file__).resolve().parents[2]
ALBUMS_JSON = _REPO_ROOT / "albums.json"
ALBUMS_FILES_JSON = _REPO_ROOT / "albums-files.json"


@cache
def _albums_by_folder() -> dict[str, dict]:
    if not ALBUMS_JSON.exists():
        return {}
    return {a["folder"]: a for a in json.loads(ALBUMS_JSON.read_text())}


@cache
def _album_files_by_folder() -> dict[str, list[str]]:
    if not ALBUMS_FILES_JSON.exists():
        return {}
    data = json.loads(ALBUMS_FILES_JSON.read_text())
    return {k: v["files"] for k, v in data.items()}


@cache
def _hiking_image_ids() -> dict[str, str]:
    """{blob_path: hiking imageId}, PRIVATE — pulled from the GCS cache at startup.

    Empty when absent or malformed (then hiking photos simply get no /share link,
    falling back to the GCS URI). Never raises — a bad file must not break the
    generation of every citation's link.
    """
    try:
        if not HIKING_IMAGE_IDS_PATH.exists():
            return {}
        data = json.loads(HIKING_IMAGE_IDS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _resolve_folder(blob_path: str) -> tuple[str, str] | None:
    """Find the longest folder-prefix of `blob_path` that matches a known
    album. Returns (folder, filename-or-tail) or None.

    Necessary because some folders are nested (`baikal/old`).
    """
    parts = blob_path.split("/")
    if len(parts) < 2:
        return None

    albums = _albums_by_folder()
    files_map = _album_files_by_folder()

    for split in range(len(parts) - 1, 0, -1):
        folder = "/".join(parts[:split])
        if folder in albums or folder in files_map:
            filename = "/".join(parts[split:])
            return folder, filename
    return None


def _colorless_url(blob_path: str) -> str | None:
    """The colorless `/share/{folder}/{n}` URL for a known album, else None."""
    resolved = _resolve_folder(blob_path)
    if resolved is None:
        return None
    folder, filename = resolved
    # Encode chars that would break the path; keep readable for alphanumerics.
    folder_url = quote(folder, safe="")

    # New-style album: 1-indexed position in the files list.
    files_map = _album_files_by_folder()
    if folder in files_map:
        try:
            n = files_map[folder].index(filename) + 1
        except ValueError:
            return None
        return f"{SITE_BASE}/share/{folder_url}/{n}"

    # Old-style album: parse the integer suffix from the filename stem.
    spec = _albums_by_folder().get(folder)
    if spec is None:
        return None
    path_name = spec.get("pathName") or ""
    if not path_name:
        return None
    stem = filename.rsplit(".", 1)[0]
    if not stem.startswith(path_name):
        return None
    rest = stem[len(path_name) :]
    if not rest.isdigit():
        return None
    return f"{SITE_BASE}/share/{folder_url}/{int(rest)}"


def site_url_for(blob_path: str) -> str | None:
    """Return a serg.vlassiev.info /share URL, or None if unsupported.

    Colorless album first (covers folders that exist in both galleries and have
    long-working colorless links), then the hiking imageId map, then None.
    """
    if len(blob_path.split("/")) < 2:
        return None

    url = _colorless_url(blob_path)
    if url:
        return url

    image_id = _hiking_image_ids().get(blob_path)
    if image_id:
        return f"{SITE_BASE}/share/hiking/image/{image_id}"

    return None
