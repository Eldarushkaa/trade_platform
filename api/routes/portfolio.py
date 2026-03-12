"""
API routes for portfolio data.

GET /api/portfolio/{bot_name}          — current live portfolio state (futures)
GET /api/portfolio/{bot_name}/history  — historical snapshots (for charting)
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from datetime import datetime
from typing import Optional

from db import repository as repo

router = APIRouter(prefix="/portfolio", tags=["Portfolio"])

# Injected at startup
_engine = None


def set_engine(engine) -> None:
    global _engine
    _engine = engine


class SnapshotOut(BaseModel):
    id: Optional[int]
    bot_id: str
    usdt_balance: float
    asset_balance: float
    asset_symbol: str
    total_value_usdt: float
    asset_price: Optional[float] = None
    timestamp: datetime


@router.get("/coin-positions")
async def get_coin_positions():
    """
    Return per-symbol aggregate position view across all bots.

    Response:
      coin_positions: {symbol: {total_long_qty, total_short_qty, net_qty, net_side,
                                long_bots, short_bots}}
    """
    if _engine is None:
        raise HTTPException(status_code=503, detail="Engine not available")
    return {
        "coin_positions": _engine.get_coin_positions(),
    }


@router.get("/all")
async def get_all_portfolios():
    """
    Return current portfolio state for every registered bot in one call.
    Used by the dashboard global stats bar and bot card mini-stats.
    Returns a list of portfolio state dicts, each including bot_id, symbol,
    position_side, total_value_usdt, usdt_balance, return_pct, trade_count, etc.

    NOTE: This route MUST be declared before /{bot_name} to prevent FastAPI
    from matching the literal "all" as a bot_name path parameter.
    """
    if _engine is None:
        raise HTTPException(status_code=503, detail="Engine not available")

    bots = await repo.get_all_bots()
    results = []
    for bot in bots:
        try:
            state = await _engine.get_portfolio_state(bot.id)  # BotRecord uses .id not .bot_id
            results.append(state)
        except (KeyError, Exception):
            pass
    return results


@router.get("/{bot_name}")
async def get_portfolio(bot_name: str):
    """
    Return the current live portfolio state for a bot.

    Futures fields returned:
      - position_side (LONG / SHORT / NONE)
      - position_qty, entry_price
      - leverage, margin_locked, liquidation_price, margin_ratio
      - realized_pnl, unrealized_pnl, net_pnl
      - total_value_usdt, return_pct
      - trade_count, total_fees_paid, liquidation_count
    """
    if _engine is None:
        raise HTTPException(status_code=503, detail="Engine not available")

    bot_record = await repo.get_bot(bot_name)
    if bot_record is None:
        raise HTTPException(status_code=404, detail=f"Bot '{bot_name}' not found")

    try:
        state = await _engine.get_portfolio_state(bot_name)
        return state
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Portfolio for '{bot_name}' not found in engine")


@router.get("/{bot_name}/history", response_model=list[SnapshotOut])
async def get_portfolio_history(
    bot_name: str,
    limit: int = Query(default=200, ge=1, le=1000),
):
    """
    Return historical portfolio snapshots for charting.
    Snapshots are saved every N seconds (configured via snapshot_interval_seconds).
    Returns up to `limit` entries ordered oldest→newest.
    """
    bot_record = await repo.get_bot(bot_name)
    if bot_record is None:
        raise HTTPException(status_code=404, detail=f"Bot '{bot_name}' not found")

    snapshots = await repo.get_snapshots_for_bot(bot_name, limit=limit)
    return [
        SnapshotOut(
            id=s.id,
            bot_id=s.bot_id,
            usdt_balance=s.usdt_balance,
            asset_balance=s.asset_balance,
            asset_symbol=s.asset_symbol,
            total_value_usdt=s.total_value_usdt,
            asset_price=s.asset_price,
            timestamp=s.timestamp,
        )
        for s in snapshots
    ]
