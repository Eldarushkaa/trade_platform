# Trade Platform — Architecture Overview

> **Purpose:** High-level reference for the project structure, components, and their connections.
> Detailed docs for each subsystem live in separate files in this directory.

---

## What This Project Is

A Python async crypto futures trading simulation platform that:
- Connects to **Binance Futures WebSocket** for live price data
- Runs trading bots (multiple strategies × coins) simultaneously in simulation
- Simulates **USDT-M perpetual futures** with 3× leverage, margin, and liquidation
- Provides a **FastAPI web dashboard** to monitor bots, trades, and portfolio in real-time
- Supports **backtesting**, **genetic parameter optimization**, and **Walk-Forward Optimization (WFO)** for each strategy
- Uses **on-demand OB depth fetching** (Binance REST) for realistic VWAP fill prices on live orders
- Supports **configurable candle timeframes** (1m / 5m / 15m / 1h) — each stored as a separate dataset
- Bots have a **persistent live-enabled flag** — all bots default to paused on restart; must be explicitly enabled per bot

---

## Project Structure

```
trade_platform/
├── main.py                        # App entrypoint — wires all components
├── config.py                      # Settings via pydantic-settings / .env
├── requirements.txt
│
├── core/                          # Engine layer — no HTTP, no DB imports in strategies
│   ├── base_strategy.py           # Abstract BaseStrategy + shared order helpers
│   ├── bot_manager.py             # Bot lifecycle, candle dispatch, snapshot loop
│   ├── simulation_engine.py       # BaseOrderEngine + SimulationEngine (fake exchange)
│   ├── virtual_portfolio.py       # FuturesPosition, margin, liquidation, P&L
│   ├── backtest_engine.py         # Historical candle replay + metrics (interval-aware)
│   ├── optimizer.py               # Genetic algorithm param optimizer (interval-aware)
│   └── utils.py                   # safe_float / safe_round helpers
│
├── data/                          # Market data ingestion
│   ├── binance_feed.py            # Binance Futures WebSocket aggTrade stream
│   ├── price_cache.py             # In-memory latest price + pub/sub
│   ├── candle_aggregator.py       # Builds OHLCV candles from ticks (configurable interval)
│   ├── orderbook_feed.py          # fetch_depth() — on-demand OB REST call for VWAP fills
│   └── historical.py              # REST download of historical klines (multi-interval)
│
├── strategies/                    # Signal logic only — no DB, no HTTP
│   ├── __init__.py                # Exports active strategy classes
│   ├── rsi.py                     # Wilder RSI + EMA200 proximity + ATR volatility filter
│   ├── donchian.py                # Donchian breakout (Turtle Trading) with binary entry filters
│   └── donchian_new.py            # Donchian breakout v2 — scoring-based filters (distance_score × vol_score)
│
├── db/                            # Persistence layer
│   ├── database.py                # aiosqlite connection, WAL mode, schema + migrations
│   ├── models.py                  # Dataclasses: BotRecord, TradeRecord, PortfolioSnapshot
│   └── repository.py              # All SQL queries (no raw SQL outside this file)
│
├── api/                           # HTTP layer
│   ├── routes/
│   │   ├── bots.py                # GET/POST/PATCH /api/bots — list, start, stop, live toggle, params
│   │   ├── portfolio.py           # GET /api/portfolio — balances, history
│   │   ├── trades.py              # GET /api/trades — trade history
│   │   ├── backtest.py            # POST /api/backtest/run, /optimize, /walk-forward; GET /candle-config
│   │   └── logs.py                # GET /api/logs — in-memory WARNING+ buffer
│   └── static/
│       ├── index.html             # Dashboard SPA shell
│       ├── style.css              # All CSS
│       └── app/                   # JS modules (7 files, plain globals)
│           ├── utils.js           # Shared state, fetch helpers, interval helpers
│           ├── bots.js            # Bot list, live toggle, global stats bar
│           ├── portfolio.js, settings.js
│           ├── backtest.js        # Backtest/optimize/WFO panel (interval-aware)
│           ├── logs.js, main.js
│
└── plans/
    ├── architecture.md            # This file
    └── frontend.md                # Frontend module reference
```

---

## Live Data Flow

```mermaid
graph TD
    WS[Binance Futures WebSocket\nfstream.binance.com] --> Feed[BinanceFeed]
    Feed --> PC[PriceCache\nlatest price + pub/sub]
    PC -->|on_tick| CA[CandleAggregator\nconfigurable OHLCV builder]
    PC -->|dispatch_price| BM[BotManager\nupdate_price + liquidation check]
    CA -->|on_candle| BM
    BM -->|candle per symbol| Bots[Strategy Bots\n(live_enabled only)]
    Bots -->|place_order| SE[SimulationEngine]
    SE -->|ob_fetcher: fetch_depth| Binance[Binance REST\nfapi depth]
    SE --> VP[VirtualPortfolio\nmargin / PnL / liquidation]
    SE -->|insert_trade| DB[(SQLite — aiosqlite\nWAL mode)]
    BM -->|snapshot every 60s| DB
    DB --> API[FastAPI routes]
    API --> Dashboard[Browser Dashboard]
```

---

## Bots — Live-Enabled Flag

Each bot has a `live_enabled` flag persisted in the `bots` DB table:

- **Default: `false`** — on every server restart, all bots start paused (no trading)
- The UI sidebar shows a toggle switch per bot card to enable/disable live trading
- `PATCH /api/bots/{name}/live` — persists the flag and immediately starts/stops the bot
- `main.py` startup: reads `live_enabled` from DB, only calls `manager.start_bot()` for bots with `live_enabled=True`
- Manual `Start`/`Stop` buttons still work for session-only control (don't persist `live_enabled`)

---

## Multi-Timeframe Candle Support

Historical candle storage and all backtest operations are **interval-aware**:

| Interval | Minutes | Candles/day |
|---|---|---|
| `1m` | 1 | 1440 |
| `5m` | 5 | 288 |
| `15m` | 15 | 96 |
| `1h` | 60 | 24 |

Key design decisions:
- `historical_candles` table PK is `(symbol, interval, open_time)` — each timeframe is a fully independent dataset
- All candle functions in `repository.py` accept `interval: str = "15m"` — datasets are never mixed
- `data/historical.py` exposes `SUPPORTED_INTERVALS` dict and computes step/page size dynamically per interval
- Max download cap: **5 years (1825 days)**
- `interval` flows through: API request body → `run_backtest()` → `get_historical_candles()` → optimizer folds
- UI persists the selected interval in `localStorage`; `setActiveInterval()` syncs button states + download button tooltips

---

## Core Components

### `BaseStrategy` — `core/base_strategy.py`

Abstract base for all bots. Provides:
- `PARAM_SCHEMA` — declares tunable parameters with type, default, min, max
- `get_params()` / `set_params()` — runtime parameter editing (persisted to DB)
- `for_symbol(symbol)` — factory classmethod, creates a named subclass per coin
- `_open_position(price, side)` / `_close_position(price, side, reason)` — shared order helpers
- `_candle_count` / `_last_trade_candle` — shared candle counter and cooldown state
- `on_candle(candle)` — abstract; each strategy implements its signal logic here

**Layer rule:** Strategies must NOT import from `db` or `api`. They only know about `self.engine`.

---

### `SimulationEngine` — `core/simulation_engine.py`

Implements `BaseOrderEngine` (the fake exchange). Responsibilities:
- `place_order(bot_id, symbol, side, quantity, price)` — routes BUY/SELL to open/close long/short; fetches fresh OB depth via `ob_fetcher` for VWAP fill price (live mode)
- `update_price(symbol, price)` — updates tick price, triggers liquidation checks
- `update_orderbook(symbol, snapshot)` — stores in-memory OB snapshot for `_compute_fill_price`
- `get_orderbook_snapshot(symbol)` — returns latest in-memory OB snapshot (live mode only; None in backtest)
- `get_portfolio(bot_id)` / `reset_portfolio(bot_id)` — public portfolio access
- `save_snapshot(bot_id)` — persists portfolio state to DB
- `skip_db: bool = False` constructor flag — set `True` in backtest to avoid DB writes
- `ob_fetcher: OBFetcher | None` — when provided, called on every `place_order()` for fresh depth

**OB-aware fill price:** In live mode (`ob_fetcher=fetch_depth`), each order walks Binance bid/ask levels for true VWAP impact. In backtest mode (`ob_fetcher=None`), a fixed `base_slippage_pct` fallback is used.

---

### `VirtualPortfolio` — `core/virtual_portfolio.py`

Tracks per-bot futures state:
- `FuturesPosition` dataclass: side (LONG/SHORT/NONE), qty, entry price, leverage, margin, liquidation price
- `open_long / open_short / close_long / close_short` — position transitions
- `check_liquidation(price)` — called on every price tick; forced close + margin loss on trigger
- `deduct_fee(fee_usdt)` — guarded to prevent negative balance
- `get_state(current_price)` — full snapshot dict for API/DB

---

### `BotManager` — `core/bot_manager.py`

Orchestrates all bots:
- `register(bot_class)` — instantiates bot, loads saved params, creates portfolio
- `start_bot / stop_bot / start_all / stop_all` — asyncio.Task lifecycle
- `dispatch_price(symbol, price)` — calls `engine.update_price` + per-bot `on_price_update`
- `dispatch_candle(candle)` — puts candle into each matching bot's queue
- `_candle_loop(bot)` — reads from bot's candle queue, calls `bot.on_candle()`
- `_snapshot_loop(bot_id)` — saves portfolio snapshots every N seconds with random jitter (prevents DB lock bursts)
- `_restore_balance_from_snapshot` — on startup, restores bot state from last DB snapshot

---

### `CandleAggregator` — `data/candle_aggregator.py`

Converts raw price ticks → OHLCV candles:
- `on_tick(symbol, price)` — updates in-progress candle; emits completed candle at interval boundary
- `flush()` — emits any partial in-progress candle (called on shutdown)
- `subscribe / unsubscribe` — pub/sub for candle callbacks
- `interval_seconds` constructor param controls candle duration

---

### `historical.py` — `data/historical.py`

Downloads and stores kline history from Binance:
- `SUPPORTED_INTERVALS` dict: `{'1m': {minutes:1, candles_per_day:1440}, ...}` — single source of truth
- `download_klines(symbol, days, interval, start_date, progress_callback)` — streams klines page-by-page, saves to DB via `repo.save_historical_candles(rows, interval=interval)`; step size computed dynamically per interval
- `get_data_status(symbols, interval)` — returns count, date range, and `start_ms`/`end_ms` (epoch milliseconds) of stored candles per symbol for the given interval; `start_ms`/`end_ms` are `None` when no data exists

---

### `fetch_depth` — `data/orderbook_feed.py`

On-demand Binance Futures REST call for live VWAP fill pricing:
- `fetch_depth(symbol) -> dict | None` — returns `{"bids": [(price, qty), ...], "asks": [...]}` or None on error
- Passed as `ob_fetcher=fetch_depth` to `SimulationEngine` in `main.py`
- Called on every `place_order()` in live mode; falls back to fixed slippage on timeout/error
- No background polling — purely on-demand at order time

---

### Backtest & Optimizer — `core/backtest_engine.py`, `core/optimizer.py`

- `run_backtest(bot_id, symbol, strategy_class, params, start_ms, end_ms, interval="15m")` — replays DB historical candles through a fresh `SimulationEngine(skip_db=True)` instance; fetches warmup candles before `start_ms` for indicator seeding; passes `interval` to `get_historical_candles()`; Sharpe annualized using interval-appropriate candles/year; each equity curve point includes `trend` field (`"bull"/"bear"/"warmup"`)
- `optimize_params(..., interval="15m", _candle_override=None, _warmup_override=None)` — genetic algorithm (tournament select, BLX-α crossover, adaptive mutation, elitism); `_candle_override` replaces the DB candle fetch (used for date-range filtering); `_warmup_override` replaces the warmup slice (pre-window candles for indicator seeding); passes `interval` to all `run_backtest()` calls; default fitness = `Sharpe×0.40 + ProfitFactor×0.30 + Return×0.20 - Drawdown×0.20 + sqrt(trades)×0.10`; hard gate: `if trade_count < 120: return -1000 + trade_count`; strategies can override `compute_fitness()` classmethod for custom objectives (e.g. `DonchianNewBot` uses `Return×0.40 - Drawdown×0.35 + PF×0.20 + sqrt(trades)×0.15`, hard gate `< 20 trades`)
- `walk_forward_optimize(..., interval="15m", _candle_override=None, _warmup_override=None)` — expanding-window WFO: divides candles into N folds, GA-optimizes each IS window, evaluates on OOS window; `_candle_override` narrows the dataset to the user-selected date range; `_warmup_override` passed into each fold's `optimize_params()` call; `interval` propagated to all nested calls; produces `WalkForwardResult` with per-fold metrics, WFE score, stitched OOS equity curve
- **Walk-Forward Efficiency (WFE)** = OOS return / IS return: >0.6 good generalisation, 0.3–0.6 moderate, <0.3 overfit

---

## Dependency Injection Pattern

All API routes access shared singletons via `app.state`:

```python
# main.py (lifespan startup)
app.state.bot_manager = bot_manager
app.state.engine = simulation_engine
app.state.symbols = SYMBOLS

# api/routes/bots.py
def _get_manager(request: Request):
    return getattr(request.app.state, "bot_manager", None)
```

No module-level globals with `set_xxx()` injection.

---

## Database Schema

**SQLite** via `aiosqlite`, WAL mode. All timestamps stored as `ISO 8601 UTC` with `+00:00` suffix.
Additive migrations run on every startup (`PRAGMA table_info` check + `ALTER TABLE`).

| Table | Key columns | Purpose |
|---|---|---|
| `bots` | `id TEXT PK, symbol, status, initial_balance, live_enabled INTEGER DEFAULT 0` | Bot registry + live state |
| `trades` | `bot_id FK, side, symbol, quantity, price, realized_pnl, fee_usdt, position_side, timestamp` | Trade history |
| `portfolio_snapshots` | `bot_id FK, usdt_balance, asset_balance, total_value_usdt, asset_price, timestamp` | Historical equity curve |
| `bot_params` | `bot_id PK, params_json, updated_at` | Persisted parameter overrides |
| `historical_candles` | `(symbol, interval, open_time) PK, open/high/low/close/volume, close_time` | Klines per interval; indexed by `(symbol, interval)` |
| `platform_settings` | `key TEXT PK, value TEXT` | Key/value store for platform-wide settings |

---

## API Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/bots` | List all bots with status, stats, and `live_enabled` |
| `GET` | `/api/bots/{name}` | Single bot detail |
| `POST` | `/api/bots/{name}/start` | Start a bot (session only, doesn't persist `live_enabled`) |
| `POST` | `/api/bots/{name}/stop` | Stop a bot |
| `PATCH` | `/api/bots/{name}/live` | Set `live_enabled` flag (persisted); starts/stops bot immediately |
| `POST` | `/api/bots/{name}/reset` | Reset portfolio to initial balance |
| `POST` | `/api/bots/reset-all` | Reset all bots |
| `GET` | `/api/bots/{name}/params` | Get param schema + current values |
| `PUT` | `/api/bots/{name}/params` | Update params (validated, persisted) |
| `GET` | `/api/portfolio/all` | All bot portfolio states |
| `GET` | `/api/portfolio/{name}` | Single bot futures portfolio state |
| `GET` | `/api/portfolio/{name}/history` | Portfolio snapshot history (for charting) |
| `GET` | `/api/trades/{bot_name}` | Paginated trade history for a bot |
| `GET` | `/api/trades/{bot_name}/stats` | Aggregated trade stats for a time window |
| `GET` | `/api/backtest/candle-config` | Returns `SUPPORTED_INTERVALS` dict |
| `POST` | `/api/backtest/download` | Download klines from Binance (`days` up to 1825, `interval`) |
| `GET` | `/api/backtest/data-status?interval=15m` | Available candle counts per symbol for given interval |
| `POST` | `/api/backtest/run` | Run a backtest (`interval`, optional `start_date`/`end_date`) |
| `POST` | `/api/backtest/optimize` | Start genetic optimization (async, background task; `interval`, optional `start_date`/`end_date`) |
| `POST` | `/api/backtest/walk-forward` | Start Walk-Forward Optimization (async, background task; `interval`, optional `start_date`/`end_date`) |
| `GET` | `/api/backtest/status` | Poll status of all or specific backtest/optimization tasks (TTL=1h) |
| `GET` | `/api/logs` | Recent WARNING+ log lines |
| `GET` | `/health` | Liveness check + mode/market info |
| `GET` | `/` | Dashboard HTML |

---

## UI Architecture

The frontend is a single-page app (plain JS, no framework). Key state globals in `utils.js`:

| Variable | Purpose |
|---|---|
| `_activeInterval` | Currently selected timeframe (`localStorage`-persisted, default `'15m'`) |
| `_SUPPORTED_INTERVALS` | `['1m','5m','15m','1h']` |
| `_INTERVAL_CANDLES_PER_DAY` | `{1m:1440, 5m:288, 15m:96, 1h:24}` |
| `selectedBot` | Currently viewed bot name |
| `_statsMode` | Period for stats bar: `'all'/'24h'/'3h'` |

Key UI functions:
- `setActiveInterval(iv)` — sets `_activeInterval`, syncs `.iv-btn.active` states, updates download button tooltips with `candleCountLabel()`, calls `loadDataStatus()`
- `candleCountLabel(days, interval)` — returns `"365d × 15m = 35k candles"` for button tooltips
- `toggleBotLive(name, enabled)` — calls `PATCH /api/bots/{name}/live`, updates label/toggle immediately

---

## Configuration — `config.py`

Key settings (all from `.env` or environment variables):

| Setting | Default | Purpose |
|---|---|---|
| `trading_mode` | `simulation` | `simulation` only (live = future) |
| `leverage` | `3` | Futures leverage multiplier |
| `simulation_fee_rate` | `0.0005` | 0.05% taker fee per order |
| `initial_usdt_balance` | `10000` | Starting USDT per bot |
| `snapshot_interval_seconds` | `60` | Portfolio snapshot frequency |
| `base_slippage_pct` | `0.02` | Fallback slippage when OB fetch fails |
| `max_slippage_pct` | `0.10` | Order rejection threshold (VWAP deviation) |
| `db_path` | `trade_platform.db` | SQLite file path |

---

## Async Architecture

Everything runs in a single `asyncio` event loop managed by `uvicorn`:

```
asyncio event loop
  ├── uvicorn (FastAPI HTTP)
  ├── binance-feed task (WebSocket)
  └── per-bot tasks (only live_enabled bots):
        ├── candle_loop
        └── snapshot_loop (jittered start)
```

App startup/shutdown is managed by FastAPI `lifespan` context manager in `main.py`.
On startup, `live_enabled` is read from DB per bot — only bots with `live_enabled=True` are started automatically.

---

## Migration Path: Simulation → Live

```python
class BaseOrderEngine(ABC):
    async def place_order(self, bot_id, symbol, side, quantity, price) -> dict: ...
    async def get_balance(self, bot_id, asset) -> float: ...
    async def get_portfolio_state(self, bot_id) -> dict: ...
    async def get_orderbook_snapshot(self, symbol) -> dict | None: ...
```

| Component | Simulation | Live (future) |
|---|---|---|
| Order execution | `SimulationEngine` | `LiveBinanceEngine` |
| Balance | `VirtualPortfolio` | Binance account via REST |
| OB fill pricing | `fetch_depth` (already live) | Same |
| Strategies | Unchanged | Unchanged |
| Data feed | Unchanged | Unchanged |
| DB | Unchanged | Unchanged |

To go live: implement `LiveBinanceEngine(BaseOrderEngine)` and swap it in `main.py`. Strategies, BotManager, and all API routes never change.
