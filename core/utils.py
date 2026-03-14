"""
Shared utility helpers for the trade platform core.
"""
import math


def safe_float(v: float) -> float | str | None:
    """Replace inf/nan with JSON-safe values.

    Used when serialising metric dicts that may contain infinite Sharpe ratios
    or NaN drawdowns from edge-case backtests.
    """
    if v is None:
        return None
    if isinstance(v, float):
        if math.isnan(v):
            return None
        if math.isinf(v):
            return "Infinity" if v > 0 else "-Infinity"
    return v
