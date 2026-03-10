"""
RSI Bot — Wilder's Exponential RSI for 1-minute bidirectional futures trading.

Operates on 1-MINUTE CANDLES. USDT-Margined perpetual futures with leverage.

Strategy logic (bidirectional):
    RSI < 30 (oversold)   → OPEN LONG  (expect bounce up)
    RSI > 70 (overbought) → OPEN SHORT (expect drop)

    If already in opposite position, close it first then open new direction.
    This doubles signal count vs spot — every overbought IS a trade, not just exit.

Multi-coin usage:
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
    # --- Required class attributes ---
    name = "rsi_bot"
    symbol = "BTCUSDT"

    # --- Strategy parameters (tuned for 1-minute candles) ---
    RSI_PERIOD = 10              # Shorter period = faster signals on 1-min
    OVERSOLD = 30.0              # Go LONG below this
    OVERBOUGHT = 70.0            # Go SHORT above this
    TRADE_FRACTION = 0.80        # Use 80% of free USDT for margin
    COOLDOWN_CANDLES = 3         # Min candles between trades

    PARAM_SCHEMA = {
        "RSI_PERIOD": {
            "type": "int", "default": 10, "min": 3, "max": 50,
            "description": "RSI lookback window",
        },
        "OVERSOLD": {
            "type": "float", "default": 30.0, "min": 5.0, "max": 45.0,
            "description": "Long entry threshold (RSI below this → LONG)",
        },
        "OVERBOUGHT": {
            "type": "float", "default": 70.0, "min": 55.0, "max": 95.0,
            "description": "Short entry threshold (RSI above this → SHORT)",
        },
        "TRADE_FRACTION": {
            "type": "float", "default": 0.80, "min": 0.10, "max": 1.0,
            "description": "Fraction of free USDT to use per trade",
        },
        "COOLDOWN_CANDLES": {
            "type": "int", "default": 3, "min": 0, "max": 30,
            "description": "Minimum candles between trades",
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
            {"name": f"rsi_{asset}", "symbol": symbol},
        )

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def __init__(self, engine: BaseOrderEngine) -> None:
        super().__init__(engine)
        self._avg_gain: Optional[float] = None
        self._avg_loss: Optional[float] = None
        self._prev_close: Optional[float] = None
        self._warmup_closes: list[float] = []
        self._candle_count: int = 0
        self._last_trade_candle: int = -999

    # ------------------------------------------------------------------
    # Candle logic
    # ------------------------------------------------------------------

    async def on_candle(self, candle: "Candle") -> None:
        self._candle_count += 1
        close = candle.close

        # --- Wilder RSI warmup ---
        if self._avg_gain is None:
            self._warmup_closes.append(close)
            if len(self._warmup_closes) > self.RSI_PERIOD:
                self._seed_wilder_rsi()
            else:
                self.logger.debug(
                    f"RSI warming up: {len(self._warmup_closes)}/{self.RSI_PERIOD + 1}"
                )
                return
        else:
            self._update_wilder_rsi(close)

        rsi = self._compute_rsi()
        if rsi is None:
            return

        cooldown_ok = (
            self._candle_count - self._last_trade_candle >= self.COOLDOWN_CANDLES
        )

        # Get current position: positive = LONG, negative = SHORT, 0 = none
        position = await self.engine.get_balance(self.name, "POSITION")

        self.logger.debug(
            f"close={close:.2f}  RSI={rsi:.1f}  pos={position:.6f}  "
            f"cooldown={'OK' if cooldown_ok else 'WAIT'}"
        )

        if not cooldown_ok:
            return

        # --- Oversold → go LONG ---
        if rsi < self.OVERSOLD and position <= 0:
            # Close SHORT if open
            if position < 0:
                await self._close_position(close, "BUY", "Close SHORT before LONG")
            await self._open_position(close, "BUY", rsi)

        # --- Overbought → go SHORT ---
        elif rsi > self.OVERBOUGHT and position >= 0:
            # Close LONG if open
            if position > 0:
                await self._close_position(close, "SELL", "Close LONG before SHORT")
            await self._open_position(close, "SELL", rsi)

    # ------------------------------------------------------------------
    # Wilder RSI
    # ------------------------------------------------------------------

    def _seed_wilder_rsi(self) -> None:
        prices = self._warmup_closes
        gains, losses = [], []
        for i in range(1, len(prices)):
            delta = prices[i] - prices[i - 1]
            gains.append(max(delta, 0.0))
            losses.append(max(-delta, 0.0))
        self._avg_gain = sum(gains) / self.RSI_PERIOD
        self._avg_loss = sum(losses) / self.RSI_PERIOD
        self._prev_close = prices[-1]
        self._warmup_closes.clear()
        self.logger.info(
            f"Wilder RSI({self.RSI_PERIOD}) ready. "
            f"avg_gain={self._avg_gain:.6f}  avg_loss={self._avg_loss:.6f}"
        )

    def _update_wilder_rsi(self, close: float) -> None:
        if self._prev_close is None:
            self._prev_close = close
            return
        delta = close - self._prev_close
        alpha = 1.0 / self.RSI_PERIOD
        self._avg_gain = alpha * max(delta, 0.0) + (1 - alpha) * self._avg_gain
        self._avg_loss = alpha * max(-delta, 0.0) + (1 - alpha) * self._avg_loss
        self._prev_close = close

    def _compute_rsi(self) -> Optional[float]:
        if self._avg_gain is None or self._avg_loss is None:
            return None
        if self._avg_loss == 0:
            return 100.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    # ------------------------------------------------------------------
    # Order helpers
    # ------------------------------------------------------------------

    async def _open_position(self, price: float, side: str, rsi: float) -> None:
        """Open a LONG (side=BUY) or SHORT (side=SELL) position."""
        usdt = await self.engine.get_balance(self.name, "USDT")
        if usdt < 10:
            self.logger.warning("Insufficient USDT for margin")
            return

        spend = usdt * self.TRADE_FRACTION
        quantity = round(spend / price, 6)
        direction = "LONG" if side == "BUY" else "SHORT"

        try:
            result = await self.engine.place_order(
                bot_id=self.name,
                symbol=self.symbol,
                side=side,
                quantity=quantity,
                price=price,
            )
            self._last_trade_candle = self._candle_count
            self.logger.info(
                f"OPEN {direction} {quantity:.6f} @ {price:.2f}  RSI={rsi:.1f}  "
                f"margin={result.get('margin', 0):.2f}  "
                f"liq={result.get('liquidation_price', 0):.2f}  "
                f"fee={result.get('fee_usdt', 0):.4f}  "
                f"(trade_id={result.get('trade_id')})"
            )
        except ValueError as exc:
            self.logger.error(f"OPEN {direction} failed: {exc}")

    async def _close_position(self, price: float, side: str, reason: str) -> None:
        """Close current position. side=BUY closes SHORT, side=SELL closes LONG."""
        try:
            result = await self.engine.place_order(
                bot_id=self.name,
                symbol=self.symbol,
                side=side,
                quantity=0,  # engine uses full position qty on close
                price=price,
            )
            self._last_trade_candle = self._candle_count
            pnl = result.get("realized_pnl", 0)
            self.logger.info(
                f"{reason} @ {price:.2f}  P&L={pnl:+.2f}  "
                f"fee={result.get('fee_usdt', 0):.4f}  "
                f"(trade_id={result.get('trade_id')})"
            )
        except ValueError as exc:
            self.logger.error(f"Close failed: {exc}")
