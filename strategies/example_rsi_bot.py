"""
RSI Bot — example trading strategy using the Relative Strength Index.

Operates on 1-MINUTE CANDLES. on_candle() is called once per completed
candle — RSI(14) here means 14 completed 1-minute candles ≈ 14 minutes.

Strategy logic:
- Collect close prices from completed candles in a rolling window
- Calculate RSI from price changes
- BUY  when RSI < oversold threshold (30) and no open position
- SELL when RSI > overbought threshold (70) and position is open

This bot trades a fixed fraction of the available USDT balance on each BUY.

How to register in main.py:
    from strategies.example_rsi_bot import RSIBot
    REGISTERED_BOTS = [RSIBot]
"""
from collections import deque
from typing import Optional, TYPE_CHECKING

from core.base_strategy import BaseStrategy
from core.simulation_engine import BaseOrderEngine

if TYPE_CHECKING:
    from data.candle_aggregator import Candle


class RSIBot(BaseStrategy):
    # --- Required class attributes ---
    name = "rsi_bot"
    symbol = "BTCUSDT"

    # --- Strategy parameters (tune these) ---
    RSI_PERIOD = 14          # Number of 1-minute candles for RSI
    OVERSOLD = 30.0          # Buy signal threshold
    OVERBOUGHT = 70.0        # Sell signal threshold
    TRADE_FRACTION = 0.95    # Fraction of USDT balance to use per BUY

    def __init__(self, engine: BaseOrderEngine) -> None:
        super().__init__(engine)
        # Rolling window of candle close prices
        self._closes: deque[float] = deque(maxlen=self.RSI_PERIOD + 1)
        self._in_position: bool = False

    # ------------------------------------------------------------------
    # Candle-based strategy logic (called once per 1-minute candle close)
    # ------------------------------------------------------------------

    async def on_candle(self, candle: "Candle") -> None:
        """Called once per completed 1-minute candle."""
        close = candle.close
        self._closes.append(close)

        # Need at least RSI_PERIOD + 1 closes to compute RSI
        if len(self._closes) < self.RSI_PERIOD + 1:
            self.logger.debug(
                f"Warming up: {len(self._closes)}/{self.RSI_PERIOD + 1} candles"
            )
            return

        rsi = self._calculate_rsi()
        if rsi is None:
            return

        self.logger.debug(
            f"Candle close={close:.2f}  RSI({self.RSI_PERIOD})={rsi:.1f}"
        )

        if rsi < self.OVERSOLD and not self._in_position:
            await self._buy(close)

        elif rsi > self.OVERBOUGHT and self._in_position:
            await self._sell(close)

    # ------------------------------------------------------------------
    # Order helpers
    # ------------------------------------------------------------------

    async def _buy(self, price: float) -> None:
        """Execute a BUY order using a fraction of available USDT balance."""
        usdt = await self.engine.get_balance(self.name, "USDT")
        if usdt < 10:
            self.logger.warning("Insufficient USDT balance to buy")
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
            self.logger.info(
                f"BUY  {quantity:.6f} BTC @ {price:.2f} USDT  "
                f"fee={result.get('fee_usdt', 0):.4f}  (trade_id={result.get('trade_id')})"
            )
        except ValueError as exc:
            self.logger.error(f"BUY failed: {exc}")

    async def _sell(self, price: float) -> None:
        """Sell the entire BTC position."""
        quantity = await self.engine.get_balance(self.name, "BTC")
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
                f"SELL {quantity:.6f} BTC @ {price:.2f} USDT  "
                f"P&L={pnl:+.2f}  fee={fee:.4f}  (trade_id={result.get('trade_id')})"
            )
        except ValueError as exc:
            self.logger.error(f"SELL failed: {exc}")

    # ------------------------------------------------------------------
    # RSI calculation
    # ------------------------------------------------------------------

    def _calculate_rsi(self) -> Optional[float]:
        """
        Classic Wilder's RSI using simple averages over RSI_PERIOD candles.

        Returns:
            RSI value 0–100, or None if not enough data.
        """
        prices = list(self._closes)
        if len(prices) < self.RSI_PERIOD + 1:
            return None

        gains = []
        losses = []
        for i in range(1, len(prices)):
            delta = prices[i] - prices[i - 1]
            if delta >= 0:
                gains.append(delta)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(delta))

        avg_gain = sum(gains[-self.RSI_PERIOD:]) / self.RSI_PERIOD
        avg_loss = sum(losses[-self.RSI_PERIOD:]) / self.RSI_PERIOD

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))
