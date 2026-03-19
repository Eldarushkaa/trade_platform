"""
RSI Baseline — минимальная эталонная стратегия для проверки edge RSI.

Назначение:
    Нижняя планка перед запуском сложных стратегий. Если эта не работает →
    более сложная тоже не будет.

Правила (классический RSI, уровневый вход):
    Entry LONG:  RSI < OVERSOLD   (каждую свечу пока RSI в зоне, cooldown ограничивает)
    Entry SHORT: RSI > OVERBOUGHT
    Exit LONG:   RSI > 50
    Exit SHORT:  RSI < 50

Фильтры входа (не блокируют выход):
    EMA200 proximity (ATR-based):
        distance  = abs(close - ema200)
        threshold = EMA200_ATR_K * atr          (k ~ 1.5–2.5, default 2.0)
        distance_ok = distance < threshold
        Смысл: не входить когда цена сильно оторвалась от EMA200 в единицах
        текущей волатильности (ATR). Порог адаптируется к режиму рынка.
        Не торгуем пока EMA200 или ATR не прогреты.

    ATR volatility:
        vol_ratio = atr / ema_atr
        vol_ok = vol_ratio > VOL_RATIO_MIN  (default 1.0)
        Смысл: не входить в «мёртвый» рынок, торговать только при
        волатильности выше среднего.
        Не торгуем пока ATR/EMA_ATR не прогреты.

Оптимизируемые параметры:
    OVERSOLD         15–35   (чем ниже → реже и точнее LONG-сигналы)
    OVERBOUGHT       65–85   (чем выше → реже и точнее SHORT-сигналы)
    COOLDOWN_CANDLES 1–20    (свечей между сделками; 10 = 50 мин на 15м)
    EMA200_ATR_K     1.0–5.0 (порог расстояния от EMA200; ниже = строже)
    VOL_RATIO_MIN    0.5–2.0 (мин. ATR/EMA_ATR; выше = только при высокой волатильности)

Фиксированные параметры (не оптимизируются):
    RSI_PERIOD     = 14    (стандарт Уайлдера)
    EMA200_PERIOD  = 200   (тренд-фильтр)
    ATR_PERIOD     = 14    (волатильность)
    EMA_ATR_PERIOD = 20    (базовая волатильность для vol_ratio)

Warmup:
    RSI:     ~RSI_PERIOD+1 свечей
    EMA200:  200 свечей
    ATR:     ATR_PERIOD+1 свечей
    EMA_ATR: сразу после первого ATR

Использование:
    from strategies.rsi_baseline import RSIBaseline
    REGISTERED_BOTS = [RSIBaseline.for_symbol("BTCUSDT")]
"""
from typing import Optional, TYPE_CHECKING

from core.base_strategy import BaseStrategy
from core.simulation_engine import BaseOrderEngine

if TYPE_CHECKING:
    from data.candle_aggregator import Candle


class RSIBaseline(BaseStrategy):
    """RSI(14): уровневый вход BUY<OVERSOLD / SELL>OVERBOUGHT, EXIT at 50.

    Фильтры входа: EMA200 proximity + ATR/EMA_ATR volatility.
    """

    name_prefix = "rsi_baseline"
    name = "rsi_baseline"
    symbol = "BTCUSDT"

    # --- Фиксированные параметры (не оптимизируются) ---
    RSI_PERIOD     = 14
    EMA200_PERIOD  = 200
    ATR_PERIOD     = 14
    EMA_ATR_PERIOD = 20

    # --- Оптимизируемые параметры ---
    OVERSOLD         = 25.0   # порог входа LONG
    OVERBOUGHT       = 75.0   # порог входа SHORT
    COOLDOWN_CANDLES = 10     # минимум свечей между сделками
    EMA200_ATR_K     = 2.0    # порог: distance < k * ATR (1.5 = строго, 4.0 = мягко)
    VOL_RATIO_MIN    = 1.0    # мин. ATR/EMA_ATR для входа (0.5 = почти всегда, 2.0 = только всплески)

    PARAM_SCHEMA = {
        "OVERSOLD": {
            "type": "float", "default": 25.0, "min": 15.0, "max": 35.0,
            "description": "RSI threshold for LONG entry (buy when RSI drops below this)",
        },
        "OVERBOUGHT": {
            "type": "float", "default": 75.0, "min": 65.0, "max": 85.0,
            "description": "RSI threshold for SHORT entry (sell when RSI rises above this)",
        },
        "COOLDOWN_CANDLES": {
            "type": "int", "default": 10, "min": 1, "max": 20,
            "description": "Minimum candles between entries (1 candle = 15 min)",
        },
        "EMA200_ATR_K": {
            "type": "float", "default": 2.0, "min": 1.5, "max": 2.5,
            "description": "EMA200 proximity filter: entry blocked when abs(close-EMA200) > k*ATR",
        },
        "VOL_RATIO_MIN": {
            "type": "float", "default": 1.0, "min": 0.5, "max": 2.0,
            "description": "Volatility filter: entry blocked when ATR/EMA_ATR < this threshold",
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

        # --- EMA200 state ---
        self._ema200: Optional[float] = None
        self._warmup_closes_ema200: list[float] = []

        # --- ATR state ---
        self._atr: Optional[float] = None
        self._prev_high: Optional[float] = None
        self._prev_low: Optional[float] = None
        self._prev_close_atr: Optional[float] = None
        self._warmup_tr: list[float] = []

        # --- EMA of ATR state ---
        self._ema_atr: Optional[float] = None

    # ------------------------------------------------------------------
    # Main candle handler
    # ------------------------------------------------------------------

    async def on_candle(self, candle: "Candle") -> None:
        self._candle_count += 1
        close = candle.close
        high  = candle.high
        low   = candle.low

        # Снимаем prev_close до обновления RSI
        prev_close_snapshot = self._prev_close
        self._prev_close = close

        # Обновляем все индикаторы
        self._update_rsi(close, prev_close_snapshot)
        self._update_ema200(close)
        self._update_atr(high, low, close)

        # Ждём инициализации RSI
        rsi = self._compute_rsi()
        if rsi is None:
            return

        # Текущая позиция
        position = await self.engine.get_balance(self.name, "POSITION")

        # Считаем фильтры (None = ещё не прогрет → блокируем вход)
        ema200 = self._ema200
        atr    = self._atr
        ema_atr = self._ema_atr

        if ema200 is not None and atr is not None:
            distance    = abs(close - ema200)
            threshold   = self.EMA200_ATR_K * atr
            distance_ok = distance < threshold
        else:
            distance_ok = False   # ждём прогрева EMA200 или ATR

        if atr is not None and ema_atr is not None and ema_atr > 0:
            vol_ratio = atr / ema_atr
            vol_ok    = vol_ratio > self.VOL_RATIO_MIN
        else:
            vol_ok = False   # ждём прогрева ATR / EMA_ATR

        _ema200_str = f"{ema200:.2f}" if ema200 is not None else "n/a"
        self.logger.debug(
            f"close={close:.2f}  RSI={rsi:.1f}  "
            f"EMA200={_ema200_str}  "
            f"dist_ok={distance_ok}  vol_ok={vol_ok}  pos={position:.6f}"
        )

        # ------------------------------------------------------------------
        # EXIT LOGIC (не зависит от фильтров)
        # ------------------------------------------------------------------

        if position > 0:
            if rsi > 50.0:
                await self._close_position(close, "SELL", f"RSI exit LONG ({rsi:.1f}>50)")
                position = 0

        elif position < 0:
            if rsi < 50.0:
                await self._close_position(close, "BUY", f"RSI exit SHORT ({rsi:.1f}<50)")
                position = 0

        # ------------------------------------------------------------------
        # ENTRY LOGIC: уровневый вход + cooldown + фильтры
        # ------------------------------------------------------------------

        cooldown_ok = (self._candle_count - self._last_trade_candle >= self.COOLDOWN_CANDLES)
        if not cooldown_ok or position != 0:
            return

        if not distance_ok or not vol_ok:
            return

        if rsi < self.OVERSOLD:
            result = await self._open_position(
                close, "BUY",
                RSI=f"{rsi:.1f}",
                dist=f"{distance:.4f}" if ema200 else "n/a",
                vol=f"{vol_ratio:.2f}" if (atr and ema_atr) else "n/a",
            )
            if result is not None:
                self.logger.info(f"LONG: RSI={rsi:.1f} < {self.OVERSOLD}")

        elif rsi > self.OVERBOUGHT:
            result = await self._open_position(
                close, "SELL",
                RSI=f"{rsi:.1f}",
                dist=f"{distance:.4f}" if ema200 else "n/a",
                vol=f"{vol_ratio:.2f}" if (atr and ema_atr) else "n/a",
            )
            if result is not None:
                self.logger.info(f"SHORT: RSI={rsi:.1f} > {self.OVERBOUGHT}")

    # ------------------------------------------------------------------
    # Wilder RSI
    # ------------------------------------------------------------------

    def _update_rsi(self, close: float, prev_close: Optional[float]) -> None:
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

    def _update_wilder_rsi(self, close: float, prev_close: Optional[float]) -> None:
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

    # ------------------------------------------------------------------
    # EMA200 (тренд-фильтр)
    # ------------------------------------------------------------------

    def _update_ema200(self, close: float) -> None:
        """Обновляет EMA200. До накопления 200 свечей сидит в буфере."""
        if self._ema200 is None:
            self._warmup_closes_ema200.append(close)
            if len(self._warmup_closes_ema200) >= self.EMA200_PERIOD:
                # Seed: простое среднее первых 200 свечей
                self._ema200 = sum(self._warmup_closes_ema200) / self.EMA200_PERIOD
                self._warmup_closes_ema200.clear()
        else:
            alpha = 2.0 / (self.EMA200_PERIOD + 1)
            self._ema200 = alpha * close + (1 - alpha) * self._ema200

    # ------------------------------------------------------------------
    # ATR + EMA of ATR (фильтр волатильности)
    # ------------------------------------------------------------------

    def _update_atr(self, high: float, low: float, close: float) -> None:
        """Wilder-smoothed ATR(14). Требует prev_high/low/close."""
        if self._prev_close_atr is not None:
            prev_close = self._prev_close_atr
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low  - prev_close),
            )

            if self._atr is None:
                # Накапливаем буфер для seed
                self._warmup_tr.append(tr)
                if len(self._warmup_tr) >= self.ATR_PERIOD:
                    self._atr = sum(self._warmup_tr) / self.ATR_PERIOD
                    self._warmup_tr.clear()
                    self._update_ema_atr(self._atr)
            else:
                alpha = 1.0 / self.ATR_PERIOD
                self._atr = alpha * tr + (1 - alpha) * self._atr
                self._update_ema_atr(self._atr)

        # Сохраняем текущие OHLC для следующей свечи
        self._prev_high      = high
        self._prev_low       = low
        self._prev_close_atr = close

    def _update_ema_atr(self, atr: float) -> None:
        """EMA(ATR_PERIOD=20) поверх ATR — «нормальная» волатильность."""
        if self._ema_atr is None:
            self._ema_atr = atr
        else:
            alpha = 2.0 / (self.EMA_ATR_PERIOD + 1)
            self._ema_atr = alpha * atr + (1 - alpha) * self._ema_atr
