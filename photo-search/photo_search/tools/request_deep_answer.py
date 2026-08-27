"""Answer-mode tool: the router calls this for analytical queries.

Unlike the filter tools this one constrains nothing at retrieval time — it
flips `Filters.deep`, which the server translates into the deep generation
regime (bigger thinking budget, analysis prompt, untrimmed evidence set).
The declaration itself lives in search_common.deep_answer so the log corpus
can run the same classification without importing photo tools.
"""

from __future__ import annotations

from pydantic import BaseModel

from search_common.deep_answer import DECLARATION as DECLARATION  # re-export


class Args(BaseModel):
    reason: str | None = None


def execute(args: Args, metas: list[dict]) -> bool:
    """No retrieval-side work — the marker is the result."""
    return True
