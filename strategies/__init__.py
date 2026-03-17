"""
strategies — trading strategy implementations.

Each module defines a subclass of BaseStrategy and exposes a
``for_symbol(symbol)`` classmethod to create per-coin subclasses.

Active strategies (5-minute candles):
    RSIBot            — Wilder RSI with trend filter

To add a new strategy:
    1. Create strategies/my_strategy.py subclassing BaseStrategy.
    2. Import it in main.py and add to STRATEGY_CLASSES.
"""

from strategies.example_rsi_bot import RSIBot

__all__ = [
    "RSIBot",
]
