"""Per-model generation pricing for the cost footer / usage accounting.

Rates are USD per 1M tokens (standard tier), taken from the Cloud Billing
catalog (service "Gemini API") on 2026-08-14. Output rate applies to
thinking + visible tokens combined — bill on the sum.

Unknown model → cost is None (shown as "n/a"), never a wrong number from a
stale hardcoded rate. Keep this table in sync when swapping EXPLORE_*_MODEL.

Not covered here: gemini-2.5-pro charges input at $2.50/1M beyond 200k
context — our requests never come close, so the base rate is used.
"""

from __future__ import annotations

PRICES_PER_1M: dict[str, tuple[float, float]] = {
    # model: ($ input / 1M tokens, $ output / 1M tokens)
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.6-flash": (0.75, 3.75),
}


def generation_cost(
    model: str, tokens_in: int, tokens_out_billable: int
) -> float | None:
    """Dollar cost of one generate_content call, or None for unknown models.

    `tokens_out_billable` must already include thinking tokens.
    """
    rates = PRICES_PER_1M.get(model)
    if rates is None:
        return None
    p_in, p_out = rates
    return tokens_in * p_in / 1e6 + tokens_out_billable * p_out / 1e6
