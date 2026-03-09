"""
BinanceFeed — streams real-time prices from Binance via WebSocket.

Connects to the Binance public WebSocket streams (no API key needed for market data).
On each price tick, updates PriceCache which notifies all subscribed bots.

WebSocket endpoint format:
    wss://stream.binance.com:9443/stream?streams=btcusdt@aggTrade/ethusdt@aggTrade

No API key is required for public market data streams.
"""
import asyncio
import json
import logging
from typing import Iterable

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from data.price_cache import PriceCache, price_cache as default_cache

logger = logging.getLogger(__name__)

BINANCE_WS_BASE = "wss://stream.binance.com:9443/stream"
RECONNECT_DELAY_SECONDS = 5
MAX_RECONNECT_ATTEMPTS = 10


class BinanceFeed:
    """
    Manages a WebSocket connection to Binance for live price data.

    Usage:
        feed = BinanceFeed(symbols=["BTCUSDT", "ETHUSDT"])
        asyncio.create_task(feed.start())
        ...
        await feed.stop()
    """

    def __init__(
        self,
        symbols: Iterable[str],
        cache: PriceCache | None = None,
    ) -> None:
        """
        Args:
            symbols:  List of trading pairs to subscribe to, e.g. ["BTCUSDT", "ETHUSDT"].
            cache:    PriceCache instance (defaults to the module singleton).
        """
        self.symbols = [s.upper() for s in symbols]
        self.cache = cache or default_cache
        self._running = False
        self._ws = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Start the WebSocket feed with automatic reconnection.
        Runs indefinitely until stop() is called.
        """
        self._running = True
        logger.info(f"BinanceFeed starting for symbols: {self.symbols}")

        attempt = 0
        while self._running and attempt < MAX_RECONNECT_ATTEMPTS:
            try:
                await self._connect_and_stream()
                attempt = 0  # Reset on clean disconnect
            except (ConnectionClosedError, ConnectionClosedOK) as exc:
                if not self._running:
                    break
                attempt += 1
                logger.warning(
                    f"BinanceFeed disconnected ({exc}). "
                    f"Reconnecting in {RECONNECT_DELAY_SECONDS}s "
                    f"(attempt {attempt}/{MAX_RECONNECT_ATTEMPTS})..."
                )
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
            except Exception as exc:
                if not self._running:
                    break
                attempt += 1
                logger.error(
                    f"BinanceFeed unexpected error: {exc}. "
                    f"Reconnecting in {RECONNECT_DELAY_SECONDS}s...",
                    exc_info=True,
                )
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)

        if attempt >= MAX_RECONNECT_ATTEMPTS:
            logger.error(
                f"BinanceFeed: max reconnection attempts ({MAX_RECONNECT_ATTEMPTS}) reached. Giving up."
            )

    async def stop(self) -> None:
        """Gracefully stop the WebSocket feed."""
        self._running = False
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        logger.info("BinanceFeed stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_url(self) -> str:
        """Build the combined stream URL for all symbols."""
        # aggTrade stream gives the latest trade price — very low latency
        streams = "/".join(f"{s.lower()}@aggTrade" for s in self.symbols)
        return f"{BINANCE_WS_BASE}?streams={streams}"

    async def _connect_and_stream(self) -> None:
        """Open the WebSocket connection and process incoming messages."""
        url = self._build_url()
        logger.info(f"BinanceFeed connecting to: {url}")

        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            self._ws = ws
            logger.info(f"BinanceFeed connected. Streaming {len(self.symbols)} symbol(s)...")

            async for raw_message in ws:
                if not self._running:
                    break
                await self._handle_message(raw_message)

    async def _handle_message(self, raw: str) -> None:
        """
        Parse a Binance aggTrade message and update the price cache.

        Binance combined stream format:
        {
            "stream": "btcusdt@aggTrade",
            "data": {
                "s": "BTCUSDT",   # symbol
                "p": "42000.00",  # price (string)
                ...
            }
        }
        """
        try:
            msg = json.loads(raw)
            data = msg.get("data", {})
            symbol: str = data.get("s", "")
            price_str: str = data.get("p", "")

            if not symbol or not price_str:
                return

            price = float(price_str)
            self.cache.update(symbol, price)

            logger.debug(f"Tick: {symbol} = {price:.4f}")

        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            logger.warning(f"BinanceFeed: failed to parse message: {exc} | raw={raw[:120]}")
