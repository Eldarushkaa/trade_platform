# Architectural Audit — Trade Platform

**Scan date:** 2026-03-14  
**Files reviewed:** all Python sources in `core/`, `strategies/`, `data/`, `db/`, `api/`, `main.py`, `config.py`

---

## Summary

The platform is broadly well-structured, but accumulated technical debt from iterative development shows up in ~25 distinct issues. They fall into four categories:

| Category | Count | Severity |
|----------|-------|----------|
| Dead / unused code | 6 | Low |
| Code duplication / copy-paste | 5 | Medium |
| Fragile / grey decisions | 8 | Medium–High |
| Actual bugs | 6 | High |

---

## Category 1 — Dead / Unused Code

### D1 · `market_type` config field never read
**File:** [`config.py:26`](config.py:26)  
`market_type: Literal["futures", "spot"]` is declared and set to `"futures"` but **never read anywhere** in the codebase. The entire platform is hardcoded to futures logic. Remove or wire it up.

### D2 · `maker_fee_rate` / `taker_fee_rate` are dead aliases
**File:** [`config.py:54-57`](config.py:54-57)  
Three fee fields exist: `maker_fee_rate`, `taker_fee_rate`, and `simulation_fee_rate`. Only `simulation_fee_rate` is actually used by `SimulationEngine` and `BotManager`. The other two are never read. Either collapse to one field or actually use the maker/taker distinction.

### D3 · `binance_testnet` config field never read
**File:** [`config.py:32`](config.py:32)  
`binance_testnet: bool = True` is declared but no code ever reads it. The feed always connects to the same production URL.

### D4 · `bot._price_queue` — allocated but never consumed
**File:** [`core/bot_manager.py:121`](core/bot_manager.py:121), [`core/base_strategy.py:57`](core/base_strategy.py:57)  
`bot._price_queue = asyncio.Queue()` is set on every bot at startup, but **nothing ever puts into it** from the live path and **nothing reads from it**. `BotManager.dispatch_price()` calls `engine.update_price()` directly — the queue is a dead artefact from an earlier design. Remove it from both `BaseStrategy.__init__` and `BotManager.start_bot()`.

### D5 · `strategies/__init__.py` is empty
**File:** [`strategies/__init__.py`](strategies/__init__.py)  
No exports, no registration. Currently `main.py` imports each strategy manually. The `__init__.py` could at minimum document the convention, or export a registry.

### D6 · `main.py` comment says "3 strategies × 3 coins = 9 bots" but `STRATEGY_CLASSES` has 4
**File:** [`main.py:23-26`](main.py:23-26)  
The module docstring and comment both say "9 bots" but `STRATEGY_CLASSES = [RSIBot, MACrossoverBot, BollingerBot, OrderbookWallBot]` produces 12 bots. The comments went stale when `OrderbookWallBot` was added.

---

## Category 2 — Code Duplication

### C1 · `_open_position` / `_close_position` copy-pasted across all strategies
**Files:** [`strategies/example_rsi_bot.py:188-236`](strategies/example_rsi_bot.py:188), [`strategies/example_ma_crossover.py:190-227`](strategies/example_ma_crossover.py:190), [`strategies/bollinger_bot.py:188-230`](strategies/bollinger_bot.py:188)  
All three strategies have near-identical `_open_position(price, side)` and `_close_position(price, side, reason)` methods. The only differences are the logging format strings. This logic should live on `BaseStrategy` as `_open_long / _open_short / _close_position` helpers, reducing the strategies to pure signal logic.

### C2 · `for_symbol()` classmethod copy-pasted in every strategy
**Files:** [`strategies/example_rsi_bot.py:70-77`](strategies/example_rsi_bot.py:70), [`strategies/example_ma_crossover.py:64-71`](strategies/example_ma_crossover.py:64), [`strategies/bollinger_bot.py:77-84`](strategies/bollinger_bot.py:77), [`strategies/orderbook_wall_bot.py:219-225`](strategies/orderbook_wall_bot.py:219)  
Identical factory pattern in all four strategy files. Should live on `BaseStrategy` with a `name_prefix` class attribute that each strategy sets, then `for_symbol()` uses it.

### C3 · `_candle_count` tracked individually in each strategy
**Files:** [`strategies/example_rsi_bot.py:89`](strategies/example_rsi_bot.py:89), [`strategies/example_ma_crossover.py:86`](strategies/example_ma_crossover.py:86), [`strategies/bollinger_bot.py:97`](strategies/bollinger_bot.py:97), [`strategies/orderbook_wall_bot.py:233`](strategies/orderbook_wall_bot.py:233)  
Every strategy manually tracks `self._candle_count`. This counter could be on `BaseStrategy` and incremented by the base `on_candle()` hook before calling the strategy-specific logic.

### C4 · `TradeRecord` position_side fallback repeated in two query methods
**File:** [`db/repository.py:155`](db/repository.py:155), [`db/repository.py:181`](db/repository.py:181)  
Both `get_trades_for_bot()` and `get_latest_trade()` contain `row["position_side"] if "position_side" in row.keys() else "LONG"`. This defensive fallback for the old schema exists in two places and can now be removed since the migration has been applied.

### C5 · `_safe()` / `_safe_trial()` float sanitisation duplicated
**Files:** [`core/backtest_engine.py:83-90`](core/backtest_engine.py:83), [`core/optimizer.py:73-84`](core/optimizer.py:73)  
Both files define `_safe(v)` — a helper that replaces `inf`/`nan` floats with JSON-safe values. Should be in a shared utility module.

---

## Category 3 — Fragile / Grey Decisions

### G1 · Dependency injection via module-level globals + `set_xxx()` functions
**Files:** [`core/llm_agent.py:28-36`](core/llm_agent.py:28), [`api/routes/bots.py:24-35`](api/routes/bots.py:24), [`api/routes/portfolio.py:16-22`](api/routes/portfolio.py:16), [`api/routes/backtest.py:28-37`](api/routes/backtest.py:28)  
Four different modules use the pattern `_thing = None; def set_thing(x): global _thing; _thing = x`. This is effectively global mutable state that happens after import. It's error-prone (calling a route before `set_xxx()` silently gets `None`), untestable, and non-idiomatic for FastAPI. The standard approach is **FastAPI dependency injection** (`Depends()`) or at minimum storing the dependencies on the `app.state` object.

### G2 · `bot.__class__.__bases__[0].__name__` — MRO spelunking
**File:** [`api/routes/bots.py:155`](api/routes/bots.py:155)  
```python
"strategy": bot.__class__.__bases__[0].__name__,
```
This walks the MRO to get the "real" strategy name. It works now because `for_symbol()` creates a one-level subclass, but will silently return a wrong name if the inheritance chain ever changes. The proper fix: add a `strategy_class_name: str` class attribute to `BaseStrategy` that `for_symbol()` preserves.

### G3 · `engine._skip_db` — private flag set from outside
**File:** [`core/backtest_engine.py:309`](core/backtest_engine.py:309)  
```python
engine._skip_db = True
```
The backtest engine reaches into `SimulationEngine`'s private state to suppress DB writes. The `finally:` block restores it, but this is fragile — if someone creates a `SimulationEngine` for backtest and forgets the flag, trades leak into the live DB. Should be a constructor parameter: `SimulationEngine(skip_db=True)` or a context manager.

### G4 · `BotManager` directly accesses `engine._portfolios`
**Files:** [`core/bot_manager.py:279`](core/bot_manager.py:279), [`core/bot_manager.py:387`](core/bot_manager.py:387)  
Two methods (`reset_bot()` and `_restore_balance_from_snapshot()`) do `self.engine._portfolios.get(bot_id)` and then mutate the portfolio object's fields directly. `_portfolios` is a private dict. `SimulationEngine` should expose `get_portfolio(bot_id)` and `reset_portfolio(bot_id)` methods.

### G5 · `OrderbookWallBot` imports from `db.repository` directly
**File:** [`strategies/orderbook_wall_bot.py:43`](strategies/orderbook_wall_bot.py:43)  
```python
from db import repository as repo
```
This is the only strategy that talks to the database. Strategies should be pure signal generators. The live-mode orderbook fetch (`repo.get_orderbook_full()`) violates the layer boundary — strategies should only know about `self.engine`. The engine already has an `update_orderbook()` method and a price cache; it should expose `get_orderbook(symbol)` too, and the feed/BotManager should push fresh snapshots into it.

### G6 · `_running_tasks` dict in backtest route never pruned
**File:** [`api/routes/backtest.py:31`](api/routes/backtest.py:31)  
Completed backtest/optimization tasks accumulate in `_running_tasks` indefinitely. With the optimizer's large result dicts (equity curve, all trials), this is a memory leak. Add a max-size eviction or TTL cleanup.

### G7 · Datetime timezone inconsistency in DB layer
**File:** [`db/repository.py:19-28`](db/repository.py:19)  
`_DT_FMT = "%Y-%m-%dT%H:%M:%S.%f"` stores datetimes **without timezone offset**. `_str_to_dt()` returns **naive** `datetime` objects. But `get_bot_trade_stats_since()` converts an aware datetime to naive before comparison. Mixed aware/naive datetime arithmetic will raise `TypeError` in Python if the code path ever changes. All timestamps should be stored with UTC suffix `+00:00` (ISO 8601 compliant) and consistently parsed with `datetime.fromisoformat()`.

### G8 · `CandleAggregator` drops the last partial candle on shutdown
**File:** [`data/candle_aggregator.py:137`](data/candle_aggregator.py:137)  
When the WebSocket feed stops, any in-progress candle in `self._in_progress` is silently discarded. For backtesting this doesn't matter (historical candles are complete), but in live mode the last partial minute of data is lost. A `flush()` method should be added and called on shutdown.

---

## Category 4 — Actual Bugs

### B1 · `asyncio.get_event_loop()` deprecated — in `price_cache.py`
**File:** [`data/price_cache.py:36`](data/price_cache.py:36)  
```python
loop = asyncio.get_event_loop()
loop.create_task(self._notify(symbol, price))
```
`get_event_loop()` is deprecated since Python 3.10 and will emit a `DeprecationWarning` (and in Python 3.12+ may raise `RuntimeError` if no running loop). `candle_aggregator.py` already uses the correct `asyncio.get_running_loop()`. `price_cache.py` was not updated.

### B2 · `BollingerBot._closes` deque `maxlen` is fixed at construction time
**File:** [`strategies/bollinger_bot.py:92`](strategies/bollinger_bot.py:92)  
```python
self._closes: deque[float] = deque(maxlen=self.BB_PERIOD)
```
If `set_params()` changes `BB_PERIOD` at runtime, the deque's `maxlen` stays at the original value. The Bollinger bands will silently compute over the wrong number of candles. Fix: rebuild the deque in `set_params()` when `BB_PERIOD` changes (keeping existing data, trimmed or padded as needed).

### B3 · RSI bot state is not reset when `RSI_PERIOD` parameter changes at runtime
**File:** [`strategies/example_rsi_bot.py:166-173`](strategies/example_rsi_bot.py:166)  
`set_params()` can update `self.RSI_PERIOD`, but `_avg_gain`, `_avg_loss`, `_warmup_closes`, and `_prev_close` are not reset. The Wilder RSI will continue computing with the wrong alpha (`1/old_period`). Fix: override `set_params()` or add an `on_params_changed()` hook in `BaseStrategy` that lets strategies clear their state when critical parameters change.

### B4 · `backtest_engine.py` trade timestamp is off-by-one
**File:** [`core/backtest_engine.py:322`](core/backtest_engine.py:322)  
```python
"timestamp": candle_rows[min(result.candles_processed, len(candle_rows) - 1)]["open_time"],
```
`result.candles_processed` is incremented **after** this closure runs (on line 415: `result.candles_processed = i + 1`). So a trade triggered at candle `i` records the timestamp of candle `i-1`. The fix is to capture `row["open_time"]` in the outer loop scope and close over it in the interceptor.

### B5 · `SimulationEngine.place_order` checks margin BEFORE computing fill price, but deducts fee AFTER
**File:** [`core/simulation_engine.py:312-320`](core/simulation_engine.py:312)  
The margin sufficiency check uses `margin_needed + fee_usdt` based on `fill_price`, but the fee is not yet deducted when `portfolio.open_long()` runs. Then `portfolio.deduct_fee()` is called separately after. If `fill_price > desired_price` (slippage path) the margin check is correct, but the fee deduction path can push balance negative without triggering the check because `portfolio.open_long()` only validates `margin > usdt_balance`, not `margin + fee > usdt_balance` (the engine does the combined check, portfolio doesn't). This means it's possible to end up with `usdt_balance < 0` after the fee deduction on small balances.

### B6 · Liquidation is checked on raw tick price, not candle close price, but the same portfolio uses candle-based entry prices
**File:** [`core/simulation_engine.py:135-144`](core/simulation_engine.py:135)  
Liquidation price is computed from `entry_price` which is always a **candle close price**. But liquidation is checked against every **raw tick** price from `update_price()`. For highly volatile assets, intra-candle ticks can temporarily dip below (or spike above) the liquidation price and trigger liquidation even though the candle closed safely. This leads to more liquidations in simulation than would occur on a real exchange (which uses mark price, not last price). This is a simulation-realism issue but functionally causes incorrect bot behavior. The liquidation check should at minimum note this assumption, or be moved to candle boundaries.

---

## Cleanup Priority

```
Priority 1 (bugs, correctness):
  B1 · asyncio.get_event_loop() deprecation
  B2 · BollingerBot deque maxlen not updated on param change
  B3 · RSI state not reset on RSI_PERIOD change
  B4 · Backtest trade timestamp off-by-one
  B5 · Fee deduction can push balance negative
  B6 · Tick-based liquidation vs candle-based entries (document or fix)

Priority 2 (fragile, will break):
  G2 · bot.__class__.__bases__[0] fragile MRO access
  G3 · engine._skip_db direct internal flag
  G4 · BotManager accesses engine._portfolios directly
  G7 · Datetime naive/aware inconsistency

Priority 3 (architecture, maintainability):
  G1 · Module-level global DI pattern
  G5 · Strategy DB access (OrderbookWallBot)
  C1 · Duplicate _open/_close_position across strategies
  C2 · for_symbol() copy-paste
  G6 · _running_tasks memory leak
  G8 · CandleAggregator no flush on shutdown

Priority 4 (cleanup):
  D1–D6 · Dead config fields, dead price queue, stale comments
  C3–C5 · Minor duplication
  B4 · Backtest timestamp
```

---

## Files Most Affected by Cleanup

| File | Issues |
|------|--------|
| [`core/base_strategy.py`](core/base_strategy.py) | C1, C2, C3 — add shared order helpers, for_symbol, candle_count |
| [`core/simulation_engine.py`](core/simulation_engine.py) | G3, G4, B5, B6 — expose portfolio API, fix fee logic |
| [`core/bot_manager.py`](core/bot_manager.py) | G4, D4 — remove _portfolios access, remove dead _price_queue |
| [`strategies/example_rsi_bot.py`](strategies/example_rsi_bot.py) | C1, C2, C3, B3 — inherit helpers, fix param reset |
| [`strategies/example_ma_crossover.py`](strategies/example_ma_crossover.py) | C1, C2, C3 — inherit helpers |
| [`strategies/bollinger_bot.py`](strategies/bollinger_bot.py) | C1, C2, C3, B2 — fix deque, inherit helpers |
| [`strategies/orderbook_wall_bot.py`](strategies/orderbook_wall_bot.py) | C2, G5 — remove repo import |
| [`config.py`](config.py) | D1, D2, D3 — remove dead fields |
| [`data/price_cache.py`](data/price_cache.py) | B1 — fix get_event_loop |
| [`db/repository.py`](db/repository.py) | C4, G7 — remove old fallbacks, fix datetime |
| [`api/routes/bots.py`](api/routes/bots.py) | G1, G2 — DI, fix MRO access |
| [`api/routes/portfolio.py`](api/routes/portfolio.py) | G1 — DI |
| [`api/routes/backtest.py`](api/routes/backtest.py) | G1, G6 — DI, task cleanup |
| [`core/backtest_engine.py`](core/backtest_engine.py) | G3, B4, C5 — constructor flag, fix timestamp |
| [`main.py`](main.py) | D4, D6 — remove dead queue, fix comment |
