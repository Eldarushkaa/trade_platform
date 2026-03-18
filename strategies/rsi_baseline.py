"""
RSI Baseline — минимальная эталонная стратегия для проверки edge RSI.

Назначение:
    Нижняя планка перед запуском сложных стратегий. Если эта не работает →
    более сложная тоже не будет. Никаких фильтров тренда, никакой адаптации.

Правила (классический RSI, уровневый вход):
    Entry LONG:  RSI < OVERSOLD   (каждую свечу пока RSI в зоне, cooldown ограничивает)
    Entry SHORT: RSI > OVERBOUGHT
    Exit LONG:   RSI > 50
    Exit SHORT:  RSI < 50

Никаких дополнительных фильтров:
    - Нет EMA тренда
    - Нет ATR фильтра волатильности
    - Нет динамических порогов

Оптимизируемые параметры:
    OVERSOLD         20–35   (чем ниже → реже и точнее LONG-сигналы)
    OVERBOUGHT       65–80   (чем выше → реже и точнее SHORT-сигналы)
    COOLDOWN_CANDLES 1–20    (свечей между сделками; 10 = 50 мин на 5м)

Фиксированные параметры:
    RSI_PERIOD = 14  (стандарт Уайлдера)

Warmup:
    Торговля начинается после инициализации RSI (~RSI_PERIOD+1 свечей).

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
    """RSI(14): уровневый вход BUY<OVERSOLD / SELL>OVERBOUGHT, EXIT at 50."""

    name_prefix = "rsi_baseline"
    name = "rsi_baseline"
    symbol = "BTCUSDT"

    # --- Фиксированный параметр (не оптимизируется) ---
    RSI_PERIOD = 14

    # --- Оптимизируемые параметры ---
    OVERSOLD         = 25.0   # порог входа LONG
    OVERBOUGHT       = 75.0   # порог входа SHORT
    COOLDOWN_CANDLES = 10     # минимум свечей между сделками

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
            "description": "Minimum candles between entries (1 candle = 5 min)",
        },
    }

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def __init__(self, engine: BaseOrderEngine) -> None:
        super().__init__(engine)

        # Wilder RSI state
        self._avg_gain: Optional[float] = None
        self._avg_loss: Optional[float] = None
        self._prev_close: Optional[float] = None
        self._warmup_closes: list[float] = []

    # ------------------------------------------------------------------
    # Main candle handler
    # ------------------------------------------------------------------

    async def on_candle(self, candle: "Candle") -> None:
        self._candle_count += 1
        close = candle.close

        # Снимаем prev_close до обновления
        prev_close_snapshot = self._prev_close
        self._prev_close = close

        # Обновляем RSI
        self._update_rsi(close, prev_close_snapshot)

        # Ждём инициализации RSI
        rsi = self._compute_rsi()
        if rsi is None:
            return

        # Текущая позиция
        position = await self.engine.get_balance(self.name, "POSITION")

        self.logger.debug(
            f"close={close:.2f}  RSI={rsi:.1f}  "
            f"OS={self.OVERSOLD}  OB={self.OVERBOUGHT}  pos={position:.6f}"
        )

        # ------------------------------------------------------------------
        # EXIT LOGIC
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
        # ENTRY LOGIC: уровневый вход + cooldown
        # ------------------------------------------------------------------

        cooldown_ok = (self._candle_count - self._last_trade_candle >= self.COOLDOWN_CANDLES)
        if not cooldown_ok or position != 0:
            return

        if rsi < self.OVERSOLD:
            # RSI в зоне перепроданности → LONG
            result = await self._open_position(close, "BUY", RSI=f"{rsi:.1f}")
            if result is not None:
                self.logger.info(f"LONG: RSI={rsi:.1f} < {self.OVERSOLD}")

        elif rsi > self.OVERBOUGHT:
            # RSI в зоне перекупленности → SHORT
            result = await self._open_position(close, "SELL", RSI=f"{rsi:.1f}")
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
