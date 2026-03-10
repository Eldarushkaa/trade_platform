"""
Database repository — all SQL queries live here.
Every method is async and uses the shared aiosqlite connection from database.py.
"""
import logging
from datetime import datetime
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
        (status, _dt_to_str(datetime.utcnow()), bot_id),
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


async def get_trade_count(bot_id: str) -> int:
    db = get_db()
    async with db.execute(
        "SELECT COUNT(*) as cnt FROM trades WHERE bot_id = ?", (bot_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["cnt"] if row else 0


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
