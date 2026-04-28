"""CLI: log-search "<query>" → grounded answer + citations."""

from __future__ import annotations

import argparse
import sys

from google import genai

from log_search.paths import LOCATION, MAX_K, PROJECT
from log_search.qa import generate, retrieve
from log_search.retriever import Hit, load_index


def _print_citations(hits: list[Hit]) -> None:
    print("\n--- citations ---", file=sys.stderr)
    for h in hits:
        date = h.date_iso or "-"
        print(f"  [{h.rank}] score={h.score:.3f}  {date}  {h.file} :: {h.heading_path}", file=sys.stderr)


def _clamp_k(s: str) -> int:
    """argparse type-callable: clamp -k to [1, MAX_K] silently."""
    return min(max(int(s), 1), MAX_K)


def main() -> int:
    parser = argparse.ArgumentParser(prog="log-search")
    parser.add_argument("query", nargs="+", help="natural-language query")
    parser.add_argument("-k", type=_clamp_k, default=5, help=f"top-k results to retrieve (clamped to 1..{MAX_K})")
    parser.add_argument("--retrieve-only", action="store_true", help="skip Gemini generation")
    args = parser.parse_args()

    query = " ".join(args.query)

    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)

    vectors, metas, texts = load_index()
    hits, (date_lo, _) = retrieve(query, client, vectors, metas, texts, k=args.k)
    if date_lo:
        print(f"date filter: {date_lo} ..", file=sys.stderr)

    if not hits:
        print("no matching excerpts found.", file=sys.stderr)
        return 1

    if args.retrieve_only:
        _print_citations(hits)
        for h in hits:
            print(f"\n[{h.rank}] {h.file} :: {h.heading_path} :: {h.date_iso or '-'} (score {h.score:.3f})")
            print(h.text[:400] + ("..." if len(h.text) > 400 else ""))
        return 0

    answer, usage = generate(query, hits, client)
    print(answer)
    _print_citations(hits)

    if usage["cost"] is not None:
        print(
            f"\n[tokens: in={usage['tokens_in']} out={usage['tokens_out']}  cost ~${usage['cost']:.4f}]",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
