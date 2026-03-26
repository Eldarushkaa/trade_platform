"""
strategies — trading strategy implementations.

Each module defines a subclass of BaseStrategy and exposes a
``for_symbol(symbol)`` classmethod to create per-coin subclasses.

Active strategies:
    DonchianBot       — Donchian breakout (Turtle Trading), trend-following (15m)
    DonchianStableBot — Donchian breakout with fixed optimal params (15m)
    ShortMomentumBot  — Short momentum breakout: pробой вниз + объём + ATR (5m, SHORT only)

To add a new strategy:
    1. Create strategies/my_strategy.py subclassing BaseStrategy.
    2. Import it in main.py and add to STRATEGY_CLASSES.
"""

# from strategies.rsi import RSIBot
from strategies.donchian import DonchianBot
from strategies.donchian_stable import DonchianStableBot
from strategies.short_momentum import ShortMomentumBot

__all__ = [
    # "RSIBot",
    "DonchianBot",
    "DonchianStableBot",
    "ShortMomentumBot",
]
