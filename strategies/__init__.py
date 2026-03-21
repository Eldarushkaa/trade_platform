"""
strategies — trading strategy implementations.

Each module defines a subclass of BaseStrategy and exposes a
``for_symbol(symbol)`` classmethod to create per-coin subclasses.

Active strategies (15-minute candles):
    DonchianBot       — Donchian breakout (Turtle Trading), trend-following
    DonchianStableBot — Donchian breakout with fixed optimal params (N=42, M=23, K=3.8437, VOL=1.1976)

To add a new strategy:
    1. Create strategies/my_strategy.py subclassing BaseStrategy.
    2. Import it in main.py and add to STRATEGY_CLASSES.
"""

# from strategies.rsi import RSIBot
from strategies.donchian import DonchianBot
from strategies.donchian_stable import DonchianStableBot

__all__ = [
    # "RSIBot",
    "DonchianBot",
    "DonchianStableBot",
]
