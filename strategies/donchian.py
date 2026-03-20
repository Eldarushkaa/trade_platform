"""
Donchian Breakout Bot — классическая трендовая система (Turtle Trading), trend-following.

Работает на 15-МИНУТНЫХ СВЕЧАХ. USDT-маржинальные бессрочные фьючерсы с плечом.

Концепция (trend-following):
    Если цена пробивает максимум/минимум N последних свечей — начинается движение.
    Входим по факту пробоя, выходим когда цена пробивает M-периодный обратный канал.

Канал (ВАЖНО: текущая свеча НЕ включается):
    high_N = max(high[i-N : i])   # максимум N свечей до текущей
    low_N  = min(low[i-N  : i])   # минимум N свечей до текущей

Вход:
    LONG:  close > high_N  (пробой максимума вверх)
    SHORT: close < low_N   (пробой минимума вниз)

Выход (Turtle / Donchian exit):
    exit_low  = min(low[i-M  : i])   # выход из LONG когда close < exit_low
    exit_high = max(high[i-M : i])   # выход из SHORT когда close > exit_high

Фильтры (вход):
  1. Volatility: vol_ratio = ATR / EMA(ATR) > VOL_RATIO_MIN
     Смысл: не входить на «мёртвом» рынке.

  2. EMA200 proximity (ATR-based):
     distance  = abs(close - EMA200)
     threshold = EMA200_ATR_K * ATR
     distance_ok = distance < threshold
     Смысл: breakout работает лучше когда цена недалеко от долгосрочного тренда
     (фильтр подтверждает что пробой — это действительно начало движения, а не
     возврат после экстремального отклонения).

Оптимизируемые параметры:
    N_PERIOD     20–60   Период канала входа (breakout lookback)
    M_PERIOD     10–30   Период канала выхода (exit lookback)
    EMA200_ATR_K 1.0–4.0 Порог расстояния от EMA200 в единицах ATR

Настраиваемые в UI (не оптимизируются):
    VOL_RATIO_MIN  = 1.0   Мин. ATR/EMA_ATR для входа
    TRADE_FRACTION = 1.0   Доля баланса на сделку

Фиксированные:
    EMA_SLOW_PERIOD = 200  EMA200 как тренд-якорь и proximity-фильтр
    ATR_PERIOD      = 14   Wilder ATR
    EMA_ATR_PERIOD  = 20   EMA(ATR) для baseline волатильности
    HISTORY_MAX     = 80   Размер буфера OHLC истории (≥ max N + запас)

Warmup:
    Торговля начинается когда EMA200 готова (200 свечей) + ATR готов + накоплена история.

Использование:
    from strategies.donchian import DonchianBot
    REGISTERED_BOTS = [
        DonchianBot.for_symbol("BTCUSDT"),
        DonchianBot.for_symbol("ETHUSDT"),
        DonchianBot.for_symbol("SOLUSDT"),
    ]
"""
import math
from typing import Optional, TYPE_CHECKING

from core.base_strategy import BaseStrategy
from core.simulation_engine import BaseOrderEngine

if TYPE_CHECKING:
    from data.candle_aggregator import Candle


class DonchianBot(BaseStrategy):
    name_prefix = "donchian"
    name = "donchian_bot"
    symbol = "BTCUSDT"

    # --- Фиксированные периоды индикаторов ---
    EMA_SLOW_PERIOD = 200  # EMA200 как тренд-якорь (proximity-фильтр)
    ATR_PERIOD      = 14   # Wilder ATR
    EMA_ATR_PERIOD  = 20   # EMA(ATR) — baseline для vol_ratio
    HISTORY_MAX     = 80   # Глубина буфера OHLC истории (≥ max N_PERIOD)

    # --- Оптимизируемые параметры ---
    N_PERIOD     = 20   # Breakout lookback (вход)
    M_PERIOD     = 10   # Exit lookback (выход)
    EMA200_ATR_K = 2.0  # Proximity filter: entry blocked when abs(close-EMA200) > k*ATR

    # --- Настраиваемые в UI (не оптимизируются) ---
    VOL_RATIO_MIN  = 1.0   # Мин. ATR/EMA_ATR для входа
    TRADE_FRACTION = 1.0   # Доля баланса на сделку

    # ------------------------------------------------------------------
    # Fitness overrides — trend-following objectives differ from RSI
    # ------------------------------------------------------------------

    @classmethod
    def compute_fitness(
        cls,
        sharpe: float,
        return_pct: float,
        max_dd: float,
        trade_count: int,
        profit_factor: float = 1.0,
    ) -> float:
        """
        Trend-following IS fitness for Donchian breakout.

        Breakout systems produce fewer but bigger trades.
        Weights vs default:
          - Return:       40% (need actual profit — rare large wins)
          - Drawdown:     35% (deep DDs kill trend systems)
          - Profit Factor: 20% (directional quality)
          - log(trades):  15% (harder significance floor — breakouts are infrequent)

        Hard filter: < 20 trades → disqualified (lower than RSI since breakout has fewer entries).
        """
        if trade_count < 20:
            return -1000.0 + trade_count

        pf = min(5.0,  max(0.0, profit_factor))
        r  = max(-2.0, min(2.0, return_pct / 100.0))
        dd = abs(max_dd) / 100.0

        return (
            r  * 0.40
            - dd * 0.35
            + pf * 0.20
            + math.log(max(1, trade_count)) * 0.15
        )

    PARAM_SCHEMA = {
        "N_PERIOD": {
            "type": "int", "default": 20, "min": 20, "max": 60,
            "description": "Donchian entry channel period (breakout lookback, excl. current candle)",
        },
        "M_PERIOD": {
            "type": "int", "default": 10, "min": 10, "max": 30,
            "description": "Donchian exit channel period (Turtle exit lookback, excl. current candle)",
        },
        "EMA200_ATR_K": {
            "type": "float", "default": 2.0, "min": 1.0, "max": 4.0,
            "description": "EMA200 proximity filter: entry blocked when abs(close-EMA200) > k*ATR",
        },
        "VOL_RATIO_MIN": {
            "type": "float", "default": 1.0, "min": 0.5, "max": 2.0,
            "description": "Volatility filter: entry blocked when ATR/EMA_ATR < threshold",
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

        # Rolling history of COMPLETED candles (current candle appended AFTER
        # computing channels — this guarantees no lookahead bias)
        self._high_buf: list[float] = []
        self._low_buf:  list[float] = []

        # EMA200 state (proximity filter)
        self._ema_slow:   Optional[float] = None
        self._ema_warmup: list[float] = []

        # ATR + EMA(ATR) state
        self._atr:        Optional[float] = None
        self._ema_atr:    Optional[float] = None
        self._warmup_tr:  list[float] = []
        self._prev_close: Optional[float] = None

    # ------------------------------------------------------------------
    # Main candle handler
    # ------------------------------------------------------------------

    async def on_candle(self, candle: "Candle") -> None:
        self._candle_count += 1
        close = candle.close
        high  = candle.high
        low   = candle.low

        # --- 1. Update EMA200 ---
        self._update_ema_slow(close)

        # --- 2. Update ATR (sets self._prev_close = close) ---
        self._update_atr(high, low, close)

        # --- 3. Warmup guard: wait for EMA200, ATR, and channel history ---
        if self._ema_slow is None or self._atr is None or self._ema_atr is None:
            self._append_history(high, low)
            return

        n = self.N_PERIOD
        m = self.M_PERIOD

        # Need at least N bars of history BEFORE current candle
        if len(self._high_buf) < n:
            self._append_history(high, low)
            return

        # --- 4. Compute Donchian channels (history excludes current candle) ---
        high_N = max(self._high_buf[-n:])
        low_N  = min(self._low_buf[-n:])

        # Exit channel requires M bars too
        if len(self._high_buf) < m:
            self._append_history(high, low)
            return

        exit_high = max(self._high_buf[-m:])
        exit_low  = min(self._low_buf[-m:])

        # --- 5. Filters ---
        # Volatility: trade only when ATR > EMA(ATR) * VOL_RATIO_MIN
        vol_ratio = self._atr / self._ema_atr if self._ema_atr > 0 else 0.0
        vol_ok    = vol_ratio > self.VOL_RATIO_MIN

        # EMA200 proximity: entry blocked when price too far from EMA200
        distance     = abs(close - self._ema_slow)
        distance_ok  = distance < self.EMA200_ATR_K * self._atr

        self.logger.debug(
            f"close={close:.2f}  EMA200={self._ema_slow:.2f}  "
            f"high_N={high_N:.2f}  low_N={low_N:.2f}  "
            f"exit_high={exit_high:.2f}  exit_low={exit_low:.2f}  "
            f"vol_ratio={vol_ratio:.2f}  vol_ok={vol_ok}  dist_ok={distance_ok}"
        )

        # --- 6. Position state ---
        position = await self.engine.get_balance(self.name, "POSITION")

        # --- 7. EXIT LOGIC (checked first, independent of filters) ---
        if position > 0:
            if close < exit_low:
                await self._close_position(
                    close, "SELL",
                    f"Donchian exit LONG (close={close:.2f} < exit_low={exit_low:.2f})"
                )
                position = 0

        elif position < 0:
            if close > exit_high:
                await self._close_position(
                    close, "BUY",
                    f"Donchian exit SHORT (close={close:.2f} > exit_high={exit_high:.2f})"
                )
                position = 0

        # --- 8. ENTRY LOGIC: breakout + vol filter + EMA200 proximity ---
        if position == 0 and vol_ok and distance_ok:
            if close > high_N:
                result = await self._open_position(
                    close, "BUY",
                    N=n, high_N=f"{high_N:.2f}",
                    EMA200=f"{self._ema_slow:.2f}",
                    dist=f"{distance:.4f}",
                    vol=f"{vol_ratio:.2f}"
                )
                if result is not None:
                    self.logger.info(
                        f"LONG breakout: close={close:.2f} > high_N={high_N:.2f}  "
                        f"EMA200={self._ema_slow:.2f}  dist={distance:.2f}  "
                        f"(N={n}, vol={vol_ratio:.2f})"
                    )

            elif close < low_N:
                result = await self._open_position(
                    close, "SELL",
                    N=n, low_N=f"{low_N:.2f}",
                    EMA200=f"{self._ema_slow:.2f}",
                    dist=f"{distance:.4f}",
                    vol=f"{vol_ratio:.2f}"
                )
                if result is not None:
                    self.logger.info(
                        f"SHORT breakout: close={close:.2f} < low_N={low_N:.2f}  "
                        f"EMA200={self._ema_slow:.2f}  dist={distance:.2f}  "
                        f"(N={n}, vol={vol_ratio:.2f})"
                    )

        # --- 9. Append current candle to history AFTER all logic ---
        self._append_history(high, low)

    # ------------------------------------------------------------------
    # EMA200 (proximity filter — trend anchor)
    # ------------------------------------------------------------------

    def _update_ema_slow(self, close: float) -> None:
        """
        EMA200 инициализируется SMA первых 200 свечей,
        затем обновляется по стандартной формуле EMA (k = 2/201).
        Торговля заблокирована пока EMA200 не готова.
        """
        self._ema_warmup.append(close)
        n = len(self._ema_warmup)
        k = 2.0 / (self.EMA_SLOW_PERIOD + 1)

        if self._ema_slow is None:
            if n >= self.EMA_SLOW_PERIOD:
                self._ema_slow = sum(self._ema_warmup[:self.EMA_SLOW_PERIOD]) / self.EMA_SLOW_PERIOD
                self._ema_warmup.clear()
                self.logger.info(f"EMA{self.EMA_SLOW_PERIOD}={self._ema_slow:.2f} готов — торговля разрешена")
        else:
            self._ema_slow = close * k + self._ema_slow * (1 - k)

    # ------------------------------------------------------------------
    # History buffer
    # ------------------------------------------------------------------

    def _append_history(self, high: float, low: float) -> None:
        """Append current candle's high/low to rolling buffers."""
        self._high_buf.append(high)
        self._low_buf.append(low)
        if len(self._high_buf) > self.HISTORY_MAX:
            self._high_buf = self._high_buf[-self.HISTORY_MAX:]
            self._low_buf  = self._low_buf[-self.HISTORY_MAX:]

    # ------------------------------------------------------------------
    # ATR + EMA(ATR)
    # ------------------------------------------------------------------

    def _update_atr(self, high: float, low: float, close: float) -> None:
        """
        True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
        ATR        = Wilder EMA (alpha = 1/ATR_PERIOD)
        EMA(ATR)   = standard EMA(ATR, EMA_ATR_PERIOD) — vol baseline
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
                self._atr     = sum(self._warmup_tr) / self.ATR_PERIOD
                self._ema_atr = self._atr
                self._warmup_tr.clear()
        else:
            alpha = 1.0 / self.ATR_PERIOD
            self._atr = alpha * tr + (1 - alpha) * self._atr
            k = 2.0 / (self.EMA_ATR_PERIOD + 1)
            self._ema_atr = self._atr * k + self._ema_atr * (1 - k)
