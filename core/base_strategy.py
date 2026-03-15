"""
Abstract base class for all trading strategies (bots).

To create a new bot:
1. Create a new file in strategies/
2. Subclass BaseStrategy
3. Implement on_candle() with your trading logic (recommended)
   OR implement on_price_update() if you only need the close price
4. Register in main.py REGISTERED_BOTS list

Candle-based trading (recommended):
    Override on_candle(candle) to receive a full OHLCV object at the end
    of each completed 1-minute candle. Use candle.close for the price,
    candle.high/low for range checks, etc.

Tick-based trading (legacy / advanced):
    Override on_price_update(price) if you need every raw tick.
    By default this is a no-op — on_candle() is the preferred interface.

The strategy NEVER interacts with an exchange directly.
It only calls self.engine.place_order() — which routes to either
SimulationEngine or LiveBinanceEngine depending on config.
"""
from abc import ABC
from typing import TYPE_CHECKING, Optional
import logging

if TYPE_CHECKING:
    from core.simulation_engine import BaseOrderEngine
    from data.candle_aggregator import Candle

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """
    Every trading bot must inherit from this class.

    Attributes:
        name:    Unique string identifier for this bot (used as DB primary key).
        symbol:  The trading pair this bot operates on, e.g. "BTCUSDT".
        engine:  Injected by BotManager — routes orders to sim or live exchange.
        strategy_class_name: Human-readable name of the concrete strategy class.
            Set automatically by for_symbol() to the base class name (e.g. "RSIBot"),
            so the API can report it without fragile MRO introspection.
        name_prefix: Short lowercase prefix used by for_symbol() to build the bot
            instance name (e.g. "rsi" → "rsi_btc"). Override in each strategy class.
    """

    name: str       # subclasses must define this as a class attribute
    symbol: str     # subclasses must define this as a class attribute

    # Set to the originating strategy class name by for_symbol().
    # Falls back to the class's own __name__ if for_symbol() was not used.
    strategy_class_name: str = ""

    # Short lowercase identifier used to build bot instance names in for_symbol().
    # Each concrete strategy must define this (e.g. name_prefix = "rsi").
    name_prefix: str = ""

    # Override in subclasses to define tunable parameters.
    # Format: { "PARAM_NAME": { "type": "int"|"float", "default": N,
    #           "min": N, "max": N, "description": "..." }, ... }
    PARAM_SCHEMA: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Factory — creates a per-symbol subclass at runtime
    # ------------------------------------------------------------------

    @classmethod
    def for_symbol(cls, symbol: str) -> type:
        """Return a new subclass of *cls* bound to *symbol*.

        The returned class has:
            - ``name``  = ``{cls.name_prefix}_{asset}``  (e.g. "rsi_btc")
            - ``symbol`` = the supplied trading-pair string  (e.g. "BTCUSDT")
            - ``strategy_class_name`` = ``cls.__name__``     (e.g. "RSIBot")

        Each concrete strategy must set ``name_prefix`` so this method can
        build a unique per-coin bot id.
        """
        asset = symbol.replace("USDT", "").lower()
        prefix = cls.name_prefix or cls.__name__.lower()
        return type(
            f"{cls.__name__}_{asset.upper()}",
            (cls,),
            {
                "name": f"{prefix}_{asset}",
                "symbol": symbol,
                "strategy_class_name": cls.__name__,
            },
        )

    def __init__(self, engine: "BaseOrderEngine") -> None:
        self.engine = engine
        self.is_running: bool = False
        self._task = None          # asyncio.Task, set by BotManager
        self._candle_queue = None  # asyncio.Queue, set by BotManager
        # Candle counter and cooldown tracker shared by all strategies
        self._candle_count: int = 0
        self._last_trade_candle: int = -999
        self.logger = logging.getLogger(f"strategy.{self.name}")

    # ------------------------------------------------------------------
    # Parameter introspection and live editing
    # ------------------------------------------------------------------

    def get_params(self) -> dict:
        """Return current parameter values with schema metadata."""
        result = {}
        for key, schema in self.PARAM_SCHEMA.items():
            result[key] = {
                "value": getattr(self, key, schema["default"]),
                **schema,
            }
        return result

    def set_params(self, updates: dict) -> dict:
        """
        Validate and apply parameter updates. Returns the applied values.

        Raises ValueError with details if any value is invalid.
        """
        errors = []
        coerced = {}

        for key, value in updates.items():
            if key not in self.PARAM_SCHEMA:
                errors.append(f"Unknown parameter: {key}")
                continue

            schema = self.PARAM_SCHEMA[key]
            ptype = schema["type"]

            # Type coercion
            try:
                if ptype == "int":
                    value = int(value)
                elif ptype == "float":
                    value = float(value)
            except (TypeError, ValueError):
                errors.append(f"{key}: expected {ptype}, got {type(value).__name__}")
                continue

            # Range validation
            if "min" in schema and value < schema["min"]:
                errors.append(f"{key}: {value} < min ({schema['min']})")
                continue
            if "max" in schema and value > schema["max"]:
                errors.append(f"{key}: {value} > max ({schema['max']})")
                continue

            coerced[key] = value

        if errors:
            raise ValueError("; ".join(errors))

        # Apply all validated values
        applied = {}
        for key, value in coerced.items():
            setattr(self, key, value)
            applied[key] = value
            self.logger.info(f"Parameter {key} updated to {value}")

        return applied

    # ------------------------------------------------------------------
    # Primary interface — implement on_candle() for candle-based trading
    # ------------------------------------------------------------------

    async def on_candle(self, candle: "Candle") -> None:
        """
        Called once per completed candle (e.g. every 1 minute).

        This is the RECOMMENDED place for trading logic.
        Override this in your strategy.

        Args:
            candle: Completed OHLCV candle with .open .high .low .close .volume
        """
        # Default: forward the close price to on_price_update for backward compat
        await self.on_price_update(candle.close)

    async def on_price_update(self, price: float) -> None:
        """
        Called with raw price ticks OR candle close prices (if on_candle not overridden).

        Override this ONLY if you need tick-level granularity.
        For most strategies, override on_candle() instead.

        Args:
            price: Price value (raw tick or candle close).
        """
        pass  # No-op by default — strategies override on_candle() instead

    # ------------------------------------------------------------------
    # Optional lifecycle hooks — override if needed
    # ------------------------------------------------------------------

    async def on_start(self) -> None:
        """Called once when the bot is started. Override to set up state."""
        pass

    async def on_stop(self) -> None:
        """Called once when the bot is stopped. Override to clean up state."""
        pass

    # ------------------------------------------------------------------
    # Lifecycle management (called by BotManager, not overridden)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Mark bot as running and call on_start hook."""
        self.is_running = True
        self.logger.info(f"Bot '{self.name}' starting on {self.symbol}")
        await self.on_start()

    async def stop(self) -> None:
        """Mark bot as stopped and call on_stop hook."""
        self.is_running = False
        self.logger.info(f"Bot '{self.name}' stopping")
        await self.on_stop()

    async def get_stats(self) -> dict:
        """Return a summary dict for the dashboard API."""
        portfolio = await self.engine.get_portfolio_state(self.name)
        return {
            "name": self.name,
            "symbol": self.symbol,
            "is_running": self.is_running,
            "portfolio": portfolio,
        }

    # ------------------------------------------------------------------
    # Shared order helpers — override in subclasses for custom logging
    # ------------------------------------------------------------------

    async def _open_position(self, price: float, side: str, **log_extra) -> "dict | None":
        """Open a fraction-sized market order.

        Checks USDT balance (minimum 10 USDT), sizes the order using
        ``self.TRADE_FRACTION``, calls ``engine.place_order()``, and updates
        ``_last_trade_candle``.

        Keyword arguments in *log_extra* are appended to the log line so
        subclasses can include indicator values (RSI, MACD, bands, etc.)
        without duplicating all of the boilerplate.

        Returns the raw order result dict, or None if the order could not
        be placed (insufficient balance or engine rejection).
        """
        usdt = await self.engine.get_balance(self.name, "USDT")
        if usdt < 10:
            self.logger.warning("Insufficient USDT for margin")
            return None

        trade_fraction = getattr(self, "TRADE_FRACTION", 0.95)
        spend = usdt * trade_fraction
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
            extra_str = "  ".join(f"{k}={v}" for k, v in log_extra.items())
            self.logger.info(
                f"OPEN {direction} {quantity:.6f} @ {price:.4f}"
                + (f"  {extra_str}" if extra_str else "")
                + f"  fee={result.get('fee_usdt', 0):.4f}"
                + f"  (trade_id={result.get('trade_id')})"
            )
            return result
        except ValueError as exc:
            self.logger.error(f"OPEN {direction} failed: {exc}")
            return None

    async def _close_position(self, price: float, side: str, reason: str) -> "dict | None":
        """Close the full current position with a market order.

        Passes ``quantity=0`` to the engine which interprets it as "close all".
        Updates ``_last_trade_candle`` and logs the realized P&L.

        Returns the raw order result dict, or None on engine rejection.
        """
        try:
            result = await self.engine.place_order(
                bot_id=self.name,
                symbol=self.symbol,
                side=side,
                quantity=0,  # engine closes full position on qty=0
                price=price,
            )
            self._last_trade_candle = self._candle_count
            pnl = result.get("realized_pnl", 0)
            self.logger.info(
                f"{reason} @ {price:.4f}  P&L={pnl:+.4f}"
                f"  fee={result.get('fee_usdt', 0):.4f}"
                f"  (trade_id={result.get('trade_id')})"
            )
            return result
        except ValueError as exc:
            self.logger.error(f"Close failed: {exc}")
            return None

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name} symbol={self.symbol} running={self.is_running}>"
