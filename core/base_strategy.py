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
from abc import ABC, abstractmethod
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
    """

    name: str       # subclasses must define this as a class attribute
    symbol: str     # subclasses must define this as a class attribute

    # Override in subclasses to define tunable parameters.
    # Format: { "PARAM_NAME": { "type": "int"|"float", "default": N,
    #           "min": N, "max": N, "description": "..." }, ... }
    PARAM_SCHEMA: dict[str, dict] = {}

    def __init__(self, engine: "BaseOrderEngine") -> None:
        self.engine = engine
        self.is_running: bool = False
        self._task = None          # asyncio.Task, set by BotManager
        self._price_queue = None   # asyncio.Queue, set by BotManager
        self._candle_queue = None  # asyncio.Queue, set by BotManager
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

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name} symbol={self.symbol} running={self.is_running}>"
