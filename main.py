"""
Trade Platform — main entrypoint.

Run with:
    python main.py
    # or
    uvicorn main:app --reload --host 127.0.0.1 --port 8000

Adding a new bot:
    1. Create strategies/my_bot.py subclassing BaseStrategy
    2. Import it below and add to REGISTERED_BOTS
    3. Restart — the bot appears on the dashboard automatically

Data flow:
    Binance WebSocket
        → BinanceFeed (raw aggTrade ticks)
            → PriceCache (latest price per symbol + pub/sub)
                → BotManager.dispatch_price()   (updates engine price cache)
                → CandleAggregator.on_tick()    (builds 1-min OHLC candles)
                    → BotManager.dispatch_candle() (queues candle to bots)
                        → Bot.on_candle(candle)     (strategy logic fires here)
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
from core.simulation_engine import simulation_engine
from core.bot_manager import BotManager
from data.binance_feed import BinanceFeed
from data.price_cache import price_cache
from data.candle_aggregator import CandleAggregator

# ------------------------------------------------------------------
# Import and register your bots here
# ------------------------------------------------------------------
from strategies.example_rsi_bot import RSIBot
from strategies.example_ma_crossover import MACrossoverBot

REGISTERED_BOTS = [
    RSIBot,
    MACrossoverBot,
]

# ------------------------------------------------------------------
# Configure logging
# ------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Global singletons
# ------------------------------------------------------------------
bot_manager = BotManager(engine=simulation_engine)
candle_aggregator = CandleAggregator(interval_seconds=60)  # 1-minute candles

# ------------------------------------------------------------------
# FastAPI lifespan: startup / shutdown
# ------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic for the FastAPI app."""
    logger.info(
        f"Starting Trade Platform in [{settings.trading_mode.upper()}] mode | "
        f"Fee: {settings.simulation_fee_rate * 100:.3f}% | "
        f"Candle interval: 1m"
    )

    # 1. Initialize database (runs migrations automatically)
    await init_db()

    # 2. Register all configured bots
    for bot_class in REGISTERED_BOTS:
        bot_manager.register(bot_class)

    # 3. Start all bots
    if REGISTERED_BOTS:
        await bot_manager.start_all()
        logger.info(f"Started {len(REGISTERED_BOTS)} bot(s)")
    else:
        logger.info("No bots registered. Add bots to REGISTERED_BOTS in main.py")

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
    version="0.2.0",
    lifespan=lifespan,
)

# ------------------------------------------------------------------
# Include API routers
# ------------------------------------------------------------------
from api.routes import bots as bots_router
from api.routes import trades as trades_router
from api.routes import portfolio as portfolio_router

bots_router.set_bot_manager(bot_manager)
portfolio_router.set_engine(simulation_engine)

app.include_router(bots_router.router, prefix="/api")
app.include_router(trades_router.router, prefix="/api")
app.include_router(portfolio_router.router, prefix="/api")

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
        "fee_rate_pct": settings.simulation_fee_rate * 100,
        "candle_interval": "1m",
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
