"""
LLM Agent — periodic AI-powered bot parameter management.

Calls OpenAI ChatGPT API every N minutes, passes current bot states,
parameters, and recent P&L, then applies the LLM's recommended changes.

OFF by default. Enable via config:
    LLM_ENABLED=true
    LLM_API_KEY=sk-...

The agent uses the same set_params() / start_bot() / stop_bot() APIs
as the dashboard — no special privileges.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from config import settings
from db import repository as repo

logger = logging.getLogger(__name__)

# Will be injected from main.py
_bot_manager = None
_simulation_engine = None


def set_dependencies(bot_manager, engine) -> None:
    """Inject BotManager and SimulationEngine references."""
    global _bot_manager, _simulation_engine
    _bot_manager = bot_manager
    _simulation_engine = engine


# ------------------------------------------------------------------
# System prompt — tells ChatGPT who it is and what it can do
# ------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an AI trading strategy manager for a crypto futures simulation platform.

PLATFORM OVERVIEW:
- USDT-Margined perpetual futures simulation (not real money)
- 3x leverage, 0.05% fee per trade
- 9 bots total: 3 strategies × 3 coins (BTC, ETH, SOL)
- Strategies: RSI (momentum), MACD Crossover (trend), Bollinger Bands (mean reversion)
- Each bot trades independently with its own USDT balance
- 1-minute candle interval

YOUR ROLE:
You periodically receive the current state of all bots and must decide whether
to adjust their strategy parameters to improve performance. You can also
start/stop bots.

AVAILABLE ACTIONS:
1. "set_params" — Change one or more parameters for a bot
2. "start_bot" — Start a stopped bot
3. "stop_bot" — Stop a running bot

GUIDELINES:
- Be conservative. Only change params when you have a clear reason.
- Consider recent P&L trends. If a bot is losing, consider widening thresholds
  or reducing trade fraction. If winning, don't change what works.
- Respect parameter bounds (provided in the data).
- You may choose to do nothing — respond with an empty actions list.
- Maximum {max_actions} actions per decision.
- Explain your reasoning briefly.

RESPONSE FORMAT (strict JSON):
{{
  "reasoning": "Brief explanation of your analysis and decisions",
  "actions": [
    {{"type": "set_params", "bot_id": "rsi_btc", "params": {{"OVERSOLD": 25.0}}}},
    {{"type": "stop_bot", "bot_id": "bb_sol"}},
    {{"type": "start_bot", "bot_id": "bb_sol"}}
  ]
}}

If no changes needed:
{{
  "reasoning": "All bots performing within acceptable range, no changes needed.",
  "actions": []
}}
"""


# ------------------------------------------------------------------
# Data collection — builds the user message with current bot states
# ------------------------------------------------------------------

async def _collect_bot_states() -> list[dict]:
    """Gather current state of all bots for the LLM prompt."""
    if _bot_manager is None:
        return []

    states = []
    for bot_info in _bot_manager.list_bots():
        bot_id = bot_info["name"]
        bot = _bot_manager.get_bot(bot_id)
        if bot is None:
            continue

        # Portfolio state
        portfolio = {}
        try:
            portfolio = await _simulation_engine.get_portfolio_state(bot_id)
        except Exception:
            pass

        # Current params
        params = bot.get_params()

        # Recent trades (last 10)
        recent_trades = []
        try:
            trades = await repo.get_trades_for_bot(bot_id, limit=10)
            for t in trades:
                recent_trades.append({
                    "side": t.side,
                    "action": t.position_side,
                    "price": round(t.price, 2),
                    "qty": round(t.quantity, 6),
                    "pnl": round(t.realized_pnl, 4) if t.realized_pnl else None,
                    "fee": round(t.fee_usdt, 4) if t.fee_usdt else None,
                    "time": str(t.timestamp),
                })
        except Exception:
            pass

        states.append({
            "bot_id": bot_id,
            "symbol": bot.symbol,
            "is_running": bot.is_running,
            "portfolio": {
                "usdt_balance": round(portfolio.get("usdt_balance", 0), 2),
                "total_value": round(portfolio.get("total_value_usdt", 0), 2),
                "realized_pnl": round(portfolio.get("realized_pnl", 0), 4),
                "net_pnl": round(portfolio.get("net_pnl", 0), 4),
                "return_pct": round(portfolio.get("return_pct", 0), 2),
                "trade_count": portfolio.get("trade_count", 0),
                "total_fees": round(portfolio.get("total_fees_paid", 0), 4),
                "position_side": portfolio.get("position_side", "NONE"),
                "position_qty": round(portfolio.get("position_qty", 0), 6),
                "liquidations": portfolio.get("liquidation_count", 0),
            },
            "params": {
                k: {"value": v["value"], "min": v["min"], "max": v["max"], "type": v["type"]}
                for k, v in params.items()
            },
            "recent_trades": recent_trades,
        })

    return states


def _build_user_message(bot_states: list[dict]) -> str:
    """Build the user message with current bot data."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"Current time: {now}", f"Number of bots: {len(bot_states)}", ""]

    for s in bot_states:
        p = s["portfolio"]
        lines.append(f"━━━ {s['bot_id']} ({s['symbol']}) — {'RUNNING' if s['is_running'] else 'STOPPED'} ━━━")
        lines.append(f"  Balance: ${p['usdt_balance']} | Total: ${p['total_value']} | Return: {p['return_pct']}%")
        lines.append(f"  P&L: {p['realized_pnl']:+.4f} | Net: {p['net_pnl']:+.4f} | Fees: ${p['total_fees']}")
        lines.append(f"  Trades: {p['trade_count']} | Position: {p['position_side']} {p['position_qty']} | Liquidations: {p['liquidations']}")

        lines.append("  Parameters:")
        for k, v in s["params"].items():
            lines.append(f"    {k} = {v['value']}  (range: {v['min']}–{v['max']}, type: {v['type']})")

        if s["recent_trades"]:
            lines.append("  Recent trades:")
            for t in s["recent_trades"][:5]:  # max 5 per bot to save tokens
                pnl_str = f"P&L: {t['pnl']:+.4f}" if t["pnl"] is not None else "opening"
                lines.append(f"    {t['action']} {t['side']} @ ${t['price']} qty={t['qty']} — {pnl_str}")
        else:
            lines.append("  Recent trades: none yet")

        lines.append("")

    return "\n".join(lines)


# ------------------------------------------------------------------
# OpenAI API call
# ------------------------------------------------------------------

async def _call_openai(system_prompt: str, user_message: str) -> dict:
    """Call OpenAI Chat Completions API and return parsed JSON response."""
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.3,  # Low temperature for consistent, analytical responses
        "max_tokens": 2000,
        "response_format": {"type": "json_object"},
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()

    data = resp.json()
    content = data["choices"][0]["message"]["content"]

    # Parse JSON response
    return json.loads(content)


# ------------------------------------------------------------------
# Action executor — applies LLM decisions
# ------------------------------------------------------------------

async def _execute_actions(actions: list[dict], dry_run: bool = False) -> list[str]:
    """Execute LLM-recommended actions. Returns list of result descriptions."""
    results = []

    for action in actions[:settings.llm_max_actions]:
        action_type = action.get("type", "")
        bot_id = action.get("bot_id", "")

        if not bot_id or _bot_manager.get_bot(bot_id) is None:
            results.append(f"SKIP: unknown bot '{bot_id}'")
            continue

        if action_type == "set_params":
            params = action.get("params", {})
            if not params:
                results.append(f"SKIP: {bot_id} set_params with empty params")
                continue

            if dry_run:
                results.append(f"DRY-RUN: would set {bot_id} params: {params}")
                continue

            try:
                bot = _bot_manager.get_bot(bot_id)
                applied = bot.set_params(params)
                # Persist
                current_values = {k: v["value"] for k, v in bot.get_params().items()}
                await repo.save_bot_params(bot_id, current_values)
                results.append(f"OK: {bot_id} params updated: {applied}")
            except ValueError as e:
                results.append(f"FAIL: {bot_id} set_params: {e}")

        elif action_type == "stop_bot":
            if dry_run:
                results.append(f"DRY-RUN: would stop {bot_id}")
                continue
            try:
                await _bot_manager.stop_bot(bot_id)
                results.append(f"OK: stopped {bot_id}")
            except Exception as e:
                results.append(f"FAIL: stop {bot_id}: {e}")

        elif action_type == "start_bot":
            if dry_run:
                results.append(f"DRY-RUN: would start {bot_id}")
                continue
            try:
                await _bot_manager.start_bot(bot_id)
                results.append(f"OK: started {bot_id}")
            except Exception as e:
                results.append(f"FAIL: start {bot_id}: {e}")

        else:
            results.append(f"SKIP: unknown action type '{action_type}'")

    return results


# ------------------------------------------------------------------
# Main decision cycle
# ------------------------------------------------------------------

async def run_decision_cycle() -> dict:
    """
    Run one full LLM decision cycle:
    1. Collect all bot states
    2. Build prompt
    3. Call OpenAI
    4. Parse and execute actions
    5. Log to DB

    Returns a summary dict.
    """
    logger.info("LLM Agent: starting decision cycle...")

    # 1. Collect state
    bot_states = await _collect_bot_states()
    if not bot_states:
        logger.warning("LLM Agent: no bots found, skipping")
        return {"status": "skipped", "reason": "no bots"}

    # 2. Build messages
    system = SYSTEM_PROMPT.format(max_actions=settings.llm_max_actions)
    user_msg = _build_user_message(bot_states)

    prompt_summary = f"{len(bot_states)} bots, {sum(1 for s in bot_states if s['is_running'])} running"
    logger.debug(f"LLM Agent prompt:\n{user_msg}")

    # 3. Call OpenAI
    try:
        llm_response = await _call_openai(system, user_msg)
    except Exception as e:
        error_msg = f"OpenAI API error: {e}"
        logger.error(f"LLM Agent: {error_msg}")
        await repo.insert_llm_decision(
            prompt_summary=prompt_summary,
            response_json="",
            actions_taken="",
            success=False,
            error_message=error_msg,
        )
        return {"status": "error", "error": error_msg}

    reasoning = llm_response.get("reasoning", "No reasoning provided")
    actions = llm_response.get("actions", [])

    logger.info(f"LLM Agent reasoning: {reasoning}")
    logger.info(f"LLM Agent actions: {len(actions)} proposed")

    # 4. Execute actions
    dry_run = settings.llm_dry_run
    if dry_run:
        logger.info("LLM Agent: DRY RUN mode — actions will be logged but not applied")

    action_results = await _execute_actions(actions, dry_run=dry_run)
    for r in action_results:
        logger.info(f"  → {r}")

    # 5. Log to DB
    await repo.insert_llm_decision(
        prompt_summary=prompt_summary,
        response_json=json.dumps(llm_response, ensure_ascii=False),
        actions_taken=json.dumps(action_results, ensure_ascii=False),
        success=True,
    )

    return {
        "status": "ok",
        "reasoning": reasoning,
        "actions_count": len(actions),
        "results": action_results,
        "dry_run": dry_run,
    }


# ------------------------------------------------------------------
# Periodic task — runs in background
# ------------------------------------------------------------------

_agent_task: Optional[asyncio.Task] = None
_agent_enabled: bool = False


async def _periodic_loop():
    """Background loop that runs decision cycles at configured intervals."""
    interval_secs = settings.llm_interval_minutes * 60
    logger.info(
        f"LLM Agent periodic loop started. "
        f"Interval: {settings.llm_interval_minutes}min, "
        f"Model: {settings.llm_model}, "
        f"Dry-run: {settings.llm_dry_run}"
    )

    # Initial delay — let bots warm up before first LLM call
    await asyncio.sleep(min(interval_secs, 120))

    while True:
        if _agent_enabled:
            try:
                result = await run_decision_cycle()
                logger.info(f"LLM Agent cycle complete: {result.get('status')}")
            except Exception as e:
                logger.error(f"LLM Agent unexpected error: {e}", exc_info=True)

        await asyncio.sleep(interval_secs)


async def start_agent() -> None:
    """Start the LLM agent periodic task (if enabled in config)."""
    global _agent_task, _agent_enabled

    if not settings.llm_enabled:
        logger.info("LLM Agent: disabled (set LLM_ENABLED=true to activate)")
        return

    if not settings.llm_api_key:
        logger.warning("LLM Agent: enabled but no LLM_API_KEY set — staying inactive")
        return

    _agent_enabled = True
    _agent_task = asyncio.create_task(_periodic_loop(), name="llm-agent")
    logger.info("LLM Agent: started")


async def stop_agent() -> None:
    """Stop the LLM agent periodic task."""
    global _agent_task, _agent_enabled
    _agent_enabled = False

    if _agent_task and not _agent_task.done():
        _agent_task.cancel()
        try:
            await _agent_task
        except asyncio.CancelledError:
            pass
    _agent_task = None
    logger.info("LLM Agent: stopped")


def is_enabled() -> bool:
    """Check if the LLM agent is currently active."""
    return _agent_enabled


def get_status() -> dict:
    """Return current agent status for the API."""
    return {
        "enabled": _agent_enabled,
        "config_enabled": settings.llm_enabled,
        "has_api_key": bool(settings.llm_api_key),
        "model": settings.llm_model,
        "interval_minutes": settings.llm_interval_minutes,
        "dry_run": settings.llm_dry_run,
        "max_actions": settings.llm_max_actions,
    }
