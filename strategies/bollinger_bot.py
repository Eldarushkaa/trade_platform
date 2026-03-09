"""
Bollinger Band Mean Reversion Bot — buys at the lower band, sells at the mean.

Operates on 1-MINUTE CANDLES. 1-minute price action is highly mean-reverting:
price tends to snap back to the moving average after spikes. Bollinger Bands
measure this precisely and auto-adapt to each coin's volatility.

Indicators:
    Middle band = SMA(20) — the mean
    Upper band  = SMA + 2.0 × stddev — overbought boundary
    Lower band  = SMA - 2.0 × stddev — oversold boundary
    %B          = (close - lower) / (upper - lower) — 0=lower, 1=upper
    Bandwidth   = (upper - lower) / SMA — volatility measure

Strategy logic:
    BUY  when close < lower_band  AND  bandwidth > MIN_BANDWIDTH  AND  cooldown OK
    SELL when close > middle_band  AND  in position  (take profit at mean)
    STOP when close < lower_band * 0.995  AND  in position  (0.5% stop-loss)

Multi-coin usage:
    from strategies.bollinger_bot import BollingerBot
    REGISTERED_BOTS = [
        BollingerBot.for_symbol("BTCUSDT"),
        BollingerBot.for_symbol("ETHUSDT"),
        BollingerBot.for_symbol("SOLUSDT"),
    ]
"""
import math
from collections import deque
from typing import Optional, TYPE_CHECKING

from core.base_strategy import BaseStrategy
from core.simulation_engine import BaseOrderEngine

if TYPE_CHECKING:
    from data.candle_aggregator import Candle


class BollingerBot(BaseStrategy):
    # --- Required class attributes ---
    name = "bb_bot"
    symbol = "BTCUSDT"

    # --- Bollinger Band parameters ---
    BB_PERIOD = 20                # SMA lookback in candles
    BB_STD_DEV = 2.0              # Standard deviation multiplier
    TRADE_FRACTION = 0.50         # Use 50% of USDT balance per BUY
    MIN_BANDWIDTH = 0.001         # Skip during Bollinger squeeze (< 0.1% of price)
    COOLDOWN_CANDLES = 3          # Min candles between trades
    STOP_LOSS_PCT = 0.005         # 0.5% below lower band → cut losses

    # ------------------------------------------------------------------
    # Factory: create a subclass bound to a specific symbol
    # ------------------------------------------------------------------

    @classmethod
    def for_symbol(cls, symbol: str) -> type:
        """
        Return a new subclass of BollingerBot configured for the given symbol.

        Example:
            BollingerBot.for_symbol("SOLUSDT")
            → creates class BollingerBot_SOL with name="bb_sol", symbol="SOLUSDT"
        """
        asset = symbol.replace("USDT", "").lower()
        return type(
            f"{cls.__name__}_{asset.upper()}",   # class name: BollingerBot_SOL
            (cls,),
            {"name": f"bb_{asset}", "symbol": symbol},
        )

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def __init__(self, engine: BaseOrderEngine) -> None:
        super().__init__(engine)

        # Rolling window of close prices
        self._closes: deque[float] = deque(maxlen=self.BB_PERIOD)

        # Band values (computed each candle)
        self._sma: Optional[float] = None
        self._upper: Optional[float] = None
        self._lower: Optional[float] = None
        self._bandwidth: Optional[float] = None

        # Candle counter and cooldown
        self._candle_count: int = 0
        self._last_trade_candle: int = -999

        # Position tracking
        self._in_position: bool = False
        self._entry_lower: Optional[float] = None  # lower band at entry (for stop-loss)

    # ------------------------------------------------------------------
    # Candle-based strategy logic
    # ------------------------------------------------------------------

    async def on_candle(self, candle: "Candle") -> None:
        """Called once per completed 1-minute candle."""
        self._candle_count += 1
        close = candle.close
        self._closes.append(close)

        # Need full window to compute bands
        if len(self._closes) < self.BB_PERIOD:
            self.logger.debug(
                f"BB warming up: {len(self._closes)}/{self.BB_PERIOD} candles"
            )
            return

        # --- Compute Bollinger Bands ---
        self._compute_bands()

        # %B = position within bands (0 = lower, 0.5 = middle, 1 = upper)
        band_width = self._upper - self._lower
        pct_b = (close - self._lower) / band_width if band_width > 0 else 0.5

        cooldown_ok = (
            self._candle_count - self._last_trade_candle >= self.COOLDOWN_CANDLES
        )

        self.logger.debug(
            f"close={close:.4f}  SMA={self._sma:.4f}  "
            f"upper={self._upper:.4f}  lower={self._lower:.4f}  "
            f"%B={pct_b:.3f}  BW={self._bandwidth:.5f}  "
            f"cooldown={'OK' if cooldown_ok else 'WAIT'}"
        )

        # --- Signal logic ---

        # STOP-LOSS: if in position and price drops 0.5% below entry lower band
        if self._in_position and self._entry_lower is not None:
            stop_price = self._entry_lower * (1 - self.STOP_LOSS_PCT)
            if close < stop_price:
                self.logger.warning(
                    f"STOP-LOSS triggered: close={close:.4f} < "
                    f"stop={stop_price:.4f} (lower×{1-self.STOP_LOSS_PCT})"
                )
                await self._sell(close, reason="STOP-LOSS")
                return

        # TAKE PROFIT: sell when price reverts to the mean (SMA)
        if self._in_position and close > self._sma:
            await self._sell(close, reason="TAKE-PROFIT at mean")
            return

        # BUY: price below lower band + bandwidth filter + cooldown
        if (
            not self._in_position
            and close < self._lower
            and self._bandwidth > self.MIN_BANDWIDTH
            and cooldown_ok
        ):
            await self._buy(close)

    # ------------------------------------------------------------------
    # Bollinger Band computation
    # ------------------------------------------------------------------

    def _compute_bands(self) -> None:
        """Compute SMA, upper/lower bands, and bandwidth from rolling closes."""
        prices = list(self._closes)
        n = len(prices)
        self._sma = sum(prices) / n
        variance = sum((p - self._sma) ** 2 for p in prices) / n
        stddev = math.sqrt(variance)

        self._upper = self._sma + self.BB_STD_DEV * stddev
        self._lower = self._sma - self.BB_STD_DEV * stddev
        self._bandwidth = (self._upper - self._lower) / self._sma if self._sma > 0 else 0

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
            self._entry_lower = self._lower  # remember band level for stop-loss
            self._last_trade_candle = self._candle_count
            fee = result.get("fee_usdt", 0)
            self.logger.info(
                f"BB LOWER → BUY  {quantity:.6f} @ {price:.4f}  "
                f"lower={self._lower:.4f}  BW={self._bandwidth:.5f}  "
                f"fee={fee:.4f}  (trade_id={result.get('trade_id')})"
            )
        except ValueError as exc:
            self.logger.error(f"BUY failed: {exc}")

    async def _sell(self, price: float, reason: str = "SELL") -> None:
        asset = self.symbol.replace("USDT", "")
        quantity = await self.engine.get_balance(self.name, asset)
        if quantity <= 0:
            self._in_position = False
            self._entry_lower = None
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
            self._entry_lower = None
            self._last_trade_candle = self._candle_count
            pnl = result.get("realized_pnl", 0)
            fee = result.get("fee_usdt", 0)
            self.logger.info(
                f"{reason} → SELL {quantity:.6f} @ {price:.4f}  "
                f"SMA={self._sma:.4f}  P&L={pnl:+.4f}  fee={fee:.4f}  "
                f"(trade_id={result.get('trade_id')})"
            )
        except ValueError as exc:
            self.logger.error(f"SELL failed: {exc}")
