"""
Database repository — all SQL queries live here.
Every method is async and uses the shared aiosqlite connection from database.py.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from db.database import get_db
from db.models import BotRecord, TradeRecord, PortfolioSnapshot

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _dt_to_str(dt: datetime) -> str:
    """Serialize *dt* to an ISO-8601 string with explicit +00:00 UTC offset.

    Always normalises to UTC so SQLite lexicographic string comparisons remain
    correct (all stored values share the same fixed suffix).
    """
    if dt.tzinfo is None:
        # Assume naïve datetimes are already UTC
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "+00:00"


def _str_to_dt(s: str) -> datetime:
    """Deserialize an ISO-8601 datetime string into a UTC-aware datetime.

    Handles both the old naïve format (no suffix) and the new +00:00 format
    so existing rows in the DB continue to deserialise correctly.
    """
    s_clean = s.replace("+00:00", "").rstrip("Z")
    dt = datetime.strptime(s_clean, "%Y-%m-%dT%H:%M:%S.%f")
    return dt.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------
# Bot CRUD
# ------------------------------------------------------------------

async def upsert_bot(bot: BotRecord) -> None:
    """Insert or update a bot record."""
    db = get_db()
    await db.execute(
        """
        INSERT INTO bots (id, symbol, status, initial_balance, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        (
            bot.id,
            bot.symbol,
            bot.status,
            bot.initial_balance,
            _dt_to_str(bot.created_at),
            _dt_to_str(bot.updated_at),
        ),
    )
    await db.commit()


async def update_bot_status(bot_id: str, status: str) -> None:
    """Update just the status field of a bot."""
    db = get_db()
    await db.execute(
        "UPDATE bots SET status = ?, updated_at = ? WHERE id = ?",
        (status, _dt_to_str(datetime.now(timezone.utc)), bot_id),
    )
    await db.commit()


async def get_all_bots() -> list[BotRecord]:
    """Fetch all bot records."""
    db = get_db()
    async with db.execute("SELECT * FROM bots ORDER BY created_at DESC") as cursor:
        rows = await cursor.fetchall()
    return [
        BotRecord(
            id=row["id"],
            symbol=row["symbol"],
            status=row["status"],
            initial_balance=row["initial_balance"],
            live_enabled=bool(row["live_enabled"]) if "live_enabled" in row.keys() else False,
            created_at=_str_to_dt(row["created_at"]),
            updated_at=_str_to_dt(row["updated_at"]),
        )
        for row in rows
    ]


async def get_bot(bot_id: str) -> Optional[BotRecord]:
    """Fetch a single bot by id."""
    db = get_db()
    async with db.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return BotRecord(
        id=row["id"],
        symbol=row["symbol"],
        status=row["status"],
        initial_balance=row["initial_balance"],
        live_enabled=bool(row["live_enabled"]) if "live_enabled" in row.keys() else False,
        created_at=_str_to_dt(row["created_at"]),
        updated_at=_str_to_dt(row["updated_at"]),
    )


async def set_bot_live_enabled(bot_id: str, enabled: bool) -> None:
    """Set the live_enabled flag for a bot (persists across restarts)."""
    db = get_db()
    await db.execute(
        "UPDATE bots SET live_enabled = ?, updated_at = ? WHERE id = ?",
        (1 if enabled else 0, _dt_to_str(datetime.now(timezone.utc)), bot_id),
    )
    await db.commit()


# ------------------------------------------------------------------
# Trades
# ------------------------------------------------------------------

async def insert_trade(trade: TradeRecord) -> int:
    """Insert a trade and return the auto-generated id."""
    db = get_db()
    cursor = await db.execute(
        """
        INSERT INTO trades (bot_id, side, symbol, quantity, price, realized_pnl, fee_usdt, position_side, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trade.bot_id,
            trade.side,
            trade.symbol,
            trade.quantity,
            trade.price,
            trade.realized_pnl,
            trade.fee_usdt,
            trade.position_side,
            _dt_to_str(trade.timestamp),
        ),
    )
    await db.commit()
    return cursor.lastrowid


async def get_trades_for_bot(
    bot_id: str,
    limit: int = 100,
    offset: int = 0,
) -> list[TradeRecord]:
    """Fetch recent trades for a bot, newest first."""
    db = get_db()
    async with db.execute(
        """
        SELECT * FROM trades WHERE bot_id = ?
        ORDER BY timestamp DESC LIMIT ? OFFSET ?
        """,
        (bot_id, limit, offset),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        TradeRecord(
            id=row["id"],
            bot_id=row["bot_id"],
            side=row["side"],
            symbol=row["symbol"],
            quantity=row["quantity"],
            price=row["price"],
            realized_pnl=row["realized_pnl"],
            fee_usdt=row["fee_usdt"],
            position_side=row["position_side"],
            timestamp=_str_to_dt(row["timestamp"]),
        )
        for row in rows
    ]


async def get_latest_trade(bot_id: str) -> Optional[TradeRecord]:
    """Return the most recent trade for a bot, or None if no trades exist."""
    db = get_db()
    async with db.execute(
        "SELECT * FROM trades WHERE bot_id = ? ORDER BY timestamp DESC LIMIT 1",
        (bot_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return TradeRecord(
        id=row["id"],
        bot_id=row["bot_id"],
        side=row["side"],
        symbol=row["symbol"],
        quantity=row["quantity"],
        price=row["price"],
        realized_pnl=row["realized_pnl"],
        fee_usdt=row["fee_usdt"],
        position_side=row["position_side"] if "position_side" in row.keys() else "LONG",
        timestamp=_str_to_dt(row["timestamp"]),
    )


async def get_trade_count(bot_id: str) -> int:
    db = get_db()
    async with db.execute(
        "SELECT COUNT(*) as cnt FROM trades WHERE bot_id = ?", (bot_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["cnt"] if row else 0


async def get_bot_trade_stats(bot_id: str) -> dict:
    """Return aggregated trade stats for a bot: count, total fees, total realized PnL."""
    db = get_db()
    async with db.execute(
        """
        SELECT
            COUNT(*) AS trade_count,
            COALESCE(SUM(fee_usdt), 0.0) AS total_fees_paid,
            COALESCE(SUM(realized_pnl), 0.0) AS realized_pnl
        FROM trades
        WHERE bot_id = ?
        """,
        (bot_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return {"trade_count": 0, "total_fees_paid": 0.0, "realized_pnl": 0.0}
    return {
        "trade_count": row["trade_count"],
        "total_fees_paid": float(row["total_fees_paid"]),
        "realized_pnl": float(row["realized_pnl"]),
    }


async def get_bot_trade_stats_since(bot_id: str, since: "datetime") -> dict:
    """Return aggregated trade stats for a bot since a given datetime.
    Includes: trade_count, total_fees_paid, realized_pnl, win_count, loss_count."""
    db = get_db()
    # DB now stores "+00:00" suffixed UTC strings. Serialise since consistently.
    since_str = _dt_to_str(since)
    async with db.execute(
        """
        SELECT
            COUNT(*) AS trade_count,
            COALESCE(SUM(fee_usdt), 0.0) AS total_fees_paid,
            COALESCE(SUM(realized_pnl), 0.0) AS realized_pnl,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS win_count,
            SUM(CASE WHEN realized_pnl <= 0 AND realized_pnl IS NOT NULL THEN 1 ELSE 0 END) AS loss_count
        FROM trades
        WHERE bot_id = ? AND timestamp >= ?
        """,
        (bot_id, since_str),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return {"trade_count": 0, "total_fees_paid": 0.0, "realized_pnl": 0.0, "win_count": 0, "loss_count": 0}
    return {
        "trade_count": row["trade_count"] or 0,
        "total_fees_paid": float(row["total_fees_paid"] or 0),
        "realized_pnl": float(row["realized_pnl"] or 0),
        "win_count": row["win_count"] or 0,
        "loss_count": row["loss_count"] or 0,
    }


# ------------------------------------------------------------------
# Portfolio Snapshots
# ------------------------------------------------------------------

async def insert_snapshot(snap: PortfolioSnapshot) -> None:
    """Persist a portfolio state snapshot."""
    db = get_db()
    await db.execute(
        """
        INSERT INTO portfolio_snapshots
            (bot_id, usdt_balance, asset_balance, asset_symbol, total_value_usdt, asset_price, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snap.bot_id,
            snap.usdt_balance,
            snap.asset_balance,
            snap.asset_symbol,
            snap.total_value_usdt,
            snap.asset_price,
            _dt_to_str(snap.timestamp),
        ),
    )
    await db.commit()


async def get_snapshots_for_bot(
    bot_id: str,
    limit: int = 200,
) -> list[PortfolioSnapshot]:
    """Fetch portfolio history for a bot, oldest first (good for charting)."""
    db = get_db()
    async with db.execute(
        """
        SELECT * FROM (
            SELECT * FROM portfolio_snapshots WHERE bot_id = ?
            ORDER BY timestamp DESC LIMIT ?
        ) ORDER BY timestamp ASC
        """,
        (bot_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        PortfolioSnapshot(
            id=row["id"],
            bot_id=row["bot_id"],
            usdt_balance=row["usdt_balance"],
            asset_balance=row["asset_balance"],
            asset_symbol=row["asset_symbol"],
            total_value_usdt=row["total_value_usdt"],
            asset_price=row["asset_price"] if "asset_price" in row.keys() else None,
            timestamp=_str_to_dt(row["timestamp"]),
        )
        for row in rows
    ]


async def get_latest_snapshot(bot_id: str) -> Optional[PortfolioSnapshot]:
    """Get the most recent portfolio snapshot for a bot."""
    db = get_db()
    async with db.execute(
        "SELECT * FROM portfolio_snapshots WHERE bot_id = ? ORDER BY timestamp DESC LIMIT 1",
        (bot_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return PortfolioSnapshot(
        id=row["id"],
        bot_id=row["bot_id"],
        usdt_balance=row["usdt_balance"],
        asset_balance=row["asset_balance"],
        asset_symbol=row["asset_symbol"],
        total_value_usdt=row["total_value_usdt"],
        asset_price=row["asset_price"] if "asset_price" in row.keys() else None,
        timestamp=_str_to_dt(row["timestamp"]),
    )


async def get_latest_nondefault_snapshot(
    bot_id: str, default_balance: float
) -> Optional[PortfolioSnapshot]:
    """
    Find the most recent snapshot whose total_value_usdt differs from the
    default initial balance. Used on restart to skip over snapshots that were
    saved after a cold start (when balances were reset to defaults).
    """
    db = get_db()
    # Allow 1 USDT tolerance to account for rounding
    async with db.execute(
        """SELECT * FROM portfolio_snapshots
           WHERE bot_id = ? AND ABS(total_value_usdt - ?) > 1.0
           ORDER BY timestamp DESC LIMIT 1""",
        (bot_id, default_balance),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return PortfolioSnapshot(
        id=row["id"],
        bot_id=row["bot_id"],
        usdt_balance=row["usdt_balance"],
        asset_balance=row["asset_balance"],
        asset_symbol=row["asset_symbol"],
        total_value_usdt=row["total_value_usdt"],
        asset_price=row["asset_price"] if "asset_price" in row.keys() else None,
        timestamp=_str_to_dt(row["timestamp"]),
    )


# ------------------------------------------------------------------
# Bot Parameters
# ------------------------------------------------------------------

async def get_bot_params(bot_id: str) -> Optional[dict]:
    """Load saved parameter overrides for a bot. Returns None if no saved params."""
    db = get_db()
    async with db.execute(
        "SELECT params_json FROM bot_params WHERE bot_id = ?",
        (bot_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["params_json"])
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"Corrupt params_json for bot '{bot_id}', ignoring")
        return None


async def save_bot_params(bot_id: str, params: dict) -> None:
    """Persist parameter overrides for a bot (upsert)."""
    db = get_db()
    now = _dt_to_str(datetime.now(timezone.utc))
    params_json = json.dumps(params)
    await db.execute(
        """INSERT INTO bot_params (bot_id, params_json, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(bot_id) DO UPDATE SET
               params_json = excluded.params_json,
               updated_at  = excluded.updated_at""",
        (bot_id, params_json, now),
    )
    await db.commit()
    logger.debug(f"Saved params for '{bot_id}': {params}")


# ------------------------------------------------------------------
# Historical candles
# ------------------------------------------------------------------

async def save_historical_candles(rows: list[tuple], interval: str = "15m") -> int:
    """
    Bulk-insert historical candles. Each row is a tuple:
        (symbol, open_time_ms, open, high, low, close, volume, close_time_ms)
    The interval column is set to the provided interval string for all rows.
    Uses INSERT OR REPLACE so re-downloads overwrite cleanly.
    Returns number of rows inserted.
    """
    db = get_db()
    # Inject the interval into each row: (symbol, interval, open_time, open, high, low, close, volume, close_time)
    rows_with_interval = [
        (r[0], interval, r[1], r[2], r[3], r[4], r[5], r[6], r[7])
        for r in rows
    ]
    await db.executemany(
        """INSERT OR REPLACE INTO historical_candles
           (symbol, interval, open_time, open, high, low, close, volume, close_time)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows_with_interval,
    )
    await db.commit()
    return len(rows)


async def get_historical_candles(
    symbol: str,
    interval: str = "15m",
    start_ms: int | None = None,
    end_ms: int | None = None,
    before_ms: int | None = None,
    limit: int | None = None,
) -> list[dict]:
    """
    Fetch historical candles for a symbol+interval, sorted by open_time ASC.
    Optionally filter by time range (epoch milliseconds).

    Args:
        symbol:    Trading pair, e.g. "BTCUSDT"
        interval:  Candle timeframe, e.g. "15m" (default), "1m", "5m", "1h"
        start_ms:  Only candles with open_time >= start_ms
        end_ms:    Only candles with open_time <= end_ms
        before_ms: Only candles with open_time < before_ms (for warmup: get N candles before window)
        limit:     Return at most N candles. When combined with before_ms, returns the LAST N
                   candles before before_ms (DESC LIMIT N, then reversed to ASC order).
    """
    db = get_db()

    # Special case: "last N candles before X" — needs DESC order + limit, then reverse
    if before_ms is not None and limit is not None:
        query = "SELECT * FROM historical_candles WHERE symbol = ? AND interval = ? AND open_time < ?"
        params: list = [symbol, interval, before_ms]
        query += " ORDER BY open_time DESC LIMIT ?"
        params.append(limit)
        async with db.execute(query, params) as cursor:
            rows = []
            async for row in cursor:
                rows.append({
                    "symbol": row["symbol"],
                    "interval": row["interval"],
                    "open_time": row["open_time"],
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "volume": row["volume"],
                    "close_time": row["close_time"],
                })
        rows.reverse()   # restore chronological order
        return rows

    query = "SELECT * FROM historical_candles WHERE symbol = ? AND interval = ?"
    params = [symbol, interval]

    if start_ms is not None:
        query += " AND open_time >= ?"
        params.append(start_ms)
    if end_ms is not None:
        query += " AND open_time <= ?"
        params.append(end_ms)
    if before_ms is not None:
        query += " AND open_time < ?"
        params.append(before_ms)

    query += " ORDER BY open_time ASC"

    if limit is not None:
        query += f" LIMIT {int(limit)}"

    async with db.execute(query, params) as cursor:
        rows = []
        async for row in cursor:
            rows.append({
                "symbol": row["symbol"],
                "interval": row["interval"],
                "open_time": row["open_time"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
                "close_time": row["close_time"],
            })
    return rows


async def count_historical_candles(symbol: str, interval: str = "15m") -> int:
    """Return the number of stored historical candles for a symbol+interval."""
    db = get_db()
    async with db.execute(
        "SELECT COUNT(*) as cnt FROM historical_candles WHERE symbol = ? AND interval = ?",
        (symbol, interval),
    ) as cursor:
        row = await cursor.fetchone()
    return row["cnt"] if row else 0


async def get_historical_range(symbol: str, interval: str = "15m") -> dict | None:
    """
    Return the time range of stored candles for a symbol+interval.
    Returns {min_time, max_time, count} or None.
    """
    db = get_db()
    async with db.execute(
        """SELECT MIN(open_time) as min_t, MAX(open_time) as max_t, COUNT(*) as cnt
           FROM historical_candles WHERE symbol = ? AND interval = ?""",
        (symbol, interval),
    ) as cursor:
        row = await cursor.fetchone()
    if not row or row["cnt"] == 0:
        return None
    return {
        "min_time": row["min_t"],
        "max_time": row["max_t"],
        "count": row["cnt"],
    }


async def delete_historical_candles(symbol: str, interval: str | None = None) -> int:
    """
    Delete historical candles for a symbol (and optionally a specific interval).
    Returns deleted count.
    """
    db = get_db()
    if interval is not None:
        count = await count_historical_candles(symbol, interval)
        await db.execute(
            "DELETE FROM historical_candles WHERE symbol = ? AND interval = ?",
            (symbol, interval),
        )
    else:
        async with db.execute(
            "SELECT COUNT(*) as cnt FROM historical_candles WHERE symbol = ?", (symbol,)
        ) as cur:
            row = await cur.fetchone()
        count = row["cnt"] if row else 0
        await db.execute("DELETE FROM historical_candles WHERE symbol = ?", (symbol,))
    await db.commit()
    return count


# ------------------------------------------------------------------
# Platform settings (key/value store)
# ------------------------------------------------------------------

async def get_platform_setting(key: str, default: str | None = None) -> str | None:
    """Get a platform-wide setting by key. Returns default if not set."""
    db = get_db()
    async with db.execute(
        "SELECT value FROM platform_settings WHERE key = ?", (key,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["value"] if row else default


async def set_platform_setting(key: str, value: str) -> None:
    """Persist a platform-wide setting (upsert)."""
    db = get_db()
    await db.execute(
        "INSERT INTO platform_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await db.commit()


async def reset_bot_trading_data(bot_id: str) -> dict:
    """
    Delete all trades and snapshots for a bot (keeps params and historical candles).
    Returns counts of deleted records.
    """
    db = get_db()

    async with db.execute("SELECT COUNT(*) FROM trades WHERE bot_id = ?", (bot_id,)) as cur:
        trades_count = (await cur.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM portfolio_snapshots WHERE bot_id = ?", (bot_id,)) as cur:
        snaps_count = (await cur.fetchone())[0]

    await db.execute("DELETE FROM trades WHERE bot_id = ?", (bot_id,))
    await db.execute("DELETE FROM portfolio_snapshots WHERE bot_id = ?", (bot_id,))
    await db.commit()

    return {"trades_deleted": trades_count, "snapshots_deleted": snaps_count}


