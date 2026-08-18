"""Export the corpus repo's committed state (HEAD) for indexing.

Standing policy: the log index is built from git-committed files only —
untracked drafts, scratch notes, and working files never enter the index.
This helper makes that a one-liner instead of a hand-rolled ritual:

    uv run python -m log_search.committed_corpus
    LOG_CORPUS_ROOT=~/.cache/log-search/corpus_committed \\
        uv run python -m log_search.captioner   # (if new committed images)
    LOG_CORPUS_ROOT=~/.cache/log-search/corpus_committed \\
        uv run python -m log_search.chunker
    uv run python -m log_search.embedder        # reads the cache, no corpus

The export lives under the existing cache root (transient, regenerated on
every run) and is wiped and rebuilt each time so deletions in the repo are
honored too.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from log_search.paths import CACHE_ROOT

DEFAULT_REPO = Path.home() / "projects" / "log"
EXPORT_DIR = CACHE_ROOT / "corpus_committed"


def export_committed(repo: Path, out: Path) -> int:
    """`git archive HEAD` → `out`, replacing any previous export.

    Returns the number of exported files.
    """
    if not (repo / ".git").exists():
        raise SystemExit(f"not a git repo: {repo}")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "HEAD"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["tar", "-x", "-C", str(out)], input=archive.stdout, check=True
    )
    return sum(1 for p in out.rglob("*") if p.is_file())


def main() -> int:
    ap = argparse.ArgumentParser(prog="log-search-committed-corpus")
    ap.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    ap.add_argument("--out", type=Path, default=EXPORT_DIR)
    args = ap.parse_args()

    head = subprocess.run(
        ["git", "-C", str(args.repo), "rev-parse", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    n = export_committed(args.repo, args.out)
    print(f"exported {n} files @ {head} -> {args.out}", file=sys.stderr)
    print(f"next: LOG_CORPUS_ROOT={args.out} python -m log_search.chunker", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
