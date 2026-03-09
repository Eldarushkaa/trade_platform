"""
RSI Bot — Wilder's Exponential RSI with trend filter and cooldown.

Operates on 1-MINUTE CANDLES. on_candle() is called once per completed
candle — RSI(14) here means 14 completed 1-minute candles.

Improvements over the naive version:
- Wilder's exponential smoothing (running avg_gain / avg_loss) — much
  smoother than SMA-based RSI, fewer false signals on noisy 1-min data.
- EMA(50) trend filter — only buys oversold dips when price is ABOVE the
  trend line (uptrend). Avoids catching falling knives.
- 5-candle cooldown between trades — prevents whipsaw fee burn.
- 50% trade fraction — preserves capital, allows multiple trades.

Strategy logic:
    BUY  when RSI < 25  AND  close > EMA(50)  AND  cooldown elapsed
    SELL when RSI > 75  AND  in position       AND  cooldown elapsed

Multi-coin usage:
    from strategies.example_rsi_bot import RSIBot
    REGISTERED_BOTS = [
        RSIBot.for_symbol("BTCUSDT"),
        RSIBot.for_symbol("ETHUSDT"),
        RSIBot.for_symbol("SOLUSDT"),
    ]
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

    # --- Strategy parameters (tuned for 1-minute candles) ---
    RSI_PERIOD = 14              # Wilder RSI lookback in candles
    OVERSOLD = 25.0              # Buy signal threshold (wider than classic 30)
    OVERBOUGHT = 75.0            # Sell signal threshold (wider than classic 70)
    TRADE_FRACTION = 0.50        # Use 50% of USDT balance per BUY
    COOLDOWN_CANDLES = 5         # Min candles between trades
    TREND_EMA_PERIOD = 50        # EMA for trend filter

    # ------------------------------------------------------------------
    # Factory: create a subclass bound to a specific symbol
    # ------------------------------------------------------------------

    @classmethod
    def for_symbol(cls, symbol: str) -> type:
        """
        Return a new subclass of RSIBot configured for the given symbol.

        Example:
            RSIBot.for_symbol("ETHUSDT")
            → creates class RSIBot_ETH with name="rsi_eth", symbol="ETHUSDT"
        """
        asset = symbol.replace("USDT", "").lower()
        return type(
            f"{cls.__name__}_{asset.upper()}",   # class name: RSIBot_BTC
            (cls,),
            {"name": f"rsi_{asset}", "symbol": symbol},
        )

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def __init__(self, engine: BaseOrderEngine) -> None:
        super().__init__(engine)

        # Wilder RSI state (exponential smoothing)
        self._avg_gain: Optional[float] = None
        self._avg_loss: Optional[float] = None
        self._prev_close: Optional[float] = None
        self._warmup_closes: list[float] = []

        # Trend filter: EMA(50)
        self._trend_ema: Optional[float] = None
        self._trend_warmup: list[float] = []

        # Candle counter and cooldown
        self._candle_count: int = 0
        self._last_trade_candle: int = -999  # allows immediate first trade

        # Position tracking
        self._in_position: bool = False

    # ------------------------------------------------------------------
    # Candle-based strategy logic
    # ------------------------------------------------------------------

    async def on_candle(self, candle: "Candle") -> None:
        """Called once per completed 1-minute candle."""
        self._candle_count += 1
        close = candle.close

        # --- Update trend EMA ---
        self._update_trend_ema(close)

        # --- Wilder RSI warmup phase ---
        if self._avg_gain is None:
            self._warmup_closes.append(close)
            if len(self._warmup_closes) > self.RSI_PERIOD:
                self._seed_wilder_rsi()
            else:
                self.logger.debug(
                    f"RSI warming up: {len(self._warmup_closes)}/{self.RSI_PERIOD + 1} candles"
                )
                return
        else:
            # --- Live: update Wilder RSI ---
            self._update_wilder_rsi(close)

        rsi = self._compute_rsi()
        if rsi is None:
            return

        trend_ok = (
            self._trend_ema is not None and close > self._trend_ema
        )
        cooldown_ok = (
            self._candle_count - self._last_trade_candle >= self.COOLDOWN_CANDLES
        )

        trend_ema_str = f"{self._trend_ema:.2f}" if self._trend_ema is not None else "N/A"
        self.logger.debug(
            f"close={close:.2f}  RSI={rsi:.1f}  "
            f"trend_ema={trend_ema_str}  "
            f"trend={'UP' if trend_ok else 'DOWN'}  "
            f"cooldown={'OK' if cooldown_ok else 'WAIT'}"
        )

        # --- Signal logic ---
        if rsi < self.OVERSOLD and not self._in_position and trend_ok and cooldown_ok:
            await self._buy(close)
        elif rsi > self.OVERBOUGHT and self._in_position and cooldown_ok:
            await self._sell(close)

    # ------------------------------------------------------------------
    # Wilder RSI internals
    # ------------------------------------------------------------------

    def _seed_wilder_rsi(self) -> None:
        """Seed avg_gain / avg_loss from the warmup window (SMA for first period)."""
        prices = self._warmup_closes
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

        self._avg_gain = sum(gains) / self.RSI_PERIOD
        self._avg_loss = sum(losses) / self.RSI_PERIOD
        self._prev_close = prices[-1]
        self._warmup_closes.clear()  # free memory

        self.logger.info(
            f"Wilder RSI warmup complete. "
            f"avg_gain={self._avg_gain:.6f}  avg_loss={self._avg_loss:.6f}"
        )

    def _update_wilder_rsi(self, close: float) -> None:
        """Update running Wilder averages with new close price."""
        if self._prev_close is None:
            self._prev_close = close
            return

        delta = close - self._prev_close
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)

        alpha = 1.0 / self.RSI_PERIOD
        self._avg_gain = alpha * gain + (1 - alpha) * self._avg_gain
        self._avg_loss = alpha * loss + (1 - alpha) * self._avg_loss
        self._prev_close = close

    def _compute_rsi(self) -> Optional[float]:
        """Compute RSI from current Wilder averages."""
        if self._avg_gain is None or self._avg_loss is None:
            return None
        if self._avg_loss == 0:
            return 100.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    # ------------------------------------------------------------------
    # Trend EMA
    # ------------------------------------------------------------------

    def _update_trend_ema(self, close: float) -> None:
        """Update EMA(50) trend filter."""
        if self._trend_ema is None:
            self._trend_warmup.append(close)
            if len(self._trend_warmup) >= self.TREND_EMA_PERIOD:
                self._trend_ema = sum(self._trend_warmup) / len(self._trend_warmup)
                self._trend_warmup.clear()
        else:
            k = 2.0 / (self.TREND_EMA_PERIOD + 1)
            self._trend_ema = close * k + self._trend_ema * (1 - k)

    # ------------------------------------------------------------------
    # Order helpers
    # ------------------------------------------------------------------

    async def _buy(self, price: float) -> None:
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
            self._last_trade_candle = self._candle_count
            rsi = self._compute_rsi()
            self.logger.info(
                f"BUY  {quantity:.6f} @ {price:.2f}  RSI={rsi:.1f}  "
                f"fee={result.get('fee_usdt', 0):.4f}  "
                f"(trade_id={result.get('trade_id')})"
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
            self._last_trade_candle = self._candle_count
            rsi = self._compute_rsi()
            pnl = result.get("realized_pnl", 0)
            fee = result.get("fee_usdt", 0)
            self.logger.info(
                f"SELL {quantity:.6f} @ {price:.2f}  RSI={rsi:.1f}  "
                f"P&L={pnl:+.2f}  fee={fee:.4f}  "
                f"(trade_id={result.get('trade_id')})"
            )
        except ValueError as exc:
            self.logger.error(f"SELL failed: {exc}")
