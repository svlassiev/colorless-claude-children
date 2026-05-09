"""Walk the corpus, split into chunks, write chunks.jsonl.

Strategy:
- Split on **every heading level**: `#`, `##`, `###`, `####`, `#####`.
  Maintain a stack of currently-open headings. A chunk's heading_path is
  the breadcrumb of all open headings (e.g. `# 20260326 :: ## new job ::
  ### LinkedIn outreach`).
- The breadcrumb is also prepended into the chunk's `text` so retrieval
  can match parent terms — fine-grained chunks still respond to broad
  queries.
- Inline image references (`![](…)` and GitHub-style `<img src="…"/>`)
  are augmented with the image's Pro-generated caption, looked up from
  IMAGE_CAPTION_CACHE. The caption is what makes screenshots reachable
  via prose queries.
- Tiny chunks (< MIN_CHUNK_CHARS, after caption injection) are merged
  into the preceding emitted chunk. The deeper provenance is lost from
  heading_path, but only chunks with enough text actually hit the index —
  near-empty fragments don't dilute embedding quality.
- Date headers (`# YYYYMMDD`) propagate their `date_iso` to all child
  chunks until the next H1.
- Long chunks (> MAX_CHARS) get sliding-window sub-chunks with overlap.

This module reads the caption cache produced by `captioner.py`. If a
referenced image isn't in the cache the original reference stays, just
without an injected caption — the chunker never blocks on captioning.

Run:  uv run --directory log-search python -m log_search.chunker
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from log_search.paths import (
    CHUNKS_PATH,
    CORPUS_ROOT,
    IMAGE_CAPTION_CACHE,
    MIN_CHUNK_CHARS,
    ensure_cache_dir,
)

# Match any heading line: 1-5 hashes, a space, then heading text. Captured
# groups: (level-as-hashes, heading-text).
HEADING_RE = re.compile(r"^(#{1,5})\s+(.+?)\s*$", re.MULTILINE)
# H1 whose text is exactly an 8-digit date (e.g. `# 20260326`).
DATE_HEADING_RE = re.compile(r"^\d{8}$")

# Image reference patterns — must match the ones in captioner.py so chunk-
# time injection finds the same references the captioner saw.
_IMG_REF_RE = re.compile(
    r'(?P<full>'
    r'!\[[^\]]*\]\((?P<md_path>[^)]+)\)'
    r'|<img\b[^>]*?\bsrc=["\'](?P<html_path>[^"\']+)["\'][^>]*?/?>'
    r')',
    re.IGNORECASE,
)

MAX_CHARS = 6000  # ~1500 tokens at 4 chars/token
WINDOW_CHARS = 3200  # ~800 tokens
OVERLAP_CHARS = 400  # ~100 tokens


@dataclass
class Chunk:
    id: str
    file: str
    date_iso: str | None
    heading_path: str
    text: str
    char_count: int
    sha: str


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _date_iso(yyyymmdd: str) -> str | None:
    if not DATE_HEADING_RE.match(yyyymmdd):
        return None
    return f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def _sliding_window(text: str) -> list[str]:
    if len(text) <= MAX_CHARS:
        return [text]
    out = []
    start = 0
    while start < len(text):
        end = min(start + WINDOW_CHARS, len(text))
        out.append(text[start:end])
        if end == len(text):
            break
        start += WINDOW_CHARS - OVERLAP_CHARS
    return out


def _load_caption_cache() -> dict[str, str]:
    """Return path → caption. We resolve the .md-relative reference to
    disk in `_inject_captions` and look up by sha there, but having both
    the original `path` and `sha` in the cache file lets us also do a
    quick path-based lookup as a fallback.
    """
    if not IMAGE_CAPTION_CACHE.exists():
        return {}
    sha_to_caption: dict[str, str] = {}
    for line in IMAGE_CAPTION_CACHE.open():
        row = json.loads(line)
        sha_to_caption[row["sha"]] = row["caption"]
    return sha_to_caption


def _resolve_image_path(ref: str, md_file: Path) -> Path | None:
    """Same resolution rules as captioner.py — kept here so the two stages
    agree on which image a reference points to."""
    if ref.startswith(("http://", "https://", "data:")):
        return None
    p = Path(ref)
    if p.is_absolute() and p.exists():
        return p
    for c in (md_file.parent / p, CORPUS_ROOT / p):
        if c.exists() and c.is_file():
            return c.resolve()
    return None


def _inject_captions(text: str, md_file: Path, captions: dict[str, str]) -> str:
    """Return `text` with `*Image: <caption>*` appended after each image
    reference. Misses (image not on disk, or sha not in cache) silently
    leave the original reference untouched.
    """
    if not captions:
        return text

    def _replace(m: re.Match[str]) -> str:
        full = m.group("full")
        ref = m.group("md_path") or m.group("html_path")
        if not ref:
            return full
        disk = _resolve_image_path(ref, md_file)
        if disk is None:
            return full
        try:
            sha = hashlib.sha256(disk.read_bytes()).hexdigest()[:16]
        except OSError:
            return full
        caption = captions.get(sha)
        if not caption:
            return full
        return f"{full}\n\n*Image: {caption}*"

    return _IMG_REF_RE.sub(_replace, text)


@dataclass
class _RawChunk:
    """An intermediate chunk before sliding-window + size-merging passes."""

    date_iso: str | None
    heading_path: str
    body: str  # may include the heading line and trailing text up to next heading


def _walk_headings(text: str) -> list[_RawChunk]:
    """Split `text` into chunks bounded by heading lines of any level.

    Maintains a stack of (level, heading_text). A chunk's heading_path is
    the breadcrumb of all currently-open headings. Date H1s
    (`# YYYYMMDD`) set a propagating `date_iso`.
    """
    headings = list(HEADING_RE.finditer(text))

    # Anything before the first heading becomes a "(prelude)" chunk.
    chunks: list[_RawChunk] = []
    if not headings:
        body = text.strip()
        if body:
            chunks.append(_RawChunk(date_iso=None, heading_path="(whole file)", body=body))
        return chunks

    if headings[0].start() > 0:
        prelude = text[: headings[0].start()].strip()
        if prelude:
            chunks.append(_RawChunk(date_iso=None, heading_path="(prelude)", body=prelude))

    stack: list[tuple[int, str]] = []  # (level, heading_text)
    current_date_iso: str | None = None

    for i, m in enumerate(headings):
        level = len(m.group(1))
        heading_text = m.group(2).strip()

        # Pop any sibling/deeper levels off the stack before pushing this one.
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, heading_text))

        # Date propagation: an H1 of `# YYYYMMDD` sets the active date for
        # all subsequent (lower-level) chunks until another H1 appears.
        if level == 1:
            current_date_iso = _date_iso(heading_text)

        body_start = m.start()  # include the heading line in the chunk
        body_end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        body = text[body_start:body_end].rstrip()
        if not body:
            continue

        breadcrumb = " :: ".join(f"{'#' * lvl} {h}" for lvl, h in stack)
        chunks.append(
            _RawChunk(date_iso=current_date_iso, heading_path=breadcrumb, body=body)
        )
    return chunks


def _merge_tiny(chunks: list[_RawChunk]) -> list[_RawChunk]:
    """Merge chunks whose body is below MIN_CHUNK_CHARS into the preceding
    emitted chunk. Heading-path of the merged-into chunk is preserved (it's
    the broader scope); the tiny chunk's text is appended.

    A *very first* tiny chunk has no predecessor and is emitted as-is — we
    never silently drop content.
    """
    if not chunks:
        return chunks
    out: list[_RawChunk] = [chunks[0]]
    for c in chunks[1:]:
        if len(c.body) < MIN_CHUNK_CHARS and out:
            prev = out[-1]
            # Append the tiny chunk's body to the predecessor's body. The
            # predecessor keeps its own heading_path (and its own date_iso
            # — they're consistent because date propagates forward).
            out[-1] = _RawChunk(
                date_iso=prev.date_iso,
                heading_path=prev.heading_path,
                body=prev.body + "\n\n" + c.body,
            )
        else:
            out.append(c)
    return out


def chunk_file(path: Path, captions: dict[str, str]) -> list[Chunk]:
    text = path.read_text(encoding="utf-8", errors="replace")
    relative = path.relative_to(CORPUS_ROOT).as_posix()

    # Stage 1: structural split on every heading level.
    raw = _walk_headings(text)

    # Stage 2: caption injection (turns `<img …/>` into image+caption text).
    raw = [
        _RawChunk(
            date_iso=r.date_iso,
            heading_path=r.heading_path,
            body=_inject_captions(r.body, path, captions),
        )
        for r in raw
    ]

    # Stage 3: merge sub-MIN chunks. We do this *after* caption injection
    # so an image-only `####` subsection rises above MIN_CHUNK_CHARS and
    # stays as its own chunk.
    raw = _merge_tiny(raw)

    chunks: list[Chunk] = []
    for r in raw:
        # Prepend the heading breadcrumb to the embedded text so parent
        # terms participate in retrieval. Date is duplicated only when
        # not already implied by the breadcrumb's H1.
        prefix = f"[{r.heading_path}]\n\n" if r.heading_path != "(whole file)" else ""
        full_text = (prefix + r.body).strip()

        for piece in _sliding_window(full_text):
            sha = _sha(f"{relative}|{r.heading_path}|{piece}")
            chunks.append(
                Chunk(
                    id=sha,
                    file=relative,
                    date_iso=r.date_iso,
                    heading_path=r.heading_path,
                    text=piece,
                    char_count=len(piece),
                    sha=sha,
                )
            )
    return chunks


# Filename → date_iso. Recognises a few common screenshot/photo patterns.
# Returns None on no match — the orphan chunk then has no date filter.
_FILENAME_DATE_PATTERNS = [
    re.compile(r"(\d{4})(\d{2})(\d{2})"),                  # 20260326
    re.compile(r"(\d{4})-(\d{2})-(\d{2})"),                # 2026-03-27
]


def _date_from_filename(name: str) -> str | None:
    for pat in _FILENAME_DATE_PATTERNS:
        m = pat.search(name)
        if not m:
            continue
        y, mo, d = m.group(1), m.group(2), m.group(3)
        # Plausibility guard — guards against random 8-digit numbers in
        # filenames being misread as dates.
        if 1990 <= int(y) <= 2100 and 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            return f"{y}-{mo}-{d}"
    return None


def _orphan_chunks(captions: dict[str, str]) -> list[Chunk]:
    """Synthesize one chunk per orphan image — images present in the corpus
    but not inline-referenced from any .md. The image's caption is the
    chunk text; the chunk's file/heading_path place it under its containing
    folder so retrieval surfaces it in folder-scoped queries.

    Reads orphan list from the captioner module to avoid duplicating the
    image-walk logic.
    """
    from log_search.captioner import collect_orphans, collect_refs

    refs = collect_refs(CORPUS_ROOT)
    referenced = {r.sha for r in refs}
    orphans = collect_orphans(CORPUS_ROOT, referenced)
    if not orphans:
        return []

    chunks: list[Chunk] = []
    skipped = 0
    for orph in orphans:
        caption = captions.get(orph.sha)
        if not caption:
            # Orphan exists on disk but hasn't been captioned yet — skip
            # rather than emit an empty chunk. Re-running captioner.py
            # picks them up.
            skipped += 1
            continue
        rel_path = orph.path  # corpus-relative
        parent = Path(rel_path).parent.as_posix() or "."
        filename = Path(rel_path).name
        date_iso = _date_from_filename(filename)
        heading_path = f"(image) {filename}"
        text = f"[{heading_path}]\n\n*Image: {caption}*"
        sha = _sha(f"{rel_path}|{heading_path}|{caption}")
        chunks.append(
            Chunk(
                id=sha,
                file=parent,
                date_iso=date_iso,
                heading_path=heading_path,
                text=text,
                char_count=len(text),
                sha=sha,
            )
        )
    if skipped:
        print(
            f"  warn: {skipped} orphan image(s) on disk but not yet captioned — "
            "re-run captioner to include them",
            file=sys.stderr,
        )
    return chunks


def main() -> int:
    if not CORPUS_ROOT.exists():
        print(f"corpus not found: {CORPUS_ROOT}", file=sys.stderr)
        return 1

    captions = _load_caption_cache()
    if captions:
        print(f"using {len(captions)} cached image caption(s)")
    else:
        print(
            "no image captions cached — run `python -m log_search.captioner` first "
            "for image-bearing chunks to be captioned. continuing without captions.",
            file=sys.stderr,
        )

    ensure_cache_dir()
    md_files = sorted(CORPUS_ROOT.rglob("*.md"))
    print(f"found {len(md_files)} markdown files under {CORPUS_ROOT}")

    total_chunks: list[Chunk] = []
    for path in md_files:
        chunks = chunk_file(path, captions)
        total_chunks.extend(chunks)
        rel = path.relative_to(CORPUS_ROOT).as_posix()
        print(f"  {rel}: {len(chunks)} chunks")

    # Stage 4: orphan-image chunks (one per image not referenced from any .md).
    orphan_chunks = _orphan_chunks(captions)
    if orphan_chunks:
        print(f"  + {len(orphan_chunks)} orphan-image chunks")
        total_chunks.extend(orphan_chunks)

    with CHUNKS_PATH.open("w") as f:
        for c in total_chunks:
            f.write(json.dumps(asdict(c)) + "\n")

    char_total = sum(c.char_count for c in total_chunks)
    print(
        f"\nwrote {len(total_chunks)} chunks ({char_total:,} chars, "
        f"~{char_total // 4:,} tokens) to {CHUNKS_PATH}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
