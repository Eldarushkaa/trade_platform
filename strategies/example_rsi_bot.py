"""
RSI Bot — Wilder's RSI with EMA50/200 trend filter and ATR volatility filter.

Operates on 5-MINUTE CANDLES. USDT-Margined perpetual futures with leverage.

Entry logic (trend-filtered mean-reversion):
    Entry is allowed only when ALL three conditions are met:
      1. RSI is oversold/overbought (entry signal)
      2. EMA trend agrees: LONG only if EMA50 > EMA200 (bull trend), SHORT only if EMA50 < EMA200
      3. ATR/price >= ATR_MIN_PCT (enough volatility for mean-reversion to work)

    RSI < OVERSOLD  AND EMA50 > EMA200  AND atr_ok → OPEN LONG
    RSI > OVERBOUGHT AND EMA50 < EMA200 AND atr_ok → OPEN SHORT

    If already in opposite position, close it first then open new direction.

Warmup:
    No trades are placed until candle #200 (EMA200 requires 200 bars to initialise).
    EMA50 and ATR(14) are also computed during warmup.

Exit logic (two independent mechanisms):
    1. Mean-reversion exit (RSI recovery):
       - LONG exits when RSI recovers above EXIT_RSI_LONG (default 55)
       - SHORT exits when RSI drops below EXIT_RSI_SHORT (default 45)

    2. Max-hold fallback (time-based):
       - Force-closes any open position after MAX_HOLD_CANDLES candles (default 30 = 2.5 hrs)
       - Prevents runaway drawdown if RSI never recovers

Indicators (all fixed periods, not optimized — keeps search space small):
    EMA_FAST  = 50   candles  (trend direction)
    EMA_SLOW  = 200  candles  (macro trend filter)
    ATR       = 14   candles  (volatility, True Range)

ATR:
    True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
    ATR = Wilder EMA of TR (alpha = 1/14)
    Filter: atr / close >= ATR_MIN_PCT  (skip when market is too quiet)

Optimizable parameters (all with tight, realistic ranges):
    RSI_PERIOD       7–21     RSI lookback
    OVERSOLD         20–35    Long entry threshold
    OVERBOUGHT       65–80    Short entry threshold
    EXIT_RSI_LONG    45–65    Close LONG exit level
    EXIT_RSI_SHORT   35–55    Close SHORT exit level
    MAX_HOLD_CANDLES 10–50    Time-stop in candles
    COOLDOWN_CANDLES 0–10     Min candles between entries
    ATR_MIN_PCT      0.001–0.008  Min ATR/price volatility filter

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
    name = "rsi_bot"
    symbol = "BTCUSDT"

    # --- Fixed indicator periods (not optimized — keeps search space small) ---
    EMA_FAST_PERIOD = 50
    EMA_SLOW_PERIOD = 200   # warmup guard: no trades until this many candles seen
    ATR_PERIOD      = 14

    # --- Optimizable strategy parameters ---
    RSI_PERIOD       = 14       # Wilder RSI lookback
    OVERSOLD         = 30.0     # Go LONG below this
    OVERBOUGHT       = 70.0     # Go SHORT above this
    EXIT_RSI_LONG    = 55.0     # Close LONG when RSI recovers above this
    EXIT_RSI_SHORT   = 45.0     # Close SHORT when RSI drops below this
    MAX_HOLD_CANDLES = 30       # Force-close after this many candles (~2.5 hrs)
    TRADE_FRACTION   = 1.0      # Use 100% of free USDT for margin
    COOLDOWN_CANDLES = 3        # Min candles between new entries
    ATR_MIN_PCT      = 0.002    # Min ATR/price ratio to allow trade entry

    PARAM_SCHEMA = {
        "RSI_PERIOD": {
            "type": "int", "default": 14, "min": 7, "max": 21,
            "description": "RSI lookback window (Wilder EMA)",
        },
        "OVERSOLD": {
            "type": "float", "default": 30.0, "min": 20.0, "max": 35.0,
            "description": "Long entry threshold (RSI below this → LONG)",
        },
        "OVERBOUGHT": {
            "type": "float", "default": 70.0, "min": 65.0, "max": 80.0,
            "description": "Short entry threshold (RSI above this → SHORT)",
        },
        "EXIT_RSI_LONG": {
            "type": "float", "default": 55.0, "min": 45.0, "max": 65.0,
            "description": "Close LONG when RSI recovers above this (mean-reversion exit)",
        },
        "EXIT_RSI_SHORT": {
            "type": "float", "default": 45.0, "min": 35.0, "max": 55.0,
            "description": "Close SHORT when RSI drops below this (mean-reversion exit)",
        },
        "MAX_HOLD_CANDLES": {
            "type": "int", "default": 30, "min": 10, "max": 50,
            "description": "Force-close position after this many candles (time-stop)",
        },
        "TRADE_FRACTION": {
            "type": "float", "default": 1.0, "min": 0.10, "max": 1.0,
            "description": "Fraction of free USDT to use per trade",
            "optimize": False,
        },
        "COOLDOWN_CANDLES": {
            "type": "int", "default": 3, "min": 0, "max": 10,
            "description": "Minimum candles between new entries",
        },
        "ATR_MIN_PCT": {
            "type": "float", "default": 0.002, "min": 0.001, "max": 0.008,
            "description": "Min ATR/price ratio for entry (skip low-volatility candles)",
        },
    }

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def __init__(self, engine: BaseOrderEngine) -> None:
        super().__init__(engine)

        # --- Wilder RSI state ---
        self._avg_gain: Optional[float] = None
        self._avg_loss: Optional[float] = None
        self._prev_close: Optional[float] = None
        self._warmup_closes: list[float] = []

        # --- EMA50 / EMA200 trend filter state ---
        self._ema_fast: Optional[float] = None   # EMA50
        self._ema_slow: Optional[float] = None   # EMA200
        # Shared warmup buffer for EMAs (we collect up to EMA_SLOW_PERIOD closes)
        self._ema_warmup: list[float] = []

        # --- ATR volatility filter state ---
        self._atr: Optional[float] = None
        self._warmup_tr: list[float] = []

        # --- Position tracking ---
        self._position_opened_candle: int = -1

    def set_params(self, updates: dict) -> dict:
        """Reset RSI state when RSI_PERIOD changes (Wilder alpha depends on period)."""
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
    # Main candle handler
    # ------------------------------------------------------------------

    async def on_candle(self, candle: "Candle") -> None:
        self._candle_count += 1
        close = candle.close
        high  = candle.high
        low   = candle.low

        # Capture prev_close BEFORE _update_atr overwrites self._prev_close.
        # Both ATR and RSI need the same "previous close" reference.
        prev_close_snapshot = self._prev_close

        # --- Update all indicators ---
        self._update_emas(close)
        self._update_atr(high, low, close)          # updates self._prev_close → close
        self._update_rsi(close, prev_close_snapshot) # uses snapshot of prev close

        # --- Warmup guard: wait until EMA200 is ready ---
        # EMA200 requires exactly EMA_SLOW_PERIOD closes to initialise.
        if self._ema_slow is None:
            self.logger.debug(
                f"Warmup: {len(self._ema_warmup)}/{self.EMA_SLOW_PERIOD} "
                f"candles (waiting for EMA{self.EMA_SLOW_PERIOD})"
            )
            return

        # RSI also needs its own warmup
        rsi = self._compute_rsi()
        if rsi is None:
            return

        # Get current position: positive = LONG, negative = SHORT, 0 = flat
        position = await self.engine.get_balance(self.name, "POSITION")

        self.logger.debug(
            f"close={close:.2f}  RSI={rsi:.1f}  "
            f"EMA50={self._ema_fast:.2f}  EMA200={self._ema_slow:.2f}  "
            f"ATR%={self._atr/close*100:.3f}  pos={position:.6f}"
        )

        # ------------------------------------------------------------------
        # EXIT LOGIC (checked before entry)
        # ------------------------------------------------------------------

        if position > 0:
            candles_held = self._candle_count - self._position_opened_candle
            if rsi > self.EXIT_RSI_LONG:
                await self._close_position(close, "SELL", f"RSI exit LONG ({rsi:.1f}>{self.EXIT_RSI_LONG})")
                position = 0
            elif candles_held >= self.MAX_HOLD_CANDLES:
                await self._close_position(close, "SELL", f"Time-stop LONG ({candles_held} candles)")
                position = 0

        elif position < 0:
            candles_held = self._candle_count - self._position_opened_candle
            if rsi < self.EXIT_RSI_SHORT:
                await self._close_position(close, "BUY", f"RSI exit SHORT ({rsi:.1f}<{self.EXIT_RSI_SHORT})")
                position = 0
            elif candles_held >= self.MAX_HOLD_CANDLES:
                await self._close_position(close, "BUY", f"Time-stop SHORT ({candles_held} candles)")
                position = 0

        # ------------------------------------------------------------------
        # ENTRY LOGIC: RSI signal + EMA trend filter + ATR volatility filter
        # ------------------------------------------------------------------

        cooldown_ok = (self._candle_count - self._last_trade_candle >= self.COOLDOWN_CANDLES)
        if not cooldown_ok or position != 0:
            return

        # Trend direction from EMA50 vs EMA200
        trend_up   = self._ema_fast > self._ema_slow   # bullish: fast above slow
        trend_down = self._ema_fast < self._ema_slow   # bearish: fast below slow

        # Volatility filter: require minimum ATR/price ratio
        atr_ok = (self._atr is None) or (self._atr / close >= self.ATR_MIN_PCT)

        # --- Oversold + bullish trend + enough volatility → LONG ---
        if rsi < self.OVERSOLD and trend_up and atr_ok:
            result = await self._open_position(close, "BUY", RSI=f"{rsi:.1f}")
            if result is not None:
                self._position_opened_candle = self._candle_count

        # --- Overbought + bearish trend + enough volatility → SHORT ---
        elif rsi > self.OVERBOUGHT and trend_down and atr_ok:
            result = await self._open_position(close, "SELL", RSI=f"{rsi:.1f}")
            if result is not None:
                self._position_opened_candle = self._candle_count

    # ------------------------------------------------------------------
    # EMA50 / EMA200 (trend filter)
    # ------------------------------------------------------------------

    def _update_emas(self, close: float) -> None:
        """
        Compute EMA50 and EMA200 using standard EMA formula (k = 2/(N+1)).

        During warmup (first EMA_SLOW_PERIOD candles):
          - Both EMAs accumulate closes in self._ema_warmup
          - EMA50 is seeded at candle #50 using SMA50
          - EMA200 is seeded at candle #200 using SMA200

        After warmup both EMAs update every candle.
        Trading is blocked until EMA200 is ready.
        """
        self._ema_warmup.append(close)
        n = len(self._ema_warmup)

        k_fast = 2.0 / (self.EMA_FAST_PERIOD + 1)
        k_slow = 2.0 / (self.EMA_SLOW_PERIOD + 1)

        if self._ema_fast is None:
            if n >= self.EMA_FAST_PERIOD:
                # Seed EMA50 with SMA of first 50 closes
                self._ema_fast = sum(self._ema_warmup[:self.EMA_FAST_PERIOD]) / self.EMA_FAST_PERIOD
                # Apply EMA updates for remaining candles in buffer
                for c in self._ema_warmup[self.EMA_FAST_PERIOD:]:
                    self._ema_fast = c * k_fast + self._ema_fast * (1 - k_fast)
        else:
            self._ema_fast = close * k_fast + self._ema_fast * (1 - k_fast)

        if self._ema_slow is None:
            if n >= self.EMA_SLOW_PERIOD:
                # Seed EMA200 with SMA of first 200 closes
                self._ema_slow = sum(self._ema_warmup[:self.EMA_SLOW_PERIOD]) / self.EMA_SLOW_PERIOD
                # Clear the buffer (no longer needed)
                self._ema_warmup.clear()
                self.logger.info(
                    f"EMA{self.EMA_FAST_PERIOD}={self._ema_fast:.2f}  "
                    f"EMA{self.EMA_SLOW_PERIOD}={self._ema_slow:.2f} ready — trading enabled"
                )
        else:
            self._ema_slow = close * k_slow + self._ema_slow * (1 - k_slow)

    # ------------------------------------------------------------------
    # ATR (volatility filter)
    # ------------------------------------------------------------------

    def _update_atr(self, high: float, low: float, close: float) -> None:
        """
        True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
        ATR = Wilder EMA of TR (alpha = 1/ATR_PERIOD).

        Warmup: collect ATR_PERIOD True Range values, seed with their simple average.
        """
        prev_close = self._prev_close if self._prev_close is not None else close

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low  - prev_close),
        )
        self._prev_close = close

        if self._atr is None:
            self._warmup_tr.append(tr)
            if len(self._warmup_tr) >= self.ATR_PERIOD:
                self._atr = sum(self._warmup_tr) / self.ATR_PERIOD
                self._warmup_tr.clear()
        else:
            alpha = 1.0 / self.ATR_PERIOD
            self._atr = alpha * tr + (1 - alpha) * self._atr

    # ------------------------------------------------------------------
    # Wilder RSI
    # ------------------------------------------------------------------

    def _update_rsi(self, close: float, prev_close: Optional[float] = None) -> None:
        """Buffer closes for RSI warmup; after warmup update Wilder EMA on each candle.

        prev_close is the snapshot taken BEFORE _update_atr ran this candle,
        ensuring RSI delta = close - prev_candle_close (not close - close = 0).
        """
        if self._avg_gain is None:
            # Use a separate warmup list for RSI (independent of EMA warmup)
            self._warmup_closes.append(close)
            if len(self._warmup_closes) > self.RSI_PERIOD:
                self._seed_wilder_rsi()
        else:
            self._update_wilder_rsi(close, prev_close)

    def _seed_wilder_rsi(self) -> None:
        prices = self._warmup_closes
        gains, losses = [], []
        for i in range(1, len(prices)):
            delta = prices[i] - prices[i - 1]
            gains.append(max(delta, 0.0))
            losses.append(max(-delta, 0.0))
        self._avg_gain = sum(gains) / self.RSI_PERIOD
        self._avg_loss = sum(losses) / self.RSI_PERIOD
        self._warmup_closes.clear()
        self.logger.debug(
            f"Wilder RSI({self.RSI_PERIOD}) seeded: "
            f"avg_gain={self._avg_gain:.6f}  avg_loss={self._avg_loss:.6f}"
        )

    def _update_wilder_rsi(self, close: float, prev_close: Optional[float] = None) -> None:
        # prev_close is the snapshot taken before _update_atr ran this candle.
        # This ensures delta = close - last_candle_close (correct Wilder calculation).
        prev = prev_close if prev_close is not None else close
        delta = close - prev
        alpha = 1.0 / self.RSI_PERIOD
        self._avg_gain = alpha * max(delta, 0.0) + (1 - alpha) * self._avg_gain
        self._avg_loss = alpha * max(-delta, 0.0) + (1 - alpha) * self._avg_loss

    def _compute_rsi(self) -> Optional[float]:
        if self._avg_gain is None or self._avg_loss is None:
            return None
        if self._avg_loss == 0:
            return 100.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - (100.0 / (1.0 + rs))
