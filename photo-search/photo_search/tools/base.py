"""Shared types for photo_search tools — the dataclasses each tool's
executor returns, and the `Filters` bag the routing layer composes.

Why these are dataclasses, not Pydantic: the *arg* models (what Gemini
sees + what we validate model output against) are Pydantic, in each
tool's own file. The *output* of an executor is internal plumbing —
plain frozen dataclasses are enough, faster to construct, and play
nicely with `dataclasses.replace`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DateFilter:
    """Date-range filter applied to a photo's `exif_date_iso` (or
    folder-name fallback). Either bound may be None to express an
    open-ended range — 'before 2020-03-01' is (None, '2020-03-01').
    """

    start_iso: str | None
    end_iso: str | None

    @property
    def is_open(self) -> bool:
        return self.start_iso is None and self.end_iso is None


@dataclass(frozen=True)
class LocationFilter:
    """Set of photo shas judged to match a named place.

    `place_name` is what the user/model wrote — kept for display in the
    SSE citations event ('filtered to: Хибины'). `matched_shas` is the
    actual masking material — the retriever AND-masks the candidate
    pool against it.
    """

    place_name: str
    matched_shas: frozenset[str]


@dataclass(frozen=True)
class Filters:
    """All filters resolved by the router for one query. Either slot may
    be None — the retriever treats None as 'no constraint on this axis.'"""

    date: DateFilter | None = None
    location: LocationFilter | None = None

    @property
    def is_empty(self) -> bool:
        return self.date is None and self.location is None
