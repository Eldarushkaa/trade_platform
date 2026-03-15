"""
MACD Crossover Bot — bidirectional futures trading with MACD.

Operates on 1-MINUTE CANDLES. USDT-Margined perpetual futures with leverage.

Strategy logic (bidirectional):
    Bullish crossover (MACD > Signal, MACD > 0) → OPEN LONG
    Bearish crossover (MACD < Signal, MACD < 0) → OPEN SHORT

    If already in opposite position, close it first then open new direction.

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
    name_prefix = "ma"
    # --- Required class attributes ---
    name = "ma_crossover_bot"
    symbol = "BTCUSDT"

    # --- MACD parameters ---
    FAST_PERIOD = 12
    SLOW_PERIOD = 26
    SIGNAL_PERIOD = 9
    TRADE_FRACTION = 1.0

    PARAM_SCHEMA = {
        "FAST_PERIOD": {
            "type": "int", "default": 12, "min": 3, "max": 50,
            "description": "Fast EMA period",
        },
        "SLOW_PERIOD": {
            "type": "int", "default": 26, "min": 10, "max": 100,
            "description": "Slow EMA period",
        },
        "SIGNAL_PERIOD": {
            "type": "int", "default": 9, "min": 3, "max": 30,
            "description": "Signal line smoothing period",
        },
        "TRADE_FRACTION": {
            "type": "float", "default": 1.0, "min": 0.10, "max": 1.0,
            "description": "Fraction of free USDT to use per trade",
            "optimize": False,
        },
    }

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def __init__(self, engine: BaseOrderEngine) -> None:
        super().__init__(engine)
        self._fast_ema: Optional[float] = None
        self._slow_ema: Optional[float] = None
        self._macd: Optional[float] = None
        self._signal: Optional[float] = None
        self._histogram: Optional[float] = None
        self._prev_macd: Optional[float] = None
        self._prev_signal: Optional[float] = None
        self._warmup_closes: list[float] = []
        self._signal_warmup: list[float] = []

    def set_params(self, updates: dict) -> dict:
        """
        Override to enforce FAST_PERIOD < SLOW_PERIOD and reset EMA state.

        The optimizer mutates each parameter independently and can produce
        FAST >= SLOW (inverted MACD = swapped/broken signals). We swap silently
        before applying so the constraint is always satisfied.
        """
        if "FAST_PERIOD" in updates or "SLOW_PERIOD" in updates:
            fast = int(updates.get("FAST_PERIOD", self.FAST_PERIOD))
            slow = int(updates.get("SLOW_PERIOD", self.SLOW_PERIOD))
            if fast >= slow:
                updates = dict(updates)
                lo, hi = min(fast, slow), max(fast, slow)
                schema = self.PARAM_SCHEMA
                updates["FAST_PERIOD"] = max(schema["FAST_PERIOD"]["min"],
                                             min(schema["FAST_PERIOD"]["max"], lo))
                updates["SLOW_PERIOD"] = max(schema["SLOW_PERIOD"]["min"],
                                             min(schema["SLOW_PERIOD"]["max"], hi + 1))

        applied = super().set_params(updates)

        # Reset all EMA state whenever any period changes
        if any(k in applied for k in ("FAST_PERIOD", "SLOW_PERIOD", "SIGNAL_PERIOD")):
            self._fast_ema = None
            self._slow_ema = None
            self._macd = None
            self._signal = None
            self._histogram = None
            self._prev_macd = None
            self._prev_signal = None
            self._warmup_closes = []
            self._signal_warmup = []
            self._candle_count = 0
            self.logger.info(
                f"MA periods changed (FAST={self.FAST_PERIOD} SLOW={self.SLOW_PERIOD} "
                f"SIGNAL={self.SIGNAL_PERIOD}): EMA state reset"
            )

        return applied

    # ------------------------------------------------------------------
    # Candle logic
    # ------------------------------------------------------------------

    async def on_candle(self, candle: "Candle") -> None:
        self._candle_count += 1
        close = candle.close

        # Phase 1: EMA warmup
        if self._candle_count <= self.SLOW_PERIOD:
            self._warmup_closes.append(close)
            if self._candle_count == self.SLOW_PERIOD:
                self._seed_emas()
            else:
                self.logger.debug(f"EMA warming up: {self._candle_count}/{self.SLOW_PERIOD}")
            return

        # Phase 2: Signal warmup
        if self._signal is None:
            self._update_emas(close)
            self._macd = self._fast_ema - self._slow_ema
            self._signal_warmup.append(self._macd)
            if len(self._signal_warmup) >= self.SIGNAL_PERIOD:
                self._seed_signal()
            else:
                self.logger.debug(f"Signal warming up: {len(self._signal_warmup)}/{self.SIGNAL_PERIOD}")
            return

        # Phase 3: Live
        self._prev_macd = self._macd
        self._prev_signal = self._signal
        self._update_emas(close)
        self._macd = self._fast_ema - self._slow_ema
        self._update_signal()
        self._histogram = self._macd - self._signal

        self.logger.debug(
            f"close={close:.4f}  MACD={self._macd:.6f}  "
            f"Signal={self._signal:.6f}  Hist={self._histogram:.6f}"
        )

        await self._check_signals(close)

    # ------------------------------------------------------------------
    # EMA / MACD
    # ------------------------------------------------------------------

    def _seed_emas(self) -> None:
        prices = self._warmup_closes
        self._slow_ema = sum(prices) / len(prices)
        self._fast_ema = sum(prices[-self.FAST_PERIOD:]) / self.FAST_PERIOD
        self._macd = self._fast_ema - self._slow_ema
        self._warmup_closes.clear()
        self.logger.info(f"EMA warmup complete. MACD={self._macd:.6f}")

    def _seed_signal(self) -> None:
        self._signal = sum(self._signal_warmup) / len(self._signal_warmup)
        self._histogram = self._macd - self._signal
        self._signal_warmup.clear()
        self.logger.info(f"Signal line ready. Signal={self._signal:.6f}")

    def _update_emas(self, close: float) -> None:
        k_fast = 2.0 / (self.FAST_PERIOD + 1)
        k_slow = 2.0 / (self.SLOW_PERIOD + 1)
        self._fast_ema = close * k_fast + self._fast_ema * (1 - k_fast)
        self._slow_ema = close * k_slow + self._slow_ema * (1 - k_slow)

    def _update_signal(self) -> None:
        k = 2.0 / (self.SIGNAL_PERIOD + 1)
        self._signal = self._macd * k + self._signal * (1 - k)

    # ------------------------------------------------------------------
    # Signal detection (bidirectional)
    # ------------------------------------------------------------------

    async def _check_signals(self, price: float) -> None:
        if None in (self._prev_macd, self._prev_signal):
            return

        was_above = self._prev_macd > self._prev_signal
        is_above = self._macd > self._signal

        position = await self.engine.get_balance(self.name, "POSITION")

        # Bullish crossover + MACD positive → LONG
        if not was_above and is_above and self._macd > 0 and position <= 0:
            if position < 0:
                await self._close_position(price, "BUY", "Close SHORT → LONG")
            await self._open_position(price, "BUY")

        # Bearish crossover + MACD negative → SHORT
        elif was_above and not is_above and self._macd < 0 and position >= 0:
            if position > 0:
                await self._close_position(price, "SELL", "Close LONG → SHORT")
            await self._open_position(price, "SELL")

