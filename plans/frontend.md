# Trade Platform — Frontend Reference (`api/static/`)

> Last updated: 2026-03  
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
    ├── backtest.js     ← data download, date pickers, run/optimize/results
    ├── logs.js         ← log viewer panel
    └── main.js         ← refresh loop, Moscow clock, bot reset, bootstrap
```

Scripts are loaded in **dependency order** at the bottom of `index.html`:

```html
<script src="/static/app/utils.js?v=18"></script>    <!-- must be first -->
<script src="/static/app/bots.js?v=18"></script>
<script src="/static/app/portfolio.js?v=18"></script>
<script src="/static/app/settings.js?v=18"></script>
<script src="/static/app/backtest.js?v=18"></script>
<script src="/static/app/logs.js?v=18"></script>
<script src="/static/app/main.js?v=18"></script>     <!-- must be last (runs bootstrap) -->
```

Bump the `?v=N` cache-buster whenever you change any module.

---

## 2. Global State Inventory

All shared mutable state lives in `utils.js` as `let` declarations at the top level.  
Any module can read and write them freely (there is no module encapsulation).

| Variable | Type | Owner (primary writer) | Consumers |
|---|---|---|---|
| `API` | `string` const `'/api'` | utils | all |
| `selectedBot` | `string\|null` | bots (`selectBot`) | bots, portfolio, settings, backtest, main |
| `portfolioChart` | `Chart\|null` | portfolio (`_renderChart`) | portfolio |
| `_paramsCache` | `object` | settings (`loadParams`) | settings |
| `_settingsOpen` | `bool` | settings (`toggleSettings`) | settings |
| `_backtestOpen` | `bool` | backtest (`toggleBacktest`) | backtest |
| `_backtestChart` | `Chart\|null` | backtest (`renderBacktestChart`) | backtest |
| `_lastOptResult` | `object\|null` | backtest (`renderOptResults`) | backtest |
| `_botsCache` | `array` | bots (`loadBots`) | backtest (`loadDataStatus`) |
| `_statsMode` | `'all'\|'24h'\|'3h'` | bots (`toggleStatsMode`) | bots, portfolio |
| `_portfolioData` | `{p, stats24h, stats3h}\|null` | portfolio (`loadPortfolio`) | bots (re-render on toggle) |
| `_historyData` | `{snaps, trades}\|null` | portfolio (`loadHistory`) | bots (re-render on toggle) |
| `_globalStatsData` | `object\|null` | bots (`loadBots`) | bots (period toggle re-render) |
| `_lastTestWindow` | `{start, end}\|null` | backtest (`downloadTestData`) | backtest (`fillTestDates`) |
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
```

**Key functions:**
- `loadBots()` — full sidebar refresh
- `renderGlobalStats(portfolios, bots, periodStats)` — renders the top stats bar including the strategy×coin return matrix
- `controlBot(name, action)` — POST start/stop, then refresh
- `selectBot(name)` — sets `selectedBot`, shows detail panel, resets backtest UI, calls `loadBotDetail()`
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
Handles all data download, backtesting, and genetic-algorithm optimization.

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

**Run flow:**
```
runBacktest()
  → reads bt-start-date / bt-end-date
  → POST /api/backtest/run  {bot_id, fee_rate, start_date?, end_date?}
  → calls renderBacktestResults(r) → renderBacktestChart(r.equity_curve)
```

**Optimization flow:**
```
runOptimize()
  → POST /api/backtest/optimize  → {task_id}
  → polls GET /api/backtest/status?task_id=… every 2s
  → on completed → renderOptResults(r)
    → shows param comparison table + GA stats
    → [✅ Apply] → applyOptParams() → PUT /api/bots/{id}/params
    → [▶ Backtest with Optimized] → runBacktestWithOpt()
```

**Backtest equity chart** (`renderBacktestChart`):
- `yEq` axis: USDT balance (fill) + Total value line (colored by position side: green=LONG, red=SHORT, gray=NONE)
- `yPr` axis: Coin price line
- Tooltip shows position side tag on Total Value

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
- **Bootstrap**: calls `refresh()`, `loadDataStatus()`, `loadLogs()`, starts `_refreshInterval`

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
| backtest | GET | `/api/backtest/status?task_id=…` | Poll optimization progress |
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
| `backtest-section`, `backtest-body`, `bt-chevron` | backtest | Collapsible backtest panel |
| `bt-data-info`, `bt-data-badge` | backtest | Candle count status text |
| `bt-test-start-date` | backtest | Test window start date picker |
| `bt-start-date`, `bt-end-date` | backtest | Backtest date range pickers |
| `bt-run-btn`, `bt-opt-btn` | backtest | Action buttons (disabled if no data) |
| `bt-opt-iters` | backtest | Optimization iteration count select |
| `bt-fee-pct` | backtest | Fee rate % input |
| `bt-status` | backtest | Status text (running/done/error) |
| `bt-results`, `bt-metrics`, `bt-chart` | backtest | Result section + metrics grid + canvas |
| `bt-opt-results` | backtest | Optimization result section (innerHTML replaced) |
| `log-panel`, `log-panel-body`, `log-chevron` | logs | Collapsible log panel |
| `log-level-select` | logs | WARNING/ERROR filter select |
| `log-count-badge` | logs | Warning count badge (hidden when 0) |
| `log-entries` | logs | Log entry container |
| `msk-clock` | main | Moscow time clock |
| `refresh-select` | main | Refresh interval selector |

---

## 6. CSS Conventions (`style.css`)

The stylesheet is a single flat file, ~435 lines. Key patterns:

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

---

## 8. Adding a New UI Panel

1. Create `api/static/app/mypanel.js`
2. Add the `<script>` tag in `index.html` before `main.js`
3. If the panel needs new shared state, add `let _myState = ...` in `utils.js`
4. If the panel participates in the refresh loop, call your load function inside `refresh()` in `main.js`
5. Bump `?v=N` on all script tags

---

## 9. Adding a New API Endpoint to the UI

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

## 10. Chart.js Usage Summary

Two separate Chart.js instances are maintained:

| Variable | Canvas ID | Module | Datasets |
|---|---|---|---|
| `portfolioChart` | `portfolio-chart` | portfolio.js | USDT fill, coin value fill, total value line, coin price line, long/short markers |
| `_backtestChart` | `bt-chart` | backtest.js | USDT balance fill, total value line (colored by side), coin price line |

Both charts are **destroyed and recreated** on every render call (no incremental update). This is intentional — data shape changes between renders.

The `dashedSegment` helper in `portfolio.js` renders gap spans as dashed lines using Chart.js `segment` callbacks.
