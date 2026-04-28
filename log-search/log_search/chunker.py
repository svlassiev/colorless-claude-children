"""Walk the corpus, split into chunks, write chunks.jsonl.

Strategy:
- Date-headed files (matches `# YYYYMMDD`): one chunk per date entry.
- Otherwise: split on level-2 headings (`## `).
- Long chunks (> MAX_CHARS): sliding-window sub-chunks with overlap.
- Each chunk row: {id, file, date_iso, heading_path, text, char_count, sha}.

No API calls — runs entirely locally on the corpus directory.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from log_search.paths import CHUNKS_PATH, CORPUS_ROOT, ensure_cache_dir

DATE_HEADER_RE = re.compile(r"^#\s+(\d{8})\s*$", re.MULTILINE)
SECTION_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
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
    if len(yyyymmdd) != 8:
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


def _split_date_headed(text: str) -> list[tuple[str | None, str, str]]:
    """Return [(date_iso, heading, body)] split on `# YYYYMMDD` headers.

    The portion before the first date header (if any) becomes a single
    untagged section so we don't lose it.
    """
    matches = list(DATE_HEADER_RE.finditer(text))
    if not matches:
        return []

    sections = []
    if matches[0].start() > 0:
        prelude = text[: matches[0].start()].strip()
        if prelude:
            sections.append((None, "(prelude)", prelude))

    for i, m in enumerate(matches):
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        if body:
            sections.append((_date_iso(m.group(1)), f"# {m.group(1)}", body))
    return sections


def _split_section_headed(text: str) -> list[tuple[None, str, str]]:
    """Split on `## ` headings. Falls back to whole-file if no headings."""
    matches = list(SECTION_HEADER_RE.finditer(text))
    if not matches:
        body = text.strip()
        return [(None, "(whole file)", body)] if body else []

    sections = []
    if matches[0].start() > 0:
        prelude = text[: matches[0].start()].strip()
        if prelude:
            sections.append((None, "(prelude)", prelude))

    for i, m in enumerate(matches):
        body_start = m.start()  # include the heading itself
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        if body:
            sections.append((None, f"## {m.group(1)}", body))
    return sections


def chunk_file(path: Path) -> list[Chunk]:
    text = path.read_text(encoding="utf-8", errors="replace")
    relative = path.relative_to(CORPUS_ROOT).as_posix()

    sections: list[tuple[str | None, str, str]]
    if DATE_HEADER_RE.search(text):
        sections = _split_date_headed(text)
    else:
        sections = _split_section_headed(text)

    chunks: list[Chunk] = []
    for date_iso, heading, body in sections:
        for piece in _sliding_window(body):
            sha = _sha(f"{relative}|{heading}|{piece}")
            chunks.append(
                Chunk(
                    id=sha,
                    file=relative,
                    date_iso=date_iso,
                    heading_path=heading,
                    text=piece,
                    char_count=len(piece),
                    sha=sha,
                )
            )
    return chunks


def main() -> int:
    if not CORPUS_ROOT.exists():
        print(f"corpus not found: {CORPUS_ROOT}", file=sys.stderr)
        return 1

    ensure_cache_dir()
    md_files = sorted(CORPUS_ROOT.rglob("*.md"))
    print(f"found {len(md_files)} markdown files under {CORPUS_ROOT}")

    total_chunks: list[Chunk] = []
    for path in md_files:
        chunks = chunk_file(path)
        total_chunks.extend(chunks)
        rel = path.relative_to(CORPUS_ROOT).as_posix()
        print(f"  {rel}: {len(chunks)} chunks")

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
