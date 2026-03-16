"""
RSI Bot — Wilder's RSI with EMA50/200 trend filter, ATR volatility filter,
and dynamic RSI thresholds via vol_factor = ATR / EMA(ATR, 50).

Operates on 5-MINUTE CANDLES. USDT-Margined perpetual futures with leverage.

Entry logic (trend-filtered mean-reversion with reversal crossover):
    Entry is allowed only when ALL four conditions are met:
      1. RSI crosses the reversal entry level in the right direction
      2. EMA trend agrees: LONG only if EMA50 > EMA200 (bull trend), SHORT only if EMA50 < EMA200
      3. ATR/price >= ATR_MIN_PCT = 0.004 (enough volatility for mean-reversion to work)
      4. Cooldown period has passed since last trade

    dynamic_oversold  = OVERSOLD  - 8 * (vol_factor - 1)
    dynamic_overbought= OVERBOUGHT + 8 * (vol_factor - 1)
    where vol_factor  = ATR / EMA(ATR, 50)

    Entry levels (hardcoded REVERSAL_BUFFER = 5.0 RSI points):
      rsi_entry_long  = dynamic_oversold  + REVERSAL_BUFFER
      rsi_entry_short = dynamic_overbought - REVERSAL_BUFFER

    LONG:  rsi_prev < rsi_entry_long  AND rsi_now >= rsi_entry_long   (RSI crossed UP)
           AND EMA50 > EMA200 AND atr_ok  → OPEN LONG
    SHORT: rsi_prev > rsi_entry_short AND rsi_now <= rsi_entry_short  (RSI crossed DOWN)
           AND EMA50 < EMA200 AND atr_ok  → OPEN SHORT

    Using a crossover (prev/now) instead of a simple level check ensures entries happen
    exactly at the moment the RSI turns from extreme → more entries, less lag.

    If already in opposite position, close it first then open new direction.

Warmup:
    No trades are placed until candle #200 (EMA200 requires 200 bars to initialise).
    EMA50 and ATR(14) and EMA_ATR(50) are also computed during warmup.

Exit logic (two independent mechanisms):
    1. Mean-reversion exit (RSI recovery) — derived from dynamic thresholds:
       exit_long  = dynamic_oversold  + 20
       exit_short = dynamic_overbought - 20
       (These float with volatility; in calm markets exits are tighter.)

    2. Max-hold fallback (time-based):
       - Force-closes any open position after MAX_HOLD_CANDLES candles (default 30 = 2.5 hrs)
       - Prevents runaway drawdown if RSI never recovers

Indicators (all fixed periods, not optimized — keeps search space small):
    EMA_FAST      = 50   candles  (trend direction)
    EMA_SLOW      = 200  candles  (macro trend filter)
    ATR_PERIOD    = 14   candles  (volatility, True Range)
    EMA_ATR_PERIOD= 50   candles  (smooth ATR for vol_factor baseline)
    ATR_MIN_PCT   = 0.004         (hardcoded; skip low-volatility candles)

ATR:
    True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
    ATR = Wilder EMA of TR (alpha = 1/14)
    EMA_ATR = standard EMA of ATR values (k = 2/51)
    vol_factor = ATR / EMA_ATR  (>1 = volatile, <1 = quiet)

Fixed internal constants (not optimised):
    REVERSAL_BUFFER  5.0      RSI points above oversold / below overbought for reversal entry

Optimizable parameters (all with tight, realistic ranges):
    RSI_PERIOD       7–21     RSI lookback
    OVERSOLD         20–35    Base long entry threshold (adjusted by vol_factor)
    OVERBOUGHT       65–80    Base short entry threshold (adjusted by vol_factor)
    MAX_HOLD_CANDLES 10–40    Time-stop in candles
    COOLDOWN_CANDLES 0–10     Min candles between entries

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
    EMA_FAST_PERIOD  = 50
    EMA_SLOW_PERIOD  = 200   # warmup guard: no trades until this many candles seen
    ATR_PERIOD       = 14
    EMA_ATR_PERIOD   = 50    # smoothing window for ATR baseline (vol_factor denominator)
    ATR_MIN_PCT      = 0.004 # hardcoded min ATR/price filter (skip low-vol candles)
    REVERSAL_BUFFER  = 5.0   # RSI points above oversold / below overbought for reversal entry (not optimized)

    # --- Optimizable strategy parameters ---
    RSI_PERIOD       = 14       # Wilder RSI lookback
    OVERSOLD         = 30.0     # Base LONG threshold (shifted by vol_factor)
    OVERBOUGHT       = 70.0     # Base SHORT threshold (shifted by vol_factor)
    MAX_HOLD_CANDLES = 30       # Force-close after this many candles (~2.5 hrs)
    TRADE_FRACTION   = 1.0      # Use 100% of free USDT for margin
    COOLDOWN_CANDLES = 3        # Min candles between new entries

    PARAM_SCHEMA = {
        "RSI_PERIOD": {
            "type": "int", "default": 14, "min": 7, "max": 21,
            "description": "RSI lookback window (Wilder EMA)",
        },
        "OVERSOLD": {
            "type": "float", "default": 30.0, "min": 20.0, "max": 35.0,
            "description": "Base long entry threshold (shifted down in high-vol via vol_factor)",
        },
        "OVERBOUGHT": {
            "type": "float", "default": 70.0, "min": 65.0, "max": 80.0,
            "description": "Base short entry threshold (shifted up in high-vol via vol_factor)",
        },
        "MAX_HOLD_CANDLES": {
            "type": "int", "default": 30, "min": 10, "max": 40,
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

        # --- ATR volatility filter + EMA(ATR) for vol_factor ---
        self._atr: Optional[float] = None
        self._ema_atr: Optional[float] = None    # EMA of ATR for baseline (vol_factor denominator)
        self._warmup_tr: list[float] = []

        # --- Position tracking ---
        self._position_opened_candle: int = -1

        # --- Reversal crossover state ---
        self._rsi_prev: Optional[float] = None  # RSI value from previous candle (crossover detection)

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

        # Also wait for EMA_ATR to be ready (needs ATR * EMA_ATR_PERIOD samples)
        if self._ema_atr is None or self._atr is None:
            return

        # ------------------------------------------------------------------
        # DYNAMIC RSI THRESHOLDS via vol_factor
        # ------------------------------------------------------------------
        # vol_factor > 1 → more volatile than average → widen thresholds
        # vol_factor < 1 → quieter than average       → tighten thresholds
        vol_factor = self._atr / self._ema_atr
        adj = 8.0 * (vol_factor - 1.0)
        dyn_oversold   = self.OVERSOLD   - adj   # lower in high-vol (easier to reach)
        dyn_overbought = self.OVERBOUGHT + adj   # higher in high-vol (easier to reach)
        # Exit levels derived from dynamic entry levels
        exit_long  = dyn_oversold   + 20.0
        exit_short = dyn_overbought - 20.0

        # Get current position: positive = LONG, negative = SHORT, 0 = flat
        position = await self.engine.get_balance(self.name, "POSITION")

        self.logger.debug(
            f"close={close:.2f}  RSI={rsi:.1f}  "
            f"EMA50={self._ema_fast:.2f}  EMA200={self._ema_slow:.2f}  "
            f"ATR%={self._atr/close*100:.3f}  vol_factor={vol_factor:.3f}  "
            f"OS={dyn_oversold:.1f}  OB={dyn_overbought:.1f}  pos={position:.6f}"
        )

        # ------------------------------------------------------------------
        # EXIT LOGIC (checked before entry)
        # ------------------------------------------------------------------

        if position > 0:
            candles_held = self._candle_count - self._position_opened_candle
            if rsi > exit_long:
                await self._close_position(close, "SELL", f"RSI exit LONG ({rsi:.1f}>{exit_long:.1f})")
                position = 0
            elif candles_held >= self.MAX_HOLD_CANDLES:
                await self._close_position(close, "SELL", f"Time-stop LONG ({candles_held} candles)")
                position = 0

        elif position < 0:
            candles_held = self._candle_count - self._position_opened_candle
            if rsi < exit_short:
                await self._close_position(close, "BUY", f"RSI exit SHORT ({rsi:.1f}<{exit_short:.1f})")
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

        # Volatility filter: require minimum ATR/price ratio (hardcoded 0.004)
        atr_ok = self._atr / close >= self.ATR_MIN_PCT

        # Reversal entry levels: buffer above oversold / below overbought
        rsi_entry_long  = dyn_oversold  + self.REVERSAL_BUFFER
        rsi_entry_short = dyn_overbought - self.REVERSAL_BUFFER

        # --- RSI crossed UP through rsi_entry_long + bullish trend + enough volatility → LONG ---
        if (self._rsi_prev is not None
                and self._rsi_prev < rsi_entry_long
                and rsi >= rsi_entry_long
                and trend_up and atr_ok):
            result = await self._open_position(close, "BUY", RSI=f"{rsi:.1f}", RSIprev=f"{self._rsi_prev:.1f}")
            if result is not None:
                self._position_opened_candle = self._candle_count

        # --- RSI crossed DOWN through rsi_entry_short + bearish trend + enough volatility → SHORT ---
        elif (self._rsi_prev is not None
                and self._rsi_prev > rsi_entry_short
                and rsi <= rsi_entry_short
                and trend_down and atr_ok):
            result = await self._open_position(close, "SELL", RSI=f"{rsi:.1f}", RSIprev=f"{self._rsi_prev:.1f}")
            if result is not None:
                self._position_opened_candle = self._candle_count

        # Save RSI for next candle's crossover detection
        self._rsi_prev = rsi

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
        EMA_ATR = standard EMA of ATR values (k = 2/(EMA_ATR_PERIOD+1)).

        Warmup: collect ATR_PERIOD True Range values, seed ATR with their simple average.
        EMA_ATR seeds on the first ATR value and then updates every candle.
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
                # Seed EMA_ATR with the first ATR value
                self._ema_atr = self._atr
        else:
            alpha_atr = 1.0 / self.ATR_PERIOD
            self._atr = alpha_atr * tr + (1 - alpha_atr) * self._atr
            # Update EMA(ATR, 50) — standard EMA formula
            k_ema_atr = 2.0 / (self.EMA_ATR_PERIOD + 1)
            self._ema_atr = self._atr * k_ema_atr + self._ema_atr * (1 - k_ema_atr)

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
