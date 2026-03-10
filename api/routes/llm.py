"""
API routes for LLM Agent management.

GET  /api/llm/status     — current agent status
GET  /api/llm/log        — recent LLM decisions
POST /api/llm/trigger    — manually trigger one decision cycle
POST /api/llm/enable     — enable the agent
POST /api/llm/disable    — disable the agent
"""
from fastapi import APIRouter, HTTPException

from core import llm_agent
from db import repository as repo

router = APIRouter(prefix="/llm", tags=["LLM Agent"])


@router.get("/status")
async def get_status():
    """Return the current LLM agent status and configuration."""
    status = llm_agent.get_status()
    # Add last decision timestamp
    decisions = await repo.get_llm_decisions(limit=1)
    status["last_decision"] = decisions[0] if decisions else None
    return status


@router.get("/log")
async def get_log(limit: int = 20):
    """Return recent LLM decisions, newest first."""
    if limit < 1 or limit > 100:
        limit = 20
    return await repo.get_llm_decisions(limit=limit)


@router.post("/trigger")
async def trigger_cycle():
    """Manually trigger one LLM decision cycle (for testing)."""
    if not llm_agent.is_enabled():
        raise HTTPException(
            status_code=400,
            detail="LLM agent is not enabled. Set LLM_ENABLED=true and LLM_API_KEY in config.",
        )
    try:
        result = await llm_agent.run_decision_cycle()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/enable")
async def enable_agent():
    """Enable the LLM agent at runtime."""
    from config import settings
    if not settings.llm_api_key:
        raise HTTPException(
            status_code=400,
            detail="Cannot enable: LLM_API_KEY is not set in config/.env",
        )
    await llm_agent.start_agent()
    return {"message": "LLM agent enabled", "status": llm_agent.get_status()}


@router.post("/disable")
async def disable_agent():
    """Disable the LLM agent at runtime."""
    await llm_agent.stop_agent()
    return {"message": "LLM agent disabled", "status": llm_agent.get_status()}
