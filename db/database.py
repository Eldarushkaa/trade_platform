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

    await db.commit()
