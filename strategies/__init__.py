"""
strategies — trading strategy implementations.

Each module defines a subclass of BaseStrategy and exposes a
``for_symbol(symbol)`` classmethod to create per-coin subclasses.

Active strategies (15-minute candles):
    RSIBot         — Wilder RSI crossover with EMA200 proximity, volatility,
                     and EMA slope filters
    DonchianBot    — Donchian breakout (Turtle Trading), trend-following
    DonchianNewBot — Donchian breakout v2 with scoring-based filters

To add a new strategy:
    1. Create strategies/my_strategy.py subclassing BaseStrategy.
    2. Import it in main.py and add to STRATEGY_CLASSES.
"""

from strategies.rsi import RSIBot
from strategies.donchian import DonchianBot
from strategies.donchian_new import DonchianNewBot

__all__ = [
    "RSIBot",
    "DonchianBot",
    "DonchianNewBot",
]
