"""Build serg.vlassiev.info /share/ URLs from a GCS blob path.

The `/share/*` routes (served by hiking-api per root PLAN.md "Social media
link previews") provide rich OG metadata previews and friendly canonical
URLs that work for both colorless and hiking content:

- Old-style colorless album (`pathName` + integer in filename): N parsed
  from the filename's digit suffix.
  Example: `pohod1497/Picture085.jpg` → `/share/pohod1497/85`.

- New-style colorless album (`useFiles` + files list in albums-files.json):
  N is the 1-indexed position in that list.
  Example: `Lovozero2012/IMG_0038.jpg` → `/share/Lovozero2012/1`.

- Hiking blob (`<imageId>/<variantId>.jpg`, both UUIDs): the *folder* UUID
  is the imageId; the file UUID is just a variant identifier (V800/V1024/
  V2048/original). Per HIKING-PLAN.md.
  Example: `7f08f072-..../4a074e9d-....jpg`
  → `/share/hiking/image/7f08f072-...`

- Anything else: returns None — the caller (CLI / API) should fall back to
  the GCS URI. The repo's known albums cover the colorless-days corpus;
  unknowns mostly indicate photos written by other workloads sharing the
  bucket.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from urllib.parse import quote

SITE_BASE = "https://serg.vlassiev.info"

# This module: photo-search/photo_search/site.py
# albums.json:  <repo-root>/albums.json   (i.e. parents[2])
_REPO_ROOT = Path(__file__).resolve().parents[2]
ALBUMS_JSON = _REPO_ROOT / "albums.json"
ALBUMS_FILES_JSON = _REPO_ROOT / "albums-files.json"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


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


def site_url_for(blob_path: str) -> str | None:
    """Return a serg.vlassiev.info /share URL, or None if unsupported."""
    parts = blob_path.split("/")
    if len(parts) < 2:
        return None

    # Hiking blob: <imageId>/<variantId>.jpg — folder UUID is the imageId.
    if _UUID_RE.match(parts[0]):
        image_id = parts[0]
        return f"{SITE_BASE}/share/hiking/image/{image_id}"

    resolved = _resolve_folder(blob_path)
    if resolved is None:
        return None
    folder, filename = resolved
    # Path segment for /share/{folder}/{n}. Encode chars that would break the
    # path (slashes, spaces) but keep it readable for typical alphanumeric folders.
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
    n = int(rest)
    return f"{SITE_BASE}/share/{folder_url}/{n}"
