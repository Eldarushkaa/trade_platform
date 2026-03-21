# Trade Platform — Frontend Reference (`api/static/`)

> Last updated: 2026-03-21
> Stack: plain HTML + vanilla JS + Chart.js 4.4 (CDN), served as static files by FastAPI.  
> No build tool, no bundler, no ES modules — all files are plain `<script>` globals.

---

## 1. File Structure

```
api/static/
├── index.html          ← single-page shell + <script> tags
├── style.css           ← all CSS (dark theme, components)
└── app/
    ├── utils.js        ← shared state, formatters, fetch helpers
    ├── bots.js         ← sidebar bot list, global stats bar, bot control
    ├── portfolio.js    ← portfolio panel, stats grid, chart, trades table
    ├── settings.js     ← strategy parameter editor, toast
    ├── backtest.js     ← standalone backtest/optimize/WFO panel (independent of sidebar)
    ├── logs.js         ← log viewer panel
    └── main.js         ← refresh loop, Moscow clock, bot reset, bootstrap
```

Scripts are loaded in **dependency order** at the bottom of `index.html`:

```html
<script src="/static/app/utils.js?v=19"></script>    <!-- must be first -->
<script src="/static/app/bots.js?v=19"></script>
<script src="/static/app/portfolio.js?v=19"></script>
<script src="/static/app/settings.js?v=19"></script>
<script src="/static/app/backtest.js?v=19"></script>
<script src="/static/app/logs.js?v=19"></script>
<script src="/static/app/main.js?v=19"></script>     <!-- must be last (runs bootstrap) -->
```

Bump the `?v=N` cache-buster whenever you change any module.

---

## 2. Global State Inventory

All shared mutable state lives in `utils.js` as `let` declarations at the top level.  
Any module can read and write them freely (there is no module encapsulation).

| Variable | Type | Owner (primary writer) | Consumers |
|---|---|---|---|
| `API` | `string` const `'/api'` | utils | all |
| `selectedBot` | `string\|null` | bots (`selectBot`) | bots, portfolio, settings, main |
| `portfolioChart` | `Chart\|null` | portfolio (`_renderChart`) | portfolio |
| `_paramsCache` | `object` | settings (`loadParams`) | settings |
| `_settingsOpen` | `bool` | settings (`toggleSettings`) | settings |
| `_backtestOpen` | `bool` | backtest (`toggleBacktest`) | backtest |
| `_backtestChart` | `Chart\|null` | backtest (`renderBacktestChart`) | backtest |
| `_lastOptResult` | `object\|null` | backtest (`renderOptResults`) | backtest |
| `_lastWFOResult` | `object\|null` | backtest (`renderWFOResults`) | backtest |
| `_botsCache` | `array` | bots (`loadBots`) | backtest (`populateBtBotSelect`) |
| `_statsMode` | `'all'\|'24h'\|'3h'` | bots (`toggleStatsMode`) | bots, portfolio |
| `_portfolioData` | `{p, stats24h, stats3h}\|null` | portfolio (`loadPortfolio`) | bots (re-render on toggle) |
| `_historyData` | `{snaps, trades}\|null` | portfolio (`loadHistory`) | bots (re-render on toggle) |
| `_globalStatsData` | `object\|null` | bots (`loadBots`) | bots (period toggle re-render) |
| `_lastTestWindow` | `{start, end}\|null` | backtest (`downloadTestData`) | backtest (`fillTestDates`) |
| `_dataOldestMs` | `number\|null` | backtest (`loadDataStatus`) | backtest (`_renderYearShortcuts`) |
| `_dataNewestMs` | `number\|null` | backtest (`loadDataStatus`) | backtest (`_renderYearShortcuts`) |
| `_logPanelOpen` | `bool` | logs (`toggleLogPanel`) | logs |
| `_logErrorCount` | `number` | logs | (reserved) |
| `_refreshInterval` | `number\|null` | main | main |
| `_refreshSeconds` | `number` | main (`changeRefreshRate`) | main |

---

## 3. Module Responsibilities

### `utils.js`
Pure helpers — no DOM manipulation, no API calls.

- **Timezone**: `_moscowTZ`, `_toUtcDate(isoStr)`, `fmtMoscow(isoStr)`, `fmtMoscowTime(d)`, `fmtTime(d)`
- **Fetch wrappers**: `get(url)`, `post(url)`, `put(url, body)`, `postJson(url, body)`
- **Value formatters**: `fmt(n)` (money, 2 decimals), `escapeHtml(str)`

### `bots.js`
Owns the left sidebar and the global stats bar.

**Data flow:**
```
loadBots()
  → GET /api/bots + /api/portfolio/all
  → GET /api/trades/{bot}/stats?hours=24|3   (for each bot, in parallel)
  → writes _botsCache, _globalStatsData
  → calls renderGlobalStats() + builds DOM bot cards
  → calls populateBtBotSelect(bots)          ← populates backtest panel dropdown
```

**Key functions:**
- `loadBots()` — full sidebar refresh; also calls `populateBtBotSelect(bots)`
- `renderGlobalStats(portfolios, bots, periodStats)` — renders the top stats bar including the strategy×coin return matrix
- `controlBot(name, action)` — POST start/stop, then refresh
- `selectBot(name)` — sets `selectedBot`, shows detail panel, calls `loadBotDetail()`  
  ⚠️ Does **NOT** reset backtest panel — backtest is now independent of sidebar selection
- `loadBotDetail(name)` — fan-out to `loadPortfolio + loadHistory + loadTrades + loadParams` in parallel
- `toggleStatsMode(mode)` — switches `_statsMode`, re-renders global stats bar + bot detail stats + chart

### `portfolio.js`
Owns the main panel's portfolio area (position row, stats grid, chart, trades).

**Data flow:**
```
loadPortfolio(name)
  → GET /api/portfolio/{name} + /api/trades/{name}/stats?hours=24|3
  → writes _portfolioData
  → calls _renderPortfolio()

loadHistory(name)
  → GET /api/portfolio/{name}/history?limit=1000 + /api/trades/{name}?limit=1000
  → writes _historyData
  → calls _renderChart(snaps, trades, windowMs)
```

**Chart details (`_renderChart`):**
- **Library**: Chart.js 4.4 (line chart, multi-axis)
- **Axes**: `yPortfolio` (left, USDT) and `yPrice` (right, coin price)
- **Datasets**: Margin/unrealized fill, USDT balance fill, Total value line, Coin price line, Long/Short triangle markers
- **Gap detection**: computes median candle delta from first 10 snapshots; inserts `null` gap points when delta > `max(3×median, 120s)`
- **Time window**: `windowMs` = null (all data) | 24h | 3h; filtering uses `_tsMs()` for UTC-safe parsing

**Trade markers:**
- Triangles placed at nearest snapshot timestamp within ±2h
- LONG → green up-triangle on `yPrice` axis; SHORT → red down-triangle

### `settings.js`
Manages the collapsible "Strategy Parameters" panel inside bot detail.

- `loadParams(name)` — GET `/api/bots/{name}/params`; builds input grid from `_paramsCache`
- `saveParams()` — sends only changed keys to PUT `/api/bots/{name}/params`
- `resetParams()` — sends all keys at their `default` values
- `showToast(msg, type)` / `hideToast()` — 4-second auto-hide inline notification

### `backtest.js`
**Standalone panel** (`#bt-standalone-panel`) — fully independent of `selectedBot`.  
Has its own bot dropdown, persists results across page reloads and browser tabs.

**Bot selector:**
```
<select id="bt-bot-select">    ← populated by populateBtBotSelect() called from loadBots()
getBtBot()                     ← all backtest functions use this instead of selectedBot
onBtBotChange()                ← saves selection to localStorage.bt_bot_id
```

**Task persistence (survive page reload / new tab / different device):**
- `_saveBtTask(type, taskId, botId)` — writes to `localStorage` (`bt_task_wfo`, `bt_task_opt`)
- `_clearBtTask(type)` / `_loadBtTask(type)` — read/clear with 12h TTL
- `initBtPanel()` — called once on startup **after** bots list is loaded:
  1. **Primary**: `GET /api/backtest/status` (no params) → server returns all tasks → pick most relevant (WFO > opt, running > completed) → restore results or resume polling. Works on any device/browser.
  2. **Fallback**: if server query fails, try `localStorage` task_id hints
  3. Opens panel by default so results are immediately visible

**Training data download (sidebar):**
```
[1y] [2y] [3y] buttons  →  downloadHistory(365|730|1095)
                            → POST /api/backtest/download  {days, start_date?}
```

**Test window download (sidebar):**
```
[📥 14d Test from: <date>]  →  downloadTestData()
                                → computes endDate = startDate + 14d
                                → writes _lastTestWindow
                                → calls downloadHistory(14, startDate)
```

**Backtest date filters (inside backtest panel):**
```
[From date] [To date] [✕ Clear] [Use test window]
  → clearBtDates()    — clears both inputs
  → fillTestDates()   — pre-fills from _lastTestWindow
```

**Optimizer / WFO date range (inside backtest panel, above WFO settings):**
```
[Opt / WFO range:] [opt-start-date] [opt-end-date] [✕ Clear] [opt-year-shortcuts]
  → clearOptDates()   — clears both opt-start-date and opt-end-date
  → _renderYearShortcuts(oldestMs, newestMs)
       — called by loadDataStatus() after tracking _dataOldestMs / _dataNewestMs
       — renders TWO independent rows inside #opt-year-shortcuts:
         * "From" row (green): Yr 1 … Yr N  → each sets only opt-start-date
         * "To"   row (orange): Yr 1 … Yr N  → each sets only opt-end-date
       — year count = Math.ceil(totalDays / 365)  (ceil so partial last year shown)
       — yearStart(y) = oldestMs + (y-1) × 365d
       — yearEnd(y)   = min(oldestMs + y × 365d, newestMs)
       — allows composing arbitrary multi-year ranges, e.g. "From Yr 1 / To Yr 4"
```

**Run backtest flow:**
```
runBacktest()
  → reads getBtBot() for bot_id
  → reads bt-start-date / bt-end-date
  → POST /api/backtest/run  {bot_id, fee_rate, start_date?, end_date?}
  → calls renderBacktestResults(r) → renderBacktestChart(r.equity_curve)
```

**Optimization flow:**
```
runOptimize()
  → reads opt-start-date / opt-end-date (optional)
  → POST /api/backtest/optimize  {bot_id, ..., start_date?, end_date?} → {task_id}
  → _saveBtTask('opt', taskId, botId)
  → polls GET /api/backtest/status?task_id=… every 2s
  → on completed → renderOptResults(r)
    → shows param comparison table + GA stats
    → [✅ Apply] → applyOptParams() → PUT /api/bots/{id}/params
    → [▶ Backtest with Optimized] → runBacktestWithOpt()
  → _clearBtTask('opt')
```

**Walk-Forward Optimization (WFO) flow:**
```
runWalkForward()
  → reads opt-start-date / opt-end-date (optional)
  → POST /api/backtest/walk-forward  {bot_id, n_folds, test_pct, iterations, fee_rate, start_date?, end_date?}
  → _saveBtTask('wfo', taskId, botId)
  → polls GET /api/backtest/status?task_id=… every 3s
  → on completed → renderWFOResults(r)
    → summary metrics (Avg WFE, Avg OOS Return, etc.)
    → fold-by-fold table (IS/OOS return, WFE per fold)
    → stitched OOS equity curve chart (renderWFOChart)
    → final params table
    → [✅ Apply Final Params] → applyWFOParams()
    → [▶ Backtest with Final Params] → runBacktestWithWFO()
  → _clearBtTask('wfo')
```

**Backtest equity chart** (`renderBacktestChart`):
- `yEq` axis: USDT balance (fill) + Total value line (colored by position side: green=LONG, red=SHORT, gray=NONE)
- `yPr` axis: Coin price line — colored by EMA trend: green=bull (EMA50>EMA200), red=bear, grey=warmup
- Tooltip shows position side tag + trend label (📈 Bull / 📉 Bear / ⏳ Warmup)

**WFO chart** (`renderWFOChart`): stitched out-of-sample equity curve, `canvas#bt-wfo-chart`, rendered inside `#bt-wfo-results` innerHTML

### `logs.js`
System log viewer.

- `toggleLogPanel()` — collapse/expand, loads logs on open
- `loadLogs()` — GET `/api/logs?level=WARNING|ERROR&limit=200`; updates badge count even when collapsed
- `clearLogs()` — DELETE `/api/logs`

### `main.js`
Bootstrap and refresh orchestration. Runs last.

- `refresh()` — fan-out: `loadBots()` + `loadBotDetail()` (if bot selected) + `loadLogs()` (badge only)
- `changeRefreshRate(seconds)` — resets `_refreshInterval`
- `resetAllBots()` — confirm dialog → POST `/api/bots/reset-all`
- `resetBot(name)` — confirm dialog → POST `/api/bots/{name}/reset`
- `_tickMskClock()` — runs every 1s, updates `#msk-clock`
- **Bootstrap**: `refresh().then(() => initBtPanel())` — bots loaded first so bt-bot-select is populated before initBtPanel runs

---

## 4. API Calls Summary

| Module | Method | Endpoint | Purpose |
|---|---|---|---|
| bots | GET | `/api/bots` | Bot list with status |
| bots | POST | `/api/bots/{name}/start\|stop` | Start/stop a bot |
| bots | GET | `/api/portfolio/all` | All portfolio snapshots |
| bots | GET | `/api/trades/{name}/stats?hours=24\|3` | Period trade stats |
| bots | GET | `/health` | Mode + market badge |
| portfolio | GET | `/api/portfolio/{name}` | Single bot portfolio |
| portfolio | GET | `/api/portfolio/{name}/history?limit=1000` | Portfolio snapshots |
| portfolio | GET | `/api/trades/{name}?limit=50\|1000` | Trade list |
| settings | GET | `/api/bots/{name}/params` | Strategy parameter schema |
| settings | PUT | `/api/bots/{name}/params` | Save parameter overrides |
| backtest | GET | `/api/backtest/data-status` | Historical candle counts |
| backtest | POST | `/api/backtest/download` | Download Binance klines |
| backtest | POST | `/api/backtest/run` | Run backtest |
| backtest | POST | `/api/backtest/optimize` | Start GA optimization (async) |
| backtest | POST | `/api/backtest/walk-forward` | Start Walk-Forward Optimization (async) |
| backtest | GET | `/api/backtest/status` | All tasks summary (for restore on load) |
| backtest | GET | `/api/backtest/status?task_id=…` | Poll single task progress/result |
| logs | GET | `/api/logs?level=WARNING&limit=200` | System log records |
| logs | DELETE | `/api/logs` | Clear log buffer |
| main | POST | `/api/bots/reset-all` | Reset all bot data |
| main | POST | `/api/bots/{name}/reset` | Reset single bot |

---

## 5. DOM Element ID Reference

Key IDs that JS writes to or reads from:

| ID | Module | Purpose |
|---|---|---|
| `bots-list` | bots | Bot card container |
| `global-stats-bar` | bots | Top stats bar inner HTML |
| `mode-badge`, `market-badge` | bots | Live/sim + futures badge text |
| `ptbtn-all/24h/3h` | bots | Period toggle button active state |
| `bot-detail`, `no-bot` | bots | Main panel visibility |
| `detail-title` | bots | Selected bot name heading |
| `position-row` | portfolio | Position info grid |
| `stats-grid` | portfolio | Stats card grid |
| `portfolio-chart` | portfolio | Chart.js canvas |
| `price-legend`, `price-legend-label`, `long-legend`, `short-legend` | portfolio | Chart legend visibility |
| `trades-body` | portfolio | Trade rows `<tbody>` |
| `settings-wrap`, `settings-body`, `settings-chevron` | settings | Collapsible settings panel |
| `settings-grid` | settings | Parameter input grid |
| `settings-strategy-label` | settings | Strategy name label |
| `btn-save-params` | settings | Save button disabled state |
| `settings-toast` | settings | Toast notification span |
| `bt-standalone-panel` | backtest | Standalone backtest panel (full-width, below layout) |
| `bt-bot-select` | backtest | Bot selector dropdown (independent of sidebar) |
| `backtest-body`, `bt-chevron` | backtest | Collapsible backtest panel body |
| `bt-data-info`, `bt-data-badge` | backtest | Candle count status text |
| `bt-test-start-date` | backtest | Test window start date picker (in sidebar) |
| `bt-start-date`, `bt-end-date` | backtest | Backtest date range pickers |
| `opt-start-date`, `opt-end-date` | backtest | Optimizer / WFO date range pickers (separate from backtest range) |
| `opt-year-shortcuts` | backtest | Container for year shortcut buttons (From row + To row, rendered by `_renderYearShortcuts`) |
| `bt-run-btn`, `bt-opt-btn`, `bt-wfo-btn` | backtest | Action buttons (disabled if no bot/data) |
| `bt-opt-iters` | backtest | Iteration count select (shared by Optimize + WFO) |
| `bt-wfo-folds`, `bt-wfo-testpct` | backtest | WFO fold count and OOS% selectors |
| `bt-fee-pct` | backtest | Fee rate % input |
| `bt-status` | backtest | Status text (running/done/error) |
| `bt-results`, `bt-metrics`, `bt-chart` | backtest | Backtest result section + metrics grid + canvas |
| `bt-opt-results` | backtest | Optimization result section (innerHTML replaced) |
| `bt-wfo-results` | backtest | WFO result section (innerHTML replaced; contains `bt-wfo-chart` canvas) |
| `log-panel`, `log-panel-body`, `log-chevron` | logs | Collapsible log panel |
| `log-level-select` | logs | WARNING/ERROR filter select |
| `log-count-badge` | logs | Warning count badge (hidden when 0) |
| `log-entries` | logs | Log entry container |
| `msk-clock` | main | Moscow time clock |
| `refresh-select` | main | Refresh interval selector |

---

## 6. CSS Conventions (`style.css`)

The stylesheet is a single flat file. Key patterns:

| Pattern | Classes | Notes |
|---|---|---|
| Dark theme tokens | `--bg`, `--card`, `--border`, `--accent`, `--green`, `--red`, `--yellow`, `--orange`, `--text`, `--muted` | CSS custom properties in `:root` |
| Value coloring | `.positive`, `.negative`, `.neutral` | Applied to stat values (green/red/text) |
| Collapsible panels | `.settings-body`, `.backtest-body`, `.log-panel-body` | `max-height: 0` → `.open { max-height: Npx }` transition |
| Chevron rotation | `.chevron`, `.chevron.open` | `transform: rotate(90deg)` |
| Bot cards | `.bot-card`, `.bot-card.active`, `.status-dot.running/.stopped` | Highlight on active/hover |
| Position badges | `.pos-badge.long/.short/.none` | LONG (green), SHORT (red), NONE (muted) |
| Action badges (trades) | `.action-badge.open-long/.close-long/.open-short/.close-short` | Color-coded trade action |
| Stats matrix | `.gs-matrix`, `.gs-cell-green/.gs-cell-red/.gs-cell-neutral` | Strategy×coin return grid |
| Backtest buttons | `.bt-btn-dl` (purple), `.bt-btn-run` (green), `.bt-btn-opt` (yellow), `.bt-btn-apply` (green) | |
| Standalone backtest panel | `.bt-standalone-panel` | Full-width card: `margin: 0 24px 24px`, outside the `.layout` grid |
| Responsive | `@media (max-width: 768px)` | Single column layout |

---

## 7. Backtest Workflow (Training / Test Split)

The UI supports a train/test data separation paradigm:

```
TRAINING DATA (sidebar)
  1y / 2y / 3y buttons → downloadHistory(365/730/1095)
     → POST /backtest/download {days: N}
     → downloads [now-N days, now] from Binance as 5m candles

TEST DATA (sidebar)
  Pick date → [📥 14d Test from:]
     → downloadTestData()
     → POST /backtest/download {days: 14, start_date: "YYYY-MM-DD"}
     → downloads [startDate, startDate+14d]
     → stores _lastTestWindow = {start, end}

BACKTEST ON TRAINING (default)
  Run Backtest (no dates filled) → runs over all stored candles

BACKTEST ON TEST WINDOW
  [Use test window] → fillTestDates() → fills bt-start-date / bt-end-date
  Run Backtest → POST /backtest/run {bot_id, start_date, end_date}
                → backend filters candles by epoch-ms range
```

**Optimizer / WFO date-range filtering:**

```
OPTIMIZER DATE RANGE (inside backtest panel, above WFO settings)
  opt-start-date / opt-end-date pickers  (populated via year shortcuts or manually)
  [✕ Clear] button → clearOptDates()

  If dates are set:
    runOptimize() → POST /backtest/optimize {bot_id, ..., start_date, end_date}
    runWalkForward() → POST /backtest/walk-forward {bot_id, ..., start_date, end_date}

  Backend (/api/routes/backtest.py):
    date string → epoch ms
    _opt_candles = get_historical_candles(symbol, interval, start_ms, end_ms)
    _opt_warmup  = get_historical_candles(symbol, interval, before_ms=start_ms, limit=300)
    optimize_params(..., _candle_override=_opt_candles, _warmup_override=_opt_warmup)

  Warmup candles (300 rows before start_ms) are fetched separately to pre-heat
  EMA200/ATR indicators without including those candles in the optimized window.

YEAR SHORTCUTS
  Populated when loadDataStatus() completes and data exists for selected symbol.
  _renderYearShortcuts(oldestMs, newestMs):
    - numYears = Math.ceil((newestMs - oldestMs) / 86400000 / 365)
    - "From" row (green buttons): sets opt-start-date to start of year Y
    - "To"   row (orange buttons): sets opt-end-date to end of year Y
    - Allows any range like Yr1–Yr4 or Yr3–Yr5 by independent picking
    - "✕ All" button calls clearOptDates()
```

---

## 8. RSI Strategy Parameters (current `PARAM_SCHEMA`)

After refactoring `strategies/example_rsi_bot.py`:

| Parameter | Range | Notes |
|---|---|---|
| `RSI_PERIOD` | 7–21 int | Wilder RSI lookback |
| `OVERSOLD` | 20.0–35.0 float | **Base** long entry (shifted by `vol_factor`) |
| `OVERBOUGHT` | 65.0–80.0 float | **Base** short entry (shifted by `vol_factor`) |
| `MAX_HOLD_CANDLES` | 10–40 int | Time-stop |
| `COOLDOWN_CANDLES` | 0–10 int | Min candles between entries |

**Removed from PARAM_SCHEMA (now fixed/derived):**
- `ATR_MIN_PCT` — hardcoded `0.004`
- `EXIT_RSI_LONG` — derived: `dyn_oversold + 20`
- `EXIT_RSI_SHORT` — derived: `dyn_overbought - 20`

**Dynamic RSI thresholds via `vol_factor`:**
```
EMA_ATR = EMA(ATR, 50)            # smoothed baseline
vol_factor = ATR / EMA_ATR        # >1 = volatile, <1 = quiet
adj = 8.0 * (vol_factor - 1)
dyn_oversold   = OVERSOLD   - adj  # lower entry threshold when volatile
dyn_overbought = OVERBOUGHT + adj  # higher entry threshold when volatile
exit_long      = dyn_oversold  + 20
exit_short     = dyn_overbought - 20
```

**Fixed indicator periods (not optimizable):**
- `EMA_FAST_PERIOD = 50` (trend direction)
- `EMA_SLOW_PERIOD = 200` (macro trend + warmup guard)
- `ATR_PERIOD = 14`
- `EMA_ATR_PERIOD = 50`
- Warmup: no trades until EMA200 + ATR both initialized (~200 candles)

---

## 9. Adding a New UI Panel

1. Create `api/static/app/mypanel.js`
2. Add the `<script>` tag in `index.html` before `main.js`
3. If the panel needs new shared state, add `let _myState = ...` in `utils.js`
4. If the panel participates in the refresh loop, call your load function inside `refresh()` in `main.js`
5. Bump `?v=N` on all script tags

---

## 10. Adding a New API Endpoint to the UI

1. Identify which module owns the relevant UI section
2. Use `get()`, `post()`, `postJson()`, or `put()` from `utils.js`
3. Follow the existing error-handling pattern:
   ```js
   try {
     const resp = await postJson(`${API}/your/endpoint`, body);
     if (resp.ok) { /* success */ } else { /* resp.data.detail */ }
   } catch (e) { /* network error */ }
   ```
4. If the endpoint triggers a full page refresh, call `refresh()` afterwards

---

## 11. Chart.js Usage Summary

Three separate Chart.js instances are maintained:

| Variable | Canvas ID | Module | Datasets |
|---|---|---|---|
| `portfolioChart` | `portfolio-chart` | portfolio.js | USDT fill, coin value fill, total value line, coin price line, long/short markers |
| `_backtestChart` | `bt-chart` | backtest.js | USDT balance fill, total value line (colored by side), coin price line (colored by EMA trend) |
| `window._wfoChart` | `bt-wfo-chart` | backtest.js | Stitched OOS equity curve (WFO results) |

All charts are **destroyed and recreated** on every render call (no incremental update). This is intentional — data shape changes between renders.

The `dashedSegment` helper in `portfolio.js` renders gap spans as dashed lines using Chart.js `segment` callbacks.

**Backtest price line coloring** (Chart.js `segment` callback):
```js
// Equity curve trend field: "bull" | "bear" | "warmup" | "none"
// Added by backtest_engine.py using bot._ema_fast vs bot._ema_slow
borderColor: ctx => trends[ctx.p0DataIndex] === 'bull'   ? green
                  : trends[ctx.p0DataIndex] === 'bear'   ? red
                  : trends[ctx.p0DataIndex] === 'warmup' ? grey
                  : orange
```
