"""
RSI Bot — Wilder's RSI с двумя нормализованными фильтрами (нет hardcoded порогов).

Работает на 5-МИНУТНЫХ СВЕЧАХ. USDT-маржинальные бессрочные фьючерсы с плечом.

Концепция (mean-reversion):
    RSI — инструмент возврата к среднему. Стратегия открывает позиции когда
    RSI находится в экстремальных зонах и ждёт восстановления к 50.

Два фильтра (оба нормализованные, не зависят от актива/эпохи):

1. Regime filter — "рынок не в тренде":
   distance = |close - EMA200| / EMA200 < EMA_DISTANCE_THRESHOLD (1.5%)
   Когда цена далеко от EMA200 → рынок trending → RSI ненадёжен.
   Это НЕ directional filter: LONG и SHORT оба разрешены.

2. Volatility filter — "рынок не мёртвый":
   vol_ratio = ATR / EMA(ATR, 50)
   vol_ok = vol_ratio > 1.0  (текущая волатильность выше средней)
   Нормализован относительно собственной истории → не требует подгонки под актив.

   Принцип:
     vol_ratio > 1.0 → волатильнее нормы → есть ход для mean-reversion
     vol_ratio < 1.0 → тише нормы → RSI-сигналы = шум

Логика входа (crossover + оба фильтра):
    LONG:  RSI пересёк OVERSOLD снизу вверх + regime_ok + vol_ok
    SHORT: RSI пересёк OVERBOUGHT сверху вниз + regime_ok + vol_ok

Логика выхода:
    1. RSI восстановился до 50:
       exit LONG:  RSI > 50
       exit SHORT: RSI < 50
    2. Time-stop: MAX_HOLD_CANDLES свечей (защита от runaway drawdown)

Warmup:
    Торговля начинается после инициализации EMA200 (200 свечей).
    RSI, ATR и EMA(ATR) инициализируются параллельно.

Индикаторы (все периоды фиксированы, не оптимизируются):
    EMA_SLOW_PERIOD        = 200   (anchor для regime-фильтра)
    ATR_PERIOD             = 14    (волатильность, Wilder True Range)
    EMA_ATR_PERIOD         = 50    (baseline ATR для нормализации vol_ratio)
    EMA_DISTANCE_THRESHOLD = 0.015 (порог distance до EMA200, 1.5%)

ATR:
    True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
    ATR        = Wilder EMA of TR (alpha = 1/14)
    EMA(ATR)   = стандартная EMA(ATR, 50) — baseline для vol_ratio

Оптимизируемые параметры (только 3):
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
    EMA_SLOW_PERIOD        = 200    # anchor: EMA200 для distance-фильтра
    ATR_PERIOD             = 14     # Wilder ATR (True Range)
    EMA_ATR_PERIOD         = 50     # период EMA(ATR) для baseline волатильности
    EMA_DISTANCE_THRESHOLD = 0.015  # макс. дистанция до EMA200 (1.5%) для mean-reversion режима

    # --- Оптимизируемые параметры стратегии ---
    RSI_PERIOD   = 14    # Wilder RSI lookback
    OVERSOLD     = 30.0  # Порог входа LONG
    OVERBOUGHT   = 70.0  # Порог входа SHORT

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

        # --- EMA200 (anchor для regime-фильтра) ---
        self._ema_slow: Optional[float] = None
        self._ema_warmup: list[float] = []

        # --- ATR + EMA(ATR) (относительный фильтр волатильности) ---
        self._atr: Optional[float] = None
        self._ema_atr: Optional[float] = None  # EMA(ATR) — baseline для vol_ratio
        self._warmup_tr: list[float] = []

        # --- Состояние позиции и кросс-детектор RSI ---
        self._position_opened_candle: int = -1
        self._rsi_prev: Optional[float] = None  # RSI предыдущей свечи для crossover

    def set_params(self, updates: dict) -> dict:
        """Сбросить RSI state при изменении RSI_PERIOD."""
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

        # Снимаем prev_close ДО обновления ATR
        prev_close_snapshot = self._prev_close

        # --- Обновление индикаторов ---
        self._update_ema_slow(close)
        self._update_atr(high, low, close)
        self._update_rsi(close, prev_close_snapshot)

        # --- Warmup guard: ждём EMA200 ---
        if self._ema_slow is None:
            self.logger.debug(
                f"Warmup: {len(self._ema_warmup)}/{self.EMA_SLOW_PERIOD} свечей"
            )
            return

        # RSI и ATR тоже должны быть готовы
        rsi = self._compute_rsi()
        if rsi is None or self._atr is None:
            return

        # Также ждём EMA(ATR) для vol_ratio
        if self._ema_atr is None:
            return

        # ------------------------------------------------------------------
        # FILTER 1 — REGIME: mean-reversion работает когда цена близко к EMA200
        # distance = |close - EMA200| / EMA200
        # Нормализован: не зависит от цены актива, работает везде одинаково
        # ------------------------------------------------------------------
        distance = abs(close - self._ema_slow) / self._ema_slow
        regime_ok = distance < self.EMA_DISTANCE_THRESHOLD

        # ------------------------------------------------------------------
        # FILTER 2 — VOLATILITY: торгуем только когда волатильность выше нормы
        # vol_ratio = ATR / EMA(ATR) — нормализован относительно своей истории
        # > 1.0 → сейчас волатильнее нормы → есть ход для mean-reversion
        # < 1.0 → тише нормы → сигналы RSI = шум
        # ------------------------------------------------------------------
        vol_ratio = self._atr / self._ema_atr
        vol_ok = vol_ratio > 1.0

        # --- Текущая позиция ---
        position = await self.engine.get_balance(self.name, "POSITION")

        self.logger.debug(
            f"close={close:.2f}  RSI={rsi:.1f}  "
            f"EMA200={self._ema_slow:.2f}  dist={distance*100:.2f}%  "
            f"vol_ratio={vol_ratio:.2f}  "
            f"regime={'✓' if regime_ok else '✗'}  vol={'✓' if vol_ok else '✗'}  pos={position:.6f}"
        )

        # ------------------------------------------------------------------
        # EXIT LOGIC (проверяем до входа, независимо от режима)
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
        # ENTRY LOGIC: RSI crossover + regime filter + ATR filter
        # ------------------------------------------------------------------

        cooldown_ok = (self._candle_count - self._last_trade_candle >= self.COOLDOWN_CANDLES)
        if not cooldown_ok or position != 0:
            self._rsi_prev = rsi
            return

        # Оба фильтра должны быть выполнены
        if not regime_ok or not vol_ok:
            self._rsi_prev = rsi
            return

        if self._rsi_prev is not None:
            if self._rsi_prev < self.OVERSOLD and rsi >= self.OVERSOLD:
                # RSI вышел из зоны перепроданности → LONG
                result = await self._open_position(
                    close, "BUY",
                    RSI=f"{rsi:.1f}", RSIprev=f"{self._rsi_prev:.1f}",
                    dist=f"{distance*100:.1f}%", vol=f"{vol_ratio:.2f}"
                )
                if result is not None:
                    self._position_opened_candle = self._candle_count

            elif self._rsi_prev > self.OVERBOUGHT and rsi <= self.OVERBOUGHT:
                # RSI вышел из зоны перекупленности → SHORT
                result = await self._open_position(
                    close, "SELL",
                    RSI=f"{rsi:.1f}", RSIprev=f"{self._rsi_prev:.1f}",
                    dist=f"{distance*100:.1f}%", vol=f"{vol_ratio:.2f}"
                )
                if result is not None:
                    self._position_opened_candle = self._candle_count

        self._rsi_prev = rsi

    # ------------------------------------------------------------------
    # EMA200 (anchor для regime-фильтра)
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
    # ATR (фильтр волатильности)
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
                # Seed EMA(ATR) первым значением ATR
                self._ema_atr = self._atr
        else:
            alpha = 1.0 / self.ATR_PERIOD
            self._atr = alpha * tr + (1 - alpha) * self._atr
            # Обновляем EMA(ATR, 50) — baseline для vol_ratio = ATR / EMA(ATR)
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
