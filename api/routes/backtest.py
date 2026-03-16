"""
API routes for Backtesting & Optimization.

POST /api/backtest/download       — download historical klines from Binance
GET  /api/backtest/data-status    — check what historical data is stored
POST /api/backtest/run            — run a backtest for one bot
POST /api/backtest/optimize       — optimize params for one bot
GET  /api/backtest/status         — check running backtest/optimization status
"""
import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.backtest_engine import run_backtest
from core.optimizer import optimize_params
from data.historical import download_klines, get_data_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/backtest", tags=["Backtest"])

# ------------------------------------------------------------------
# Module-level state — task tracking only (no injected dependencies)
# ------------------------------------------------------------------
_running_tasks: dict[str, dict] = {}   # task_id → {task, type, status, result, completed_at}

_TASK_TTL_SECONDS = 3600  # evict completed tasks after 1 hour


def _evict_old_tasks() -> None:
    """Remove completed tasks that finished more than _TASK_TTL_SECONDS ago."""
    now = time.monotonic()
    stale = [
        tid for tid, info in _running_tasks.items()
        if info.get("done") and (now - info.get("completed_at", now)) > _TASK_TTL_SECONDS
    ]
    for tid in stale:
        _running_tasks.pop(tid, None)
    if stale:
        logger.debug(f"Evicted {len(stale)} stale task(s): {stale}")


def _get_bot_manager(request: Request):
    manager = getattr(request.app.state, "bot_manager", None)
    if manager is None:
        raise HTTPException(status_code=500, detail="Bot manager not initialized")
    return manager


def _get_symbols(request: Request) -> list[str]:
    return getattr(request.app.state, "symbols", [])


# ------------------------------------------------------------------
# Request/response models
# ------------------------------------------------------------------

class DownloadRequest(BaseModel):
    symbols: list[str] | None = None   # defaults to all configured symbols
    days: int = 14
    start_date: str | None = None      # optional ISO date "YYYY-MM-DD"; window = [start_date, start_date+days]


class BacktestRequest(BaseModel):
    bot_id: str
    params: dict | None = None         # optional param overrides (uses current if None)
    fee_rate: float | None = None      # fee rate override, e.g. 0.0007 (0.07%); None → default
    start_date: str | None = None      # optional start filter "YYYY-MM-DD" UTC
    end_date: str | None = None        # optional end filter   "YYYY-MM-DD" UTC


class OptimizeRequest(BaseModel):
    bot_id: str
    iterations: int = 500              # max optimization iterations (200/500/1000/2000/5000)
    fee_rate: float | None = None      # fee rate override, e.g. 0.0007 (0.07%); None → default


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _get_bot_info(request: Request, bot_id: str) -> tuple:
    """
    Get (strategy_class, symbol, current_params) for a registered bot.
    The strategy_class returned is the for_symbol() subclass, ready to instantiate.
    """
    bot = _get_bot_manager(request).get_bot(bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail=f"Bot '{bot_id}' not found")

    # Get the class of the running bot (it's a for_symbol() subclass)
    strategy_class = type(bot)
    symbol = bot.symbol
    current_params = {k: v["value"] for k, v in bot.get_params().items()}

    return strategy_class, symbol, current_params


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@router.post("/download")
async def download_historical(request: Request, req: DownloadRequest):
    """Download historical 5m klines from Binance and store in DB.

    - ``days=180`` with no ``start_date`` → 6 months ending now (training window)
    - ``days=14`` with ``start_date="YYYY-MM-DD"`` → 14d starting from that date (test window)
    """
    symbols = req.symbols or _get_symbols(request)
    if not symbols:
        raise HTTPException(status_code=400, detail="No symbols specified")

    days = max(1, min(req.days, 1095))  # cap at 3 years

    # Validate start_date if provided
    start_date = req.start_date
    if start_date:
        try:
            from datetime import datetime
            datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="start_date must be YYYY-MM-DD format")

    # Run download for each symbol
    results = []
    for sym in symbols:
        try:
            result = await download_klines(sym, days=days, start_date=start_date)
            results.append(result)
        except Exception as e:
            results.append({
                "symbol": sym,
                "error": str(e),
                "candles_downloaded": 0,
            })

    total = sum(r.get("candles_downloaded", 0) for r in results)
    return {
        "message": f"Downloaded {total} candles for {len(symbols)} symbol(s)",
        "results": results,
    }


@router.get("/data-status")
async def data_status(request: Request):
    """Check what historical candle data is available."""
    symbols = _get_symbols(request) or ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    status = await get_data_status(symbols)
    return {
        "symbols": status,
        "total_candles": sum(s["count"] for s in status.values()),
    }


@router.post("/run")
async def run_backtest_endpoint(request: Request, req: BacktestRequest):
    """
    Run a backtest for one bot using current (or overridden) parameters.
    Returns full results including equity curve and metrics.

    Backtests use 5m candle data — fills execute at candle close price.
    Fee is applied on every order; use ``fee_rate`` to override the default
    (e.g. 0.0007 = 0.07%). Omit to use the platform default.

    Use ``start_date`` / ``end_date`` (YYYY-MM-DD) to restrict the candle
    window — e.g. run on the held-out 14d test set rather than all data.
    """
    from datetime import datetime, timezone

    strategy_class, symbol, current_params = _get_bot_info(request, req.bot_id)

    # Use provided params or fall back to current
    params = req.params if req.params else current_params

    task_id = f"bt_{req.bot_id}"

    # Check if already running
    if task_id in _running_tasks and not _running_tasks[task_id].get("done", True):
        raise HTTPException(status_code=409, detail="A backtest is already running for this bot")

    # Convert optional date strings → epoch ms for DB filtering
    start_ms: int | None = None
    end_ms: int | None = None
    try:
        if req.start_date:
            start_ms = int(
                datetime.strptime(req.start_date, "%Y-%m-%d")
                .replace(tzinfo=timezone.utc)
                .timestamp() * 1000
            )
        if req.end_date:
            # end_date is inclusive — advance to end of that day
            from datetime import timedelta
            end_ms = int(
                (datetime.strptime(req.end_date, "%Y-%m-%d")
                 .replace(tzinfo=timezone.utc) + timedelta(days=1))
                .timestamp() * 1000
            ) - 1
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")

    # Run backtest (blocking for the request — typically < 10 seconds for 6m of 5m candles)
    # Fill model: candle close price. Cost model: fee_rate only (no slippage).
    try:
        result = await run_backtest(
            bot_id=f"bt_{req.bot_id}",
            symbol=symbol,
            strategy_class=strategy_class,
            params=params,
            fee_rate=req.fee_rate,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        return result.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Backtest error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/optimize")
async def optimize_endpoint(request: Request, req: OptimizeRequest):
    """
    Run parameter optimization for one bot.
    Uses a genetic algorithm (population, crossover, adaptive mutation, restarts)
    to find params that maximize a composite fitness (Sharpe + return − drawdown).
    This is a long-running operation — starts in background and returns task_id.
    Poll /api/backtest/status?task_id=... for progress.
    """
    strategy_class, symbol, current_params = _get_bot_info(request, req.bot_id)

    iterations = max(10, min(req.iterations, 10000))
    task_id = f"opt_{req.bot_id}"

    # Check if already running
    if task_id in _running_tasks and not _running_tasks[task_id].get("done", True):
        raise HTTPException(status_code=409, detail="An optimization is already running for this bot")

    # Progress tracker
    progress_state = {"pct": 0, "msg": "Starting..."}

    async def on_progress(pct, msg):
        progress_state["pct"] = pct
        progress_state["msg"] = msg

    # Run in background
    async def _run():
        try:
            import os
            # Use CPU cores for parallel backtest eval (default 4, capped at 8)
            cpu_count = os.cpu_count() or 4
            concurrency = min(8, max(2, cpu_count))

            result = await optimize_params(
                bot_id=req.bot_id,
                symbol=symbol,
                strategy_class=strategy_class,
                current_params=current_params,
                max_iterations=iterations,
                fee_rate=req.fee_rate,
                progress_callback=on_progress,
                concurrency=concurrency,
            )
            _running_tasks[task_id]["result"] = result.to_dict()
            _running_tasks[task_id]["done"] = True
            _running_tasks[task_id]["status"] = "completed"
            _running_tasks[task_id]["completed_at"] = time.monotonic()
        except Exception as e:
            logger.error(f"Optimization error: {e}", exc_info=True)
            _running_tasks[task_id]["done"] = True
            _running_tasks[task_id]["status"] = "error"
            _running_tasks[task_id]["error"] = str(e)
            _running_tasks[task_id]["completed_at"] = time.monotonic()

    task = asyncio.create_task(_run(), name=task_id)
    _running_tasks[task_id] = {
        "task": task,
        "type": "optimize",
        "bot_id": req.bot_id,
        "iterations": iterations,
        "done": False,
        "status": "running",
        "progress": progress_state,
        "result": None,
        "error": None,
    }

    return {
        "task_id": task_id,
        "status": "started",
        "bot_id": req.bot_id,
        "iterations": iterations,
        "message": f"Optimization started. Poll /api/backtest/status?task_id={task_id} for progress.",
    }


@router.get("/status")
async def get_status(task_id: str | None = None):
    """
    Get status of running/completed backtest or optimization tasks.
    If task_id is given, return that task. Otherwise return all.
    Completed tasks are automatically evicted after 1 hour.
    """
    _evict_old_tasks()
    if task_id:
        info = _running_tasks.get(task_id)
        if not info:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
        return {
            "task_id": task_id,
            "type": info["type"],
            "bot_id": info["bot_id"],
            "status": info["status"],
            "progress": info.get("progress", {}),
            "result": info.get("result"),
            "error": info.get("error"),
        }

    # Return all tasks (summary)
    tasks = []
    for tid, info in _running_tasks.items():
        tasks.append({
            "task_id": tid,
            "type": info["type"],
            "bot_id": info["bot_id"],
            "status": info["status"],
            "progress": info.get("progress", {}),
            "has_result": info.get("result") is not None,
        })
    return {"tasks": tasks}
