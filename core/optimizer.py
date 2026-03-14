"""
Parameter Optimizer — evolutionary genetic algorithm for strategy tuning.

Uses a real population-based GA with:
  - Tournament selection
  - BLX-α crossover (blend crossover for continuous params)
  - Adaptive mutation (wide early, narrow late; auto-widens on stagnation)
  - Elitism (top-K always survive)
  - Stagnation detection → inject fresh random individuals
  - Parallel batch evaluation via asyncio.gather
  - Multi-objective composite fitness (Sharpe + return − drawdown penalty)

Each evaluation runs a full backtest.  Compute budget:
    iterations ≈ population_size × generations

Usage:
    result = await optimize_params("rsi_btc", "BTCUSDT", RSIBot, max_iterations=1000)
"""
import asyncio
import logging
import math
import random
import time
from dataclasses import dataclass

from core.backtest_engine import run_backtest
from core.utils import safe_float as _safe
from db import repository as repo

logger = logging.getLogger(__name__)


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
        s = _safe
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

def _compute_fitness(sharpe: float, return_pct: float, max_dd: float, trade_count: int) -> float:
    """
    Composite fitness balancing multiple objectives.

    Components (all clamped to sensible ranges):
      - Sharpe ratio:   main signal (60% weight)
      - Return %:       reward profitability (25% weight)
      - Max drawdown:   penalize risk (15% weight)
      - Trade count:    hard minimum filter (< 3 trades → heavy penalty)
    """
    if trade_count < 3:
        return -1000.0 + trade_count  # basically dead, but rank by trade count

    # Clamp Sharpe to [-10, 10] to avoid inf domination
    s = max(-10.0, min(10.0, sharpe))

    # Normalize return to [-1, 1] range (cap at ±100%)
    r = max(-1.0, min(1.0, return_pct / 100.0))

    # Drawdown penalty: 0% dd → 0 penalty, 50% dd → 0.5 penalty
    dd_penalty = min(1.0, max_dd / 100.0)

    fitness = s * 0.60 + r * 5.0 * 0.25 - dd_penalty * 5.0 * 0.15
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
    progress_callback=None,
    concurrency: int = 4,
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
        progress_callback: Optional async callable(pct, msg)
        concurrency: Max parallel backtests (match to CPU cores)

    Returns:
        OptimizationResult with best params and comparison
    """
    start_time = time.monotonic()

    schema = strategy_class.PARAM_SCHEMA
    if not schema:
        raise ValueError(f"Strategy {strategy_class.__name__} has no PARAM_SCHEMA")

    # --- Pre-load candle data once (avoid N redundant DB reads) ---
    _cached_candles = await repo.get_historical_candles(symbol)
    if not _cached_candles:
        raise ValueError(f"No historical data for {symbol}. Download it first.")
    logger.info(f"Optimizer: pre-loaded {len(_cached_candles)} candles for {symbol}")

    # --- Pre-load orderbook data for ALL strategies ---
    # Bids/asks are pre-parsed once here (into (price,qty) tuples) so that
    # engine.update_orderbook() uses the fast path (no json.loads per candle).
    # This gives realistic VWAP fills for all strategies AND keeps optimizer fast.
    _cached_orderbook: list | None = None
    _ob_data = await repo.get_orderbook_snapshots_for_backtest(symbol)
    if _ob_data:
        _cached_orderbook = _ob_data
        ob_aware = hasattr(strategy_class, "_inject_orderbook")
        logger.info(
            f"Optimizer: pre-loaded {len(_cached_orderbook)} OB snapshots for {symbol} "
            f"(pre-parsed bids/asks, fill=OB-VWAP, signal_inject={ob_aware})"
        )
    else:
        logger.info(
            f"Optimizer: no OB snapshots for {symbol} — using fixed slippage fallback."
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
    # Helper: evaluate one individual
    # ------------------------------------------------------------------
    _eval_semaphore = asyncio.Semaphore(concurrency)

    async def _evaluate(ind: _Individual, idx: int) -> _Individual:
        nonlocal evaluations_done
        async with _eval_semaphore:
            try:
                bt = await run_backtest(
                    bot_id=f"opt_{bot_id}_{evaluations_done}",
                    symbol=symbol,
                    strategy_class=strategy_class,
                    params=ind.params,
                    initial_balance=initial_balance,
                    equity_interval=20,
                    candle_data=_cached_candles,
                    orderbook_data=_cached_orderbook,
                )
                ind.sharpe = bt.sharpe_ratio
                ind.return_pct = bt.return_pct
                ind.max_dd = bt.max_drawdown_pct
                ind.win_rate = bt.win_rate
                ind.profit_factor = bt.profit_factor
                ind.trade_count = bt.trade_count
                ind.fitness = _compute_fitness(bt.sharpe_ratio, bt.return_pct, bt.max_drawdown_pct, bt.trade_count)
                ind.evaluated = True
            except Exception as e:
                logger.warning(f"Eval failed for individual: {e}")
                ind.fitness = -2000.0
                ind.evaluated = True
            evaluations_done += 1
            return ind

    async def _evaluate_batch(individuals: list[_Individual]) -> None:
        """Evaluate a batch of individuals with bounded concurrency."""
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
    # 4. Final results
    # ------------------------------------------------------------------
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
