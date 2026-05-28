"""filter_by_date_range — narrow the photo search to a time window.

Same three-part shape as filter_by_location:

1. `DECLARATION` — what Gemini reads to decide when to call this tool and
   what arguments to emit. The model resolves the natural-language time
   expression ('March 2008', 'before the pandemic', 'the 2000s') into ISO
   date bounds itself — that's the whole point of routing dates through a
   tool instead of the regex fast-path, which only knows bare years and
   season+year.

2. `Args` — Pydantic validation of the emitted bounds. ISO `YYYY-MM-DD`,
   at least one bound present, calendar-valid, low ≤ high (inverted bounds
   are swapped, not rejected — be generous, the user clearly meant a range).

3. `execute` — turns validated args into a `DateFilter`. No per-photo work
   here: the retriever's `_date_mask` does the matching at search time,
   comparing each photo's `exif_date_iso` (folder-year fallback) against the
   bounds. So `execute` ignores `metas` and just constructs the filter.

Relationship to the regex fast-path (`retriever.parse_date_filter`): the
server calls routing first, then only runs the regex if routing produced
no date filter. So this tool takes precedence for the expressions it
catches; the regex remains a cheap deterministic backstop for the common
'summer 2017' / '2014' patterns.
"""

from __future__ import annotations

from datetime import date

from google.genai import types
from pydantic import BaseModel, Field, field_validator, model_validator

from photo_search.tools.base import DateFilter

_ISO_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


# What Gemini reads. Description + parameter descriptions are the model's
# only context for when/how to call this tool. Iterate here after watching
# real routing decisions.
DECLARATION = types.FunctionDeclaration(
    name="filter_by_date_range",
    description=(
        "Narrow the photo search to photos taken within a time period. Call "
        "this when the user's query references any datable time span: a year "
        "('photos from 2014'), a month ('March 2008'), a season ('summer 2017'), "
        "an explicit range ('between 2009 and 2011'), a decade ('the 2000s'), an "
        "open-ended bound ('before 2010', 'after 2015'), or a well-known event "
        "whose date you know ('before the pandemic' → before 2020-03). Resolve "
        "the expression to ISO calendar bounds yourself and expand partial "
        "expressions to full coverage (a year → Jan 1 .. Dec 31; a month → its "
        "first .. last day; a season → its months, winter spanning Dec .. Feb of "
        "the next year). Do NOT call this for queries with no time reference "
        "('photos of snow', 'at the dacha'). Do NOT call this for relative "
        "expressions that need today's date to resolve ('recently', 'last year', "
        "'these days') — the current date is not available to you, so guessing "
        "would be wrong."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "start_date": types.Schema(
                type=types.Type.STRING,
                description=(
                    "Inclusive lower bound as ISO YYYY-MM-DD. Omit for an "
                    "open-ended 'before X' query. For a year use Jan 1 "
                    "(2014 → '2014-01-01'); for a month use its first day "
                    "('March 2008' → '2008-03-01')."
                ),
            ),
            "end_date": types.Schema(
                type=types.Type.STRING,
                description=(
                    "Inclusive upper bound as ISO YYYY-MM-DD. Omit for an "
                    "open-ended 'after X' / 'since X' query. For a year use "
                    "Dec 31 (2014 → '2014-12-31'); for a month use its actual "
                    "last day ('March 2008' → '2008-03-31', 'Feb 2009' → "
                    "'2009-02-28')."
                ),
            ),
        },
        # Neither is individually required (open-ended ranges are valid),
        # but Args enforces that at least one is present.
    ),
)


class Args(BaseModel):
    """Validated arguments for filter_by_date_range.

    Catches: non-ISO strings, impossible calendar dates (2008-02-31),
    a call with neither bound (no-op), and inverted bounds (swapped).
    """

    start_date: str | None = Field(default=None, pattern=_ISO_PATTERN)
    end_date: str | None = Field(default=None, pattern=_ISO_PATTERN)

    @field_validator("start_date", "end_date")
    @classmethod
    def _calendar_valid(cls, v: str | None) -> str | None:
        """The regex guarantees shape; `date.fromisoformat` guarantees the
        date actually exists (rejects 2008-13-01, 2009-02-30, …)."""
        if v is None:
            return v
        date.fromisoformat(v)  # raises ValueError → ValidationError upstream
        return v

    @model_validator(mode="after")
    def _at_least_one_and_ordered(self) -> "Args":
        if self.start_date is None and self.end_date is None:
            raise ValueError("filter_by_date_range needs at least one bound")
        # Be generous: a model that emits start > end clearly meant the
        # range between the two dates — swap rather than drop the filter.
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            self.start_date, self.end_date = self.end_date, self.start_date
        return self


def execute(args: Args, metas: list[dict]) -> DateFilter | None:
    """Build a DateFilter from validated bounds.

    `metas` is unused — unlike location, date matching is computed per-photo
    by the retriever's `_date_mask` at search time, not precomputed into a
    sha set. The parameter is kept to satisfy the uniform executor contract
    (`executor(args, metas)`) the router dispatches through.

    Returns None only in the degenerate both-None case (Args already rejects
    it, so this is defensive); the router treats None as 'no date filter'.
    """
    if args.start_date is None and args.end_date is None:
        return None
    return DateFilter(start_iso=args.start_date, end_iso=args.end_date)