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

_DT_FMT = "%Y-%m-%dT%H:%M:%S.%f"


def _dt_to_str(dt: datetime) -> str:
    return dt.strftime(_DT_FMT)


def _str_to_dt(s: str) -> datetime:
    return datetime.strptime(s, _DT_FMT)


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
        created_at=_str_to_dt(row["created_at"]),
        updated_at=_str_to_dt(row["updated_at"]),
    )


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
            position_side=row["position_side"] if "position_side" in row.keys() else "LONG",
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
# LLM Decisions Log
# ------------------------------------------------------------------

async def insert_llm_decision(
    prompt_summary: str,
    response_json: str,
    actions_taken: str,
    success: bool = True,
    error_message: Optional[str] = None,
) -> int:
    """Log an LLM decision to the database."""
    db = get_db()
    now = _dt_to_str(datetime.now(timezone.utc))
    cursor = await db.execute(
        """INSERT INTO llm_decisions
           (timestamp, prompt_summary, response_json, actions_taken, success, error_message)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (now, prompt_summary, response_json, actions_taken, 1 if success else 0, error_message),
    )
    await db.commit()
    return cursor.lastrowid


async def get_llm_decisions(limit: int = 20) -> list[dict]:
    """Fetch recent LLM decisions, newest first."""
    db = get_db()
    async with db.execute(
        "SELECT * FROM llm_decisions ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    ) as cursor:
        rows = []
        async for row in cursor:
            rows.append({
                "id": row["id"],
                "timestamp": row["timestamp"],
                "prompt_summary": row["prompt_summary"],
                "response_json": row["response_json"],
                "actions_taken": row["actions_taken"],
                "success": bool(row["success"]),
                "error_message": row["error_message"],
            })
    return rows


# ------------------------------------------------------------------
# Historical candles
# ------------------------------------------------------------------

async def save_historical_candles(rows: list[tuple]) -> int:
    """
    Bulk-insert historical candles. Each row is a tuple:
        (symbol, open_time_ms, open, high, low, close, volume, close_time_ms)
    Uses INSERT OR REPLACE so re-downloads overwrite cleanly.
    Returns number of rows inserted.
    """
    db = get_db()
    await db.executemany(
        """INSERT OR REPLACE INTO historical_candles
           (symbol, open_time, open, high, low, close, volume, close_time)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    await db.commit()
    return len(rows)


async def get_historical_candles(
    symbol: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict]:
    """
    Fetch historical candles for a symbol, sorted by open_time ASC.
    Optionally filter by time range (epoch milliseconds).
    """
    db = get_db()
    query = "SELECT * FROM historical_candles WHERE symbol = ?"
    params: list = [symbol]

    if start_ms is not None:
        query += " AND open_time >= ?"
        params.append(start_ms)
    if end_ms is not None:
        query += " AND open_time <= ?"
        params.append(end_ms)

    query += " ORDER BY open_time ASC"

    async with db.execute(query, params) as cursor:
        rows = []
        async for row in cursor:
            rows.append({
                "symbol": row["symbol"],
                "open_time": row["open_time"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
                "close_time": row["close_time"],
            })
    return rows


async def count_historical_candles(symbol: str) -> int:
    """Return the number of stored historical candles for a symbol."""
    db = get_db()
    async with db.execute(
        "SELECT COUNT(*) as cnt FROM historical_candles WHERE symbol = ?",
        (symbol,),
    ) as cursor:
        row = await cursor.fetchone()
    return row["cnt"] if row else 0


async def get_historical_range(symbol: str) -> dict | None:
    """
    Return the time range of stored candles for a symbol.
    Returns {min_time, max_time, count} or None.
    """
    db = get_db()
    async with db.execute(
        """SELECT MIN(open_time) as min_t, MAX(open_time) as max_t, COUNT(*) as cnt
           FROM historical_candles WHERE symbol = ?""",
        (symbol,),
    ) as cursor:
        row = await cursor.fetchone()
    if not row or row["cnt"] == 0:
        return None
    return {
        "min_time": row["min_t"],
        "max_time": row["max_t"],
        "count": row["cnt"],
    }


async def delete_historical_candles(symbol: str) -> int:
    """Delete all historical candles for a symbol. Returns deleted count."""
    db = get_db()
    count = await count_historical_candles(symbol)
    await db.execute("DELETE FROM historical_candles WHERE symbol = ?", (symbol,))
    await db.commit()
    return count


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


# ====================================================================
# Orderbook (DOM) snapshots
# ====================================================================

async def get_orderbook_snapshots_for_backtest(symbol: str) -> list[dict]:
    """
    Load ALL orderbook snapshots for a symbol ordered oldest→newest.
    Used by the backtest engine to replay historical orderbook state
    alongside candle data. Returns compact dicts (no raw bids/asks JSON
    to save memory — only metrics needed for signal detection + raw levels).
    """
    db = get_db()

    # Check if table exists
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='orderbook_snapshots'"
    ) as cur:
        if await cur.fetchone() is None:
            return []

    rows = []
    async with db.execute("""
        SELECT timestamp, depth_limit, bids_json, asks_json,
               best_bid, best_ask, spread, mid_price,
               bid_depth_usdt, ask_depth_usdt, imbalance
        FROM orderbook_snapshots
        WHERE symbol = ?
        ORDER BY timestamp ASC
    """, (symbol,)) as cursor:
        async for row in cursor:
            rows.append({
                "timestamp": row["timestamp"],
                "depth_limit": row["depth_limit"],
                "bids": row["bids_json"],   # raw JSON string — parsed lazily by bot
                "asks": row["asks_json"],
                "best_bid": row["best_bid"],
                "best_ask": row["best_ask"],
                "spread": row["spread"],
                "mid_price": row["mid_price"],
                "bid_depth_usdt": row["bid_depth_usdt"],
                "ask_depth_usdt": row["ask_depth_usdt"],
                "imbalance": row["imbalance"],
            })

    return rows


async def get_orderbook_status() -> dict:
    """
    Return collection stats for each symbol in orderbook_snapshots:
      count, first/last timestamp, latest metrics.
    """
    db = get_db()

    # Check if table exists (script may not have run yet)
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='orderbook_snapshots'"
    ) as cur:
        if await cur.fetchone() is None:
            return {}

    result = {}
    async with db.execute("""
        SELECT symbol,
               COUNT(*) as cnt,
               MIN(timestamp) as first_ts,
               MAX(timestamp) as last_ts
        FROM orderbook_snapshots
        GROUP BY symbol
        ORDER BY symbol
    """) as cursor:
        async for row in cursor:
            result[row["symbol"]] = {
                "count": row["cnt"],
                "first": row["first_ts"],
                "last": row["last_ts"],
            }

    # Add latest metrics for each symbol
    for symbol in list(result.keys()):
        async with db.execute("""
            SELECT best_bid, best_ask, spread, mid_price,
                   bid_depth_usdt, ask_depth_usdt, imbalance
            FROM orderbook_snapshots
            WHERE symbol = ?
            ORDER BY timestamp DESC
            LIMIT 1
        """, (symbol,)) as cur:
            row = await cur.fetchone()
            if row:
                result[symbol]["latest"] = {
                    "best_bid": row["best_bid"],
                    "best_ask": row["best_ask"],
                    "spread": row["spread"],
                    "mid_price": row["mid_price"],
                    "bid_depth_usdt": row["bid_depth_usdt"],
                    "ask_depth_usdt": row["ask_depth_usdt"],
                    "imbalance": row["imbalance"],
                }

    return result


async def get_orderbook_snapshots(
    symbol: str,
    limit: int = 60,
) -> list[dict]:
    """
    Fetch recent orderbook snapshots for a symbol (newest first).
    Returns summary metrics only (not full bids/asks JSON).
    """
    db = get_db()
    async with db.execute("""
        SELECT symbol, timestamp, best_bid, best_ask, spread,
               mid_price, bid_depth_usdt, ask_depth_usdt, imbalance
        FROM orderbook_snapshots
        WHERE symbol = ?
        ORDER BY timestamp DESC
        LIMIT ?
    """, (symbol, limit)) as cursor:
        rows = []
        async for row in cursor:
            rows.append({
                "symbol": row["symbol"],
                "timestamp": row["timestamp"],
                "best_bid": row["best_bid"],
                "best_ask": row["best_ask"],
                "spread": row["spread"],
                "mid_price": row["mid_price"],
                "bid_depth_usdt": row["bid_depth_usdt"],
                "ask_depth_usdt": row["ask_depth_usdt"],
                "imbalance": row["imbalance"],
            })
        return rows


async def get_orderbook_full(symbol: str) -> dict | None:
    """
    Get the latest full orderbook snapshot (including bids/asks JSON)
    for a single symbol. Returns None if no data.
    """
    db = get_db()

    # Check if table exists
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='orderbook_snapshots'"
    ) as cur:
        if await cur.fetchone() is None:
            return None

    async with db.execute("""
        SELECT symbol, timestamp, depth_limit, bids_json, asks_json,
               best_bid, best_ask, spread, mid_price,
               bid_depth_usdt, ask_depth_usdt, imbalance
        FROM orderbook_snapshots
        WHERE symbol = ?
        ORDER BY timestamp DESC
        LIMIT 1
    """, (symbol,)) as cur:
        row = await cur.fetchone()
        if row is None:
            return None
        import json
        return {
            "symbol": row["symbol"],
            "timestamp": row["timestamp"],
            "depth_limit": row["depth_limit"],
            "bids": json.loads(row["bids_json"]),
            "asks": json.loads(row["asks_json"]),
            "best_bid": row["best_bid"],
            "best_ask": row["best_ask"],
            "spread": row["spread"],
            "mid_price": row["mid_price"],
            "bid_depth_usdt": row["bid_depth_usdt"],
            "ask_depth_usdt": row["ask_depth_usdt"],
            "imbalance": row["imbalance"],
        }
