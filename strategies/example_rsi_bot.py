"""
RSI Bot — Wilder's RSI с EMA200 макро-фильтром и ATR фильтром волатильности.

Работает на 5-МИНУТНЫХ СВЕЧАХ. USDT-маржинальные бессрочные фьючерсы с плечом.

Концепция (mean-reversion):
    RSI — инструмент возврата к среднему. Стратегия открывает позиции когда
    RSI находится в экстремальных зонах и ждёт восстановления к 50.

Логика входа (3 условия):
    1. RSI пересекает порог в нужном направлении (crossover prev→now)
    2. Цена выше EMA200 → только LONG; ниже EMA200 → только SHORT
       (защита от торговли против сильного тренда)
    3. ATR / price >= ATR_MIN_PCT (минимальная волатильность для работы mean-reversion)
    4. Прошёл cooldown с последней сделки

    Пороги входа (статические, без динамической адаптации):
        LONG:  RSI пересёк OVERSOLD снизу вверх (rsi_prev < OVERSOLD, rsi_now >= OVERSOLD)
               И close > EMA200
        SHORT: RSI пересёк OVERBOUGHT сверху вниз (rsi_prev > OVERBOUGHT, rsi_now <= OVERBOUGHT)
               И close < EMA200

Логика выхода:
    1. RSI восстановился до 50 — основной выход:
       exit LONG:  RSI > 50
       exit SHORT: RSI < 50
    2. Time-stop: MAX_HOLD_CANDLES свечей без восстановления (защита от runaway drawdown)

Warmup:
    Торговля начинается после инициализации EMA200 (200 свечей).
    RSI и ATR инициализируются параллельно в течение warmup.

Индикаторы (все периоды фиксированы, не оптимизируются):
    EMA_SLOW_PERIOD = 200  (макро-фильтр тренда)
    ATR_PERIOD      = 14   (волатильность, True Range)
    ATR_MIN_PCT     = 0.004 (минимальный ATR/price, фиксировано)

ATR:
    True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
    ATR = Wilder EMA of TR (alpha = 1/14)

Оптимизируемые параметры (только 3 — минимальное пространство поиска):
    RSI_PERIOD   7–21    RSI lookback (Wilder)
    OVERSOLD     20–35   Порог входа LONG
    OVERBOUGHT   65–80   Порог входа SHORT

Фиксированные параметры (не оптимизируются):
    MAX_HOLD_CANDLES = 50   Time-stop (~4 часа на 5м свечах)
    COOLDOWN_CANDLES = 3    Минимум свечей между сделками

Использование:
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

    # --- Фиксированные периоды индикаторов (не оптимизируются) ---
    EMA_SLOW_PERIOD = 200    # warmup guard: торговля только после EMA200
    ATR_PERIOD      = 14     # Wilder ATR для фильтра волатильности
    ATR_MIN_PCT     = 0.004  # минимальный ATR/price (пропускаем низковолатильные свечи)

    # --- Оптимизируемые параметры стратегии ---
    RSI_PERIOD       = 14    # Wilder RSI lookback
    OVERSOLD         = 30.0  # Порог входа LONG
    OVERBOUGHT       = 70.0  # Порог входа SHORT

    # --- Фиксированные параметры управления позицией (не оптимизируются) ---
    MAX_HOLD_CANDLES = 50    # Time-stop (~4 часа на 5м свечах)
    COOLDOWN_CANDLES = 3     # Минимум свечей между сделками
    TRADE_FRACTION   = 1.0   # Доля баланса на сделку

    PARAM_SCHEMA = {
        "RSI_PERIOD": {
            "type": "int", "default": 14, "min": 7, "max": 21,
            "description": "RSI lookback window (Wilder EMA)",
        },
        "OVERSOLD": {
            "type": "float", "default": 30.0, "min": 20.0, "max": 35.0,
            "description": "Long entry threshold — RSI crosses above this level",
        },
        "OVERBOUGHT": {
            "type": "float", "default": 70.0, "min": 65.0, "max": 80.0,
            "description": "Short entry threshold — RSI crosses below this level",
        },
        "MAX_HOLD_CANDLES": {
            "type": "int", "default": 50, "min": 20, "max": 100,
            "description": "Force-close after this many candles (time-stop)",
            "optimize": False,
        },
        "COOLDOWN_CANDLES": {
            "type": "int", "default": 3, "min": 1, "max": 10,
            "description": "Minimum candles between entries",
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

        # --- EMA200 (макро-фильтр тренда) ---
        self._ema_slow: Optional[float] = None
        self._ema_warmup: list[float] = []

        # --- ATR (фильтр волатильности) ---
        self._atr: Optional[float] = None
        self._warmup_tr: list[float] = []

        # --- Состояние позиции и кросс-детектор RSI ---
        self._position_opened_candle: int = -1
        self._rsi_prev: Optional[float] = None  # RSI предыдущей свечи для crossover

    def set_params(self, updates: dict) -> dict:
        """Сбросить RSI state при изменении RSI_PERIOD (Wilder alpha зависит от периода)."""
        applied = super().set_params(updates)
        if "RSI_PERIOD" in applied:
            self._avg_gain = None
            self._avg_loss = None
            self._prev_close = None
            self._warmup_closes = []
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

        # Снимаем prev_close ДО обновления ATR (оба индикатора используют одно значение)
        prev_close_snapshot = self._prev_close

        # --- Обновление индикаторов ---
        self._update_ema_slow(close)
        self._update_atr(high, low, close)           # записывает self._prev_close = close
        self._update_rsi(close, prev_close_snapshot)

        # --- Warmup guard: ждём EMA200 ---
        if self._ema_slow is None:
            self.logger.debug(
                f"Warmup: {len(self._ema_warmup)}/{self.EMA_SLOW_PERIOD} свечей"
            )
            return

        # RSI тоже должен быть готов
        rsi = self._compute_rsi()
        if rsi is None:
            return

        # ATR должен быть готов для фильтра волатильности
        if self._atr is None:
            return

        # --- Текущая позиция ---
        position = await self.engine.get_balance(self.name, "POSITION")

        self.logger.debug(
            f"close={close:.2f}  RSI={rsi:.1f}  "
            f"EMA200={self._ema_slow:.2f}  "
            f"ATR%={self._atr/close*100:.3f}  pos={position:.6f}"
        )

        # ------------------------------------------------------------------
        # EXIT LOGIC (проверяем до входа)
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
        # ENTRY LOGIC: RSI crossover + EMA200 тренд + ATR фильтр
        # ------------------------------------------------------------------

        cooldown_ok = (self._candle_count - self._last_trade_candle >= self.COOLDOWN_CANDLES)
        if not cooldown_ok or position != 0:
            # Сохраняем RSI для следующей свечи
            self._rsi_prev = rsi
            return

        # Фильтр волатильности: ATR/price >= ATR_MIN_PCT
        atr_ok = (self._atr / close >= self.ATR_MIN_PCT)

        # Макро-фильтр тренда через EMA200
        price_above_ema = close > self._ema_slow
        price_below_ema = close < self._ema_slow

        # RSI crossover entry: prev < threshold, now >= threshold (или наоборот)
        if (self._rsi_prev is not None
                and self._rsi_prev < self.OVERSOLD
                and rsi >= self.OVERSOLD
                and price_above_ema
                and atr_ok):
            # RSI вышел из зоны перепроданности + цена выше EMA200 → LONG
            result = await self._open_position(
                close, "BUY",
                RSI=f"{rsi:.1f}", RSIprev=f"{self._rsi_prev:.1f}"
            )
            if result is not None:
                self._position_opened_candle = self._candle_count

        elif (self._rsi_prev is not None
                and self._rsi_prev > self.OVERBOUGHT
                and rsi <= self.OVERBOUGHT
                and price_below_ema
                and atr_ok):
            # RSI вышел из зоны перекупленности + цена ниже EMA200 → SHORT
            result = await self._open_position(
                close, "SELL",
                RSI=f"{rsi:.1f}", RSIprev=f"{self._rsi_prev:.1f}"
            )
            if result is not None:
                self._position_opened_candle = self._candle_count

        # Сохраняем RSI для следующей свечи
        self._rsi_prev = rsi

    # ------------------------------------------------------------------
    # EMA200 (макро-фильтр тренда)
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
                # Seed через SMA первых 200 свечей
                self._ema_slow = sum(self._ema_warmup[:self.EMA_SLOW_PERIOD]) / self.EMA_SLOW_PERIOD
                self._ema_warmup.clear()
                self.logger.info(f"EMA{self.EMA_SLOW_PERIOD}={self._ema_slow:.2f} готов — торговля разрешена")
        else:
            self._ema_slow = close * k + self._ema_slow * (1 - k)

    # ------------------------------------------------------------------
    # ATR (фильтр волатильности)
    # ------------------------------------------------------------------

    def _update_atr(self, high: float, low: float, close: float) -> None:
        """
        True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
        ATR = Wilder EMA of TR (alpha = 1/ATR_PERIOD).
        Используется только для фильтра ATR_MIN_PCT.
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
        """Буферизует closes для warmup RSI; после инициализации — Wilder EMA на каждой свече."""
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
