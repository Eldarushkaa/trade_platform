"""
API routes for trade history.

GET /api/trades/{bot_name}?limit=100&offset=0  — paginated trade history
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from datetime import datetime
from typing import Optional

from db import repository as repo

router = APIRouter(prefix="/trades", tags=["Trades"])


class TradeOut(BaseModel):
    id: Optional[int]
    bot_id: str
    side: str
    symbol: str
    quantity: float
    price: float
    realized_pnl: Optional[float]
    fee_usdt: Optional[float]
    position_side: str = "LONG"
    timestamp: datetime


@router.get("/{bot_name}", response_model=list[TradeOut])
async def get_trades(
    bot_name: str,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """
    Return paginated trade history for a bot, newest first.

    Args:
        bot_name: The bot's unique name.
        limit:    Max trades to return (1–1000, default 100).
        offset:   Pagination offset (default 0).
    """
    bot_record = await repo.get_bot(bot_name)
    if bot_record is None:
        raise HTTPException(status_code=404, detail=f"Bot '{bot_name}' not found")

    trades = await repo.get_trades_for_bot(bot_name, limit=limit, offset=offset)
    return [
        TradeOut(
            id=t.id,
            bot_id=t.bot_id,
            side=t.side,
            symbol=t.symbol,
            quantity=t.quantity,
            price=t.price,
            realized_pnl=t.realized_pnl,
            fee_usdt=t.fee_usdt,
            position_side=t.position_side,
            timestamp=t.timestamp,
        )
        for t in trades
    ]


@router.get("/{bot_name}/count")
async def get_trade_count(bot_name: str):
    """Return the total number of trades for a bot."""
    bot_record = await repo.get_bot(bot_name)
    if bot_record is None:
        raise HTTPException(status_code=404, detail=f"Bot '{bot_name}' not found")
    count = await repo.get_trade_count(bot_name)
    return {"bot_id": bot_name, "trade_count": count}
