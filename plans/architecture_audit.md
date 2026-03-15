# Architecture Audit — Trade Platform

> Full scan completed 2026-03-15. Issues grouped by severity and category.

---

## Summary

| Severity | Count |
|---|---|
| 🔴 Critical (breaks abstraction / correctness) | 6 |
| 🟠 Major (gray decisions / hidden coupling) | 8 |
| 🟡 Minor (style, naming, doc drift) | 8 |

---

## 🔴 Critical Issues

---

### C1 — Module-level singletons contradict the stated DI pattern

**Files:** [`core/simulation_engine.py:521`](core/simulation_engine.py:521), [`data/price_cache.py:77`](data/price_cache.py:77)

**Problem:**
```python
# core/simulation_engine.py — bottom of file
simulation_engine = SimulationEngine()

# data/price_cache.py — bottom of file
price_cache = PriceCache()
```
Both are imported directly in [`main.py`](main.py:40-45) and used throughout the app. The architecture doc explicitly states **"No module-level globals with set_xxx() injection"** and shows the `app.state` DI pattern — but these two singletons bypass it entirely.

**Impact:**
- Impossible to unit test `BotManager`, `BinanceFeed`, or strategies without the full singletons being alive
- `simulation_engine` is shared between live app and... itself (backtest always creates a fresh `SimulationEngine(skip_db=True)`)
- `price_cache` is referenced as a default argument in `BinanceFeed.__init__` (`cache = cache or default_cache`) — test isolation is broken

**Fix:**
- Remove the module-level instantiation lines
- Instantiate both inside `lifespan()` in `main.py`, store in `app.state`
- Remove `default_cache` fallback in `BinanceFeed.__init__`

---

### C2 — `BotManager` has `isinstance(engine, SimulationEngine)` checks everywhere — breaking the `BaseOrderEngine` abstraction

**File:** [`core/bot_manager.py`](core/bot_manager.py)

**Problem:** 6 separate `isinstance(self.engine, SimulationEngine)` checks scattered across `BotManager`:
- `register()` line 81
- `start_all()` line 183, 192
- `dispatch_price()` line 224
- `reset_bot()` line 276
- `_snapshot_loop()` line 361
- `_restore_balance_from_snapshot()` line 389

These guard calls to `register_bot()`, `update_price()`, `save_snapshot()`, `reset_portfolio()`, `get_portfolio()` — none of which exist on `BaseOrderEngine`. Adding a `LiveBinanceEngine` would require editing every one of these guards.

**Impact:** The migration path "swap `SimulationEngine` → `LiveBinanceEngine` and only change `main.py`" described in architecture.md is **false** — `BotManager` would also need changes.

**Fix:** Move these methods onto `BaseOrderEngine` as abstract methods (or provide default no-ops). `BotManager` should only ever call `BaseOrderEngine` methods.

---

### C3 — `_compute_fill_price` doesn't actually walk OB levels — the stated VWAP feature was silently removed

**File:** [`core/simulation_engine.py:218-249`](core/simulation_engine.py:218)

**Problem:** Architecture doc, function docstring, and even the module docstring all say:
> *"walks the relevant side (asks for BUY, bids for SELL) to compute a VWAP fill price"*

But the actual implementation when OB data **is** present:
```python
# OB data present: use desired_price (candle close) — no extra slippage
return desired_price, "ob_confirmed"
```
It does zero VWAP walking. It just returns the desired price unchanged. The `_orderbooks` dict is populated but never iterated during fill computation.

**Impact:** OB data is loaded into the engine but has no effect on fill prices (beyond the name `"ob_confirmed"`). The stated simulation realism is misleading.

**Fix:** Either implement proper OB VWAP walking (walk asks for BUY, bids for SELL until `quantity` is absorbed), or explicitly document that OB data is only used by `OrderbookWallBot` for signal generation, and update all docs to reflect that fixed slippage is always used.

---

### C4 — `run_backtest` monkey-patches `engine.place_order` to intercept trades

**File:** [`core/backtest_engine.py:312-331`](core/backtest_engine.py:312)

**Problem:**
```python
original_place_order = engine.place_order

async def intercepting_place_order(bot_id, symbol, side, quantity, price):
    order_result = await original_place_order(bot_id, symbol, side, quantity, price)
    result.trades.append(...)
    return order_result

engine.place_order = intercepting_place_order   # ← monkey-patch
```
Instance method replacement via closure is a fragile crunch. It breaks IDE navigation, type checking, and is tricky to debug if the closure captures stale state.

**Fix:** Add an optional `trade_callback` parameter to `SimulationEngine.__init__` or `place_order()`, or create a `BacktestSimulationEngine(SimulationEngine)` subclass that overrides `place_order` cleanly.

---

### C5 — `TradeRecord.position_side` defaults to `"LONG"` instead of `None`

**File:** [`db/models.py:37`](db/models.py:37)

**Problem:**
```python
position_side: str = "LONG"   # "OPEN_LONG", "CLOSE_LONG", "OPEN_SHORT", "CLOSE_SHORT"
```
The default is `"LONG"` — but this field actually holds action strings like `"OPEN_LONG"`. Any code that creates a `TradeRecord` without specifying `position_side` would silently write the wrong value. The `get_latest_trade()` fallback in `repository.py:195` uses `"LONG"` as a default too:
```python
position_side=row["position_side"] if "position_side" in row.keys() else "LONG",
```
This means old rows without the column (pre-migration) get classified as `"LONG"` actions instead of a neutral unknown state, which can corrupt `_restore_balance_from_snapshot`.

**Fix:** Default to `None` or `""`. Update `get_latest_trade()` fallback to `None`.

---

### C6 — Live `volume` (tick count) vs backtest `volume` (real Binance volume) mismatch

**File:** [`data/candle_aggregator.py:80`](data/candle_aggregator.py:80)

**Problem:** In live mode, candle `volume` is the number of WebSocket ticks received:
```python
def update(self, price: float) -> None:
    ...
    self.volume += 1.0    # tick counter
```
But historical candles from Binance store real coin volume. If any strategy uses `candle.volume` as a signal (e.g., volume confirmation), it would receive incompatible data in backtest vs live.

**Impact:** Silent backtest invalidation for any volume-aware strategy added in the future. No current strategy uses `candle.volume` for signals, but the data type contract is broken.

**Fix:** Either always store real volume (pass Binance `q` quantity from aggTrade messages through `PriceCache` → `CandleAggregator`) or rename the field to `tick_count` in the `Candle` dataclass to make the divergence explicit.

---

## 🟠 Major Issues

---

### M1 — `LLM Agent` uses module-level globals, contradicting the stated DI pattern

**File:** [`core/llm_agent.py:28-29`](core/llm_agent.py:28)

**Problem:**
```python
_app = None         # module-level
_agent_task = None  # module-level
_agent_enabled = False  # module-level
```
Architecture doc says "No module-level globals" and shows `app.state` DI. The LLM agent accesses `_app.state.bot_manager` via private helpers `_get_bot_manager()` and `_get_engine()` which gate on `_app is None`. The `_app` reference is set during `start_agent(app)` — this is the same "set_xxx() injection" pattern the architecture explicitly rejects.

**Fix:** Convert `LLMAgent` to a class, inject `bot_manager` and `engine` in the constructor, store on `app.state.llm_agent`. Routes call `app.state.llm_agent.run_decision_cycle()`.

---

### M2 — `_restore_balance_from_snapshot` is fragile position reconstruction from DB heuristics

**File:** [`core/bot_manager.py:370-463`](core/bot_manager.py:370)

**Problem:** On restart, the bot manager tries to reconstruct open positions by:
1. Loading the latest snapshot for `usdt_balance`
2. Reading the `position_side` string of the last trade to determine if a position is open
3. Manually recomputing `liquidation_price = entry - (margin / qty)` — duplicating `VirtualPortfolio` logic

This fails silently in multiple edge cases:
- Partial closes (`CLOSE_LONG_PARTIAL`) leave a non-zero position that `startswith("OPEN_")` won't catch
- If the last trade was a liquidation (no DB record), position state is lost
- The liquidation formula assumes full position; wrong for partial closures
- Two separate DB queries (`get_latest_snapshot` + `get_latest_nondefault_snapshot`) with a "tolerance of 1 USDT" heuristic to skip default-balance snapshots

**Fix:** Persist a structured `position_state` JSON blob in `portfolio_snapshots` (or a separate `bot_state` table) that stores the full `FuturesPosition` at snapshot time. Restore directly from that, not from trade reconstruction.

---

### M3 — Duplicate fee rate config with dead field

**File:** [`config.py:47-49`](config.py:47)

**Problem:**
```python
taker_fee_rate: float = Field(default=0.0005, ...)      # never used
simulation_fee_rate: float = Field(default=0.0005, ...) # used everywhere
```
`taker_fee_rate` is defined but nothing in the codebase reads it — only `simulation_fee_rate` is used. Having both creates confusion about which one to set in `.env`.

**Fix:** Remove `taker_fee_rate` and keep only `simulation_fee_rate` (or rename it to `fee_rate`).

---

### M4 — `max_slippage_pct` documented as "bps" but value is percentage

**File:** [`config.py:57`](config.py:57)

**Problem:**
```python
max_slippage_pct: float = Field(
    default=0.10,
    description="Max allowed slippage % (0.10 = 10 bps) — reject if exceeded"
)
```
`0.10 = 10%`, not `10 bps`. 10 bps = `0.001`. This comment is wrong by a factor of 100. Additionally, `max_slippage_pct` is never actually checked anywhere in `_compute_fill_price` — the "reject order if slippage exceeds this" behavior described in the architecture doc and module docstring does not exist in the code.

**Fix:** Correct the comment. Implement the slippage reject check if the feature is intended.

---

### M5 — `MACrossoverBot` doesn't reset EMA/MACD state when periods change at runtime

**File:** [`strategies/example_ma_crossover.py`](strategies/example_ma_crossover.py)

**Problem:** `RSIBot` and `BollingerBot` both override `set_params()` to reset indicator state when their period parameters change at runtime (to prevent stale EMA values). `MACrossoverBot` has no such override — if the LLM agent or dashboard changes `FAST_PERIOD`, `SLOW_PERIOD`, or `SIGNAL_PERIOD`, the old EMA values computed with the wrong multiplier continue to be used silently until convergence.

**Fix:** Override `set_params()` in `MACrossoverBot` to reset `_fast_ema`, `_slow_ema`, `_signal`, `_macd`, and `_warmup_closes` when any period changes.

---

### M6 — `MACrossoverBot` has no `COOLDOWN_CANDLES` parameter

**File:** [`strategies/example_ma_crossover.py`](strategies/example_ma_crossover.py)

**Problem:** All other three strategies have a `COOLDOWN_CANDLES` parameter limiting trade frequency. MACD bot has none. On volatile 1-minute candles, MACD can rapidly cross back and forth, generating orders on every candle.

**Fix:** Add `COOLDOWN_CANDLES` to `MACrossoverBot.PARAM_SCHEMA` and check it in `_check_signals()`.

---

### M7 — `SimulationEngine.get_orderbook_snapshot()` hits DB on every candle in live mode (cache bypass)

**File:** [`core/simulation_engine.py:472-478`](core/simulation_engine.py:472)

**Problem:**
```python
async def get_orderbook_snapshot(self, symbol: str) -> Optional[dict]:
    return await repo.get_orderbook_full(symbol)   # DB query every call
```
`OrderbookWallBot` calls `engine.get_orderbook_snapshot(self.symbol)` on every candle in live mode. The engine already has `self._orderbooks` populated and refreshed by `BotManager._orderbook_refresh_loop`. This method bypasses the in-memory cache and goes straight to DB — duplicating work and adding latency.

**Fix:** Return `self._orderbooks.get(symbol)` first, fall back to DB only if cache is empty.

---

### M8 — `backtest_engine.py` `_running_tasks` is a module-level dict holding live `asyncio.Task` references

**File:** [`api/routes/backtest.py:29`](api/routes/backtest.py:29)

**Problem:**
```python
_running_tasks: dict[str, dict] = {}
```
This is another module-level global. The dict holds live `asyncio.Task` objects. There's TTL eviction (1 hour) but:
- No upper bound on number of concurrent optimization tasks
- Old tasks are only evicted when `GET /api/backtest/status` is called (lazy eviction)
- `asyncio.Task` in the dict dict value keeps the coroutine alive in memory even after completion

**Fix:** Move task tracking into a `BacktestService` class stored on `app.state`. Use `asyncio.Task` weak references or store only metadata after task completion.

---

## 🟡 Minor Issues

---

### N1 — Strategy filenames have `example_` prefix but are production code

**Files:** [`strategies/example_rsi_bot.py`](strategies/example_rsi_bot.py), [`strategies/example_ma_crossover.py`](strategies/example_ma_crossover.py)

The `example_` prefix implies these are demo/template files. They are actually production strategies. Rename to `rsi_bot.py` and `ma_crossover_bot.py`.

---

### N2 — Architecture doc and LLM prompt have wrong bot count (9 vs 12)

**Files:** [`core/llm_agent.py:50`](core/llm_agent.py:50), [`plans/architecture.md`](plans/architecture.md)

The LLM `SYSTEM_PROMPT` says:
```
9 bots total: 3 strategies × 3 coins (BTC, ETH, SOL)
Strategies: RSI (momentum), MACD Crossover (trend), Bollinger Bands (mean reversion)
```
Actual count is **12 bots** (4 strategies × 3 coins). `OrderbookWallBot` is not mentioned in the prompt. The LLM agent will never know about or manage `ob_wall` bots.

---

### N3 — `OrderbookWallBot.name_prefix = "ob_wall"` creates `ob_wall_btc` not `ob_btc` as documented

**File:** [`strategies/orderbook_wall_bot.py:158`](strategies/orderbook_wall_bot.py:158)

Architecture doc table shows bots named `ob_btc/eth/sol`. Actual names generated by `for_symbol()` are `ob_wall_btc/eth/sol`. Minor inconsistency, but the dashboard and any hardcoded references to bot names would reflect the longer name.

---

### N4 — `VirtualPortfolio.leverage` has incorrect type annotation

**File:** [`core/virtual_portfolio.py:92`](core/virtual_portfolio.py:92)

```python
def __init__(self, ..., leverage: int = None) -> None:
```
`int = None` is invalid — should be `Optional[int] = None`.

---

### N5 — `import random` inside `_snapshot_loop` method body

**File:** [`core/bot_manager.py:353`](core/bot_manager.py:353)

```python
async def _snapshot_loop(self, bot_id: str) -> None:
    import random    # ← inside method
```
Should be at module level. Same pattern appears in `backtest_engine.py` (`import time`) and `backtest_engine.py`'s OB-trim block (`from datetime import datetime`).

---

### N6 — `portfolio.py` route ordering creates a fragile `"all"` literal vs `{bot_name}` conflict

**File:** [`api/routes/portfolio.py:85`](api/routes/portfolio.py:85)

The comment reads:
```python
# NOTE: This route MUST be declared before /{bot_name} to prevent FastAPI
# from matching the literal "all" as a bot_name path parameter.
```
This is a known FastAPI ordering gotcha. Better fix: rename `/all` → `/summary` or move to a distinct prefix like `/api/portfolios` so the conflict can't exist.

---

### N7 — `repository.py` has `import json` inside function body

**File:** [`db/repository.py:760`](db/repository.py:760)

```python
async def get_orderbook_full(symbol: str) -> dict | None:
    ...
    import json    # ← inside function
    return { "bids": json.loads(row["bids_json"]), ... }
```
`json` is already imported at the top of `repository.py`. This is redundant.

---

### N8 — `BollingerBot` and `OrderbookWallBot` have empty `# Factory` comment sections

**Files:** [`strategies/bollinger_bot.py:76-79`](strategies/bollinger_bot.py:76), [`strategies/orderbook_wall_bot.py:217-220`](strategies/orderbook_wall_bot.py:217)

```python
# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------
```
These sections are empty — the factory is inherited from `BaseStrategy`. Dead comment scaffolding from copy-paste. Clean up.

---

## Architectural Decision Record — Open Gray Areas

These are design choices that are neither clearly right nor wrong, but deserve explicit documentation or a definitive decision:

| # | Area | Current State | Tension |
|---|---|---|---|
| G1 | Liquidation on raw ticks vs candle close | Acknowledged in code comment (`simulation_engine.py:155-178`) but never fixed | More liquidations than real exchange; acknowledged but not addressed |
| G2 | Single SQLite connection shared across 12 bots' snapshot writes | WAL + `busy_timeout=10000` as mitigation | Should be explicitly load-tested; consider connection pool or write queue |
| G3 | `OrderbookWallBot` IMBALANCE_MIN and IMBALANCE_MAX are both 0.50 | Default config means any imbalance triggers both LONG and SHORT rejection | Likely intended to be asymmetric (e.g. MIN=0.55, MAX=0.45); current default defeats the imbalance filter entirely |
| G4 | Backtest `equity_interval=5` hardcoded for API calls, `equity_interval=20` for optimizer | Different equity curve densities in different contexts; no user control | Minor but creates different analysis quality for manual vs optimized backtests |

---

## Recommended Cleanup Order

```
Phase 1 — Quick wins (no behavior change, low risk)
  N1 Rename example_ strategy files
  N2 Fix LLM prompt bot count (9→12, add ob_wall)
  N3 Fix name_prefix for ob_wall or update docs
  N4 Fix VirtualPortfolio leverage type hint
  N5 Move inline imports to module level
  N7 Remove redundant import json inside function
  N8 Remove empty Factory comment sections
  M3 Remove dead taker_fee_rate config field
  M4 Fix max_slippage_pct comment (bps → %)

Phase 2 — Correctness fixes (test thoroughly)
  C5 Fix TradeRecord.position_side default (None not "LONG")
  C6 Decide on volume semantics, rename tick_count or pass real volume
  M5 Add set_params() reset to MACrossoverBot
  M6 Add COOLDOWN_CANDLES to MACrossoverBot
  M7 Fix get_orderbook_snapshot() to use cache

Phase 3 — Architecture refactors (higher risk, higher value)
  C1 Remove module-level singletons (simulation_engine, price_cache)
  C2 Move all SimulationEngine-specific methods onto BaseOrderEngine
  C4 Replace monkey-patch with BacktestSimulationEngine subclass
  M1 Convert LLM agent to class, remove module globals
  M2 Persist structured position_state in snapshots, replace heuristic restore
  M8 Move _running_tasks to BacktestService on app.state

Phase 4 — Deferred / needs design discussion
  C3 Implement real OB VWAP walking in _compute_fill_price, or remove the claim
  G3 Fix IMBALANCE_MIN/MAX defaults for OrderbookWallBot
  G2 Evaluate SQLite write contention under load
```
