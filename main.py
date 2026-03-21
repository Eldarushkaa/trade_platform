"""
Trade Platform — main entrypoint.

Run with:
    python main.py
    # or
    uvicorn main:app --reload --host 127.0.0.1 --port 8000

Adding a new bot:
    1. Create strategies/my_bot.py subclassing BaseStrategy
    2. Import it below and add to STRATEGY_CLASSES
    3. Restart — the bot appears on the dashboard automatically

Data flow:
    Binance WebSocket
        → BinanceFeed (raw aggTrade ticks)
            → PriceCache (latest price per symbol + pub/sub)
                → BotManager.dispatch_price()   (updates engine price cache)
                → CandleAggregator.on_tick()    (builds 5-min OHLC candles)
                    → BotManager.dispatch_candle() (queues candle to bots)
                        → Bot.on_candle(candle)     (strategy logic fires here)

Bot instances (1 strategy × 3 coins = 3 bots):
    rsi_btc, rsi_eth, rsi_sol  — RSI crossover + EMA proximity + vol + slope filters
"""
import asyncio
import logging
import uvicorn
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from config import settings
from db.database import init_db, close_db
from core.simulation_engine import SimulationEngine
from core.bot_manager import BotManager
from data.binance_feed import BinanceFeed
from data.price_cache import price_cache
from data.candle_aggregator import CandleAggregator
from data.orderbook_feed import fetch_depth

# ------------------------------------------------------------------
# Import strategy classes
# ------------------------------------------------------------------
from strategies.rsi import RSIBot
from strategies.donchian import DonchianBot

# ------------------------------------------------------------------
# Configuration: coins to trade and strategies to run
# ------------------------------------------------------------------
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

STRATEGY_CLASSES = [RSIBot, DonchianBot]

# Build 6 bot classes: RSI + Donchian, each for 3 symbols
REGISTERED_BOTS = (
    [RSIBot.for_symbol(sym) for sym in SYMBOLS] +
    [DonchianBot.for_symbol(sym) for sym in SYMBOLS]
)

# ------------------------------------------------------------------
# Configure logging
# ------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Install in-memory log handler for the /api/logs endpoint (WARNING+)
from api.routes.logs import install_handler as _install_log_handler
_install_log_handler()

# ------------------------------------------------------------------
# Global singletons
# ------------------------------------------------------------------
# ob_fetcher=fetch_depth: every place_order() call fetches a live Binance
# depth snapshot and walks the OB levels for a realistic VWAP fill price.
# This is used only for live-mode order execution — not for UI display.
simulation_engine = SimulationEngine(ob_fetcher=fetch_depth)
bot_manager = BotManager(engine=simulation_engine)
candle_aggregator = CandleAggregator(interval_seconds=900)  # 15-minute candles

# ------------------------------------------------------------------
# FastAPI lifespan: startup / shutdown
# ------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic for the FastAPI app."""
    logger.info(
        f"Starting Trade Platform in [{settings.trading_mode.upper()}] mode | "
        f"Fee: {settings.simulation_fee_rate * 100:.3f}% | "
        f"Candle interval: 15m | "
        f"Bots: {len(REGISTERED_BOTS)} ({len(SYMBOLS)} coins)"
    )

    # 1. Initialize database (runs migrations automatically)
    await init_db()

    # 2. Register all configured bots
    for bot_class in REGISTERED_BOTS:
        bot_manager.register(bot_class)

    # 3. Restore state + start bots that have live_enabled=True persisted in DB.
    #    - upsert_bot: creates DB record if missing (idempotent)
    #    - _restore_balance_from_snapshot: reloads USDT balance + open position
    #    - load_saved_params: reloads parameter overrides
    #    - start_bot: only if live_enabled=True (default False — user must enable via UI)
    from db import repository as repo
    from db.models import BotRecord
    from datetime import datetime, timezone
    live_started = 0
    for bot_class in REGISTERED_BOTS:
        bot = bot_manager.get_bot(bot_class.name)
        # Ensure DB record exists (idempotent — does not overwrite live_enabled)
        record = BotRecord(
            id=bot_class.name,
            symbol=bot.symbol,
            status="stopped",
            initial_balance=settings.initial_usdt_balance,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        await repo.upsert_bot(record)

        # Restore portfolio balance + open position from last DB snapshot
        await bot_manager._restore_balance_from_snapshot(bot_class.name)

        # Reload saved parameter overrides
        await bot_manager.load_saved_params(bot_class.name)

        # Auto-start if user had it running before the restart
        bot_record = await repo.get_bot(bot_class.name)
        if bot_record and bot_record.live_enabled:
            await bot_manager.start_bot(bot_class.name)
            live_started += 1

    if REGISTERED_BOTS:
        logger.info(
            f"Registered {len(REGISTERED_BOTS)} bot(s), "
            f"{live_started} started (live_enabled=True): "
            f"{', '.join(b.name for b in REGISTERED_BOTS)}"
        )
    else:
        logger.info("No bots registered. Add bots to STRATEGY_CLASSES in main.py")

    # 4. Wire data pipeline:
    #    PriceCache → BotManager (engine price updates)
    price_cache.subscribe(bot_manager.dispatch_price)
    #    PriceCache → CandleAggregator (candle building)
    price_cache.subscribe(candle_aggregator.on_tick)
    #    CandleAggregator → BotManager (completed candles → bots)
    candle_aggregator.subscribe(bot_manager.dispatch_candle)

    # 5. Start Binance WebSocket feed
    symbols = list({bot_class.symbol for bot_class in REGISTERED_BOTS})
    feed = BinanceFeed(symbols=symbols, cache=price_cache)
    feed_task = asyncio.create_task(feed.start(), name="binance-feed")
    logger.info(f"Binance feed started for symbols: {symbols}")

    # 6. Wire app.state so all routes can access singletons
    app.state.bot_manager = bot_manager
    app.state.engine = simulation_engine
    app.state.symbols = SYMBOLS

    yield  # ← App is running

    # ------ Shutdown ------
    logger.info("Shutting down...")
    await feed.stop()
    feed_task.cancel()
    try:
        await feed_task
    except asyncio.CancelledError:
        pass
    price_cache.unsubscribe(bot_manager.dispatch_price)
    price_cache.unsubscribe(candle_aggregator.on_tick)
    # Flush any partial in-progress candle before stopping bots
    await candle_aggregator.flush()
    candle_aggregator.unsubscribe(bot_manager.dispatch_candle)
    await bot_manager.stop_all()
    await close_db()
    logger.info("Shutdown complete")


# ------------------------------------------------------------------
# FastAPI app
# ------------------------------------------------------------------
app = FastAPI(
    title="Trade Platform",
    description="Crypto trading bot simulation platform",
    version="0.3.0",
    lifespan=lifespan,
)

# ------------------------------------------------------------------
# Include API routers
# ------------------------------------------------------------------
from api.routes import bots as bots_router
from api.routes import trades as trades_router
from api.routes import portfolio as portfolio_router
from api.routes import backtest as backtest_router
from api.routes import logs as logs_router

app.include_router(bots_router.router, prefix="/api")
app.include_router(trades_router.router, prefix="/api")
app.include_router(portfolio_router.router, prefix="/api")
app.include_router(backtest_router.router, prefix="/api")
app.include_router(logs_router.router, prefix="/api")

# ------------------------------------------------------------------
# Serve static dashboard
# ------------------------------------------------------------------
app.mount("/static", StaticFiles(directory="api/static"), name="static")


@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse("api/static/index.html")


# ------------------------------------------------------------------
# Health check
# ------------------------------------------------------------------
@app.get("/health", tags=["System"])
async def health():
    """Quick liveness check."""
    return {
        "status": "ok",
        "mode": settings.trading_mode,
        "leverage": settings.leverage,
        "fee_rate_pct": settings.simulation_fee_rate * 100,
        "candle_interval": "15m",
        "symbols": SYMBOLS,
        "strategies": [cls.__name__ for cls in STRATEGY_CLASSES],
        "bots": bot_manager.list_bots(),
    }


# ------------------------------------------------------------------
# Dev entrypoint
# ------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )
