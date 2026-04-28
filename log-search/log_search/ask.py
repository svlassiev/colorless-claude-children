"""CLI: log-search "<query>" → grounded answer + citations."""

from __future__ import annotations

import argparse
import sys

import numpy as np
import vertexai
from vertexai.generative_models import GenerativeModel
from vertexai.language_models import TextEmbeddingModel

from log_search.paths import EMBED_MODEL, GENERATE_MODEL, LOCATION, PROJECT
from log_search.retriever import load_index, parse_date_filter, search, Hit

PROMPT_TEMPLATE = """\
You are answering a question about Sergey's working journal.

Use ONLY the journal excerpts below. Cite each fact as [n] where n is the excerpt
number. If the excerpts do not contain the answer, say so plainly — do not make
up details. Do not invent dates, names, or numbers that aren't in the excerpts.

QUESTION:
{query}

EXCERPTS:
{excerpts}

ANSWER (1-3 short paragraphs, with [n] citations):
"""


def _format_excerpts(hits: list[Hit]) -> str:
    parts = []
    for h in hits:
        meta_line = f"[{h.rank}] {h.file} :: {h.heading_path}"
        if h.date_iso:
            meta_line += f" :: {h.date_iso}"
        parts.append(f"{meta_line}\n{h.text}")
    return "\n\n---\n\n".join(parts)


def _print_citations(hits: list[Hit]) -> None:
    print("\n--- citations ---", file=sys.stderr)
    for h in hits:
        date = h.date_iso or "-"
        print(f"  [{h.rank}] score={h.score:.3f}  {date}  {h.file} :: {h.heading_path}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(prog="log-search")
    parser.add_argument("query", nargs="+", help="natural-language query")
    parser.add_argument("-k", type=int, default=5, help="top-k chunks to retrieve")
    parser.add_argument("--retrieve-only", action="store_true", help="skip Gemini generation")
    args = parser.parse_args()

    query = " ".join(args.query)

    vertexai.init(project=PROJECT, location=LOCATION)
    embed_model = TextEmbeddingModel.from_pretrained(EMBED_MODEL)
    q_emb = np.array(embed_model.get_embeddings([query])[0].values, dtype=np.float32)

    vectors, metas, texts = load_index()
    date_lo, date_hi = parse_date_filter(query)
    if date_lo:
        print(f"date filter: {date_lo} .. {date_hi}", file=sys.stderr)

    hits = search(q_emb, vectors, metas, texts, k=args.k, date_lo=date_lo, date_hi=date_hi)

    if not hits:
        print("no matching excerpts found.", file=sys.stderr)
        return 1

    if args.retrieve_only:
        _print_citations(hits)
        for h in hits:
            print(f"\n[{h.rank}] {h.file} :: {h.heading_path} :: {h.date_iso or '-'} (score {h.score:.3f})")
            print(h.text[:400] + ("..." if len(h.text) > 400 else ""))
        return 0

    excerpts = _format_excerpts(hits)
    prompt = PROMPT_TEMPLATE.format(query=query, excerpts=excerpts)

    gen = GenerativeModel(GENERATE_MODEL)
    resp = gen.generate_content(prompt)
    print(resp.text.strip())
    _print_citations(hits)

    if hasattr(resp, "usage_metadata") and resp.usage_metadata:
        u = resp.usage_metadata
        in_tok = getattr(u, "prompt_token_count", 0)
        out_tok = getattr(u, "candidates_token_count", 0)
        # Gemini 2.5 Pro: $1.25/1M input (≤200k context), $10/1M output
        cost = in_tok * 1.25 / 1_000_000 + out_tok * 10 / 1_000_000
        print(f"\n[tokens: in={in_tok} out={out_tok}  cost ~${cost:.4f}]", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
