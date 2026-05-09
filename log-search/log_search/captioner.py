"""Caption inline images referenced from the journal's .md files using
Gemini 2.5 Pro, then cache the results so the chunker can inline them.

Why a separate step (and Pro, not Flash):
- The dominant image syntax in the corpus is GitHub-pasted HTML
  `<img src="images/<uuid>.png" …/>` (~80 % of references), the rest are
  standard markdown `![alt](path)`. Both forms are extracted here.
- Captions act as the *load-bearing text* for image content in chunks —
  retrieval has to find an image through a prose query, so the caption
  must read like prose, not like a label. Pro gives us 1–2 paragraphs of
  specific, faithful description (Flash tends to be terser and more
  generic).
- Captions are deterministic-enough per image; we cache by image-bytes
  sha so re-runs on unchanged corpora cost nothing.

Cost: ~$0.008–0.012 per image (258 image tokens + ~30 prompt + up to
~1500 output tokens at $1.25/$10 per 1M). For ~95 images today: ~$1.

Run:  uv run --directory log-search python -m log_search.captioner
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from google import genai
from google.genai import types

from log_search.paths import (
    CAPTION_MODEL,
    CORPUS_ROOT,
    IMAGE_CAPTION_CACHE,
    LOCATION,
    PROJECT,
    ensure_cache_dir,
)

# Both syntaxes seen in the corpus (~80 % HTML, ~20 % markdown). The two
# alternations capture the path into a named group `path`.
#   markdown:  ![alt](path)
#   html:      <img ... src="path" ... />
_IMG_REF_RE = re.compile(
    r'!\[[^\]]*\]\((?P<md_path>[^)]+)\)'
    r'|<img\b[^>]*?\bsrc=["\'](?P<html_path>[^"\']+)["\'][^>]*?/?>',
    re.IGNORECASE,
)

CAPTION_PROMPT = """\
You are describing a single image from Sergey's working journal.

Write 1 to 2 paragraphs (concrete, specific, faithful to what's visible — \
no speculation about people's identities). Cover:
- What the image actually shows (layout, foreground/background, key UI elements).
- The application or page it's from when recognisable (Gmail, LinkedIn, GitHub, \
Slack, Jira, a chart, a code editor, a dashboard, a chat, a screenshot of an \
article, etc.).
- Any text, names, numbers, dates, error messages, code snippets, or labels \
that are visible — quote short strings verbatim where useful.
- Anything notable that would help recall this image later from a prose query.

If the image is ambiguous or low-resolution, say so plainly. Output the \
description as prose — no bullet lists, no markdown headings.
"""

# Generous output budget — Pro is a reasoning model, so leave headroom for
# thinking tokens. 2 paragraphs is ~400-500 tokens visible; the 1500 cap
# covers thinking + visible without truncating.
MAX_OUTPUT_TOKENS = 1500


@dataclass(frozen=True)
class ImageRef:
    """One image reference found in the corpus.

    `path` is what the .md file wrote (relative to the .md's directory or
    to the corpus root for absolute-style refs). `disk_path` is where we
    actually find the bytes. `sha` is the cache key — same image referenced
    from two .md files only gets captioned once.
    """

    path: str
    disk_path: Path
    sha: str


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def _resolve_path(ref: str, md_file: Path) -> Path | None:
    """Resolve a reference like `images/uuid.png` against the .md file's
    directory. Absolute paths and corpus-rooted paths are kept as-is.
    Returns None if the file doesn't exist on disk.
    """
    p = Path(ref)
    if p.is_absolute() and p.exists():
        return p
    candidates = [
        md_file.parent / p,                 # most common: relative to .md
        CORPUS_ROOT / p,                    # corpus-rooted reference
    ]
    for c in candidates:
        if c.exists() and c.is_file():
            return c.resolve()
    return None


def _extract_refs_in_file(md_file: Path) -> Iterable[ImageRef]:
    """Yield (path, disk_path, sha) for every image reference in `md_file`.

    Skips references whose target doesn't exist on disk (silently — the
    journal sometimes references screenshots that have been cleaned up).
    """
    text = md_file.read_text(encoding="utf-8", errors="replace")
    for m in _IMG_REF_RE.finditer(text):
        ref = m.group("md_path") or m.group("html_path")
        if not ref:
            continue
        # Skip remote URLs; we only caption local files.
        if ref.startswith(("http://", "https://", "data:")):
            continue
        disk = _resolve_path(ref, md_file)
        if disk is None:
            print(f"  warn: missing image referenced from {md_file.name}: {ref}", file=sys.stderr)
            continue
        sha = _sha(disk.read_bytes())
        yield ImageRef(path=ref, disk_path=disk, sha=sha)


def collect_refs(corpus_root: Path = CORPUS_ROOT) -> list[ImageRef]:
    """Walk all .md files under `corpus_root`, return deduplicated image
    refs (one entry per unique sha — the same image referenced from
    multiple .md files captioned once)."""
    seen_sha: set[str] = set()
    refs: list[ImageRef] = []
    for md in sorted(corpus_root.rglob("*.md")):
        for ref in _extract_refs_in_file(md):
            if ref.sha in seen_sha:
                continue
            seen_sha.add(ref.sha)
            refs.append(ref)
    return refs


# Suffixes considered "image" when scanning for orphans. Lower-case match.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
# Folders inside the corpus we never scan for orphans (git internals, IDE
# state). Anything else is fair game.
_SKIP_DIRS = {".git", ".idea", ".vscode", "__pycache__"}


def collect_orphans(
    corpus_root: Path,
    referenced_shas: set[str],
) -> list[ImageRef]:
    """Find image files on disk that aren't referenced by any .md.

    These get captioned too and become synthetic chunks at index-build
    time so the index covers screenshots that haven't been inlined yet —
    e.g. the `new job/` Gmail/LinkedIn screenshots.

    Dedup is by sha: an image referenced from one .md and also sitting
    loose in another folder is captioned only once.
    """
    seen_sha: set[str] = set(referenced_shas)
    orphans: list[ImageRef] = []
    for path in sorted(corpus_root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in _IMAGE_EXTS:
            continue
        try:
            sha = _sha(path.read_bytes())
        except OSError:
            continue
        if sha in seen_sha:
            continue
        seen_sha.add(sha)
        # `path` for orphans is the corpus-relative path, used by the chunker
        # to bucket orphans under their containing folder.
        rel = path.relative_to(corpus_root).as_posix()
        orphans.append(ImageRef(path=rel, disk_path=path.resolve(), sha=sha))
    return orphans


def _load_cache() -> dict[str, str]:
    if not IMAGE_CAPTION_CACHE.exists():
        return {}
    cache: dict[str, str] = {}
    for line in IMAGE_CAPTION_CACHE.open():
        row = json.loads(line)
        cache[row["sha"]] = row["caption"]
    return cache


def _append_to_cache(sha: str, caption: str, path: str) -> None:
    """Append a single (sha, caption) entry. We keep `path` for human
    debugging when reading the cache file directly — the lookup key is sha."""
    ensure_cache_dir()
    with IMAGE_CAPTION_CACHE.open("a") as f:
        f.write(json.dumps({"sha": sha, "path": path, "caption": caption}) + "\n")


def _mime_for(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix in ("jpg", "jpeg"):
        return "image/jpeg"
    if suffix == "png":
        return "image/png"
    if suffix == "webp":
        return "image/webp"
    if suffix == "gif":
        return "image/gif"
    return "application/octet-stream"


def _caption_one(ref: ImageRef, client: genai.Client) -> tuple[ImageRef, str]:
    """Sync helper: caption a single image. Raises on transport / API failure
    so the caller can record per-image failures without aborting the batch."""
    img_bytes = ref.disk_path.read_bytes()
    resp = client.models.generate_content(
        model=CAPTION_MODEL,
        contents=[
            types.Part.from_bytes(data=img_bytes, mime_type=_mime_for(ref.disk_path)),
            CAPTION_PROMPT,
        ],
        config=types.GenerateContentConfig(max_output_tokens=MAX_OUTPUT_TOKENS),
    )
    return ref, (resp.text or "").strip()


def caption_all(
    refs: list[ImageRef], client: genai.Client, *, workers: int = 5
) -> dict[str, str]:
    """Caption every ref (skipping ones in the cache), append new entries
    to the cache as they complete, and return the full sha→caption map."""
    cache = _load_cache()
    todo = [r for r in refs if r.sha not in cache]
    if not todo:
        print(f"all {len(refs)} image(s) already captioned. nothing to do.")
        return cache

    print(f"captioning {len(todo)} image(s) ({len(refs) - len(todo)} cached)…")
    failures = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_caption_one, ref, client): ref for ref in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            ref = futures[fut]
            try:
                _, caption = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"  [{i:>3}/{len(todo)}] FAIL {ref.path}: {type(e).__name__}: {e}", file=sys.stderr)
                failures += 1
                continue
            cache[ref.sha] = caption
            _append_to_cache(ref.sha, caption, ref.path)
            print(f"  [{i:>3}/{len(todo)}] {ref.path}  ({len(caption)} chars)")
    if failures:
        print(f"WARNING: {failures} image(s) failed to caption — they'll be skipped at chunk-time", file=sys.stderr)
    return cache


def main() -> int:
    parser = argparse.ArgumentParser(prog="log-search-captioner")
    parser.add_argument(
        "--workers", type=int, default=5,
        help="concurrent caption requests (default 5; Pro tier has lower rate limits than Flash)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="enumerate references but don't call Gemini",
    )
    args = parser.parse_args()

    if not CORPUS_ROOT.exists():
        print(f"corpus not found: {CORPUS_ROOT}", file=sys.stderr)
        return 1

    refs = collect_refs()
    referenced_shas = {r.sha for r in refs}
    orphans = collect_orphans(CORPUS_ROOT, referenced_shas)
    all_refs = refs + orphans
    print(
        f"found {len(refs)} inline image ref(s) + {len(orphans)} orphan image(s) "
        f"= {len(all_refs)} unique to caption"
    )

    if args.dry_run:
        for label, group in (("inline", refs), ("orphan", orphans)):
            for r in group[:5]:
                print(f"  {label:6}  {r.sha}  {r.path}")
            if len(group) > 5:
                print(f"  {label:6}  … and {len(group) - 5} more")
        return 0

    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    caption_all(all_refs, client, workers=args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
