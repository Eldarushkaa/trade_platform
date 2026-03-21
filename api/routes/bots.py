"""
API routes for bot management.

GET   /api/bots                    — list all registered bots
GET   /api/bots/{name}             — get a single bot's status + portfolio
POST  /api/bots/{name}/start       — start a bot
POST  /api/bots/{name}/stop        — stop a bot
PATCH /api/bots/{name}/live        — toggle live_enabled (persisted to DB)
GET   /api/bots/{name}/params      — get current parameters + schema
PUT   /api/bots/{name}/params      — update strategy parameters
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from db import repository as repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bots", tags=["Bots"])


def _get_manager(request: Request):
    """Read BotManager from app.state (stored during startup)."""
    manager = getattr(request.app.state, "bot_manager", None)
    if manager is None:
        raise HTTPException(status_code=500, detail="BotManager not initialized")
    return manager


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
async def list_bots(request: Request):
    """List all registered bots and their running status."""
    return _get_manager(request).list_bots()


@router.get("/{name}", response_model=BotDetail)
async def get_bot(request: Request, name: str):
    """Get detailed status and current portfolio for a single bot."""
    manager = _get_manager(request)
    bot = manager.get_bot(name)
    if bot is None:
        raise HTTPException(status_code=404, detail=f"Bot '{name}' not found")
    stats = await bot.get_stats()
    return stats


@router.post("/{name}/start")
async def start_bot(request: Request, name: str):
    """Start a registered bot and persist live_enabled=True so it auto-starts on restart."""
    manager = _get_manager(request)
    if manager.get_bot(name) is None:
        raise HTTPException(status_code=404, detail=f"Bot '{name}' not found")
    try:
        await manager.start_bot(name)
        await repo.set_bot_live_enabled(name, True)
        return {"message": f"Bot '{name}' started"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/{name}/stop")
async def stop_bot(request: Request, name: str):
    """Stop a running bot and persist live_enabled=False so it stays paused on restart."""
    manager = _get_manager(request)
    if manager.get_bot(name) is None:
        raise HTTPException(status_code=404, detail=f"Bot '{name}' not found")
    try:
        await manager.stop_bot(name)
        await repo.set_bot_live_enabled(name, False)
        return {"message": f"Bot '{name}' stopped"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.patch("/{name}/live")
async def set_live_enabled(request: Request, name: str, body: dict):
    """
    Toggle live_enabled for a bot (persisted to DB, survives restarts).

    Body: {"enabled": true|false}

    live_enabled controls ONLY whether the bot auto-starts on server restart.
    It does NOT start/stop the bot immediately — use POST /{name}/start or /stop for that.

    When enabled=true:  bot will auto-start on next server restart.
    When enabled=false: bot stays paused on next restart (existing run is unaffected).
    """
    manager = _get_manager(request)
    bot = manager.get_bot(name)
    if bot is None:
        raise HTTPException(status_code=404, detail=f"Bot '{name}' not found")

    enabled = bool(body.get("enabled", False))
    try:
        await repo.set_bot_live_enabled(name, enabled)
        logger.info(f"Bot '{name}' live_enabled set to {enabled} (no immediate start/stop)")
        return {"bot_id": name, "live_enabled": enabled, "message": f"Bot '{name}' live_enabled set to {enabled}"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ------------------------------------------------------------------
# Reset trading data
# ------------------------------------------------------------------

@router.post("/{name}/reset")
async def reset_bot(request: Request, name: str):
    """
    Reset a bot's trading state to defaults.
    Clears all trades and snapshots, resets balance to initial.
    Keeps: strategy parameters, historical candle data.
    """
    manager = _get_manager(request)
    if manager.get_bot(name) is None:
        raise HTTPException(status_code=404, detail=f"Bot '{name}' not found")
    try:
        result = await manager.reset_bot(name)
        return {
            "message": f"Bot '{name}' reset to defaults",
            **result,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/reset-all")
async def reset_all_bots(request: Request):
    """Reset ALL bots' trading state to defaults."""
    manager = _get_manager(request)
    total = {"trades_deleted": 0, "snapshots_deleted": 0, "bots_reset": 0}
    for info in manager.list_bots():
        try:
            result = await manager.reset_bot(info["name"])
            total["trades_deleted"] += result["trades_deleted"]
            total["snapshots_deleted"] += result["snapshots_deleted"]
            total["bots_reset"] += 1
        except Exception as exc:
            logger.error(f"Reset error for '{info['name']}': {exc}")
    return {"message": f"All {total['bots_reset']} bots reset", **total}


# ------------------------------------------------------------------
# Parameter editing
# ------------------------------------------------------------------

@router.get("/{name}/params")
async def get_params(request: Request, name: str):
    """Return the current parameter values and schema for a bot."""
    manager = _get_manager(request)
    bot = manager.get_bot(name)
    if bot is None:
        raise HTTPException(status_code=404, detail=f"Bot '{name}' not found")
    strategy_name = (
        bot.strategy_class_name
        or bot.__class__.__name__  # fallback for bots not created via for_symbol()
    )
    return {
        "bot_id": bot.name,
        "strategy": strategy_name,
        "params": bot.get_params(),
    }


@router.put("/{name}/params")
async def update_params(request: Request, name: str, body: dict):
    """
    Update strategy parameters for a bot.

    Accepts a JSON dict of param_name → new_value.
    Validates types and min/max bounds. Returns the applied values.
    Changes take effect immediately on the next candle.
    """
    manager = _get_manager(request)
    bot = manager.get_bot(name)
    if bot is None:
        raise HTTPException(status_code=404, detail=f"Bot '{name}' not found")

    try:
        applied = bot.set_params(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Persist to DB so params survive restarts
    current_values = {k: v["value"] for k, v in bot.get_params().items()}
    await repo.save_bot_params(bot.name, current_values)

    return {
        "message": f"Updated {len(applied)} parameter(s)",
        "applied": applied,
        "params": bot.get_params(),
    }
