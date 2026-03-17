"""
RSI Baseline — минимальная эталонная стратегия для проверки edge RSI.

Назначение:
    Прежде чем оптимизировать что-либо, нужно убедиться что сама идея RSI
    mean-reversion работает на данном инструменте. Эта стратегия является
    НИЖНЕЙ ПЛАНКОЙ: если она не работает → более сложная тоже не будет.

Правила (классический RSI, без адаптации):
    Entry LONG:  RSI < 30  (простая проверка уровня, не кросс)
    Entry SHORT: RSI > 70
    Exit LONG:   RSI > 50
    Exit SHORT:  RSI < 50

Никаких фильтров:
    - Нет EMA тренда
    - Нет ATR фильтра волатильности
    - Нет динамических порогов
    - Нет cooldown
    - Нет time-stop (MAX_HOLD)

Параметры (только для информации, не оптимизируются):
    RSI_PERIOD = 14  (фиксировано — стандарт Уайлдера)

Warmup:
    Торговля начинается после свечи #RSI_PERIOD+1 (RSI инициализирован).

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
    """Чистый RSI(14): BUY < 30, SELL > 70, EXIT at 50. Без фильтров."""

    name_prefix = "rsi_baseline"
    name = "rsi_baseline"
    symbol = "BTCUSDT"

    # --- Фиксированные параметры (не оптимизируются) ---
    RSI_PERIOD = 14

    # --- Нет оптимизируемых параметров ---
    PARAM_SCHEMA: dict = {}

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

        self.logger.debug(f"close={close:.2f}  RSI={rsi:.1f}  pos={position:.6f}")

        # ------------------------------------------------------------------
        # EXIT LOGIC
        # ------------------------------------------------------------------

        if position > 0:
            # LONG exit: RSI восстановился выше 50
            if rsi > 50.0:
                await self._close_position(close, "SELL", f"RSI exit LONG ({rsi:.1f}>50)")
                position = 0

        elif position < 0:
            # SHORT exit: RSI восстановился ниже 50
            if rsi < 50.0:
                await self._close_position(close, "BUY", f"RSI exit SHORT ({rsi:.1f}<50)")
                position = 0

        # ------------------------------------------------------------------
        # ENTRY LOGIC (только если нет открытой позиции)
        # ------------------------------------------------------------------

        if position != 0:
            return

        if rsi < 30.0:
            # RSI в зоне перепроданности → LONG
            result = await self._open_position(close, "BUY", RSI=f"{rsi:.1f}")
            if result is not None:
                self.logger.info(f"LONG открыт: RSI={rsi:.1f} < 30")

        elif rsi > 70.0:
            # RSI в зоне перекупленности → SHORT
            result = await self._open_position(close, "SELL", RSI=f"{rsi:.1f}")
            if result is not None:
                self.logger.info(f"SHORT открыт: RSI={rsi:.1f} > 70")

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
