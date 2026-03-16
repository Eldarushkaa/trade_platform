"""
Parameter Optimizer — evolutionary genetic algorithm for strategy tuning.

Uses a real population-based GA with:
  - Tournament selection
  - BLX-α crossover (blend crossover for continuous params)
  - Adaptive mutation (wide early, narrow late; auto-widens on stagnation)
  - Elitism (top-K always survive)
  - Stagnation detection → inject fresh random individuals
  - Parallel batch evaluation via asyncio.gather
  - Multi-objective composite fitness (Return + Sharpe + ProfitFactor − drawdown penalty)

Each evaluation runs a full backtest.  Compute budget:
    iterations ≈ population_size × generations

Walk-Forward Optimization:
    Divides data into expanding train windows + fixed OOS test windows.
    Runs GA on each train window, evaluates best_params on OOS window.
    Produces a stitched OOS equity curve and WFE metric (OOS/IS return ratio).

Usage:
    result = await optimize_params("rsi_btc", "BTCUSDT", RSIBot, max_iterations=1000)
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
) -> None:
    """
    Called ONCE when a worker process starts.
    Reconstructs the strategy class and caches shared data in module globals.
    Avoids pickling thousands of candle rows for each individual evaluation.
    """
    import importlib
    module = importlib.import_module(strategy_module)
    base_cls = getattr(module, strategy_cls_name)
    _worker_state["strategy_class"] = base_cls.for_symbol(symbol)
    _worker_state["candle_rows"] = candle_rows
    _worker_state["fee_rate"] = fee_rate
    _worker_state["initial_balance"] = initial_balance
    _worker_state["symbol"] = symbol


def _worker_evaluate_params(params: dict) -> dict:
    """
    Evaluate one parameter set in a worker process.
    Creates a fresh asyncio event loop (worker processes have no running loop).
    Returns a plain dict (must be picklable for IPC).
    """
    import asyncio
    strategy_class = _worker_state["strategy_class"]
    candle_rows = _worker_state["candle_rows"]
    fee_rate = _worker_state["fee_rate"]
    initial_balance = _worker_state["initial_balance"]
    symbol = _worker_state["symbol"]

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
        ))
        return {
            "ok": True,
            "sharpe": bt.sharpe_ratio,
            "return_pct": bt.return_pct,
            "max_dd": bt.max_drawdown_pct,
            "win_rate": bt.win_rate,
            "profit_factor": bt.profit_factor,
            "trade_count": bt.trade_count,
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

        # GA stats
        self.generations_run: int = 0
        self.population_size: int = 0
        self.stagnation_restarts: int = 0
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
            "ga_stats": {
                "generations": self.generations_run,
                "population_size": self.population_size,
                "stagnation_restarts": self.stagnation_restarts,
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
) -> float:
    """
    Composite fitness balancing profitability, risk, and statistical significance.

    Components:
      - Sharpe ratio:   40% — risk-adjusted stability
      - Profit factor:  30% — gross_profit / gross_loss quality
      - Return %:       20% — absolute profitability
      - Max drawdown:   20% — risk penalty
      - log(trades):    10% — reward statistically significant sample sizes

    Hard filter: < 120 trades → disqualified (target: 3 years of data).
    This prevents the GA from finding high-return params that only trade 10 times
    (classic overfitting pattern).

    log(trades) bonus grows slowly: 120→4.8, 300→5.7, 500→6.2 — so it rewards
    having enough trades without pushing towards infinite churning.
    """
    # Hard filter: statistically meaningless with fewer than 120 trades on 3yr data
    if trade_count < 120:
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


def _mutate(params: dict, schema: dict, mutation_rate: float, mutation_strength: float) -> dict:
    """
    Mutate a parameter set with adaptive rate and strength.

    mutation_rate:     probability of mutating each gene [0, 1]
    mutation_strength: fraction of param range for perturbation [0, 1]
    """
    result = dict(params)
    for key, spec in schema.items():
        if spec.get("optimize", True) is False:
            result[key] = spec["default"]
            continue
        if random.random() < mutation_rate:
            lo, hi = spec["min"], spec["max"]
            current = result.get(key, spec["default"])
            span = (hi - lo) * mutation_strength
            new_val = current + random.gauss(0, span * 0.5)  # Gaussian perturbation
            new_val = max(lo, min(hi, new_val))
            if spec["type"] == "int":
                new_val = int(round(new_val))
            else:
                new_val = round(new_val, 4)
            result[key] = new_val
    return result


def _blx_crossover(parent_a: dict, parent_b: dict, schema: dict, alpha: float = 0.3) -> dict:
    """
    BLX-α crossover: for each gene, sample uniformly from
    [min(a,b) - α*range, max(a,b) + α*range], clamped to bounds.

    This naturally explores beyond the parents while staying in the feasible space.
    """
    child = {}
    for key, spec in schema.items():
        if spec.get("optimize", True) is False:
            child[key] = spec["default"]
            continue
        lo, hi = spec["min"], spec["max"]
        va = parent_a.get(key, spec["default"])
        vb = parent_b.get(key, spec["default"])
        gene_min = min(va, vb)
        gene_max = max(va, vb)
        gene_range = gene_max - gene_min
        sample_lo = max(lo, gene_min - alpha * gene_range)
        sample_hi = min(hi, gene_max + alpha * gene_range)
        val = random.uniform(sample_lo, sample_hi)
        if spec["type"] == "int":
            val = int(round(val))
        else:
            val = round(val, 4)
        child[key] = val
    return child


def _tournament_select(population: list[_Individual], tournament_size: int = 3) -> _Individual:
    """Select one individual via tournament selection (higher fitness wins)."""
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
    max_iterations: int = 500,
    initial_balance: float | None = None,
    fee_rate: float | None = None,
    progress_callback=None,
    concurrency: int = 4,
    _candle_override: list | None = None,
) -> OptimizationResult:
    """
    Find optimal parameters using a genetic algorithm with:
      - Population-based evolution
      - BLX-α crossover + adaptive Gaussian mutation
      - Tournament selection with elitism
      - Stagnation detection → random restart injection
      - Parallel batch evaluation (asyncio.gather, up to `concurrency` tasks)

    Budget: max_iterations total backtest evaluations (including initial population).

    Recommended iterations:
      - 200:   Quick scan  — ~2 min for 7d data
      - 500:   Balanced    — ~5 min, good coverage
      - 1000:  Thorough    — ~10 min, deep exploration
      - 2000:  Exhaustive  — ~20 min, maximum quality

    Args:
        bot_id: Bot identifier
        symbol: Trading pair
        strategy_class: Strategy class (already for_symbol'd)
        current_params: Current parameter values (evaluated as baseline)
        max_iterations: Total backtest evaluations budget
        initial_balance: Starting USDT for each backtest
        fee_rate: Fee rate override for every backtest evaluation
                  (e.g. 0.0007 = 0.07%). Defaults to settings.simulation_fee_rate.
                  Set from UI when starting an optimization run.
        progress_callback: Optional async callable(pct, msg)
        concurrency: Max parallel backtests (match to CPU cores)
        _candle_override: Internal — pre-loaded candle list to use instead of DB query.
                          Used by walk_forward_optimize() to pass fold-specific windows.

    Returns:
        OptimizationResult with best params and comparison
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
        _cached_candles = await repo.get_historical_candles(symbol)
        if not _cached_candles:
            raise ValueError(f"No historical data for {symbol}. Download it first.")

    # Resolve the original (non-for_symbol) base class for pickling in workers.
    # for_symbol() creates a dynamic subclass; its __bases__[0] is the real class.
    _base_cls = strategy_class.__bases__[0] if strategy_class.__bases__ else strategy_class
    _strategy_module = _base_cls.__module__
    _strategy_cls_name = _base_cls.__name__
    _initial_balance = initial_balance or settings.initial_usdt_balance

    logger.info(
        f"Optimizer: pre-loaded {len(_cached_candles)} candles for {symbol} "
        f"(fill=candle close, fee={fee_rate if fee_rate is not None else 'default'}, "
        f"workers={concurrency})"
    )

    # Count optimizable dimensions
    n_dims = sum(1 for v in schema.values() if v.get("optimize", True))

    # --- Population sizing ---
    # Heuristic: pop_size = 5-8× number of dimensions, minimum 12, max 40
    pop_size = max(12, min(40, n_dims * 6))
    # But don't exceed 1/3 of budget (need at least 3 generations)
    pop_size = min(pop_size, max(8, max_iterations // 3))

    elite_count = max(2, pop_size // 5)          # top 20% survive unchanged
    tournament_size = max(2, pop_size // 6)      # tournament pressure
    stagnation_limit = 5                         # generations before restart
    max_generations = max_iterations // pop_size  # rough budget

    result = OptimizationResult()
    result.bot_id = bot_id
    result.symbol = symbol
    result.strategy_name = strategy_class.__name__
    result.max_iterations = max_iterations
    result.population_size = pop_size
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
        ),
    )

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
                )
            else:
                logger.warning(f"Eval failed for individual: {res.get('error')}")
                ind.fitness = -2000.0
            ind.evaluated = True
        except Exception as e:
            logger.warning(f"Eval exception for individual: {e}")
            ind.fitness = -2000.0
            ind.evaluated = True
        evaluations_done += 1
        return ind

    async def _evaluate_batch(individuals: list[_Individual]) -> None:
        """Evaluate a batch of individuals in parallel across worker processes."""
        tasks = [_evaluate(ind, i) for i, ind in enumerate(individuals)]
        await asyncio.gather(*tasks)

    # ------------------------------------------------------------------
    # 1. Baseline evaluation (current params)
    # ------------------------------------------------------------------
    if current_params:
        result.current_params = dict(current_params)
        baseline = _Individual(params=dict(current_params), label="baseline")
        await _evaluate(baseline, 0)
        if baseline.evaluated and baseline.fitness > -1000:
            result.current_sharpe = baseline.sharpe
            result.current_return_pct = baseline.return_pct
            result.best_sharpe = baseline.sharpe
            result.best_return_pct = baseline.return_pct
            result.best_max_drawdown = baseline.max_dd
            result.best_win_rate = baseline.win_rate
            result.best_profit_factor = baseline.profit_factor
            result.best_trade_count = baseline.trade_count
            result.best_params = dict(current_params)
            result.best_fitness = baseline.fitness

            result.trials.append({
                "iteration": 0,
                "params": dict(current_params),
                "sharpe": round(baseline.sharpe, 2),
                "return_pct": round(baseline.return_pct, 2),
                "max_dd": round(baseline.max_dd, 2),
                "trades": baseline.trade_count,
                "fitness": round(baseline.fitness, 3),
                "label": "baseline",
            })

    if progress_callback:
        await progress_callback(3, f"Baseline done. Building population of {pop_size}...")

    # ------------------------------------------------------------------
    # 2. Initialize population via Latin Hypercube + current params
    # ------------------------------------------------------------------
    lhs_samples = _latin_hypercube_sample(schema, pop_size - (1 if current_params else 0))
    population: list[_Individual] = [
        _Individual(params=p, label="lhs_init") for p in lhs_samples
    ]
    # Include current params as an elite seed
    if current_params:
        population.insert(0, _Individual(params=dict(current_params), label="baseline_seed"))

    # Evaluate initial population
    await _evaluate_batch(population)

    # Record trials
    for ind in population:
        result.trials.append({
            "iteration": evaluations_done,
            "params": ind.params,
            "sharpe": round(ind.sharpe, 2),
            "return_pct": round(ind.return_pct, 2),
            "max_dd": round(ind.max_dd, 2),
            "trades": ind.trade_count,
            "fitness": round(ind.fitness, 3),
            "label": ind.label,
        })

    # Update global best from initial pop
    for ind in population:
        if ind.fitness > result.best_fitness:
            result.best_fitness = ind.fitness
            result.best_sharpe = ind.sharpe
            result.best_return_pct = ind.return_pct
            result.best_max_drawdown = ind.max_dd
            result.best_win_rate = ind.win_rate
            result.best_profit_factor = ind.profit_factor
            result.best_trade_count = ind.trade_count
            result.best_params = dict(ind.params)

    if progress_callback:
        pct = evaluations_done / max_iterations * 95
        await progress_callback(min(pct, 15), f"Initial population evaluated. Best fitness: {result.best_fitness:.3f}")

    # ------------------------------------------------------------------
    # 3. Evolutionary loop
    # ------------------------------------------------------------------
    generations_without_improvement = 0
    prev_best_fitness = result.best_fitness
    generation = 0

    while evaluations_done < max_iterations:
        generation += 1
        result.generations_run = generation

        # How many offspring can we afford this generation?
        budget_left = max_iterations - evaluations_done
        if budget_left <= 0:
            break
        offspring_count = min(pop_size, budget_left)

        # --- Adaptive mutation parameters ---
        # progress: 0.0 → 1.0 over the optimization
        progress_ratio = evaluations_done / max_iterations

        # Base mutation: starts wide (0.6), narrows to 0.15
        base_mutation_rate = 0.6 - 0.45 * progress_ratio
        base_mutation_strength = 0.4 - 0.25 * progress_ratio  # 40% → 15% of range

        # If stagnating, widen mutation
        if generations_without_improvement >= 3:
            stag_boost = min(2.0, 1.0 + 0.3 * generations_without_improvement)
            mutation_rate = min(0.9, base_mutation_rate * stag_boost)
            mutation_strength = min(0.6, base_mutation_strength * stag_boost)
        else:
            mutation_rate = base_mutation_rate
            mutation_strength = base_mutation_strength

        # --- Stagnation restart: inject fresh individuals ---
        if generations_without_improvement >= stagnation_limit:
            inject_count = pop_size // 2
            logger.info(
                f"Optimizer [{generation}] stagnation restart: "
                f"injecting {inject_count} fresh individuals"
            )
            fresh = [_Individual(params=_sample_params(schema), label="restart") for _ in range(inject_count)]
            # Replace worst individuals with fresh blood
            population.sort(key=lambda ind: ind.fitness, reverse=True)
            population = population[:pop_size - inject_count] + fresh
            # Evaluate the fresh ones
            unevaluated = [ind for ind in population if not ind.evaluated]
            if unevaluated:
                to_eval = unevaluated[:min(len(unevaluated), budget_left)]
                await _evaluate_batch(to_eval)
                evaluations_done_check = evaluations_done
                for ind in to_eval:
                    result.trials.append({
                        "iteration": evaluations_done_check,
                        "params": ind.params,
                        "sharpe": round(ind.sharpe, 2),
                        "return_pct": round(ind.return_pct, 2),
                        "max_dd": round(ind.max_dd, 2),
                        "trades": ind.trade_count,
                        "fitness": round(ind.fitness, 3),
                        "label": ind.label,
                    })
                budget_left = max_iterations - evaluations_done
                offspring_count = min(pop_size, budget_left)
                if offspring_count <= 0:
                    break

            generations_without_improvement = 0
            result.stagnation_restarts += 1

        # --- Selection + Crossover + Mutation → offspring ---
        offspring: list[_Individual] = []

        # Elitism: carry top individuals unchanged
        population.sort(key=lambda ind: ind.fitness, reverse=True)
        elites = population[:elite_count]
        for e in elites:
            # Clone elite (already evaluated, no need to re-evaluate)
            offspring.append(_Individual(
                params=dict(e.params), fitness=e.fitness, sharpe=e.sharpe,
                return_pct=e.return_pct, max_dd=e.max_dd, win_rate=e.win_rate,
                profit_factor=e.profit_factor, trade_count=e.trade_count,
                evaluated=True, label="elite"
            ))

        # Generate remaining offspring
        new_needed = offspring_count - len(offspring)
        children_to_eval: list[_Individual] = []

        for _ in range(max(0, new_needed)):
            if random.random() < 0.85:
                # Crossover + mutation
                parent_a = _tournament_select(population, tournament_size)
                parent_b = _tournament_select(population, tournament_size)
                # Avoid identical parents
                attempts = 0
                while parent_b is parent_a and attempts < 3:
                    parent_b = _tournament_select(population, tournament_size)
                    attempts += 1
                child_params = _blx_crossover(parent_a.params, parent_b.params, schema, alpha=0.3)
                child_params = _mutate(child_params, schema, mutation_rate, mutation_strength)
                child = _Individual(params=child_params, label="crossover")
            else:
                # Pure random exploration (15% of offspring)
                child = _Individual(params=_sample_params(schema), label="random")

            children_to_eval.append(child)
            offspring.append(child)

        # --- Evaluate new offspring in parallel ---
        if children_to_eval:
            remaining_budget = max_iterations - evaluations_done
            batch = children_to_eval[:remaining_budget]
            await _evaluate_batch(batch)

            for ind in batch:
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
                    "label": ind.label,
                })

        # --- Update population (only keep evaluated individuals) ---
        evaluated_offspring = [ind for ind in offspring if ind.evaluated]
        if evaluated_offspring:
            population = sorted(evaluated_offspring, key=lambda ind: ind.fitness, reverse=True)[:pop_size]
        # else keep previous population

        # --- Update global best ---
        gen_best = population[0] if population else None
        if gen_best and gen_best.fitness > result.best_fitness:
            result.best_fitness = gen_best.fitness
            result.best_sharpe = gen_best.sharpe
            result.best_return_pct = gen_best.return_pct
            result.best_max_drawdown = gen_best.max_dd
            result.best_win_rate = gen_best.win_rate
            result.best_profit_factor = gen_best.profit_factor
            result.best_trade_count = gen_best.trade_count
            result.best_params = dict(gen_best.params)
            logger.info(
                f"Optimizer gen {generation} new best: "
                f"fitness={gen_best.fitness:.3f} Sharpe={gen_best.sharpe:.2f} "
                f"Return={gen_best.return_pct:+.2f}% DD={gen_best.max_dd:.1f}%"
            )

        # Track stagnation
        if result.best_fitness > prev_best_fitness + 0.01:
            generations_without_improvement = 0
            prev_best_fitness = result.best_fitness
        else:
            generations_without_improvement += 1

        result.iterations_run = evaluations_done

        # --- Progress callback ---
        if progress_callback:
            pct = min(95, evaluations_done / max_iterations * 95)
            pop_diversity = _population_diversity(population, schema)
            await progress_callback(
                pct,
                f"Gen {generation} | {evaluations_done}/{max_iterations} evals | "
                f"Best fitness: {result.best_fitness:.3f} (Sharpe {result.best_sharpe:.2f}) | "
                f"Diversity: {pop_diversity:.1f}%"
            )

    # ------------------------------------------------------------------
    # 4. Cleanup + final results
    # ------------------------------------------------------------------
    _pool.shutdown(wait=False)   # don't block — workers are done
    result.iterations_run = evaluations_done
    result.duration_seconds = time.monotonic() - start_time

    if progress_callback:
        await progress_callback(100, f"Done! Best Sharpe: {result.best_sharpe:.2f}, Fitness: {result.best_fitness:.3f}")

    logger.info(
        f"Optimization {bot_id} complete: {evaluations_done} evaluations, "
        f"{result.generations_run} generations in {result.duration_seconds:.1f}s | "
        f"Best fitness: {result.best_fitness:.3f}, Sharpe: {result.best_sharpe:.2f} "
        f"(was {result.current_sharpe:.2f}), Return: {result.best_return_pct:+.2f}% | "
        f"Restarts: {result.stagnation_restarts}"
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
    all_candles = await repo.get_historical_candles(symbol)
    if not all_candles:
        raise ValueError(f"No historical data for {symbol}. Download it first.")

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
                _candle_override=train_candles,  # internal: skip DB, use these candles
            )
        except Exception as e:
            logger.error(f"WFO fold {fold_num} optimization failed: {e}", exc_info=True)
            continue

        best_params = opt.best_params

        # --- Evaluate best_params on OOS test data ---
        try:
            _initial_balance = initial_balance or settings.initial_usdt_balance
            oos_bt = await run_backtest(
                bot_id=f"{bot_id}_wf{fold_num}_oos",
                symbol=symbol,
                strategy_class=strategy_class,
                params=best_params,
                initial_balance=_initial_balance,
                fee_rate=fee_rate,
                equity_interval=5,
                candle_data=test_candles,
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
            _candle_override=all_candles,
        )
        result.final_params = final_opt.best_params
        result.final_is_return_pct = final_opt.best_return_pct
        result.final_is_sharpe = final_opt.best_sharpe
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
