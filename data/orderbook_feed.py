"""
OrderbookFeed — fetches live order-book depth from Binance Futures REST API.

Two interfaces:

1. ``fetch_depth(symbol)`` — single on-demand REST call.
   Used by SimulationEngine as an ``ob_fetcher`` callable:
   every time a bot places an order, the engine fetches the current
   depth snapshot and walks the levels for a realistic VWAP fill price.

2. ``OrderbookFeed`` — optional background poller that keeps a warm
   in-memory cache (useful for the dashboard / ob_wall bot signals
   without hitting REST on every candle).

Binance Futures public depth endpoint — no API key required:
    GET https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=20
"""
import asyncio
import logging
from typing import Callable, Awaitable

import httpx

logger = logging.getLogger(__name__)

# Binance Futures public depth endpoint (no auth required)
_DEPTH_URL = "https://fapi.binance.com/fapi/v1/depth"

# How many levels per side to fetch.
# 20 is plenty for VWAP walk on typical bot order sizes (95% of USDT balance).
# Deeper = more accurate for very large orders, but slower REST response.
_DEPTH_LIMIT = 20


async def fetch_depth(symbol: str) -> dict | None:
    """
    Fetch the current order-book depth for *symbol* from Binance Futures.

    Returns a dict with pre-parsed bids/asks ready for ``engine.update_orderbook()``:
        {
            "bids": [(price, qty), ...],   # descending price (best bid first)
            "asks": [(price, qty), ...],   # ascending price  (best ask first)
        }

    Returns None on any network or parse error (caller falls back to fixed slippage).

    This function is designed to be passed as ``ob_fetcher`` to ``SimulationEngine``:
        engine = SimulationEngine(ob_fetcher=fetch_depth)
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                _DEPTH_URL,
                params={"symbol": symbol.upper(), "limit": _DEPTH_LIMIT},
            )
            resp.raise_for_status()
            data = resp.json()

        bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in data.get("asks", [])]

        if not bids or not asks:
            logger.warning(f"fetch_depth: empty bids/asks for {symbol}")
            return None

        logger.debug(
            f"fetch_depth {symbol}: best_bid={bids[0][0]} best_ask={asks[0][0]} "
            f"({len(bids)} bid levels, {len(asks)} ask levels)"
        )
        return {"bids": bids, "asks": asks}

    except httpx.TimeoutException:
        logger.warning(f"fetch_depth {symbol}: timeout — falling back to fixed slippage")
        return None
    except Exception as exc:
        logger.warning(f"fetch_depth {symbol}: {exc} — falling back to fixed slippage")
        return None


# ---------------------------------------------------------------------------
# Optional background poller (for dashboard / ob_wall signal context)
# ---------------------------------------------------------------------------

# Callback type: called whenever a fresh snapshot is loaded
OBCallback = Callable[[str, dict], Awaitable[None]]


class OrderbookFeed:
    """
    Optional background poller that periodically fetches OB depth for all
    configured symbols and calls registered async callbacks.

    NOT required for RSI/MA fill price simulation — those use on-demand
    ``fetch_depth()`` inside ``SimulationEngine.place_order()``.

    Useful for:
      - Keeping a warm cache for ``ob_wall`` signal generation
      - Feeding ``engine.update_orderbook()`` for dashboard coin-position views

    Usage:
        feed = OrderbookFeed(symbols=["BTCUSDT"], interval_seconds=30)
        feed.subscribe(engine.update_orderbook_async)
        asyncio.create_task(feed.start())
        ...
        await feed.stop()
    """

    def __init__(
        self,
        symbols: list[str],
        interval_seconds: int = 30,
    ) -> None:
        self.symbols = [s.upper() for s in symbols]
        self.interval_seconds = interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None
        self._callbacks: list[OBCallback] = []

    def subscribe(self, callback: OBCallback) -> None:
        """Register an async callback: async def cb(symbol, snapshot) -> None"""
        self._callbacks.append(callback)

    def unsubscribe(self, callback: OBCallback) -> None:
        try:
            self._callbacks.remove(callback)
        except ValueError:
            pass

    async def start(self) -> None:
        """Start the background polling loop."""
        self._running = True
        logger.info(
            f"OrderbookFeed starting for {self.symbols} every {self.interval_seconds}s"
        )
        try:
            while self._running:
                for symbol in self.symbols:
                    if not self._running:
                        break
                    snapshot = await fetch_depth(symbol)
                    if snapshot is not None:
                        for cb in self._callbacks:
                            try:
                                await cb(symbol, snapshot)
                            except Exception as exc:
                                logger.error(
                                    f"OrderbookFeed callback error for {symbol}: {exc}",
                                    exc_info=True,
                                )
                await asyncio.sleep(self.interval_seconds)
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("OrderbookFeed stopped")

    async def stop(self) -> None:
        """Stop the background polling loop."""
        self._running = False
        logger.info("OrderbookFeed stopping")
