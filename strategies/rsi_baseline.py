"""
RSI Baseline — минимальная эталонная стратегия для проверки edge RSI.

Назначение:
    Прежде чем оптимизировать что-либо, нужно убедиться что сама идея RSI
    mean-reversion работает на данном инструменте. Эта стратегия является
    НИЖНЕЙ ПЛАНКОЙ: если она не работает → более сложная тоже не будет.

Правила (классический RSI, crossover-вход):
    Entry LONG:  RSI пересёк OVERSOLD снизу вверх (был ниже, стал выше)
    Entry SHORT: RSI пересёк OVERBOUGHT сверху вниз (был выше, стал ниже)
    Exit LONG:   RSI > 50
    Exit SHORT:  RSI < 50

Crossover vs уровень:
    Уровневый вход (rsi < 30) срабатывает КАЖДУЮ свечу пока RSI в зоне.
    Crossover вход срабатывает ОДИН РАЗ — в момент выхода из экстремума.
    Это уменьшает количество сделок и снижает съедание комиссией.

Фиксированные параметры (не оптимизируются):
    RSI_PERIOD      = 14    (стандарт Уайлдера)
    OVERSOLD        = 30.0  (порог входа LONG)
    OVERBOUGHT      = 70.0  (порог входа SHORT)
    COOLDOWN_CANDLES = 5    (минимум свечей между сделками)

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
    """RSI(14): crossover-вход при пересечении 30/70, EXIT at 50. Без фильтров тренда."""

    name_prefix = "rsi_baseline"
    name = "rsi_baseline"
    symbol = "BTCUSDT"

    # --- Фиксированные параметры (не оптимизируются) ---
    RSI_PERIOD       = 14
    OVERSOLD         = 30.0
    OVERBOUGHT       = 70.0
    COOLDOWN_CANDLES = 5     # минимум свечей между сделками (снижает частоту)

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

        # RSI предыдущей свечи для crossover-детектора
        self._rsi_prev: Optional[float] = None

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
        # ENTRY LOGIC: crossover + cooldown (только если нет позиции)
        # ------------------------------------------------------------------

        cooldown_ok = (self._candle_count - self._last_trade_candle >= self.COOLDOWN_CANDLES)

        if position == 0 and cooldown_ok and self._rsi_prev is not None:
            if self._rsi_prev < self.OVERSOLD and rsi >= self.OVERSOLD:
                # RSI пересёк OVERSOLD снизу вверх → конец перепроданности → LONG
                result = await self._open_position(close, "BUY", RSI=f"{rsi:.1f}", RSIprev=f"{self._rsi_prev:.1f}")
                if result is not None:
                    self.logger.info(f"LONG: RSI {self._rsi_prev:.1f} → {rsi:.1f} (пересёк {self.OVERSOLD})")

            elif self._rsi_prev > self.OVERBOUGHT and rsi <= self.OVERBOUGHT:
                # RSI пересёк OVERBOUGHT сверху вниз → конец перекупленности → SHORT
                result = await self._open_position(close, "SELL", RSI=f"{rsi:.1f}", RSIprev=f"{self._rsi_prev:.1f}")
                if result is not None:
                    self.logger.info(f"SHORT: RSI {self._rsi_prev:.1f} → {rsi:.1f} (пересёк {self.OVERBOUGHT})")

        # Сохраняем RSI для следующей свечи
        self._rsi_prev = rsi

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
