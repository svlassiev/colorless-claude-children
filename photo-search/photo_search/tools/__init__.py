"""Registry of photo-search tools exposed to the Flash routing call.

Each entry is `(FunctionDeclaration, ArgsModel, executor)`:
  - `FunctionDeclaration` is what Gemini reads.
  - `ArgsModel` is the Pydantic class used to validate the args the model
    emitted before we hand them to the executor.
  - `executor` is the pure function that turns validated args + index
    metadata into a typed filter fragment (`LocationFilter`, `DateFilter`,
    …) consumed by the retriever.

Adding a new tool is "drop a file in this package, append one entry here."
The router (`routing.py`) reads `ALL_DECLARATIONS` for the Gemini call and
dispatches return values back through `TOOL_REGISTRY[name]`.
"""

from __future__ import annotations

from photo_search.tools import (
    filter_by_date_range,
    filter_by_location,
    filter_by_proximity,
)
from photo_search.tools.base import (
    DateFilter,
    Filters,
    LocationFilter,
    ProximityFilter,
)

TOOL_REGISTRY = {
    "filter_by_location": (
        filter_by_location.DECLARATION,
        filter_by_location.Args,
        filter_by_location.execute,
    ),
    "filter_by_date_range": (
        filter_by_date_range.DECLARATION,
        filter_by_date_range.Args,
        filter_by_date_range.execute,
    ),
    "filter_by_proximity": (
        filter_by_proximity.DECLARATION,
        filter_by_proximity.Args,
        filter_by_proximity.execute,
    ),
}

ALL_DECLARATIONS = [entry[0] for entry in TOOL_REGISTRY.values()]

__all__ = [
    "ALL_DECLARATIONS",
    "DateFilter",
    "Filters",
    "LocationFilter",
    "ProximityFilter",
    "TOOL_REGISTRY",
]
