"""
strategies — trading strategy implementations.

Each module defines a subclass of BaseStrategy and exposes a
``for_symbol(symbol)`` classmethod to create per-coin subclasses.

Available strategies:
    RSIBot            — Wilder RSI with trend filter
    MACrossoverBot    — MACD crossover + histogram
    BollingerBot      — Bollinger Band mean reversion
    OrderbookWallBot  — Orderbook gap-and-wall (rush-to-wall thesis)

To add a new strategy:
    1. Create strategies/my_strategy.py subclassing BaseStrategy.
    2. Import it in main.py and add to STRATEGY_CLASSES.
"""

from strategies.example_rsi_bot import RSIBot
from strategies.example_ma_crossover import MACrossoverBot
from strategies.bollinger_bot import BollingerBot
from strategies.orderbook_wall_bot import OrderbookWallBot

__all__ = [
    "RSIBot",
    "MACrossoverBot",
    "BollingerBot",
    "OrderbookWallBot",
]
