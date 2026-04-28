"""CLI: photo-search "<query>" → grounded answer + cited photos."""

from __future__ import annotations

import argparse
import sys

import vertexai
from google import genai
from google.cloud import storage
from vertexai.vision_models import MultiModalEmbeddingModel

from photo_search.paths import EMBED_MODEL, LOCATION, MAX_K, PROJECT
from photo_search.qa import generate, retrieve
from photo_search.retriever import Hit, load_index
from photo_search.site import site_url_for


def _print_citations(hits: list[Hit]) -> None:
    print("\n--- citations ---", file=sys.stderr)
    for h in hits:
        date = h.date_iso or "-"
        print(f"  [{h.rank}] score={h.score:.3f}  {date}", file=sys.stderr)
        site = site_url_for(h.blob_path)
        if site:
            print(f"      site:    {site}", file=sys.stderr)
        print(f"      gcs:     {h.gcs_uri}", file=sys.stderr)
        if h.caption:
            cap = h.caption[:140].replace("\n", " ")
            print(f"      caption: {cap}", file=sys.stderr)


def _clamp_k(s: str) -> int:
    """argparse type-callable: clamp -k to [1, MAX_K] silently."""
    return min(max(int(s), 1), MAX_K)


def main() -> int:
    parser = argparse.ArgumentParser(prog="photo-search")
    parser.add_argument("query", nargs="+", help="natural-language query")
    parser.add_argument("-k", type=_clamp_k, default=5, help=f"top-k results to retrieve (clamped to 1..{MAX_K})")
    parser.add_argument(
        "--retrieve-only",
        action="store_true",
        help="skip Gemini generation; print citations only (~$0.0002 per query)",
    )
    args = parser.parse_args()

    query = " ".join(args.query)

    vertexai.init(project=PROJECT, location=LOCATION)
    embed_model = MultiModalEmbeddingModel.from_pretrained(EMBED_MODEL)

    vectors, metas = load_index()
    print(f"index: {len(vectors)} vectors", file=sys.stderr)

    hits, (date_lo, date_hi) = retrieve(query, embed_model, vectors, metas, k=args.k)
    if date_lo:
        print(f"date filter: {date_lo} .. {date_hi}", file=sys.stderr)

    if not hits:
        print("no matching photos found.", file=sys.stderr)
        return 1

    if args.retrieve_only:
        _print_citations(hits)
        return 0

    gen_client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    storage_client = storage.Client(project=PROJECT)
    answer, usage = generate(query, hits, gen_client, storage_client)
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
