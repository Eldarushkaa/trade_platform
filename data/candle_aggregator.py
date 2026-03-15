"""
CandleAggregator — builds OHLCV candles from raw price ticks.

Sits between PriceCache (raw aggTrade ticks) and BotManager (strategy dispatch).

How it works:
    - On every price tick, updates the current in-progress candle for that symbol.
    - When wall-clock time crosses a candle boundary (e.g. every 60 seconds),
      the completed candle is finalized and all registered candle-subscribers
      are notified via async callbacks.
    - Strategies receive complete, meaningful OHLCV periods — not raw ticks.

Candle boundary detection:
    Uses UTC epoch seconds divided by interval. A new candle starts when
    int(time.time() / interval) increases. This is fully aligned to wall-clock
    minute boundaries (00:00, 00:01, 00:02, ...) just like Binance candles.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

# Callback type: async def on_candle(symbol, candle) -> None
CandleCallback = Callable[["Candle"], Awaitable[None]]


@dataclass
class Candle:
    """A completed OHLCV candle for one symbol."""
    symbol: str
    interval_seconds: int     # e.g. 60 for 1-minute
    open: float
    high: float
    low: float
    close: float
    volume: float             # tick count (proxy for volume without real volume data)
    open_time: float          # UTC epoch seconds when candle opened
    close_time: float         # UTC epoch seconds when candle closed

    @property
    def interval_label(self) -> str:
        """Human-readable interval, e.g. '1m', '5m', '1h'."""
        secs = self.interval_seconds
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m"
        return f"{secs // 3600}h"

    def __repr__(self) -> str:
        return (
            f"<Candle {self.symbol} {self.interval_label} "
            f"O={self.open:.4f} H={self.high:.4f} L={self.low:.4f} C={self.close:.4f} "
            f"ticks={int(self.volume)}>"
        )


@dataclass
class _InProgress:
    """Mutable state for the candle currently being built."""
    symbol: str
    interval_seconds: int
    candle_index: int          # int(epoch / interval) — changes on boundary
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    open_time: float = field(default_factory=time.time)

    def update(self, price: float) -> None:
        if price > self.high:
            self.high = price
        if price < self.low:
            self.low = price
        self.close = price
        self.volume += 1.0

    def to_candle(self, close_time: float) -> Candle:
        return Candle(
            symbol=self.symbol,
            interval_seconds=self.interval_seconds,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            open_time=self.open_time,
            close_time=close_time,
        )


class CandleAggregator:
    """
    Aggregates raw price ticks into fixed-interval OHLCV candles.

    Usage:
        aggregator = CandleAggregator(interval_seconds=60)
        aggregator.subscribe(bot_manager.dispatch_candle)
        price_cache.subscribe(aggregator.on_tick)

    The aggregator receives ticks via on_tick() and fires registered
    async callbacks with completed Candle objects.
    """

    def __init__(self, interval_seconds: int = 60) -> None:
        if interval_seconds < 1:
            raise ValueError("interval_seconds must be >= 1")
        self.interval_seconds = interval_seconds
        self._in_progress: dict[str, _InProgress] = {}   # symbol → current candle
        self._subscribers: list[CandleCallback] = []

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------

    def subscribe(self, callback: CandleCallback) -> None:
        """Register an async callback to receive completed candles."""
        self._subscribers.append(callback)
        logger.debug(
            f"CandleAggregator: subscriber added ({len(self._subscribers)} total)"
        )

    def unsubscribe(self, callback: CandleCallback) -> None:
        try:
            self._subscribers.remove(callback)
        except ValueError:
            pass

    # ------------------------------------------------------------------
    # Tick ingestion (called by PriceCache subscriber)
    # ------------------------------------------------------------------

    async def on_tick(self, symbol: str, price: float) -> None:
        """
        Process a single price tick.
        If the tick crosses a candle boundary, finalizes the previous candle
        and starts a new one before updating with the new price.
        """
        now = time.time()
        current_index = int(now / self.interval_seconds)

        if symbol not in self._in_progress:
            # First tick for this symbol — start a fresh candle
            # Bug #3 fix: seed volume=1.0 so this tick is counted, not lost
            self._in_progress[symbol] = _InProgress(
                symbol=symbol,
                interval_seconds=self.interval_seconds,
                candle_index=current_index,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1.0,
                open_time=current_index * self.interval_seconds,
            )
            return

        candle = self._in_progress[symbol]

        if current_index > candle.candle_index:
            # --- Candle boundary crossed → close current candle ---
            close_time = current_index * self.interval_seconds
            completed = candle.to_candle(close_time=close_time)
            logger.debug(f"Candle closed: {completed}")

            # Start fresh candle for new period (opens at current price)
            self._in_progress[symbol] = _InProgress(
                symbol=symbol,
                interval_seconds=self.interval_seconds,
                candle_index=current_index,
                open=price,
                high=price,
                low=price,
                close=price,
                open_time=close_time,
            )

            # Notify all subscribers with the completed candle
            # Bug #2 fix: get_event_loop() is deprecated in Python 3.10+;
            # on_tick() is always called from within a running event loop,
            # so get_running_loop() is correct and safe here.
            asyncio.get_running_loop().create_task(
                self._notify(completed)
            )
        else:
            # Same candle period — just update
            candle.update(price)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def flush(self) -> None:
        """Finalize and emit all in-progress candles.

        Call this at shutdown so the last partial candle is not silently dropped.
        The candle's close_time is set to now (it may be shorter than
        interval_seconds if the app shuts down mid-interval).
        """
        now = time.time()
        for symbol, candle in list(self._in_progress.items()):
            if candle.volume > 0:
                completed = candle.to_candle(close_time=now)
                logger.debug(f"CandleAggregator.flush(): emitting partial candle for {symbol}")
                await self._notify(completed)
        self._in_progress.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _notify(self, candle: Candle) -> None:
        """Call all subscribers with a completed candle."""
        for cb in self._subscribers:
            try:
                await cb(candle)
            except Exception as exc:
                logger.error(
                    f"CandleAggregator subscriber error for {candle.symbol}: {exc}",
                    exc_info=True,
                )
