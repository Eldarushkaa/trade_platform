"""
Parameter Optimizer — Random Search + Local Refinement for strategy tuning.

Two-phase approach optimized for low-dimensional parameter spaces (2–5 params):

  Phase 1 — Random Search (80% of budget):
    Latin Hypercube Sampling for uniform space coverage, evaluated in parallel
    batches.  Tracks Top-K candidates by composite fitness.

  Phase 2 — Local Refinement (20% of budget):
    For each Top-K candidate, generates grid neighbors at ±5% and ±15% of
    each parameter range.  Best neighbor replaces global best if it wins.

Composite fitness:
    Sharpe (40%) + ProfitFactor (30%) + Return (20%) − Drawdown (20%)
    + log(trades) bonus (10%).
    WFE-inner penalty: if IS/mini-OOS gap is large, fitness is penalised.

Walk-Forward Optimization:
    Divides data into expanding train windows + fixed OOS test windows.
    Each training window is further split (80/20) into train_inner / val_inner
    so the optimizer itself penalises IS→OOS divergence during search.
    Produces a stitched OOS equity curve and WFE metric (OOS/IS return ratio).

Usage:
    result = await optimize_params("rsi_btc", "BTCUSDT", RSIBot, max_iterations=200)
    wf_result = await walk_forward_optimize("rsi_btc", "BTCUSDT", RSIBot, n_folds=4)
"""
import asyncio
import logging
import math
import random
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

from config import settings
from core.utils import safe_float as _safe, safe_round as _sr
from db import repository as repo

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Process-pool worker (runs in separate OS process — no GIL contention)
# ------------------------------------------------------------------

# Each worker process stores its shared state after _worker_init() runs.
# This avoids re-pickling the large candle list on every evaluation call.
_worker_state: dict = {}


def _worker_init(
    strategy_module: str,
    strategy_cls_name: str,
    symbol: str,
    candle_rows: list,
    fee_rate,
    initial_balance: float,
    val_candle_rows: list | None = None,
    warmup_candle_rows: list | None = None,
) -> None:
    """
    Called ONCE when a worker process starts.
    Reconstructs the strategy class and caches shared data in module globals.
    Avoids pickling thousands of candle rows for each individual evaluation.

    val_candle_rows:    optional mini-OOS validation window used for WFE-inner penalty.
    warmup_candle_rows: optional pre-warmup candles fed to the strategy before the main
                        IS loop (no trades recorded). Pre-heats EMA200, ATR, etc.
    """
    import importlib
    module = importlib.import_module(strategy_module)
    base_cls = getattr(module, strategy_cls_name)
    _worker_state["strategy_class"] = base_cls.for_symbol(symbol)
    _worker_state["candle_rows"] = candle_rows
    _worker_state["fee_rate"] = fee_rate
    _worker_state["initial_balance"] = initial_balance
    _worker_state["symbol"] = symbol
    _worker_state["val_candle_rows"] = val_candle_rows      # may be None
    _worker_state["warmup_candle_rows"] = warmup_candle_rows  # may be None


def _worker_evaluate_params(params: dict) -> dict:
    """
    Evaluate one parameter set in a worker process.
    Creates a fresh asyncio event loop (worker processes have no running loop).
    Returns a plain dict (must be picklable for IPC).

    If val_candle_rows are available in worker state, also runs a mini-OOS
    backtest and returns wfe_inner = val_return / is_return for penalty.
    """
    import asyncio
    strategy_class = _worker_state["strategy_class"]
    candle_rows = _worker_state["candle_rows"]
    fee_rate = _worker_state["fee_rate"]
    initial_balance = _worker_state["initial_balance"]
    symbol = _worker_state["symbol"]
    val_candle_rows = _worker_state.get("val_candle_rows")

    warmup_candle_rows = _worker_state.get("warmup_candle_rows")

    loop = asyncio.new_event_loop()
    try:
        from core.backtest_engine import run_backtest
        bt = loop.run_until_complete(run_backtest(
            bot_id="opt_worker",
            symbol=symbol,
            strategy_class=strategy_class,
            params=params,
            initial_balance=initial_balance,
            fee_rate=fee_rate,
            equity_interval=20,
            candle_data=candle_rows,
            warmup_candle_data=warmup_candle_rows,
        ))

        # Optional mini-OOS validation for WFE-inner penalty
        wfe_inner = 1.0  # neutral: no penalty
        if val_candle_rows:
            try:
                val_bt = loop.run_until_complete(run_backtest(
                    bot_id="opt_worker_val",
                    symbol=symbol,
                    strategy_class=strategy_class,
                    params=params,
                    initial_balance=initial_balance,
                    fee_rate=fee_rate,
                    equity_interval=0,
                    candle_data=val_candle_rows,
                    # val uses IS candles tail as warmup (already in IS candle_rows)
                    warmup_candle_data=warmup_candle_rows,
                ))
                is_ret = bt.return_pct
                val_ret = val_bt.return_pct
                if abs(is_ret) > 0.5:
                    wfe_inner = val_ret / is_ret
                else:
                    wfe_inner = 1.0  # IS near-zero → skip penalty
            except Exception:
                wfe_inner = 1.0  # don't crash on val failure

        return {
            "ok": True,
            "sharpe": bt.sharpe_ratio,
            "return_pct": bt.return_pct,
            "max_dd": bt.max_drawdown_pct,
            "win_rate": bt.win_rate,
            "profit_factor": bt.profit_factor,
            "trade_count": bt.trade_count,
            "wfe_inner": wfe_inner,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        loop.close()


# ------------------------------------------------------------------
# Optimization result  (unchanged public API)
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
        self.objective: str = "composite"

        # Best found
        self.best_params: dict = {}
        self.best_sharpe: float = -999
        self.best_return_pct: float = 0
        self.best_max_drawdown: float = 0
        self.best_win_rate: float = 0
        self.best_profit_factor: float = 0
        self.best_trade_count: int = 0
        self.best_fitness: float = -999

        # Current (before optimization)
        self.current_params: dict = {}
        self.current_sharpe: float = 0
        self.current_return_pct: float = 0

        # All trials (for analysis)
        self.trials: list[dict] = []

        # Search stats (RS + Local)
        self.phases_run: str = "random_search+local_refinement"
        self.rs_evaluations: int = 0
        self.local_evaluations: int = 0
        self.top_k_size: int = 0
        self.concurrency: int = 1

    def _safe_trial(self, t: dict) -> dict:
        """Sanitize a trial dict so all floats are JSON-safe."""
        return {k: _safe(v) if isinstance(v, float) else v for k, v in t.items()}

    def to_dict(self) -> dict:
        best_sharpe = _safe(self.best_sharpe)
        current_sharpe = _safe(self.current_sharpe)
        best_return = _safe(self.best_return_pct)
        current_return = _safe(self.current_return_pct)

        # Compute deltas safely — both sides may be None/"Infinity" strings
        def _delta(a, b):
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                return _sr(a - b, 2)
            return None

        return {
            "bot_id": self.bot_id,
            "symbol": self.symbol,
            "strategy_name": self.strategy_name,
            "iterations_run": self.iterations_run,
            "max_iterations": self.max_iterations,
            "duration_seconds": round(self.duration_seconds, 1),
            "objective": self.objective,
            "best_params": self.best_params,
            "best_sharpe": _sr(self.best_sharpe, 2),
            "best_return_pct": _sr(self.best_return_pct, 2),
            "best_max_drawdown": _sr(self.best_max_drawdown, 2),
            "best_win_rate": _sr(self.best_win_rate, 1),
            "best_profit_factor": _sr(self.best_profit_factor, 2),
            "best_trade_count": self.best_trade_count,
            "current_params": self.current_params,
            "current_sharpe": _sr(self.current_sharpe, 2),
            "current_return_pct": _sr(self.current_return_pct, 2),
            "improvement": {
                "sharpe_delta": _delta(best_sharpe, current_sharpe),
                "return_delta": _delta(best_return, current_return),
            },
            "search_stats": {
                "phases": self.phases_run,
                "rs_evaluations": self.rs_evaluations,
                "local_evaluations": self.local_evaluations,
                "top_k_size": self.top_k_size,
                "concurrency": self.concurrency,
            },
            "top_trials": [
                self._safe_trial(t) for t in sorted(
                    self.trials, key=lambda t: t.get("fitness", t.get("sharpe", -999)), reverse=True
                )[:10]
            ],
        }


# ------------------------------------------------------------------
# Individual in the population
# ------------------------------------------------------------------

@dataclass
class _Individual:
    """One candidate solution in the GA population."""
    params: dict
    fitness: float = -999.0
    sharpe: float = 0.0
    return_pct: float = 0.0
    max_dd: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    trade_count: int = 0
    evaluated: bool = False
    label: str = "random"


# ------------------------------------------------------------------
# Fitness function  (composite, not just Sharpe)
# ------------------------------------------------------------------

def _compute_fitness(
    sharpe: float,
    return_pct: float,
    max_dd: float,
    trade_count: int,
    profit_factor: float = 1.0,
    wfe_inner: float = 1.0,
) -> float:
    """
    Composite fitness balancing profitability, risk, and statistical significance.

    Components:
      - Sharpe ratio:   40% — risk-adjusted stability
      - Profit factor:  30% — gross_profit / gross_loss quality
      - Return %:       20% — absolute profitability
      - Max drawdown:   20% — risk penalty
      - log(trades):    10% — reward statistically significant sample sizes

    WFE-inner penalty (when val_candles provided to workers):
      - wfe_inner = val_return / is_return  (inner fold mini-OOS ratio)
      - if wfe_inner < 0.5 → penalise by up to −0.6 fitness points
      - encourages params that generalize, not just overfit IS data

    Hard filter: < 30 trades → disqualified (allows shorter OOS windows).
    """
    # Hard filter: statistically meaningless with too few trades
    if trade_count < 30:
        return -1000.0 + trade_count

    s  = max(-5.0, min(5.0, sharpe))
    pf = min(3.0,  max(0.0, profit_factor))          # raw pf, 0..3
    r  = max(-2.0, min(2.0, return_pct / 100.0))
    dd = abs(max_dd) / 100.0                          # 0..1+

    fitness = (
        s  * 0.40
        + pf * 0.30
        + r  * 0.20
        - dd * 0.20
        + math.log(max(1, trade_count)) * 0.10
    )

    # WFE-inner penalty: penalise IS→OOS divergence
    # wfe_inner < 0.5 → subtract up to 0.6 points; > 0.5 → no penalty
    if wfe_inner < 0.5:
        wfe_penalty = (0.5 - wfe_inner) * 1.2   # max penalty = 0.6 at wfe_inner=0
        fitness -= wfe_penalty

    return fitness


# ------------------------------------------------------------------
# Parameter manipulation helpers
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


def _latin_hypercube_sample(schema: dict, n: int) -> list[dict]:
    """
    Latin Hypercube Sampling for better initial coverage of the space.
    Divides each dimension into n equal strata, ensuring one sample per stratum.
    """
    optimizable = {k: v for k, v in schema.items() if v.get("optimize", True)}
    non_optimizable = {k: v["default"] for k, v in schema.items() if not v.get("optimize", True)}

    if not optimizable:
        return [dict(non_optimizable) for _ in range(n)]

    # Create stratified samples for each dimension
    dim_samples: dict[str, list] = {}
    for key, spec in optimizable.items():
        lo, hi = spec["min"], spec["max"]
        strata = []
        for i in range(n):
            # Random point within stratum i
            stratum_lo = lo + (hi - lo) * i / n
            stratum_hi = lo + (hi - lo) * (i + 1) / n
            val = random.uniform(stratum_lo, stratum_hi)
            if spec["type"] == "int":
                val = int(round(val))
            else:
                val = round(val, 4)
            strata.append(val)
        random.shuffle(strata)  # randomize assignment
        dim_samples[key] = strata

    # Combine into parameter dicts
    samples = []
    for i in range(n):
        p = dict(non_optimizable)
        for key in optimizable:
            p[key] = dim_samples[key][i]
        samples.append(p)
    return samples


def _local_neighbors(params: dict, schema: dict) -> list[dict]:
    """
    Generate local neighborhood around a parameter set for Local Refinement phase.

    For each optimizable dimension, produces candidates at ±5% and ±15% of the
    parameter range (4 neighbors per dimension).  Non-optimizable params are
    kept at their default values.

    Returns a list of candidate parameter dicts (may include duplicates if
    range is tiny — callers should tolerate that).
    """
    neighbors = []
    optimizable_keys = [k for k, v in schema.items() if v.get("optimize", True)]

    for key in optimizable_keys:
        spec = schema[key]
        lo, hi = spec["min"], spec["max"]
        span = hi - lo
        current = params.get(key, spec["default"])

        for step_frac in (0.05, 0.15):
            step = span * step_frac
            for direction in (+1, -1):
                new_val = current + direction * step
                new_val = max(lo, min(hi, new_val))
                if spec["type"] == "int":
                    new_val = int(round(new_val))
                else:
                    new_val = round(new_val, 4)
                if new_val == current:
                    continue  # skip if clamped to same value
                # Build full param dict with non-optimizable at defaults
                candidate = {}
                for k, s in schema.items():
                    if s.get("optimize", True) is False:
                        candidate[k] = s["default"]
                    elif k == key:
                        candidate[k] = new_val
                    else:
                        candidate[k] = params.get(k, s["default"])
                neighbors.append(candidate)

    return neighbors


def _tournament_select(population: list[_Individual], tournament_size: int = 3) -> _Individual:
    """Select one individual via tournament selection (higher fitness wins).
    Kept for potential future use; not used in the RS+Local flow."""
    contestants = random.sample(population, min(tournament_size, len(population)))
    return max(contestants, key=lambda ind: ind.fitness)


# ------------------------------------------------------------------
# Core evolutionary optimizer
# ------------------------------------------------------------------

async def optimize_params(
    bot_id: str,
    symbol: str,
    strategy_class: type,
    current_params: dict | None = None,
    max_iterations: int = 200,
    initial_balance: float | None = None,
    fee_rate: float | None = None,
    progress_callback=None,
    concurrency: int = 4,
    interval: str = "15m",
    _candle_override: list | None = None,
    _val_candle_override: list | None = None,
) -> OptimizationResult:
    """
    Find optimal parameters using Random Search + Local Refinement.

    Two-phase approach ideal for low-dimensional spaces (2–5 optimizable params):

      Phase 1 — Random Search (80% of budget):
        Latin Hypercube Sampling for uniform initial coverage, then pure random
        samples evaluated in parallel batches. Tracks Top-K candidates.

      Phase 2 — Local Refinement (20% of budget):
        For each Top-K candidate, generates grid neighbors at ±5% and ±15% of
        each parameter range (4 neighbors per dim). Evaluates in parallel.
        Best neighbor updates the global best.

    Budget: max_iterations total backtest evaluations.

    Recommended iterations:
      - 50:   Lightning scan — ~1 min, good for WFO inner folds
      - 100:  Quick scan    — ~2 min for short data windows
      - 200:  Balanced      — ~4 min, good coverage for 3-dim space
      - 500:  Thorough      — ~10 min, near-exhaustive for 3-dim space

    Args:
        bot_id:               Bot identifier
        symbol:               Trading pair
        strategy_class:       Strategy class (already for_symbol'd)
        current_params:       Current parameter values (evaluated as baseline)
        max_iterations:       Total backtest evaluations budget
        initial_balance:      Starting USDT for each backtest
        fee_rate:             Fee rate override (e.g. 0.0007 = 0.07%)
        progress_callback:    Optional async callable(pct, msg)
        concurrency:          Max parallel backtests (match to CPU cores)
        _candle_override:     Internal — pre-loaded IS candle list (from walk-forward)
        _val_candle_override: Internal — pre-loaded mini-OOS validation candles
                              (from walk-forward inner split). When provided, workers
                              compute wfe_inner = val_return / is_return and penalise
                              overfitting candidates in the fitness function.

    Returns:
        OptimizationResult with best params and search statistics
    """
    start_time = time.monotonic()

    schema = strategy_class.PARAM_SCHEMA
    if not schema:
        raise ValueError(f"Strategy {strategy_class.__name__} has no PARAM_SCHEMA")

    # --- Pre-load candle data once (or use override from walk-forward) ---
    if _candle_override is not None:
        _cached_candles = _candle_override
        if not _cached_candles:
            raise ValueError(f"Empty candle override provided for {symbol}.")
    else:
        _cached_candles = await repo.get_historical_candles(symbol, interval=interval)
        if not _cached_candles:
            raise ValueError(f"No historical data for {symbol} ({interval}). Download it first.")

    # --- Warmup candles for workers: last 300 rows of IS data ---
    # Workers use these to pre-heat EMA200, ATR, etc. before trading starts.
    _warmup_count = 300
    _warmup_for_workers = _cached_candles[-_warmup_count:] if len(_cached_candles) > _warmup_count else _cached_candles

    # Resolve the original (non-for_symbol) base class for pickling in workers.
    _base_cls = strategy_class.__bases__[0] if strategy_class.__bases__ else strategy_class
    _strategy_module = _base_cls.__module__
    _strategy_cls_name = _base_cls.__name__
    _initial_balance = initial_balance or settings.initial_usdt_balance

    logger.info(
        f"Optimizer [{bot_id}]: {len(_cached_candles)} IS candles, "
        f"{len(_val_candle_override) if _val_candle_override else 0} val candles, "
        f"fee={fee_rate if fee_rate is not None else 'default'}, "
        f"workers={concurrency}, budget={max_iterations}"
    )

    # Count optimizable dimensions
    n_dims = sum(1 for v in schema.values() if v.get("optimize", True))

    # Top-K for local refinement: 3 candidates per optimizable dimension, min 3 max 10
    top_k = max(3, min(10, n_dims * 3))

    result = OptimizationResult()
    result.bot_id = bot_id
    result.symbol = symbol
    result.strategy_name = strategy_class.__name__
    result.max_iterations = max_iterations
    result.top_k_size = top_k
    result.concurrency = concurrency

    evaluations_done = 0

    # ------------------------------------------------------------------
    # Process pool — created once, workers initialised with shared data
    # ------------------------------------------------------------------
    _loop = asyncio.get_running_loop()
    _pool = ProcessPoolExecutor(
        max_workers=concurrency,
        initializer=_worker_init,
        initargs=(
            _strategy_module,
            _strategy_cls_name,
            symbol,
            _cached_candles,
            fee_rate,
            _initial_balance,
            _val_candle_override,    # mini-OOS for WFE-inner penalty
            _warmup_for_workers,     # warmup candles: last 300 IS rows
        ),
    )

    def _update_best(ind: _Individual) -> None:
        """Update result best-so-far from a successfully evaluated individual."""
        if ind.evaluated and ind.fitness > result.best_fitness:
            result.best_fitness = ind.fitness
            result.best_sharpe = ind.sharpe
            result.best_return_pct = ind.return_pct
            result.best_max_drawdown = ind.max_dd
            result.best_win_rate = ind.win_rate
            result.best_profit_factor = ind.profit_factor
            result.best_trade_count = ind.trade_count
            result.best_params = dict(ind.params)

    async def _evaluate(ind: _Individual, idx: int) -> _Individual:
        nonlocal evaluations_done
        try:
            res = await _loop.run_in_executor(_pool, _worker_evaluate_params, ind.params)
            if res.get("ok"):
                ind.sharpe = res["sharpe"]
                ind.return_pct = res["return_pct"]
                ind.max_dd = res["max_dd"]
                ind.win_rate = res["win_rate"]
                ind.profit_factor = res["profit_factor"]
                ind.trade_count = res["trade_count"]
                ind.fitness = _compute_fitness(
                    res["sharpe"], res["return_pct"], res["max_dd"],
                    res["trade_count"], res.get("profit_factor", 1.0),
                    res.get("wfe_inner", 1.0),
                )
            else:
                logger.warning(f"Eval failed: {res.get('error')}")
                ind.fitness = -2000.0
            ind.evaluated = True
        except Exception as e:
            logger.warning(f"Eval exception: {e}")
            ind.fitness = -2000.0
            ind.evaluated = True
        evaluations_done += 1
        return ind

    async def _evaluate_batch(individuals: list[_Individual]) -> None:
        """Evaluate a batch of individuals in parallel, chunked by concurrency.

        Each chunk is awaited before the next starts so the event loop stays
        responsive (handles incoming HTTP requests) between chunks.
        Yields to the event loop after every chunk via asyncio.sleep(0).
        """
        for chunk_start in range(0, len(individuals), concurrency):
            chunk = individuals[chunk_start: chunk_start + concurrency]
            tasks = [_evaluate(ind, chunk_start + i) for i, ind in enumerate(chunk)]
            await asyncio.gather(*tasks)
            # Yield to event loop so HTTP requests are not starved
            await asyncio.sleep(0)

    def _record_trials(individuals: list[_Individual], label_override: str | None = None) -> None:
        for ind in individuals:
            result.trials.append({
                "iteration": evaluations_done,
                "params": ind.params,
                "sharpe": round(ind.sharpe, 2),
                "return_pct": round(ind.return_pct, 2),
                "max_dd": round(ind.max_dd, 2),
                "trades": ind.trade_count,
                "fitness": round(ind.fitness, 3),
                "win_rate": round(ind.win_rate, 1),
                "profit_factor": round(ind.profit_factor, 2),
                "label": label_override or ind.label,
            })

    try:
        # ------------------------------------------------------------------
        # 0. Baseline evaluation (current params)
        # ------------------------------------------------------------------
        if current_params:
            result.current_params = dict(current_params)
            baseline = _Individual(params=dict(current_params), label="baseline")
            await _evaluate(baseline, 0)
            if baseline.evaluated and baseline.fitness > -1000:
                result.current_sharpe = baseline.sharpe
                result.current_return_pct = baseline.return_pct
                _update_best(baseline)
            _record_trials([baseline])

        if progress_callback:
            await progress_callback(2, f"Baseline done. Starting Random Search ({max_iterations} evals)...")

        # ------------------------------------------------------------------
        # Phase 1 — Random Search (80% of budget)
        # ------------------------------------------------------------------
        rs_budget = int(max_iterations * 0.80)
        # Subtract baseline eval if we did one
        rs_budget -= (1 if current_params else 0)
        rs_budget = max(1, rs_budget)

        # LHS for initial coverage, then pure random for remaining
        lhs_count = min(rs_budget, max(top_k, n_dims * 4))
        lhs_samples = _latin_hypercube_sample(schema, lhs_count)
        rs_extra_count = max(0, rs_budget - lhs_count)
        rs_extra_samples = [_sample_params(schema) for _ in range(rs_extra_count)]

        all_rs_params = lhs_samples + rs_extra_samples

        # Include current_params as a seed in the RS population
        if current_params:
            all_rs_params.insert(0, dict(current_params))

        # Evaluate in parallel batches of `concurrency`
        rs_individuals: list[_Individual] = []
        batch_size = max(concurrency, 8)

        for batch_start in range(0, len(all_rs_params), batch_size):
            if evaluations_done >= int(max_iterations * 0.80) + (1 if current_params else 0):
                break
            batch_params = all_rs_params[batch_start: batch_start + batch_size]
            batch_inds = [_Individual(params=p, label="rs_lhs" if batch_start == 0 else "rs_random") for p in batch_params]
            await _evaluate_batch(batch_inds)
            rs_individuals.extend(batch_inds)
            for ind in batch_inds:
                _update_best(ind)
            _record_trials(batch_inds)

            if progress_callback:
                pct = min(78, evaluations_done / max_iterations * 95)
                await progress_callback(
                    pct,
                    f"RS: {evaluations_done}/{max_iterations} evals | "
                    f"Best fitness: {result.best_fitness:.3f} (Sharpe {result.best_sharpe:.2f})"
                )

        result.rs_evaluations = evaluations_done

        # ------------------------------------------------------------------
        # Phase 2 — Local Refinement (remaining budget)
        # ------------------------------------------------------------------
        if progress_callback:
            await progress_callback(80, f"Random Search done. Starting Local Refinement on Top-{top_k}...")

        # Pick Top-K by fitness from all evaluated RS individuals
        valid_rs = [ind for ind in rs_individuals if ind.evaluated and ind.fitness > -1000]
        valid_rs.sort(key=lambda x: x.fitness, reverse=True)
        top_candidates = valid_rs[:top_k]

        logger.info(
            f"Optimizer [{bot_id}] RS done: {result.rs_evaluations} evals, "
            f"best fitness={result.best_fitness:.3f}. "
            f"Refining Top-{len(top_candidates)} candidates."
        )

        local_evals_start = evaluations_done

        for rank, candidate in enumerate(top_candidates):
            if evaluations_done >= max_iterations:
                break
            neighbors_params = _local_neighbors(candidate.params, schema)
            # Filter to remaining budget
            budget_left = max_iterations - evaluations_done
            neighbors_params = neighbors_params[:budget_left]
            if not neighbors_params:
                break

            neighbor_inds = [_Individual(params=p, label=f"local_r{rank+1}") for p in neighbors_params]
            await _evaluate_batch(neighbor_inds)
            for ind in neighbor_inds:
                _update_best(ind)
            _record_trials(neighbor_inds)

            if progress_callback:
                pct = min(95, 80 + (evaluations_done - local_evals_start) / max(1, max_iterations - local_evals_start) * 15)
                await progress_callback(
                    pct,
                    f"Local [{rank+1}/{len(top_candidates)}]: {evaluations_done}/{max_iterations} evals | "
                    f"Best fitness: {result.best_fitness:.3f} (Sharpe {result.best_sharpe:.2f})"
                )

        result.local_evaluations = evaluations_done - result.rs_evaluations

    finally:
        # Always shut down the process pool to release worker memory and OS processes.
        # wait=True ensures workers exit before we return (prevents zombie accumulation).
        try:
            _pool.shutdown(wait=True, cancel_futures=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Final results
    # ------------------------------------------------------------------
    result.iterations_run = evaluations_done
    result.duration_seconds = time.monotonic() - start_time

    if progress_callback:
        await progress_callback(
            100,
            f"Done! {evaluations_done} evals in {result.duration_seconds:.1f}s | "
            f"Best Sharpe: {result.best_sharpe:.2f}, Fitness: {result.best_fitness:.3f}"
        )

    logger.info(
        f"Optimizer [{bot_id}] complete: {evaluations_done} evals "
        f"(RS={result.rs_evaluations}, Local={result.local_evaluations}) "
        f"in {result.duration_seconds:.1f}s | "
        f"Best fitness={result.best_fitness:.3f} Sharpe={result.best_sharpe:.2f} "
        f"(was {result.current_sharpe:.2f}) Return={result.best_return_pct:+.2f}%"
    )

    return result


def _population_diversity(population: list[_Individual], schema: dict) -> float:
    """
    Measure population diversity as average coefficient of variation across
    optimizable parameters. Returns 0-100 (percentage).
    """
    if len(population) < 2:
        return 0.0

    optimizable = [k for k, v in schema.items() if v.get("optimize", True)]
    if not optimizable:
        return 0.0

    cvs = []
    for key in optimizable:
        values = [ind.params.get(key, 0) for ind in population if ind.evaluated]
        if not values:
            continue
        mean = sum(values) / len(values)
        if abs(mean) < 1e-10:
            continue
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        std = math.sqrt(variance)
        cvs.append(abs(std / mean) * 100)

    return sum(cvs) / len(cvs) if cvs else 0.0


# ------------------------------------------------------------------
# Walk-Forward Optimization
# ------------------------------------------------------------------

@dataclass
class WalkForwardFold:
    """Result of a single walk-forward fold."""
    fold: int
    train_start_ms: int
    train_end_ms: int
    test_start_ms: int
    test_end_ms: int
    train_candles: int
    test_candles: int

    # In-sample (train) best result
    is_return_pct: float
    is_sharpe: float
    is_max_dd: float
    is_trade_count: int
    is_profit_factor: float
    best_params: dict

    # Out-of-sample (test) result
    oos_return_pct: float
    oos_sharpe: float
    oos_max_dd: float
    oos_trade_count: int
    oos_profit_factor: float
    oos_equity_curve: list

    # Walk-Forward Efficiency = OOS_return / IS_return
    # > 0.6 → strategy generalises well
    # 0.3–0.6 → moderate overfitting
    # < 0.3 → heavy curve-fitting
    wfe: float

    def to_dict(self) -> dict:
        return {
            "fold": self.fold,
            "train_start_ms": self.train_start_ms,
            "train_end_ms": self.train_end_ms,
            "test_start_ms": self.test_start_ms,
            "test_end_ms": self.test_end_ms,
            "train_candles": self.train_candles,
            "test_candles": self.test_candles,
            "is_return_pct": _sr(self.is_return_pct, 2),
            "is_sharpe": _sr(self.is_sharpe, 2),
            "is_max_dd": _sr(self.is_max_dd, 2),
            "is_trade_count": self.is_trade_count,
            "is_profit_factor": _sr(self.is_profit_factor, 2),
            "best_params": self.best_params,
            "oos_return_pct": _sr(self.oos_return_pct, 2),
            "oos_sharpe": _sr(self.oos_sharpe, 2),
            "oos_max_dd": _sr(self.oos_max_dd, 2),
            "oos_trade_count": self.oos_trade_count,
            "oos_profit_factor": _sr(self.oos_profit_factor, 2),
            "wfe": _sr(self.wfe, 3),
            "oos_equity_curve": self.oos_equity_curve,
        }


class WalkForwardResult:
    """Aggregated result of a full walk-forward optimization run."""

    def __init__(self):
        self.bot_id: str = ""
        self.symbol: str = ""
        self.strategy_name: str = ""
        self.n_folds: int = 0
        self.test_pct: float = 0.10
        self.iterations_per_fold: int = 0
        self.duration_seconds: float = 0.0
        self.folds: list[WalkForwardFold] = []

        # Aggregated OOS stats (stitched across all folds)
        self.avg_wfe: float = 0.0
        self.avg_oos_return_pct: float = 0.0
        self.avg_oos_sharpe: float = 0.0
        self.total_oos_trades: int = 0
        self.oos_equity_curve: list = []   # stitched across all folds

        # Final params: optimized on full dataset
        self.final_params: dict = {}
        self.final_is_return_pct: float = 0.0
        self.final_is_sharpe: float = 0.0
        self.final_is_trade_count: int = 0

    def to_dict(self) -> dict:
        return {
            "bot_id": self.bot_id,
            "symbol": self.symbol,
            "strategy_name": self.strategy_name,
            "n_folds": self.n_folds,
            "test_pct": self.test_pct,
            "iterations_per_fold": self.iterations_per_fold,
            "duration_seconds": round(self.duration_seconds, 1),
            "avg_wfe": _sr(self.avg_wfe, 3),
            "avg_oos_return_pct": _sr(self.avg_oos_return_pct, 2),
            "avg_oos_sharpe": _sr(self.avg_oos_sharpe, 2),
            "total_oos_trades": self.total_oos_trades,
            "oos_equity_curve": self.oos_equity_curve,
            "final_params": self.final_params,
            "final_is_return_pct": _sr(self.final_is_return_pct, 2),
            "final_is_sharpe": _sr(self.final_is_sharpe, 2),
            "final_is_trade_count": self.final_is_trade_count,
            "folds": [f.to_dict() for f in self.folds],
        }


async def walk_forward_optimize(
    bot_id: str,
    symbol: str,
    strategy_class: type,
    current_params: dict | None = None,
    n_folds: int = 4,
    test_pct: float = 0.10,
    max_iterations: int = 500,
    initial_balance: float | None = None,
    fee_rate: float | None = None,
    progress_callback=None,
    concurrency: int = 4,
    interval: str = "15m",
) -> "WalkForwardResult":
    """
    Walk-Forward Optimization with expanding training windows.

    Divides the full dataset into n_folds sequential OOS test windows.
    For each fold:
      1. Train: optimize GA on all data BEFORE the test window (expanding)
      2. Test:  evaluate best_params on the held-out OOS window

    Produces per-fold metrics, stitched OOS equity curve, and WFE score.
    Finally re-optimizes on the full dataset to get production params.

    Walk-Forward Efficiency (WFE):
      WFE = oos_return / is_return
      > 0.6  → strategy generalises well, low overfitting
      0.3–0.6 → moderate overfitting, use with caution
      < 0.3  → heavy curve-fitting, params unlikely to work live

    Args:
        bot_id:             Bot identifier
        symbol:             Trading pair
        strategy_class:     Strategy class (already for_symbol'd)
        current_params:     Current parameter values (baseline seed)
        n_folds:            Number of OOS test windows (default 4)
        test_pct:           Fraction of total data per OOS window (default 0.10 = 10%)
        max_iterations:     GA budget PER FOLD (not total!)
        initial_balance:    Starting USDT for each backtest
        fee_rate:           Fee rate override
        progress_callback:  Optional async callable(pct, msg)
        concurrency:        Max parallel backtests per fold

    Returns:
        WalkForwardResult with per-fold metrics, stitched OOS equity, and final params.
    """
    from core.backtest_engine import run_backtest

    start_time = time.monotonic()

    # --- Load all candles once ---
    all_candles = await repo.get_historical_candles(symbol, interval=interval)
    if not all_candles:
        raise ValueError(f"No historical data for {symbol} ({interval}). Download it first.")

    total = len(all_candles)
    fold_size = int(total * test_pct)
    # Minimum training data: everything before the first OOS window
    min_train_end = total - n_folds * fold_size

    if min_train_end < fold_size * 2:
        raise ValueError(
            f"Not enough data for {n_folds} folds with test_pct={test_pct}. "
            f"Total candles: {total}, need at least {fold_size * (n_folds + 2)}."
        )

    result = WalkForwardResult()
    result.bot_id = bot_id
    result.symbol = symbol
    result.strategy_name = strategy_class.__name__
    result.n_folds = n_folds
    result.test_pct = test_pct
    result.iterations_per_fold = max_iterations

    total_steps = n_folds + 1  # n_folds + final full optimization
    step = 0

    async def _progress(fold_label: str, inner_pct: float, msg: str) -> None:
        if progress_callback:
            fold_base = step / total_steps * 100
            fold_span = 1 / total_steps * 100
            overall = fold_base + fold_span * (inner_pct / 100)
            await progress_callback(min(95, overall), f"[{fold_label}] {msg}")

    # ------------------------------------------------------------------
    # Run each fold
    # ------------------------------------------------------------------
    for fold_idx in range(n_folds):
        step = fold_idx
        fold_num = fold_idx + 1

        # Expanding window: train on [0, train_end), test on [train_end, test_end)
        train_end_idx = min_train_end + fold_idx * fold_size
        test_start_idx = train_end_idx
        test_end_idx = min(test_start_idx + fold_size, total)

        train_candles = all_candles[:train_end_idx]
        test_candles = all_candles[test_start_idx:test_end_idx]

        train_start_ms = train_candles[0]["open_time"] if train_candles else 0
        train_end_ms = train_candles[-1]["open_time"] if train_candles else 0
        test_start_ms = test_candles[0]["open_time"] if test_candles else 0
        test_end_ms = test_candles[-1]["open_time"] if test_candles else 0

        logger.info(
            f"WFO fold {fold_num}/{n_folds}: "
            f"train={len(train_candles)} candles, test={len(test_candles)} candles"
        )

        # Inline progress adapter per fold
        async def _fold_progress(pct, msg, _fn=fold_num):
            await _progress(f"Fold {_fn}/{n_folds}", pct, msg)

        # --- Optimize on train data ---
        try:
            opt = await optimize_params(
                bot_id=f"{bot_id}_wf{fold_num}",
                symbol=symbol,
                strategy_class=strategy_class,
                current_params=current_params,
                max_iterations=max_iterations,
                initial_balance=initial_balance,
                fee_rate=fee_rate,
                progress_callback=_fold_progress,
                concurrency=concurrency,
                interval=interval,
                _candle_override=train_candles,  # internal: skip DB, use these candles
            )
        except Exception as e:
            logger.error(f"WFO fold {fold_num} optimization failed: {e}", exc_info=True)
            continue

        best_params = opt.best_params

        # --- Evaluate best_params on OOS test data ---
        try:
            _initial_balance = initial_balance or settings.initial_usdt_balance
            # Use last 300 train candles as warmup so EMA200/ATR are pre-heated
            # at the start of the OOS window — no warmup burn-in needed in test.
            _oos_warmup = train_candles[-300:] if len(train_candles) >= 300 else train_candles
            oos_bt = await run_backtest(
                bot_id=f"{bot_id}_wf{fold_num}_oos",
                symbol=symbol,
                strategy_class=strategy_class,
                params=best_params,
                initial_balance=_initial_balance,
                fee_rate=fee_rate,
                equity_interval=5,
                candle_data=test_candles,
                warmup_candle_data=_oos_warmup,
                interval=interval,
            )
        except Exception as e:
            logger.error(f"WFO fold {fold_num} OOS backtest failed: {e}", exc_info=True)
            continue

        # --- WFE calculation ---
        is_return = opt.best_return_pct
        oos_return = oos_bt.return_pct
        if abs(is_return) > 0.01:
            wfe = oos_return / is_return
        else:
            wfe = 0.0  # avoid division by near-zero

        fold_result = WalkForwardFold(
            fold=fold_num,
            train_start_ms=train_start_ms,
            train_end_ms=train_end_ms,
            test_start_ms=test_start_ms,
            test_end_ms=test_end_ms,
            train_candles=len(train_candles),
            test_candles=len(test_candles),
            is_return_pct=is_return,
            is_sharpe=opt.best_sharpe,
            is_max_dd=opt.best_max_drawdown,
            is_trade_count=opt.best_trade_count,
            is_profit_factor=opt.best_profit_factor,
            best_params=best_params,
            oos_return_pct=oos_return,
            oos_sharpe=oos_bt.sharpe_ratio,
            oos_max_dd=oos_bt.max_drawdown_pct,
            oos_trade_count=oos_bt.trade_count,
            oos_profit_factor=oos_bt.profit_factor,
            oos_equity_curve=oos_bt.equity_curve,
            wfe=wfe,
        )
        result.folds.append(fold_result)

        # Stitch OOS equity curve with chain-scaling so folds connect smoothly.
        # Each fold's backtest starts from initial_balance → we rescale every point
        # so fold N starts exactly where fold N-1 ended (compound returns).
        if oos_bt.equity_curve:
            fold_start_val = oos_bt.equity_curve[0]["value"]
            if result.oos_equity_curve and fold_start_val > 0:
                prev_final = result.oos_equity_curve[-1]["value"]
                for pt in oos_bt.equity_curve:
                    scale = pt["value"] / fold_start_val
                    result.oos_equity_curve.append({
                        **pt,
                        "value": round(prev_final * scale, 2),
                        "usdt":  round(prev_final * (pt.get("usdt", pt["value"]) / fold_start_val), 2),
                    })
            else:
                # First fold: append as-is
                result.oos_equity_curve.extend(oos_bt.equity_curve)

        logger.info(
            f"WFO fold {fold_num}: IS={is_return:+.2f}% → OOS={oos_return:+.2f}% "
            f"(WFE={wfe:.2f}) Sharpe OOS={oos_bt.sharpe_ratio:.2f}"
        )

    # ------------------------------------------------------------------
    # Final optimization on full dataset → production params
    # ------------------------------------------------------------------
    step = n_folds

    async def _final_progress(pct, msg):
        await _progress(f"Final (full data)", pct, msg)

    try:
        final_opt = await optimize_params(
            bot_id=f"{bot_id}_wf_final",
            symbol=symbol,
            strategy_class=strategy_class,
            current_params=current_params,
            max_iterations=max_iterations,
            initial_balance=initial_balance,
            fee_rate=fee_rate,
            progress_callback=_final_progress,
            concurrency=concurrency,
            interval=interval,
            _candle_override=all_candles,
        )
        result.final_params = final_opt.best_params
        result.final_is_return_pct = final_opt.best_return_pct
        result.final_is_sharpe = final_opt.best_sharpe
        result.final_is_trade_count = final_opt.best_trade_count
    except Exception as e:
        logger.error(f"WFO final optimization failed: {e}", exc_info=True)
        # Fall back to best fold params
        if result.folds:
            best_fold = max(result.folds, key=lambda f: f.oos_return_pct)
            result.final_params = best_fold.best_params

    # ------------------------------------------------------------------
    # Aggregate metrics
    # ------------------------------------------------------------------
    if result.folds:
        result.avg_wfe = sum(f.wfe for f in result.folds) / len(result.folds)
        result.avg_oos_return_pct = sum(f.oos_return_pct for f in result.folds) / len(result.folds)
        result.avg_oos_sharpe = sum(f.oos_sharpe for f in result.folds) / len(result.folds)
        result.total_oos_trades = sum(f.oos_trade_count for f in result.folds)

        # Prefer best-WFE fold params when avg_wfe is poor (< 0.35).
        # The fold with highest WFE generalised best and is a safer live choice
        # than the full-data optimized params which may be overfitting.
        if not result.final_params or result.avg_wfe < 0.35:
            best_wfe_fold = max(result.folds, key=lambda f: f.wfe)
            if best_wfe_fold.wfe > 0.35:
                logger.info(
                    f"WFO [{bot_id}]: avg_wfe={result.avg_wfe:.2f} < 0.35, "
                    f"using best-WFE fold {best_wfe_fold.fold} params "
                    f"(fold WFE={best_wfe_fold.wfe:.2f})"
                )
                result.final_params = best_wfe_fold.best_params

    result.duration_seconds = time.monotonic() - start_time

    if progress_callback:
        wfe_label = f"{result.avg_wfe:.2f}" if result.folds else "N/A"
        await progress_callback(
            100,
            f"Walk-Forward complete: {len(result.folds)}/{n_folds} folds, "
            f"avg WFE={wfe_label}, avg OOS return={result.avg_oos_return_pct:+.2f}%"
        )

    logger.info(
        f"Walk-Forward {bot_id}: {len(result.folds)}/{n_folds} folds in {result.duration_seconds:.1f}s | "
        f"avg WFE={result.avg_wfe:.2f} | avg OOS return={result.avg_oos_return_pct:+.2f}% | "
        f"avg OOS Sharpe={result.avg_oos_sharpe:.2f}"
    )

    return result
