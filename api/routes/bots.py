"""
API routes for bot management.

GET  /api/bots              — list all registered bots
GET  /api/bots/{name}       — get a single bot's status + portfolio
POST /api/bots/{name}/start — start a bot
POST /api/bots/{name}/stop  — stop a bot
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/bots", tags=["Bots"])


# Injected at startup from main.py
_bot_manager = None


def set_bot_manager(manager) -> None:
    global _bot_manager
    _bot_manager = manager


def _get_manager():
    if _bot_manager is None:
        raise RuntimeError("BotManager not injected")
    return _bot_manager


# ------------------------------------------------------------------
# Response models
# ------------------------------------------------------------------

class BotSummary(BaseModel):
    name: str
    symbol: str
    is_running: bool


class BotDetail(BaseModel):
    name: str
    symbol: str
    is_running: bool
    portfolio: dict


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@router.get("", response_model=list[BotSummary])
async def list_bots():
    """List all registered bots and their running status."""
    return _get_manager().list_bots()


@router.get("/{name}", response_model=BotDetail)
async def get_bot(name: str):
    """Get detailed status and current portfolio for a single bot."""
    manager = _get_manager()
    bot = manager.get_bot(name)
    if bot is None:
        raise HTTPException(status_code=404, detail=f"Bot '{name}' not found")
    stats = await bot.get_stats()
    return stats


@router.post("/{name}/start")
async def start_bot(name: str):
    """Start a registered bot."""
    manager = _get_manager()
    if manager.get_bot(name) is None:
        raise HTTPException(status_code=404, detail=f"Bot '{name}' not found")
    try:
        await manager.start_bot(name)
        return {"message": f"Bot '{name}' started"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/{name}/stop")
async def stop_bot(name: str):
    """Stop a running bot."""
    manager = _get_manager()
    if manager.get_bot(name) is None:
        raise HTTPException(status_code=404, detail=f"Bot '{name}' not found")
    try:
        await manager.stop_bot(name)
        return {"message": f"Bot '{name}' stopped"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
