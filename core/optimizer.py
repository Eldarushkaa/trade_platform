"""
Parameter Optimizer — finds optimal strategy parameters via optimization.

Uses scipy.optimize.differential_evolution for global optimization over
the parameter space defined by each strategy's PARAM_SCHEMA.

Objective: maximize Sharpe ratio (or minimize negative Sharpe).

Each evaluation runs a full backtest, so the cost is:
    iterations × candles × strategy_cost ≈ 100 × 3000 × cheap = seconds.

Usage:
    result = await optimize_params("rsi_btc", "BTCUSDT", RSIBot, max_iterations=100)
"""
import asyncio
import logging
import random
import math
from datetime import datetime, timezone
from typing import Optional

from core.backtest_engine import run_backtest

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Optimization result
# ------------------------------------------------------------------

class OptimizationResult:
    """Result of a parameter optimization run."""

    def __init__(self):
        self.bot_id: str = ""
        self.symbol: str = ""
        self.strategy_name: str = ""
        self.iterations_run: int = 0
        self.max_iterations: int = 0
        self.duration_seconds: float = 0
        self.objective: str = "sharpe_ratio"

        # Best found
        self.best_params: dict = {}
        self.best_sharpe: float = -999
        self.best_return_pct: float = 0
        self.best_max_drawdown: float = 0
        self.best_win_rate: float = 0
        self.best_profit_factor: float = 0
        self.best_trade_count: int = 0

        # Current (before optimization)
        self.current_params: dict = {}
        self.current_sharpe: float = 0
        self.current_return_pct: float = 0

        # All trials (for analysis)
        self.trials: list[dict] = []

    @staticmethod
    def _safe(v):
        """Replace inf/nan with JSON-safe values."""
        if isinstance(v, float):
            if math.isinf(v):
                return 9999.99 if v > 0 else -9999.99
            if math.isnan(v):
                return 0.0
        return v

    def _safe_trial(self, t: dict) -> dict:
        """Sanitize a trial dict so all floats are JSON-safe."""
        return {k: self._safe(v) if isinstance(v, float) else v for k, v in t.items()}

    def to_dict(self) -> dict:
        s = self._safe
        best_sharpe = s(self.best_sharpe)
        current_sharpe = s(self.current_sharpe)
        best_return = s(self.best_return_pct)
        current_return = s(self.current_return_pct)

        return {
            "bot_id": self.bot_id,
            "symbol": self.symbol,
            "strategy_name": self.strategy_name,
            "iterations_run": self.iterations_run,
            "max_iterations": self.max_iterations,
            "duration_seconds": round(self.duration_seconds, 1),
            "objective": self.objective,
            "best_params": self.best_params,
            "best_sharpe": round(best_sharpe, 2),
            "best_return_pct": round(best_return, 2),
            "best_max_drawdown": round(s(self.best_max_drawdown), 2),
            "best_win_rate": round(s(self.best_win_rate), 1),
            "best_profit_factor": round(s(self.best_profit_factor), 2),
            "best_trade_count": self.best_trade_count,
            "current_params": self.current_params,
            "current_sharpe": round(current_sharpe, 2),
            "current_return_pct": round(current_return, 2),
            "improvement": {
                "sharpe_delta": round(s(best_sharpe - current_sharpe), 2),
                "return_delta": round(s(best_return - current_return), 2),
            },
            "top_trials": [
                self._safe_trial(t) for t in sorted(
                    self.trials, key=lambda t: t["sharpe"], reverse=True
                )[:10]
            ],
        }


# ------------------------------------------------------------------
# Random search optimizer (no scipy dependency needed)
# ------------------------------------------------------------------

def _sample_params(schema: dict) -> dict:
    """Generate a random parameter set within PARAM_SCHEMA bounds.
    Skips params with optimize=False (uses their default value instead)."""
    params = {}
    for key, spec in schema.items():
        if spec.get("optimize", True) is False:
            params[key] = spec["default"]
            continue
        lo, hi = spec["min"], spec["max"]
        if spec["type"] == "int":
            params[key] = random.randint(int(lo), int(hi))
        else:
            params[key] = round(random.uniform(lo, hi), 4)
    return params


def _mutate_params(base: dict, schema: dict, mutation_rate: float = 0.3) -> dict:
    """Create a mutated version of a parameter set.
    Skips params with optimize=False (keeps their default value)."""
    params = dict(base)
    for key, spec in schema.items():
        if spec.get("optimize", True) is False:
            params[key] = spec["default"]  # always reset to default, never mutate
            continue
        if random.random() < mutation_rate:
            lo, hi = spec["min"], spec["max"]
            # Small perturbation around current value
            current = params.get(key, spec["default"])
            span = (hi - lo) * 0.2  # 20% of range
            new_val = current + random.uniform(-span, span)
            new_val = max(lo, min(hi, new_val))
            if spec["type"] == "int":
                new_val = int(round(new_val))
            else:
                new_val = round(new_val, 4)
            params[key] = new_val
    return params


async def optimize_params(
    bot_id: str,
    symbol: str,
    strategy_class: type,
    current_params: dict | None = None,
    max_iterations: int = 500,
    initial_balance: float | None = None,
    progress_callback=None,
) -> OptimizationResult:
    """
    Find optimal parameters for a strategy using evolutionary search.

    Method:
        1. Evaluate current params as baseline
        2. Random search phase (60% of budget) — explore the space
        3. Mutation phase (40% of budget) — refine around best found
        4. Return best params found

    For a 5-parameter strategy, recommended iterations:
        - 50:  Quick scan (~50s) — good for a rough idea
        - 200: Balanced (~3 min) — solid coverage of the space
        - 500: Thorough (~8 min) — deep exploration, best results

    Args:
        bot_id: Bot identifier
        symbol: Trading pair
        strategy_class: Strategy class (already for_symbol'd)
        current_params: Current parameter values (to evaluate as baseline)
        max_iterations: Total optimization iterations (each = 1 backtest)
        initial_balance: Starting USDT for each backtest
        progress_callback: Optional async callable(pct, msg)

    Returns:
        OptimizationResult with best params and comparison
    """
    import time
    start_time = time.monotonic()

    # Get param schema from strategy class
    schema = strategy_class.PARAM_SCHEMA
    if not schema:
        raise ValueError(f"Strategy {strategy_class.__name__} has no PARAM_SCHEMA")

    result = OptimizationResult()
    result.bot_id = bot_id
    result.symbol = symbol
    result.strategy_name = strategy_class.__name__
    result.max_iterations = max_iterations

    # --- Evaluate current params as baseline ---
    if current_params:
        result.current_params = dict(current_params)
        try:
            bt = await run_backtest(
                bot_id=f"opt_{bot_id}_baseline",
                symbol=symbol,
                strategy_class=strategy_class,
                params=current_params,
                initial_balance=initial_balance,
                equity_interval=20,  # coarser for speed
            )
            result.current_sharpe = bt.sharpe_ratio
            result.current_return_pct = bt.return_pct
            # Use baseline as initial best
            result.best_sharpe = bt.sharpe_ratio
            result.best_return_pct = bt.return_pct
            result.best_max_drawdown = bt.max_drawdown_pct
            result.best_win_rate = bt.win_rate
            result.best_profit_factor = bt.profit_factor
            result.best_trade_count = bt.trade_count
            result.best_params = dict(current_params)

            result.trials.append({
                "iteration": 0,
                "params": dict(current_params),
                "sharpe": round(bt.sharpe_ratio, 2),
                "return_pct": round(bt.return_pct, 2),
                "max_dd": round(bt.max_drawdown_pct, 2),
                "trades": bt.trade_count,
                "label": "baseline",
            })
        except Exception as e:
            logger.warning(f"Baseline backtest failed: {e}")

    if progress_callback:
        await progress_callback(5, "Baseline evaluated. Starting search...")

    # --- Optimization loop ---
    random_phase = int(max_iterations * 0.6)  # 60% random exploration

    for i in range(max_iterations):
        # Generate candidate params
        if i < random_phase:
            candidate = _sample_params(schema)
            label = "random"
        else:
            candidate = _mutate_params(result.best_params or _sample_params(schema), schema)
            label = "mutation"

        # Run backtest with candidate
        try:
            bt = await run_backtest(
                bot_id=f"opt_{bot_id}_{i}",
                symbol=symbol,
                strategy_class=strategy_class,
                params=candidate,
                initial_balance=initial_balance,
                equity_interval=20,
            )
        except Exception as e:
            logger.warning(f"Optimization iteration {i} failed: {e}")
            continue

        trial = {
            "iteration": i + 1,
            "params": candidate,
            "sharpe": round(bt.sharpe_ratio, 2),
            "return_pct": round(bt.return_pct, 2),
            "max_dd": round(bt.max_drawdown_pct, 2),
            "trades": bt.trade_count,
            "win_rate": round(bt.win_rate, 1),
            "profit_factor": round(bt.profit_factor, 2),
            "label": label,
        }
        result.trials.append(trial)
        result.iterations_run = i + 1

        # Update best if improved
        # Require at least 3 trades and prefer higher Sharpe
        if bt.trade_count >= 3 and bt.sharpe_ratio > result.best_sharpe:
            result.best_sharpe = bt.sharpe_ratio
            result.best_return_pct = bt.return_pct
            result.best_max_drawdown = bt.max_drawdown_pct
            result.best_win_rate = bt.win_rate
            result.best_profit_factor = bt.profit_factor
            result.best_trade_count = bt.trade_count
            result.best_params = dict(candidate)
            logger.info(
                f"Optimizer [{i+1}/{max_iterations}] new best: "
                f"Sharpe={bt.sharpe_ratio:.2f}, Return={bt.return_pct:+.2f}%, "
                f"Params={candidate}"
            )

        # Progress
        if progress_callback and (i % 5 == 0 or i == max_iterations - 1):
            pct = 5 + (i + 1) / max_iterations * 90
            await progress_callback(
                min(pct, 99),
                f"Iteration {i+1}/{max_iterations} — best Sharpe: {result.best_sharpe:.2f}"
            )

    result.duration_seconds = time.monotonic() - start_time

    if progress_callback:
        await progress_callback(100, f"Done! Best Sharpe: {result.best_sharpe:.2f}")

    logger.info(
        f"Optimization {bot_id} complete: {result.iterations_run} iterations in "
        f"{result.duration_seconds:.1f}s | Best Sharpe: {result.best_sharpe:.2f} "
        f"(was {result.current_sharpe:.2f}), Return: {result.best_return_pct:+.2f}%"
    )

    return result
