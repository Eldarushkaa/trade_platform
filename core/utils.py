"""
Shared utility helpers for the trade platform core.
"""
import math


def safe_float(v) -> float | str | None:
    """Replace inf/nan with JSON-safe values.

    Returns None for NaN, the string "Infinity"/"-Infinity" for ±inf,
    and the original value otherwise.  Use safe_round() when the result
    will also be passed to round(), since round() rejects str arguments.
    """
    if v is None:
        return None
    if isinstance(v, float):
        if math.isnan(v):
            return None
        if math.isinf(v):
            return "Infinity" if v > 0 else "-Infinity"
    return v


def safe_round(v, ndigits: int = 2) -> float | str | None:
    """Sanitize *v* and round it in one step.

    Equivalent to round(safe_float(v), ndigits) but safe for inf/nan values
    that safe_float() converts to strings (which round() cannot handle).
    Always use this instead of round(safe_float(v), n) at call sites.
    """
    sanitized = safe_float(v)
    if sanitized is None or isinstance(sanitized, str):
        return sanitized          # None or "Infinity"/"-Infinity" — not roundable
    return round(sanitized, ndigits)
