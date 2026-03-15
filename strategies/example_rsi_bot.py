"""
RSI Bot — Wilder's Exponential RSI for 1-minute bidirectional futures trading.

Operates on 1-MINUTE CANDLES. USDT-Margined perpetual futures with leverage.

Entry logic (bidirectional):
    RSI < OVERSOLD  (default 30) → OPEN LONG  (expect bounce up)
    RSI > OVERBOUGHT(default 70) → OPEN SHORT (expect drop)

    If already in opposite position, close it first then open new direction.

Exit logic (two independent mechanisms):
    1. Mean-reversion exit (RSI recovery):
       - LONG exits when RSI recovers above EXIT_RSI_LONG (default 55)
       - SHORT exits when RSI drops below EXIT_RSI_SHORT (default 45)
       This produces typical hold times of 15 min – 2 hours.

    2. Max-hold fallback (time-based):
       - Force-closes any open position after MAX_HOLD_CANDLES candles (default 120 = 2 hrs)
       - Prevents runaway drawdown if RSI never recovers

Both mechanisms are optimizable, giving the GA many ways to tune hold duration.

Multi-coin usage:
    from strategies.example_rsi_bot import RSIBot
    REGISTERED_BOTS = [
        RSIBot.for_symbol("BTCUSDT"),
        RSIBot.for_symbol("ETHUSDT"),
        RSIBot.for_symbol("SOLUSDT"),
    ]
"""
from typing import Optional, TYPE_CHECKING

from core.base_strategy import BaseStrategy
from core.simulation_engine import BaseOrderEngine

if TYPE_CHECKING:
    from data.candle_aggregator import Candle


class RSIBot(BaseStrategy):
    name_prefix = "rsi"
    # --- Required class attributes ---
    name = "rsi_bot"
    symbol = "BTCUSDT"

    # --- Strategy parameters (tuned for 1-minute candles) ---
    RSI_PERIOD       = 10        # Shorter period = faster signals on 1-min
    OVERSOLD         = 30.0      # Go LONG below this
    OVERBOUGHT       = 70.0      # Go SHORT above this
    EXIT_RSI_LONG    = 55.0      # Close LONG when RSI recovers to this level
    EXIT_RSI_SHORT   = 45.0      # Close SHORT when RSI drops to this level
    MAX_HOLD_CANDLES = 120       # Force-close after this many candles (120 = 2 hrs)
    TRADE_FRACTION   = 1.0       # Use 100% of free USDT for margin
    COOLDOWN_CANDLES = 3         # Min candles between new entries

    PARAM_SCHEMA = {
        "RSI_PERIOD": {
            "type": "int", "default": 10, "min": 3, "max": 50,
            "description": "RSI lookback window",
        },
        "OVERSOLD": {
            "type": "float", "default": 30.0, "min": 5.0, "max": 45.0,
            "description": "Long entry threshold (RSI below this → LONG)",
        },
        "OVERBOUGHT": {
            "type": "float", "default": 70.0, "min": 55.0, "max": 95.0,
            "description": "Short entry threshold (RSI above this → SHORT)",
        },
        "EXIT_RSI_LONG": {
            "type": "float", "default": 55.0, "min": 30.0, "max": 75.0,
            "description": "Close LONG when RSI recovers above this (mean-reversion exit)",
        },
        "EXIT_RSI_SHORT": {
            "type": "float", "default": 45.0, "min": 25.0, "max": 70.0,
            "description": "Close SHORT when RSI drops below this (mean-reversion exit)",
        },
        "MAX_HOLD_CANDLES": {
            "type": "int", "default": 120, "min": 10, "max": 1440,
            "description": "Force-close position after this many candles (time-stop)",
        },
        "TRADE_FRACTION": {
            "type": "float", "default": 1.0, "min": 0.10, "max": 1.0,
            "description": "Fraction of free USDT to use per trade",
            "optimize": False,
        },
        "COOLDOWN_CANDLES": {
            "type": "int", "default": 3, "min": 0, "max": 30,
            "description": "Minimum candles between new entries",
        },
    }

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def __init__(self, engine: BaseOrderEngine) -> None:
        super().__init__(engine)
        self._avg_gain: Optional[float] = None
        self._avg_loss: Optional[float] = None
        self._prev_close: Optional[float] = None
        self._warmup_closes: list[float] = []
        self._position_opened_candle: int = -1   # candle count when position was opened

    def set_params(self, updates: dict) -> dict:
        """
        Override to reset Wilder RSI state when RSI_PERIOD changes.

        Wilder RSI uses alpha = 1/period for the exponential smoothing.
        If RSI_PERIOD is updated at runtime, the running avg_gain/avg_loss
        were computed with the old alpha and would silently produce wrong RSI
        values until the indicator re-converges. Resetting forces a clean
        re-warmup with the new period starting from the next candle.
        """
        applied = super().set_params(updates)
        if "RSI_PERIOD" in applied:
            self._avg_gain = None
            self._avg_loss = None
            self._prev_close = None
            self._warmup_closes = []
            self.logger.info(
                f"RSI_PERIOD changed to {self.RSI_PERIOD}: "
                "Wilder RSI state reset — re-warming up from next candle"
            )
        return applied

    # ------------------------------------------------------------------
    # Candle logic
    # ------------------------------------------------------------------

    async def on_candle(self, candle: "Candle") -> None:
        self._candle_count += 1
        close = candle.close

        # --- Wilder RSI warmup ---
        if self._avg_gain is None:
            self._warmup_closes.append(close)
            if len(self._warmup_closes) > self.RSI_PERIOD:
                self._seed_wilder_rsi()
            else:
                self.logger.debug(
                    f"RSI warming up: {len(self._warmup_closes)}/{self.RSI_PERIOD + 1}"
                )
                return
        else:
            self._update_wilder_rsi(close)

        rsi = self._compute_rsi()
        if rsi is None:
            return

        # Get current position: positive = LONG, negative = SHORT, 0 = none
        position = await self.engine.get_balance(self.name, "POSITION")

        self.logger.debug(
            f"close={close:.2f}  RSI={rsi:.1f}  pos={position:.6f}"
        )

        # ------------------------------------------------------------------
        # EXIT LOGIC (checked before entry to avoid stale positions)
        # ------------------------------------------------------------------

        if position > 0:
            # --- LONG position: check mean-reversion exit or time-stop ---
            candles_held = self._candle_count - self._position_opened_candle
            if rsi > self.EXIT_RSI_LONG:
                await self._close_position(close, "SELL", f"RSI exit LONG ({rsi:.1f}>{self.EXIT_RSI_LONG})")
                position = 0
            elif candles_held >= self.MAX_HOLD_CANDLES:
                await self._close_position(close, "SELL", f"Time-stop LONG ({candles_held} candles)")
                position = 0

        elif position < 0:
            # --- SHORT position: check mean-reversion exit or time-stop ---
            candles_held = self._candle_count - self._position_opened_candle
            if rsi < self.EXIT_RSI_SHORT:
                await self._close_position(close, "BUY", f"RSI exit SHORT ({rsi:.1f}<{self.EXIT_RSI_SHORT})")
                position = 0
            elif candles_held >= self.MAX_HOLD_CANDLES:
                await self._close_position(close, "BUY", f"Time-stop SHORT ({candles_held} candles)")
                position = 0

        # ------------------------------------------------------------------
        # ENTRY LOGIC (only when flat + cooldown satisfied)
        # ------------------------------------------------------------------

        cooldown_ok = (
            self._candle_count - self._last_trade_candle >= self.COOLDOWN_CANDLES
        )
        if not cooldown_ok or position != 0:
            return

        # --- Oversold → go LONG ---
        if rsi < self.OVERSOLD:
            result = await self._open_position(close, "BUY", RSI=f"{rsi:.1f}")
            if result is not None:
                self._position_opened_candle = self._candle_count

        # --- Overbought → go SHORT ---
        elif rsi > self.OVERBOUGHT:
            result = await self._open_position(close, "SELL", RSI=f"{rsi:.1f}")
            if result is not None:
                self._position_opened_candle = self._candle_count

    # ------------------------------------------------------------------
    # Wilder RSI
    # ------------------------------------------------------------------

    def _seed_wilder_rsi(self) -> None:
        prices = self._warmup_closes
        gains, losses = [], []
        for i in range(1, len(prices)):
            delta = prices[i] - prices[i - 1]
            gains.append(max(delta, 0.0))
            losses.append(max(-delta, 0.0))
        self._avg_gain = sum(gains) / self.RSI_PERIOD
        self._avg_loss = sum(losses) / self.RSI_PERIOD
        self._prev_close = prices[-1]
        self._warmup_closes.clear()
        self.logger.info(
            f"Wilder RSI({self.RSI_PERIOD}) ready. "
            f"avg_gain={self._avg_gain:.6f}  avg_loss={self._avg_loss:.6f}"
        )

    def _update_wilder_rsi(self, close: float) -> None:
        if self._prev_close is None:
            self._prev_close = close
            return
        delta = close - self._prev_close
        alpha = 1.0 / self.RSI_PERIOD
        self._avg_gain = alpha * max(delta, 0.0) + (1 - alpha) * self._avg_gain
        self._avg_loss = alpha * max(-delta, 0.0) + (1 - alpha) * self._avg_loss
        self._prev_close = close

    def _compute_rsi(self) -> Optional[float]:
        if self._avg_gain is None or self._avg_loss is None:
            return None
        if self._avg_loss == 0:
            return 100.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - (100.0 / (1.0 + rs))
