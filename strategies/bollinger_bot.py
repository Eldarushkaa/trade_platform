"""
Bollinger Band Mean Reversion Bot — bidirectional futures trading.

Operates on 1-MINUTE CANDLES. USDT-Margined perpetual futures with leverage.

Strategy logic (bidirectional):
    Close < lower band → OPEN LONG  (expect reversion to upper band)
    Close > upper band → OPEN SHORT (expect reversion to lower band)

    Take profit at opposite band. Stop-loss at 1% from entry.
    Bandwidth filter skips Bollinger squeeze.

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
    BB_PERIOD = 20
    BB_STD_DEV = 2.0
    TRADE_FRACTION = 1.0
    MIN_BANDWIDTH = 0.0005
    COOLDOWN_CANDLES = 3
    STOP_LOSS_PCT = 0.01          # 1% stop-loss from entry

    PARAM_SCHEMA = {
        "BB_PERIOD": {
            "type": "int", "default": 20, "min": 5, "max": 50,
            "description": "Bollinger Band lookback period",
        },
        "BB_STD_DEV": {
            "type": "float", "default": 2.0, "min": 0.5, "max": 4.0,
            "description": "Standard deviation multiplier for bands",
        },
        "TRADE_FRACTION": {
            "type": "float", "default": 1.0, "min": 0.10, "max": 1.0,
            "description": "Fraction of free USDT to use per trade",
            "optimize": False,
        },
        "MIN_BANDWIDTH": {
            "type": "float", "default": 0.0005, "min": 0.0, "max": 0.01,
            "description": "Minimum bandwidth to trade (squeeze filter)",
        },
        "COOLDOWN_CANDLES": {
            "type": "int", "default": 3, "min": 0, "max": 30,
            "description": "Minimum candles between trades",
        },
        "STOP_LOSS_PCT": {
            "type": "float", "default": 0.01, "min": 0.001, "max": 0.05,
            "description": "Stop-loss percentage from entry price",
        },
    }

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def for_symbol(cls, symbol: str) -> type:
        asset = symbol.replace("USDT", "").lower()
        return type(
            f"{cls.__name__}_{asset.upper()}",
            (cls,),
            {"name": f"bb_{asset}", "symbol": symbol},
        )

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def __init__(self, engine: BaseOrderEngine) -> None:
        super().__init__(engine)
        self._closes: deque[float] = deque(maxlen=self.BB_PERIOD)
        self._sma: Optional[float] = None
        self._upper: Optional[float] = None
        self._lower: Optional[float] = None
        self._bandwidth: Optional[float] = None
        self._candle_count: int = 0
        self._last_trade_candle: int = -999
        self._entry_price: Optional[float] = None

    # ------------------------------------------------------------------
    # Candle logic
    # ------------------------------------------------------------------

    async def on_candle(self, candle: "Candle") -> None:
        self._candle_count += 1
        close = candle.close
        self._closes.append(close)

        if len(self._closes) < self.BB_PERIOD:
            self.logger.debug(f"BB warming up: {len(self._closes)}/{self.BB_PERIOD}")
            return

        self._compute_bands()

        band_width = self._upper - self._lower
        pct_b = (close - self._lower) / band_width if band_width > 0 else 0.5
        cooldown_ok = (self._candle_count - self._last_trade_candle >= self.COOLDOWN_CANDLES)

        # Get position: positive=LONG, negative=SHORT, 0=none
        position = await self.engine.get_balance(self.name, "POSITION")

        self.logger.debug(
            f"close={close:.4f}  SMA={self._sma:.4f}  "
            f"upper={self._upper:.4f}  lower={self._lower:.4f}  "
            f"%B={pct_b:.3f}  BW={self._bandwidth:.5f}  pos={position:.6f}"
        )

        # --- STOP-LOSS for any open position ---
        if position != 0 and self._entry_price is not None:
            if position > 0:  # LONG
                stop = self._entry_price * (1 - self.STOP_LOSS_PCT)
                if close < stop:
                    self.logger.warning(f"LONG STOP-LOSS: close={close:.4f} < stop={stop:.4f}")
                    await self._close_position(close, "SELL", "STOP-LOSS LONG")
                    return
            elif position < 0:  # SHORT
                stop = self._entry_price * (1 + self.STOP_LOSS_PCT)
                if close > stop:
                    self.logger.warning(f"SHORT STOP-LOSS: close={close:.4f} > stop={stop:.4f}")
                    await self._close_position(close, "BUY", "STOP-LOSS SHORT")
                    return

        # --- TAKE PROFIT ---
        if position > 0 and close >= self._upper:
            # LONG → take profit at upper band
            await self._close_position(close, "SELL", "TP UPPER BAND")
            return
        elif position < 0 and close <= self._lower:
            # SHORT → take profit at lower band
            await self._close_position(close, "BUY", "TP LOWER BAND")
            return

        # --- ENTRY SIGNALS (with bandwidth filter + cooldown) ---
        if self._bandwidth < self.MIN_BANDWIDTH or not cooldown_ok:
            return

        # Price below lower band → LONG
        if close < self._lower and position <= 0:
            if position < 0:
                await self._close_position(close, "BUY", "Close SHORT → LONG")
            await self._open_position(close, "BUY")

        # Price above upper band → SHORT
        elif close > self._upper and position >= 0:
            if position > 0:
                await self._close_position(close, "SELL", "Close LONG → SHORT")
            await self._open_position(close, "SELL")

    # ------------------------------------------------------------------
    # Bollinger Band computation
    # ------------------------------------------------------------------

    def _compute_bands(self) -> None:
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

    async def _open_position(self, price: float, side: str) -> None:
        usdt = await self.engine.get_balance(self.name, "USDT")
        if usdt < 10:
            self.logger.warning("Insufficient USDT for margin")
            return

        spend = usdt * self.TRADE_FRACTION
        quantity = round(spend / price, 6)
        direction = "LONG" if side == "BUY" else "SHORT"

        try:
            result = await self.engine.place_order(
                bot_id=self.name, symbol=self.symbol,
                side=side, quantity=quantity, price=price,
            )
            self._entry_price = price
            self._last_trade_candle = self._candle_count
            self.logger.info(
                f"BB → OPEN {direction} {quantity:.6f} @ {price:.4f}  "
                f"lower={self._lower:.4f}  upper={self._upper:.4f}  "
                f"BW={self._bandwidth:.5f}  "
                f"fee={result.get('fee_usdt', 0):.4f}  "
                f"(trade_id={result.get('trade_id')})"
            )
        except ValueError as exc:
            self.logger.error(f"OPEN {direction} failed: {exc}")

    async def _close_position(self, price: float, side: str, reason: str) -> None:
        try:
            result = await self.engine.place_order(
                bot_id=self.name, symbol=self.symbol,
                side=side, quantity=0, price=price,
            )
            self._entry_price = None
            self._last_trade_candle = self._candle_count
            pnl = result.get("realized_pnl", 0)
            self.logger.info(
                f"{reason} → CLOSE @ {price:.4f}  P&L={pnl:+.4f}  "
                f"fee={result.get('fee_usdt', 0):.4f}  "
                f"(trade_id={result.get('trade_id')})"
            )
        except ValueError as exc:
            self.logger.error(f"Close failed: {exc}")
