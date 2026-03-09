"""
Moving Average Crossover Bot — example trading strategy.

Operates on 1-MINUTE CANDLES. on_candle() is called once per completed
candle — EMA(9/21) here spans 9 and 21 completed 1-minute candles.

Strategy logic:
- Maintain a fast EMA (9 candles) and slow EMA (21 candles)
- BUY  when fast EMA crosses ABOVE slow EMA (golden cross) → uptrend starting
- SELL when fast EMA crosses BELOW slow EMA (death cross) → downtrend starting

How to register in main.py:
    from strategies.example_ma_crossover import MACrossoverBot
    REGISTERED_BOTS = [MACrossoverBot]
"""
from typing import Optional, TYPE_CHECKING

from core.base_strategy import BaseStrategy
from core.simulation_engine import BaseOrderEngine

if TYPE_CHECKING:
    from data.candle_aggregator import Candle


class MACrossoverBot(BaseStrategy):
    # --- Required class attributes ---
    name = "ma_crossover_bot"
    symbol = "ETHUSDT"

    # --- Strategy parameters ---
    FAST_PERIOD = 9           # Fast EMA in 1-minute candles
    SLOW_PERIOD = 21          # Slow EMA in 1-minute candles
    TRADE_FRACTION = 0.95     # Fraction of USDT balance to use per BUY

    def __init__(self, engine: BaseOrderEngine) -> None:
        super().__init__(engine)
        self._fast_ema: Optional[float] = None
        self._slow_ema: Optional[float] = None
        self._prev_fast_ema: Optional[float] = None
        self._prev_slow_ema: Optional[float] = None
        self._candle_count: int = 0
        self._warmup_closes: list[float] = []
        self._in_position: bool = False

    # ------------------------------------------------------------------
    # Candle-based strategy logic (called once per 1-minute candle close)
    # ------------------------------------------------------------------

    async def on_candle(self, candle: "Candle") -> None:
        """Called once per completed 1-minute candle."""
        self._candle_count += 1
        close = candle.close

        # --- Warmup phase: collect SLOW_PERIOD candles to seed EMAs ---
        if self._candle_count <= self.SLOW_PERIOD:
            self._warmup_closes.append(close)
            if self._candle_count == self.SLOW_PERIOD:
                prices = self._warmup_closes
                self._slow_ema = sum(prices) / len(prices)
                self._fast_ema = sum(prices[-self.FAST_PERIOD:]) / self.FAST_PERIOD
                self.logger.info(
                    f"EMA warmup complete ({self.SLOW_PERIOD} candles). "
                    f"Fast={self._fast_ema:.4f}  Slow={self._slow_ema:.4f}"
                )
                # Bug #5 fix: release warmup buffer — no longer needed after seeding EMAs
                self._warmup_closes.clear()
            else:
                self.logger.debug(
                    f"Warming up: {self._candle_count}/{self.SLOW_PERIOD} candles"
                )
            return

        # --- Live phase: update EMAs on each new candle ---
        self._prev_fast_ema = self._fast_ema
        self._prev_slow_ema = self._slow_ema
        self._fast_ema = self._ema(close, self._fast_ema, self.FAST_PERIOD)
        self._slow_ema = self._ema(close, self._slow_ema, self.SLOW_PERIOD)

        self.logger.debug(
            f"Candle close={close:.4f}  "
            f"fast={self._fast_ema:.4f}  slow={self._slow_ema:.4f}"
        )

        await self._check_signals(close)

    async def _check_signals(self, price: float) -> None:
        """Detect crossover and fire orders."""
        if None in (self._prev_fast_ema, self._prev_slow_ema):
            return

        was_above = self._prev_fast_ema > self._prev_slow_ema
        is_above  = self._fast_ema > self._slow_ema

        # Golden cross: fast EMA crossed above slow → BUY
        if not was_above and is_above and not self._in_position:
            await self._buy(price)

        # Death cross: fast EMA crossed below slow → SELL
        elif was_above and not is_above and self._in_position:
            await self._sell(price)

    # ------------------------------------------------------------------
    # Order helpers
    # ------------------------------------------------------------------

    async def _buy(self, price: float) -> None:
        usdt = await self.engine.get_balance(self.name, "USDT")
        if usdt < 10:
            self.logger.warning("Insufficient USDT to buy")
            return

        spend = usdt * self.TRADE_FRACTION
        quantity = round(spend / price, 6)

        try:
            result = await self.engine.place_order(
                bot_id=self.name,
                symbol=self.symbol,
                side="BUY",
                quantity=quantity,
                price=price,
            )
            self._in_position = True
            fee = result.get("fee_usdt", 0)
            self.logger.info(
                f"GOLDEN CROSS → BUY  {quantity:.6f} ETH @ {price:.4f}  "
                f"fee={fee:.4f}  (trade_id={result.get('trade_id')})"
            )
        except ValueError as exc:
            self.logger.error(f"BUY failed: {exc}")

    async def _sell(self, price: float) -> None:
        asset = self.symbol.replace("USDT", "")
        quantity = await self.engine.get_balance(self.name, asset)
        if quantity <= 0:
            self._in_position = False
            return

        quantity = round(quantity, 6)
        try:
            result = await self.engine.place_order(
                bot_id=self.name,
                symbol=self.symbol,
                side="SELL",
                quantity=quantity,
                price=price,
            )
            self._in_position = False
            pnl = result.get("realized_pnl", 0)
            fee = result.get("fee_usdt", 0)
            self.logger.info(
                f"DEATH CROSS → SELL {quantity:.6f} ETH @ {price:.4f}  "
                f"P&L={pnl:+.4f}  fee={fee:.4f}  (trade_id={result.get('trade_id')})"
            )
        except ValueError as exc:
            self.logger.error(f"SELL failed: {exc}")

    # ------------------------------------------------------------------
    # EMA formula
    # ------------------------------------------------------------------

    @staticmethod
    def _ema(price: float, prev_ema: float, period: int) -> float:
        """
        Exponential Moving Average update step.
        k = 2 / (period + 1) — standard smoothing factor.
        """
        k = 2.0 / (period + 1)
        return price * k + prev_ema * (1 - k)
