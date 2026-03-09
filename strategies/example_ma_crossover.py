"""
MACD Crossover Bot — Moving Average Convergence Divergence strategy.

Operates on 1-MINUTE CANDLES. Upgraded from a simple EMA(9/21) crossover
to a full MACD system with signal line confirmation and histogram filter.

Improvements over the naive EMA crossover:
- Standard MACD: EMA(12) - EMA(26) with 9-period signal line.
- Histogram filter: only trades when histogram confirms direction, filtering
  weak crossovers that revert immediately.
- Dead zone filter: ignores signals when |MACD| < price * 0.00005 — EMAs
  are too close together, indicating a flat/ranging market.
- 50% trade fraction instead of 95% — preserves capital.

Strategy logic:
    BUY  when MACD crosses above Signal  AND  histogram > 0  AND  not in position
    SELL when MACD crosses below Signal  AND  histogram < 0  AND  in position

Multi-coin usage:
    from strategies.example_ma_crossover import MACrossoverBot
    REGISTERED_BOTS = [
        MACrossoverBot.for_symbol("BTCUSDT"),
        MACrossoverBot.for_symbol("ETHUSDT"),
        MACrossoverBot.for_symbol("SOLUSDT"),
    ]
"""
from typing import Optional, TYPE_CHECKING

from core.base_strategy import BaseStrategy
from core.simulation_engine import BaseOrderEngine

if TYPE_CHECKING:
    from data.candle_aggregator import Candle


class MACrossoverBot(BaseStrategy):
    # --- Required class attributes ---
    name = "ma_crossover_bot"
    symbol = "BTCUSDT"

    # --- MACD parameters (tuned for 1-minute candles) ---
    FAST_PERIOD = 12              # Fast EMA period (standard MACD)
    SLOW_PERIOD = 26              # Slow EMA period (standard MACD)
    SIGNAL_PERIOD = 9             # Signal line EMA period
    TRADE_FRACTION = 0.50         # Use 50% of USDT balance per BUY
    DEAD_ZONE_FACTOR = 0.00005   # Ignore MACD when |macd| < price * this

    # ------------------------------------------------------------------
    # Factory: create a subclass bound to a specific symbol
    # ------------------------------------------------------------------

    @classmethod
    def for_symbol(cls, symbol: str) -> type:
        """
        Return a new subclass of MACrossoverBot configured for the given symbol.

        Example:
            MACrossoverBot.for_symbol("ETHUSDT")
            → creates class MACrossoverBot_ETH with name="ma_eth", symbol="ETHUSDT"
        """
        asset = symbol.replace("USDT", "").lower()
        return type(
            f"{cls.__name__}_{asset.upper()}",   # class name: MACrossoverBot_BTC
            (cls,),
            {"name": f"ma_{asset}", "symbol": symbol},
        )

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def __init__(self, engine: BaseOrderEngine) -> None:
        super().__init__(engine)

        # EMA state
        self._fast_ema: Optional[float] = None
        self._slow_ema: Optional[float] = None

        # MACD / Signal / Histogram
        self._macd: Optional[float] = None
        self._signal: Optional[float] = None
        self._histogram: Optional[float] = None

        # Previous values for crossover detection
        self._prev_macd: Optional[float] = None
        self._prev_signal: Optional[float] = None

        # Warmup
        self._candle_count: int = 0
        self._warmup_closes: list[float] = []
        self._signal_warmup: list[float] = []  # MACD values during signal warmup

        # Position tracking
        self._in_position: bool = False

    # ------------------------------------------------------------------
    # Candle-based strategy logic
    # ------------------------------------------------------------------

    async def on_candle(self, candle: "Candle") -> None:
        """Called once per completed 1-minute candle."""
        self._candle_count += 1
        close = candle.close

        # --- Phase 1: Warmup — collect SLOW_PERIOD candles to seed EMAs ---
        if self._candle_count <= self.SLOW_PERIOD:
            self._warmup_closes.append(close)
            if self._candle_count == self.SLOW_PERIOD:
                self._seed_emas()
            else:
                self.logger.debug(
                    f"EMA warming up: {self._candle_count}/{self.SLOW_PERIOD} candles"
                )
            return

        # --- Phase 2: Signal warmup — collect SIGNAL_PERIOD MACD values ---
        if self._signal is None:
            self._update_emas(close)
            self._macd = self._fast_ema - self._slow_ema
            self._signal_warmup.append(self._macd)

            if len(self._signal_warmup) >= self.SIGNAL_PERIOD:
                self._seed_signal()
            else:
                self.logger.debug(
                    f"Signal warming up: {len(self._signal_warmup)}/{self.SIGNAL_PERIOD} "
                    f"MACD values"
                )
            return

        # --- Phase 3: Live trading ---
        self._prev_macd = self._macd
        self._prev_signal = self._signal

        self._update_emas(close)
        self._macd = self._fast_ema - self._slow_ema
        self._update_signal()
        self._histogram = self._macd - self._signal

        # Dead zone check: skip if MACD is too close to zero (flat market)
        dead_zone = close * self.DEAD_ZONE_FACTOR
        in_dead_zone = abs(self._macd) < dead_zone

        self.logger.debug(
            f"close={close:.4f}  MACD={self._macd:.6f}  "
            f"Signal={self._signal:.6f}  Hist={self._histogram:.6f}  "
            f"dead_zone={'YES' if in_dead_zone else 'no'}"
        )

        if in_dead_zone:
            return

        await self._check_signals(close)

    # ------------------------------------------------------------------
    # EMA / MACD internals
    # ------------------------------------------------------------------

    def _seed_emas(self) -> None:
        """Seed fast and slow EMAs from warmup closes."""
        prices = self._warmup_closes
        self._slow_ema = sum(prices) / len(prices)
        self._fast_ema = sum(prices[-self.FAST_PERIOD:]) / self.FAST_PERIOD
        self._macd = self._fast_ema - self._slow_ema
        self._warmup_closes.clear()
        self.logger.info(
            f"EMA warmup complete ({self.SLOW_PERIOD} candles). "
            f"Fast={self._fast_ema:.4f}  Slow={self._slow_ema:.4f}  "
            f"MACD={self._macd:.6f}"
        )

    def _seed_signal(self) -> None:
        """Seed the signal line EMA from collected MACD values."""
        self._signal = sum(self._signal_warmup) / len(self._signal_warmup)
        self._histogram = self._macd - self._signal
        self._signal_warmup.clear()
        self.logger.info(
            f"Signal line warmup complete ({self.SIGNAL_PERIOD} MACD values). "
            f"Signal={self._signal:.6f}  Histogram={self._histogram:.6f}"
        )

    def _update_emas(self, close: float) -> None:
        """Update fast and slow EMAs with new close price."""
        k_fast = 2.0 / (self.FAST_PERIOD + 1)
        k_slow = 2.0 / (self.SLOW_PERIOD + 1)
        self._fast_ema = close * k_fast + self._fast_ema * (1 - k_fast)
        self._slow_ema = close * k_slow + self._slow_ema * (1 - k_slow)

    def _update_signal(self) -> None:
        """Update signal line EMA with current MACD value."""
        k = 2.0 / (self.SIGNAL_PERIOD + 1)
        self._signal = self._macd * k + self._signal * (1 - k)

    # ------------------------------------------------------------------
    # Signal detection
    # ------------------------------------------------------------------

    async def _check_signals(self, price: float) -> None:
        """Detect MACD/Signal crossover with histogram confirmation."""
        if None in (self._prev_macd, self._prev_signal):
            return

        # Was MACD above Signal in the previous candle?
        was_above = self._prev_macd > self._prev_signal
        is_above = self._macd > self._signal

        # Bullish crossover: MACD crossed above Signal + histogram confirms
        if not was_above and is_above and self._histogram > 0 and not self._in_position:
            await self._buy(price)

        # Bearish crossover: MACD crossed below Signal + histogram confirms
        elif was_above and not is_above and self._histogram < 0 and self._in_position:
            await self._sell(price)

    # ------------------------------------------------------------------
    # Order helpers
    # ------------------------------------------------------------------

    async def _buy(self, price: float) -> None:
        usdt = await self.engine.get_balance(self.name, "USDT")
        if usdt < 10:
            self.logger.warning("Insufficient USDT to buy")
            return

        spend = usdt * self.TRADE_FRACTION
        quantity = round(spend / price, 6)

        try:
            result = await self.engine.place_order(
                bot_id=self.name,
                symbol=self.symbol,
                side="BUY",
                quantity=quantity,
                price=price,
            )
            self._in_position = True
            fee = result.get("fee_usdt", 0)
            self.logger.info(
                f"MACD CROSS ↑ → BUY  {quantity:.6f} @ {price:.4f}  "
                f"MACD={self._macd:.6f}  Hist={self._histogram:.6f}  "
                f"fee={fee:.4f}  (trade_id={result.get('trade_id')})"
            )
        except ValueError as exc:
            self.logger.error(f"BUY failed: {exc}")

    async def _sell(self, price: float) -> None:
        asset = self.symbol.replace("USDT", "")
        quantity = await self.engine.get_balance(self.name, asset)
        if quantity <= 0:
            self._in_position = False
            return

        quantity = round(quantity, 6)
        try:
            result = await self.engine.place_order(
                bot_id=self.name,
                symbol=self.symbol,
                side="SELL",
                quantity=quantity,
                price=price,
            )
            self._in_position = False
            pnl = result.get("realized_pnl", 0)
            fee = result.get("fee_usdt", 0)
            self.logger.info(
                f"MACD CROSS ↓ → SELL {quantity:.6f} @ {price:.4f}  "
                f"MACD={self._macd:.6f}  Hist={self._histogram:.6f}  "
                f"P&L={pnl:+.4f}  fee={fee:.4f}  (trade_id={result.get('trade_id')})"
            )
        except ValueError as exc:
            self.logger.error(f"SELL failed: {exc}")
