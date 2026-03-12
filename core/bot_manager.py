"""
BotManager — lifecycle management for all trading bots.

Responsibilities:
- Register bots and their virtual portfolios with the engine
- Start/stop bots as independent asyncio Tasks
- Periodically save portfolio snapshots to the DB
- Expose bot state for the API layer

Usage:
    manager = BotManager(engine=simulation_engine)
    manager.register(MyBot)   # pass the class, not an instance
    await manager.start_all()
    await manager.stop_bot("my_bot")
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Type

from core.base_strategy import BaseStrategy
from core.simulation_engine import BaseOrderEngine, SimulationEngine
from db import repository as repo
from db.models import BotRecord
from config import settings

logger = logging.getLogger(__name__)


class BotManager:
    """
    Manages the full lifecycle of all registered trading bots.

    Thread-safety note: designed for a single asyncio event loop.
    All public methods are async-safe.
    """

    def __init__(self, engine: BaseOrderEngine) -> None:
        self.engine = engine
        # bot_id → strategy instance
        self._bots: dict[str, BaseStrategy] = {}
        # bot_id → asyncio.Task (the main candle loop for that bot)
        self._tasks: dict[str, asyncio.Task] = {}
        # bot_id → asyncio.Task (periodic snapshot saver)
        self._snapshot_tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        strategy_class: Type[BaseStrategy],
        initial_usdt: float | None = None,
    ) -> BaseStrategy:
        """
        Register a strategy class with the manager.

        Creates the bot instance, registers its virtual portfolio with the
        engine, and persists a bot record to the database.

        Args:
            strategy_class: A subclass of BaseStrategy (the class, not an instance).
            initial_usdt:   Override starting USDT balance (uses config default if None).

        Returns:
            The created strategy instance.
        """
        bot_id = strategy_class.name

        if bot_id in self._bots:
            logger.warning(f"Bot '{bot_id}' is already registered. Skipping.")
            return self._bots[bot_id]

        # Wire the engine into the bot
        bot = strategy_class(engine=self.engine)

        # Register portfolio with SimulationEngine
        if isinstance(self.engine, SimulationEngine):
            self.engine.register_bot(
                bot_id=bot_id,
                symbol=bot.symbol,
                initial_usdt=initial_usdt,
            )

        self._bots[bot_id] = bot
        logger.info(f"Registered bot '{bot_id}' ({strategy_class.__name__}) on {bot.symbol}")
        return bot

    async def load_saved_params(self, bot_id: str) -> None:
        """Load saved parameter overrides from the DB and apply to the bot instance."""
        saved = await repo.get_bot_params(bot_id)
        if saved:
            bot = self._get_bot(bot_id)
            try:
                bot.set_params(saved)
                logger.info(f"Loaded saved params for '{bot_id}': {saved}")
            except ValueError as exc:
                logger.warning(f"Ignoring invalid saved params for '{bot_id}': {exc}")

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    async def start_bot(self, bot_id: str) -> None:
        """Start a single bot by its name."""
        bot = self._get_bot(bot_id)

        if bot.is_running:
            logger.warning(f"Bot '{bot_id}' is already running.")
            return

        await bot.start()
        await repo.update_bot_status(bot_id, "running")

        # Candle queue: receives completed Candle objects from CandleAggregator
        bot._candle_queue = asyncio.Queue()
        # Price queue: receives raw ticks (used for engine price cache updates only)
        bot._price_queue = asyncio.Queue()

        task = asyncio.create_task(
            self._candle_loop(bot),
            name=f"bot-{bot_id}",
        )
        self._tasks[bot_id] = task
        task.add_done_callback(lambda t: self._on_task_done(bot_id, t))

        # Start periodic snapshot task
        snap_task = asyncio.create_task(
            self._snapshot_loop(bot_id),
            name=f"snapshot-{bot_id}",
        )
        self._snapshot_tasks[bot_id] = snap_task

        logger.info(f"Bot '{bot_id}' started")

    async def stop_bot(self, bot_id: str) -> None:
        """Stop a single bot by its name."""
        bot = self._get_bot(bot_id)

        if not bot.is_running:
            logger.warning(f"Bot '{bot_id}' is not running.")
            return

        await bot.stop()
        await repo.update_bot_status(bot_id, "stopped")

        # Cancel the main loop task
        task = self._tasks.pop(bot_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Cancel the snapshot task
        snap_task = self._snapshot_tasks.pop(bot_id, None)
        if snap_task and not snap_task.done():
            snap_task.cancel()
            try:
                await snap_task
            except asyncio.CancelledError:
                pass

        logger.info(f"Bot '{bot_id}' stopped")

    async def start_all(self) -> None:
        """Start all registered bots and persist their DB records."""
        for bot_id, bot in self._bots.items():
            # Persist bot record on first start
            record = BotRecord(
                id=bot_id,
                symbol=bot.symbol,
                status="stopped",
                initial_balance=settings.initial_usdt_balance,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            await repo.upsert_bot(record)

            # Restore portfolio balance from last snapshot (survives restarts)
            if isinstance(self.engine, SimulationEngine):
                await self._restore_balance_from_snapshot(bot_id)

            # Load any saved parameter overrides from DB
            await self.load_saved_params(bot_id)
            await self.start_bot(bot_id)

    async def stop_all(self) -> None:
        """Stop all running bots gracefully."""
        for bot_id in list(self._tasks.keys()):
            await self.stop_bot(bot_id)

    # ------------------------------------------------------------------
    # Price / Candle dispatch
    # ------------------------------------------------------------------

    async def dispatch_price(self, symbol: str, price: float) -> None:
        """
        Update the engine's price cache with the latest tick.
        Raw ticks are NOT forwarded to bots directly — the CandleAggregator
        handles that by calling dispatch_candle() at candle close.

        Called by PriceCache on every tick (via subscription).
        """
        if isinstance(self.engine, SimulationEngine):
            self.engine.update_price(symbol, price)

    async def dispatch_candle(self, candle) -> None:
        """
        Dispatch a completed candle to all bots that trade the candle's symbol.
        Called by CandleAggregator once per completed candle interval.

        Args:
            candle: Completed Candle object from CandleAggregator.
        """
        for bot in self._bots.values():
            if bot.is_running and bot.symbol == candle.symbol:
                if bot._candle_queue is not None:
                    await bot._candle_queue.put(candle)

    # ------------------------------------------------------------------
    # State queries (used by API)
    # ------------------------------------------------------------------

    def list_bots(self) -> list[dict]:
        """Return summary info for all registered bots."""
        return [
            {
                "name": bot.name,
                "symbol": bot.symbol,
                "is_running": bot.is_running,
            }
            for bot in self._bots.values()
        ]

    def get_bot(self, bot_id: str) -> BaseStrategy | None:
        """Return a bot instance by id, or None if not found."""
        return self._bots.get(bot_id)

    async def reset_bot(self, bot_id: str) -> dict:
        """
        Reset a bot's trading state to defaults: stop it, clear trades/snapshots
        from DB, reset portfolio to initial balance, then restart.
        Keeps: bot params, historical candle data.
        """
        bot = self._get_bot(bot_id)

        # Stop first if running
        was_running = bot.is_running
        if was_running:
            await self.stop_bot(bot_id)

        # Clear DB data
        result = await repo.reset_bot_trading_data(bot_id)

        # Reset in-memory portfolio
        if isinstance(self.engine, SimulationEngine):
            portfolio = self.engine._portfolios.get(bot_id)
            if portfolio:
                portfolio.usdt_balance = settings.initial_usdt_balance
                portfolio.position.reset()
                portfolio.realized_pnl = 0.0
                portfolio.total_fees_paid = 0.0
                portfolio.trade_count = 0
                portfolio.liquidation_count = 0

        # Restart if it was running
        if was_running:
            await self.start_bot(bot_id)

        logger.info(
            f"Reset '{bot_id}': {result['trades_deleted']} trades, "
            f"{result['snapshots_deleted']} snapshots deleted"
        )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _candle_loop(self, bot: BaseStrategy) -> None:
        """
        Main event loop for a single bot.
        Waits for completed Candle objects from CandleAggregator and
        calls bot.on_candle(). The default on_candle() implementation
        forwards candle.close to on_price_update() for backward compat.
        """
        logger.debug(f"Bot '{bot.name}' candle loop started")
        try:
            while bot.is_running:
                try:
                    candle = await asyncio.wait_for(
                        bot._candle_queue.get(),
                        timeout=90.0,   # 1.5× candle interval — warn if no candle
                    )
                    await bot.on_candle(candle)
                except asyncio.TimeoutError:
                    logger.debug(
                        f"Bot '{bot.name}': no candle received for 90s "
                        f"(waiting for next 1-minute close)"
                    )
                except Exception as exc:
                    logger.error(
                        f"Bot '{bot.name}' error in on_candle: {exc}",
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            logger.debug(f"Bot '{bot.name}' candle loop cancelled")
            raise

    async def _snapshot_loop(self, bot_id: str) -> None:
        """Periodically save a portfolio snapshot to the DB."""
        try:
            while True:
                await asyncio.sleep(settings.snapshot_interval_seconds)
                try:
                    if isinstance(self.engine, SimulationEngine):
                        await self.engine.save_snapshot(bot_id)
                        logger.debug(f"Portfolio snapshot saved for '{bot_id}'")
                except Exception as exc:
                    logger.error(f"Snapshot error for '{bot_id}': {exc}", exc_info=True)
        except asyncio.CancelledError:
            pass

    async def _restore_balance_from_snapshot(self, bot_id: str) -> None:
        """
        Restore a bot's portfolio balance and open position from DB on startup.

        Uses total_value_usdt from the snapshot as the single source of truth
        (most reliable — always equals usdt + margin + unrealized at save time).

        Steps:
        1. Load latest snapshot → get total_value_usdt as the anchor.
        2. Check latest trade → if it was OPEN_*, reconstruct the position.
        3. Derive free cash = total_value - margin (unrealized ≈ 0 at restart
           since we don't have a live price yet; first tick will correct it).
        4. Restore trade_count, total_fees_paid, realized_pnl from DB aggregates.
        """
        snap = await repo.get_latest_snapshot(bot_id)
        if snap is None:
            logger.debug(f"No snapshot for '{bot_id}' — starting with default balance")
            return

        portfolio = self.engine._portfolios.get(bot_id)
        if portfolio is None:
            return

        # --- Use total_value_usdt as the reliable anchor ---
        total_value = snap.total_value_usdt

        # If latest snapshot is at default balance, try to find a real one
        if abs(total_value - settings.initial_usdt_balance) < 0.01:
            better_snap = await repo.get_latest_nondefault_snapshot(
                bot_id, settings.initial_usdt_balance
            )
            if better_snap is not None:
                total_value = better_snap.total_value_usdt
                logger.info(
                    f"Restored '{bot_id}' from older non-default snapshot: "
                    f"total=${total_value:.2f} (from {better_snap.timestamp})"
                )

        # --- Check if there's an open position from last trade ---
        last_trade = await repo.get_latest_trade(bot_id)
        has_open_position = False

        if last_trade is not None:
            ps = last_trade.position_side or ""
            if ps.startswith("OPEN_"):
                side = "LONG" if "LONG" in ps else "SHORT"
                qty = last_trade.quantity
                entry = last_trade.price
                notional = qty * entry
                margin = notional / portfolio.leverage

                # Reconstruct position
                portfolio.position.side = side
                portfolio.position.quantity = qty
                portfolio.position.entry_price = entry
                portfolio.position.margin = margin
                if side == "LONG":
                    liq = entry - (margin / qty) if qty > 0 else 0.0
                    portfolio.position.liquidation_price = max(liq, 0.0)
                else:
                    liq = entry + (margin / qty) if qty > 0 else 0.0
                    portfolio.position.liquidation_price = liq

                # Free cash = total - margin (unrealized treated as 0 at restart)
                portfolio.usdt_balance = max(0.0, total_value - margin)
                has_open_position = True

                logger.info(
                    f"Restored '{bot_id}' open {side} position: "
                    f"{qty:.6f} @ {entry:.2f} | margin={margin:.2f} | "
                    f"free={portfolio.usdt_balance:.2f} | total={total_value:.2f}"
                )
            else:
                logger.debug(f"'{bot_id}' last trade was '{ps}' — no open position to restore")

        if not has_open_position:
            # No position — all value is free cash
            portfolio.usdt_balance = total_value

        # --- Restore cumulative counters from DB aggregates ---
        trade_stats = await repo.get_bot_trade_stats(bot_id)
        portfolio.trade_count = trade_stats["trade_count"]
        portfolio.total_fees_paid = trade_stats["total_fees_paid"]
        portfolio.realized_pnl = trade_stats["realized_pnl"]

        logger.info(
            f"Restored '{bot_id}': total={total_value:.2f} | "
            f"free={portfolio.usdt_balance:.2f} USDT | "
            f"position={portfolio.position.side} qty={portfolio.position.quantity:.6f} | "
            f"trades={portfolio.trade_count} fees={portfolio.total_fees_paid:.4f} "
            f"realized_pnl={portfolio.realized_pnl:.4f}"
        )

    def _on_task_done(self, bot_id: str, task: asyncio.Task) -> None:
        """Callback when a bot task finishes unexpectedly."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"Bot '{bot_id}' task crashed: {exc}", exc_info=exc)
            self._bots[bot_id].is_running = False

    def _get_bot(self, bot_id: str) -> BaseStrategy:
        bot = self._bots.get(bot_id)
        if bot is None:
            raise KeyError(f"Bot '{bot_id}' is not registered.")
        return bot
