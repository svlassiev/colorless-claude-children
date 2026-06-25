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
class ProximityFilter:
    """Set of photo shas within `radius_km` of a named place's pins.

    Distinct from LocationFilter: location matches by *label* (this photo is
    tagged Хибины), proximity matches by *distance* (this photo's coordinate
    is within N km of any Хибины pin). A photo with no coordinate can never
    satisfy proximity, even if its label matches.

    `place_name` + `radius_km` are kept for the SSE display ('within 5 km of
    Купчино'); `matched_shas` is the masking material.
    """

    place_name: str
    radius_km: float
    matched_shas: frozenset[str]


@dataclass(frozen=True)
class PersonFilter:
    """Set of photo shas containing any of the resolved identities.

    A query name may resolve to SEVERAL identities (a shared first name, or a
    shared family surname, can match more than one person), so `names` is the
    resolved identity set and `matched_shas` is the UNION of their photos.
    `query` is what the user/model wrote — kept for the SSE display label.

    `groups` keeps the per-search-term breakdown when several names were ANDed
    ("Anna and Ivanova" -> (("Anna", (..the Annas..)), ("Ivanova", (..the family..)))).
    This lets generation map each tagged person back to the term they matched —
    without it the model can't tell that a person tagged with a given name is the
    surname the user searched for. Empty for the single-term case (then
    `query`/`names` already say it).
    """

    query: str
    names: tuple[str, ...]
    matched_shas: frozenset[str]
    groups: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclass(frozen=True)
class Filters:
    """All filters resolved by the router for one query. Any slot may be
    None — the retriever treats None as 'no constraint on this axis.'"""

    date: DateFilter | None = None
    location: LocationFilter | None = None
    proximity: ProximityFilter | None = None
    person: PersonFilter | None = None

    @property
    def is_empty(self) -> bool:
        return (
            self.date is None
            and self.location is None
            and self.proximity is None
            and self.person is None
        )
