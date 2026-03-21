"""
RSI Bot — Wilder's RSI с фильтрами режима и волатильности.

Работает на 15-МИНУТНЫХ СВЕЧАХ. USDT-маржинальные бессрочные фьючерсы с плечом.

Концепция (mean-reversion):
    RSI — инструмент возврата к среднему. Стратегия открывает позиции когда
    RSI находится в экстремальных зонах и ждёт восстановления к 50.

Вход (crossover):
    LONG:  RSI пересёк OVERSOLD снизу вверх (prev_rsi < OVERSOLD and rsi >= OVERSOLD)
    SHORT: RSI пересёк OVERBOUGHT сверху вниз (prev_rsi > OVERBOUGHT and rsi <= OVERBOUGHT)

Выход:
    1. RSI восстановился до 50:
       exit LONG:  RSI > 50
       exit SHORT: RSI < 50
    2. Time-stop: MAX_HOLD_CANDLES свечей (защита от runaway drawdown)

Фильтры входа (не блокируют выход):

  1. EMA200 proximity (ATR-based):
       distance  = abs(close - ema200)
       threshold = EMA200_ATR_K * atr
       distance_ok = distance < threshold
       Смысл: не входить когда цена сильно оторвалась от EMA200 в единицах
       текущей волатильности. Порог адаптируется к режиму рынка.

  2. ATR volatility:
       vol_ratio = atr / ema_atr
       vol_ok = vol_ratio > VOL_RATIO_MIN
       Смысл: не входить в «мёртвый» рынок, торговать только при
       волатильности выше среднего.

  3. EMA slope (flatness):
       ema200_n  = EMA200 значение EMA_SLOPE_LOOKBACK свечей назад
       ema_slope = abs(ema200 - ema200_n) / ema200
       slope_ok  = ema_slope <= EMA_SLOPE_THRESHOLD
       Смысл: RSI mean-reversion работает только на flat рынке.
       При крутом наклоне EMA200 (тренд) вход блокируется.

Оптимизируемые параметры:
    RSI_PERIOD   7–21    RSI lookback (Wilder)
    OVERSOLD     15–35   Порог входа LONG
    OVERBOUGHT   65–85   Порог входа SHORT
    EMA200_ATR_K 1.0–4.0 Порог расстояния от EMA200 в единицах ATR

Настраиваемые в UI (не оптимизируются):
    MAX_HOLD_CANDLES   = 50    Time-stop (~12 часов на 15м свечах)
    COOLDOWN_CANDLES   = 5     Минимум свечей между сделками (~75 мин)
    VOL_RATIO_MIN      = 1.0   Мин. ATR/EMA_ATR для входа
    EMA_SLOPE_LOOKBACK = 10    Свечей назад для расчёта наклона EMA200
    EMA_SLOPE_THRESHOLD= 0.001 Макс. допустимый наклон EMA200

Фиксированные (не меняются):
    EMA_SLOW_PERIOD  = 200  (EMA200 как тренд-якорь)
    ATR_PERIOD       = 14   (Wilder ATR)
    EMA_ATR_PERIOD   = 20   (EMA(ATR) как baseline волатильности)

Warmup:
    Торговля начинается после инициализации EMA200 (200 свечей).
    RSI, ATR и EMA(ATR) инициализируются параллельно.

Использование:
    from strategies.rsi import RSIBot
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

    # --- Фиксированные периоды индикаторов (не оптимизируются) ---
    EMA_SLOW_PERIOD = 200   # EMA200 как тренд-якорь и proximity-фильтр
    ATR_PERIOD      = 14    # Wilder ATR (True Range)
    EMA_ATR_PERIOD  = 20    # период EMA(ATR) для baseline волатильности

    # --- Оптимизируемые параметры ---
    RSI_PERIOD   = 14    # Wilder RSI lookback
    OVERSOLD     = 25.0  # Порог входа LONG
    OVERBOUGHT   = 75.0  # Порог входа SHORT
    EMA200_ATR_K = 2.0   # Proximity filter: entry blocked when abs(close-EMA200) > k*ATR

    # --- Настраиваемые в UI (не оптимизируются) ---
    MAX_HOLD_CANDLES    = 50     # Time-stop (~12 часов на 15м)
    COOLDOWN_CANDLES    = 5      # Минимум свечей между сделками
    VOL_RATIO_MIN       = 1.0    # Мин. ATR/EMA_ATR для входа
    EMA_SLOPE_LOOKBACK  = 10     # Свечей назад для расчёта наклона EMA200
    EMA_SLOPE_THRESHOLD = 0.001  # Макс. slope = abs(ema200 - ema200_n) / ema200
    TRADE_FRACTION      = 1.0    # Доля баланса на сделку

    PARAM_SCHEMA = {
        "RSI_PERIOD": {
            "type": "int", "default": 14, "min": 7, "max": 21,
            "description": "RSI lookback window (Wilder EMA)",
        },
        "OVERSOLD": {
            "type": "float", "default": 25.0, "min": 15.0, "max": 35.0,
            "description": "Long entry threshold — RSI crosses above this level",
        },
        "OVERBOUGHT": {
            "type": "float", "default": 75.0, "min": 65.0, "max": 85.0,
            "description": "Short entry threshold — RSI crosses below this level",
        },
        "EMA200_ATR_K": {
            "type": "float", "default": 2.0, "min": 1.0, "max": 4.0,
            "description": "EMA200 proximity filter: entry blocked when abs(close-EMA200) > k*ATR",
        },
        "MAX_HOLD_CANDLES": {
            "type": "int", "default": 50, "min": 5, "max": 200,
            "description": "Force-close after this many candles (time-stop)",
            "optimize": False,
        },
        "COOLDOWN_CANDLES": {
            "type": "int", "default": 5, "min": 1, "max": 20,
            "description": "Minimum candles between entries (1 candle = 15 min)",
            "optimize": False,
        },
        "VOL_RATIO_MIN": {
            "type": "float", "default": 1.0, "min": 0.5, "max": 2.0,
            "description": "Volatility filter: entry blocked when ATR/EMA_ATR < this threshold",
            "optimize": False,
        },
        "EMA_SLOPE_LOOKBACK": {
            "type": "int", "default": 10, "min": 2, "max": 50,
            "description": "Candles back for EMA200 slope calculation (ema200_n)",
            "optimize": False,
        },
        "EMA_SLOPE_THRESHOLD": {
            "type": "float", "default": 0.001, "min": 0.0001, "max": 0.01,
            "description": "Max EMA200 slope: abs(ema200-ema200_n)/ema200. Above = trending → skip",
            "optimize": False,
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

        # --- Wilder RSI state ---
        self._avg_gain: Optional[float] = None
        self._avg_loss: Optional[float] = None
        self._prev_close: Optional[float] = None
        self._warmup_closes: list[float] = []
        self._rsi_prev: Optional[float] = None   # RSI предыдущей свечи для crossover

        # --- EMA200 (anchor для фильтров режима и наклона) ---
        self._ema_slow: Optional[float] = None
        self._ema_warmup: list[float] = []
        self._ema200_history: list[float] = []   # rolling buffer для slope-фильтра

        # --- ATR + EMA(ATR) (фильтры волатильности и proximity) ---
        self._atr: Optional[float] = None
        self._ema_atr: Optional[float] = None
        self._warmup_tr: list[float] = []

        # --- Состояние позиции ---
        self._position_opened_candle: int = -1

    def set_params(self, updates: dict) -> dict:
        """Сбросить RSI state при изменении RSI_PERIOD."""
        applied = super().set_params(updates)
        if "RSI_PERIOD" in applied:
            self._avg_gain = None
            self._avg_loss = None
            self._prev_close = None
            self._warmup_closes = []
            self._rsi_prev = None
            self.logger.info(
                f"RSI_PERIOD изменён на {self.RSI_PERIOD}: "
                "Wilder RSI state сброшен — переинициализация"
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

        # Снимаем prev_close ДО обновления ATR
        prev_close_snapshot = self._prev_close

        # --- Обновление индикаторов ---
        self._update_ema_slow(close)
        self._update_atr(high, low, close)
        self._update_rsi(close, prev_close_snapshot)

        # --- Warmup guard: ждём EMA200 ---
        if self._ema_slow is None:
            return

        # RSI и ATR тоже должны быть готовы
        rsi = self._compute_rsi()
        if rsi is None or self._atr is None or self._ema_atr is None:
            return

        # Сохраняем rsi до любых ранних return (для crossover на следующей свечe)
        rsi_prev_snapshot = self._rsi_prev
        self._rsi_prev = rsi

        # --- Текущая позиция ---
        position = await self.engine.get_balance(self.name, "POSITION")

        # ------------------------------------------------------------------
        # FILTER 1 — EMA200 PROXIMITY (ATR-based)
        # distance = abs(close - EMA200);  entry blocked when distance > k * ATR
        # ------------------------------------------------------------------
        distance    = abs(close - self._ema_slow)
        threshold   = self.EMA200_ATR_K * self._atr
        distance_ok = distance < threshold

        # ------------------------------------------------------------------
        # FILTER 2 — VOLATILITY: торгуем только когда волатильность выше нормы
        # vol_ratio = ATR / EMA(ATR)  > VOL_RATIO_MIN
        # ------------------------------------------------------------------
        vol_ratio = self._atr / self._ema_atr if self._ema_atr > 0 else 0.0
        vol_ok    = vol_ratio > self.VOL_RATIO_MIN

        # self.logger.debug(
        #     f"close={close:.2f}  RSI={rsi:.1f}  "
        #     f"EMA200={self._ema_slow:.2f}  dist_ok={distance_ok}  "
        #     f"vol_ratio={vol_ratio:.2f}  vol_ok={vol_ok}  pos={position:.6f}"
        # )

        # ------------------------------------------------------------------
        # EXIT LOGIC (проверяем до входа, независимо от фильтров)
        # ------------------------------------------------------------------

        if position > 0:
            candles_held = self._candle_count - self._position_opened_candle
            if rsi > 50.0:
                await self._close_position(close, "SELL", f"RSI exit LONG ({rsi:.1f}>50)")
                position = 0
            elif candles_held >= self.MAX_HOLD_CANDLES:
                await self._close_position(close, "SELL", f"Time-stop LONG ({candles_held} свечей)")
                position = 0

        elif position < 0:
            candles_held = self._candle_count - self._position_opened_candle
            if rsi < 50.0:
                await self._close_position(close, "BUY", f"RSI exit SHORT ({rsi:.1f}<50)")
                position = 0
            elif candles_held >= self.MAX_HOLD_CANDLES:
                await self._close_position(close, "BUY", f"Time-stop SHORT ({candles_held} свечей)")
                position = 0

        # ------------------------------------------------------------------
        # ENTRY LOGIC: crossover + cooldown + все фильтры
        # ------------------------------------------------------------------

        cooldown_ok = (self._candle_count - self._last_trade_candle >= self.COOLDOWN_CANDLES)
        if not cooldown_ok or position != 0:
            return

        if not distance_ok or not vol_ok:
            return

        # ------------------------------------------------------------------
        # FILTER 3 — EMA SLOPE: mean-reversion работает только на flat рынке
        # ema_slope = abs(ema200 - ema200_n) / ema200 ≤ EMA_SLOPE_THRESHOLD
        # ------------------------------------------------------------------
        if len(self._ema200_history) >= self.EMA_SLOPE_LOOKBACK:
            ema200_n  = self._ema200_history[-self.EMA_SLOPE_LOOKBACK]
            ema_slope = abs(self._ema_slow - ema200_n) / self._ema_slow
            slope_ok  = ema_slope <= self.EMA_SLOPE_THRESHOLD
        else:
            slope_ok = False   # ждём накопления EMA_SLOPE_LOOKBACK свечей истории

        if not slope_ok:
            return

        # --- RSI crossover entry ---
        if rsi_prev_snapshot is not None:
            if rsi_prev_snapshot < self.OVERSOLD and rsi >= self.OVERSOLD:
                # RSI вышел из зоны перепроданности снизу вверх → LONG
                result = await self._open_position(
                    close, "BUY",
                    RSI=f"{rsi:.1f}", RSIprev=f"{rsi_prev_snapshot:.1f}",
                    dist=f"{distance:.4f}", vol=f"{vol_ratio:.2f}"
                )
                if result is not None:
                    self._position_opened_candle = self._candle_count
                    self.logger.info(f"LONG crossover: RSI {rsi_prev_snapshot:.1f} → {rsi:.1f} (crossed {self.OVERSOLD})")

            elif rsi_prev_snapshot > self.OVERBOUGHT and rsi <= self.OVERBOUGHT:
                # RSI вышел из зоны перекупленности сверху вниз → SHORT
                result = await self._open_position(
                    close, "SELL",
                    RSI=f"{rsi:.1f}", RSIprev=f"{rsi_prev_snapshot:.1f}",
                    dist=f"{distance:.4f}", vol=f"{vol_ratio:.2f}"
                )
                if result is not None:
                    self._position_opened_candle = self._candle_count
                    self.logger.info(f"SHORT crossover: RSI {rsi_prev_snapshot:.1f} → {rsi:.1f} (crossed {self.OVERBOUGHT})")

    # ------------------------------------------------------------------
    # EMA200 (anchor для proximity + slope фильтров)
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

        # Ведём скользящий буфер для slope-фильтра
        if self._ema_slow is not None:
            self._ema200_history.append(self._ema_slow)
            max_hist = self.EMA_SLOPE_LOOKBACK + 1
            if len(self._ema200_history) > max_hist:
                self._ema200_history = self._ema200_history[-max_hist:]

    # ------------------------------------------------------------------
    # ATR + EMA(ATR) (фильтры волатильности и proximity)
    # ------------------------------------------------------------------

    def _update_atr(self, high: float, low: float, close: float) -> None:
        """
        True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
        ATR        = Wilder EMA of TR (alpha = 1/ATR_PERIOD)
        EMA(ATR)   = стандартная EMA(ATR, EMA_ATR_PERIOD) — baseline для vol_ratio
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
                self._ema_atr = self._atr
        else:
            alpha = 1.0 / self.ATR_PERIOD
            self._atr = alpha * tr + (1 - alpha) * self._atr
            k = 2.0 / (self.EMA_ATR_PERIOD + 1)
            self._ema_atr = self._atr * k + self._ema_atr * (1 - k)

    # ------------------------------------------------------------------
    # Wilder RSI
    # ------------------------------------------------------------------

    def _update_rsi(self, close: float, prev_close: Optional[float] = None) -> None:
        if self._avg_gain is None:
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
            f"Wilder RSI({self.RSI_PERIOD}) инициализирован: "
            f"avg_gain={self._avg_gain:.6f}  avg_loss={self._avg_loss:.6f}"
        )

    def _update_wilder_rsi(self, close: float, prev_close: Optional[float] = None) -> None:
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
