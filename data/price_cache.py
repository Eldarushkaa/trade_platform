"""
PriceCache — in-memory store for the latest price per symbol.

Updated by BinanceFeed on every WebSocket tick.
Read by SimulationEngine and strategies via get_price().
"""
import asyncio
import logging
from typing import Optional, Callable, Awaitable

logger = logging.getLogger(__name__)

# Subscriber type: an async function that receives (symbol, price)
PriceSubscriber = Callable[[str, float], Awaitable[None]]


class PriceCache:
    """
    Thread-safe (asyncio-safe) in-memory cache for latest prices.

    Also maintains a list of subscribers that get called on every update,
    allowing BotManager.dispatch_price() to be wired in automatically.
    """

    def __init__(self) -> None:
        self._prices: dict[str, float] = {}
        self._subscribers: list[PriceSubscriber] = []

    def update(self, symbol: str, price: float) -> None:
        """
        Update the cached price for a symbol.
        Schedules subscriber notifications as a fire-and-forget coroutine.
        """
        self._prices[symbol] = price
        # Schedule async notifications without blocking the WebSocket receive loop.
        # get_running_loop() is correct here: update() is always called from within
        # an active asyncio event loop (via BinanceFeed WebSocket handler).
        # get_event_loop() is deprecated since Python 3.10 and raises RuntimeError
        # in Python 3.12+ when no current event loop is set on the thread.
        asyncio.get_running_loop().create_task(self._notify(symbol, price))

    def get(self, symbol: str) -> Optional[float]:
        """Return the latest cached price for a symbol, or None if not yet received."""
        return self._prices.get(symbol)

    def get_all(self) -> dict[str, float]:
        """Return a snapshot of all cached prices."""
        return dict(self._prices)

    def subscribe(self, callback: PriceSubscriber) -> None:
        """
        Register an async callback to be called on every price update.

        Args:
            callback: async def callback(symbol: str, price: float) -> None
        """
        self._subscribers.append(callback)

    def unsubscribe(self, callback: PriceSubscriber) -> None:
        """Remove a previously registered subscriber."""
        try:
            self._subscribers.remove(callback)
        except ValueError:
            pass

    async def _notify(self, symbol: str, price: float) -> None:
        """Call all subscribers with the new price."""
        for cb in self._subscribers:
            try:
                await cb(symbol, price)
            except Exception as exc:
                logger.error(f"PriceCache subscriber error: {exc}", exc_info=True)


# Singleton used across the app
price_cache = PriceCache()
