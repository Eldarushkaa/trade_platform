"""
Database connection management and schema initialization.
Uses aiosqlite for fully async SQLite access.
"""
import aiosqlite
import logging
from config import settings

logger = logging.getLogger(__name__)

# Module-level connection, initialized on app startup
_db: aiosqlite.Connection | None = None


async def init_db() -> None:
    """
    Open the database connection and create all tables if they don't exist.
    Call this once on application startup.
    """
    global _db
    logger.info(f"Initializing database at '{settings.db_path}'")
    _db = await aiosqlite.connect(settings.db_path)
    _db.row_factory = aiosqlite.Row  # rows accessible by column name

    # Enable WAL mode so the collector script can write concurrently
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA busy_timeout=10000")  # wait up to 10 s on lock

    await _create_tables()
    logger.info("Database ready")


async def close_db() -> None:
    """Close the database connection. Call on application shutdown."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None
        logger.info("Database connection closed")


def get_db() -> aiosqlite.Connection:
    """
    Return the active database connection.
    Raises RuntimeError if init_db() has not been called.
    """
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _db


async def _create_tables() -> None:
    """Create all platform tables and run any needed column migrations."""
    db = get_db()

    await db.executescript("""
        CREATE TABLE IF NOT EXISTS bots (
            id              TEXT PRIMARY KEY,
            symbol          TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'stopped',
            initial_balance REAL NOT NULL DEFAULT 10000.0,
            live_enabled    INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id          TEXT NOT NULL REFERENCES bots(id),
            side            TEXT NOT NULL,          -- 'BUY' or 'SELL'
            symbol          TEXT NOT NULL,
            quantity        REAL NOT NULL,
            price           REAL NOT NULL,
            realized_pnl    REAL,                   -- NULL for open, set on close
            fee_usdt        REAL,                   -- trading fee deducted by SimulationEngine
            position_side   TEXT DEFAULT 'LONG',    -- 'OPEN_LONG', 'CLOSE_LONG', 'OPEN_SHORT', 'CLOSE_SHORT'
            timestamp       TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_trades_bot_id ON trades(bot_id);
        CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);

        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id          TEXT NOT NULL REFERENCES bots(id),
            usdt_balance    REAL NOT NULL,
            asset_balance   REAL NOT NULL,
            asset_symbol    TEXT NOT NULL,
            total_value_usdt REAL NOT NULL,
            asset_price     REAL,
            timestamp       TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_bot_id ON portfolio_snapshots(bot_id);
        CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON portfolio_snapshots(timestamp);

        CREATE TABLE IF NOT EXISTS bot_params (
            bot_id      TEXT PRIMARY KEY REFERENCES bots(id),
            params_json TEXT NOT NULL DEFAULT '{}',
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS historical_candles (
            symbol          TEXT NOT NULL,
            interval        TEXT NOT NULL DEFAULT '15m',  -- '1m','5m','15m','1h'
            open_time       INTEGER NOT NULL,     -- UTC epoch milliseconds
            open            REAL NOT NULL,
            high            REAL NOT NULL,
            low             REAL NOT NULL,
            close           REAL NOT NULL,
            volume          REAL NOT NULL,
            close_time      INTEGER NOT NULL,     -- UTC epoch milliseconds
            PRIMARY KEY (symbol, interval, open_time)
        );

        CREATE INDEX IF NOT EXISTS idx_hist_symbol_interval ON historical_candles(symbol, interval);

        CREATE TABLE IF NOT EXISTS platform_settings (
            key     TEXT PRIMARY KEY,
            value   TEXT NOT NULL
        );
    """)

    # --- Additive migrations (safe to run on existing DBs) ---
    async with db.execute("PRAGMA table_info(trades)") as cursor:
        columns = {row["name"] async for row in cursor}
    if "fee_usdt" not in columns:
        await db.execute("ALTER TABLE trades ADD COLUMN fee_usdt REAL")
        logger.info("Migration applied: added 'fee_usdt' column to trades table")
    if "position_side" not in columns:
        await db.execute("ALTER TABLE trades ADD COLUMN position_side TEXT DEFAULT 'LONG'")
        logger.info("Migration applied: added 'position_side' column to trades table")

    async with db.execute("PRAGMA table_info(portfolio_snapshots)") as cursor:
        snap_cols = {row["name"] async for row in cursor}
    if "asset_price" not in snap_cols:
        await db.execute("ALTER TABLE portfolio_snapshots ADD COLUMN asset_price REAL")
        logger.info("Migration applied: added 'asset_price' column to portfolio_snapshots table")

    async with db.execute("PRAGMA table_info(bots)") as cursor:
        bot_cols = {row["name"] async for row in cursor}
    if "live_enabled" not in bot_cols:
        await db.execute("ALTER TABLE bots ADD COLUMN live_enabled INTEGER NOT NULL DEFAULT 0")
        logger.info("Migration applied: added 'live_enabled' column to bots table")

    # historical_candles: add 'interval' column and rebuild unique index if needed
    async with db.execute("PRAGMA table_info(historical_candles)") as cursor:
        hc_cols = {row["name"] async for row in cursor}
    if "interval" not in hc_cols:
        await db.execute("ALTER TABLE historical_candles ADD COLUMN interval TEXT NOT NULL DEFAULT '15m'")
        logger.info("Migration applied: added 'interval' column to historical_candles table")
        # Rebuild the composite index (old idx_hist_symbol may still exist)
        await db.execute("DROP INDEX IF EXISTS idx_hist_symbol")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_hist_symbol_interval ON historical_candles(symbol, interval)"
        )
        logger.info("Migration applied: rebuilt historical_candles index with interval")

    await db.commit()
