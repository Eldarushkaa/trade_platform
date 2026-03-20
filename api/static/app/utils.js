// ── Shared state ───────────────────────────────────────────────
const API = '/api';
let selectedBot = null;
let portfolioChart = null;
let _paramsCache = {};         // param_name → {value, default, type, ...}
let _settingsOpen = false;
let _backtestOpen = false;
let _backtestChart = null;
let _lastOptResult = null;     // cached optimization result for "Apply" button
let _lastWFOResult = null;     // cached walk-forward result for "Apply" button
let _botsCache = [];           // last known bots list [{name, symbol, is_running}]
let _statsMode = 'all';        // 'all' | '24h' | '3h' — period for stats grid + chart
let _portfolioData = null;     // cached portfolio state + trade stats for current bot
let _historyData = null;       // cached {snaps, trades} for chart re-render on mode switch
let _globalStatsData = null;   // cached {portfolios, bots, coinData, obStatus, periodStats}
                               // periodStats: { botId: {h24: {...}, h3: {...}} }

let _llmEnabled = false;
let _logPanelOpen = false;
let _logErrorCount = 0;
let _refreshInterval = null;
let _refreshSeconds = 60;

// ── Active candle interval ──────────────────────────────────────
// Persisted in localStorage. All backtest/optimize/download calls use this.
const _SUPPORTED_INTERVALS = ['1m', '5m', '15m', '1h'];
const _INTERVAL_CANDLES_PER_DAY = { '1m': 1440, '5m': 288, '15m': 96, '1h': 24 };
let _activeInterval = localStorage.getItem('activeInterval') || '15m';

function setActiveInterval(iv) {
  if (!_SUPPORTED_INTERVALS.includes(iv)) return;
  _activeInterval = iv;
  localStorage.setItem('activeInterval', iv);
  // Update interval button active states
  _SUPPORTED_INTERVALS.forEach(i => {
    const btn = document.getElementById(`iv-btn-${i}`);
    if (btn) btn.classList.toggle('active', i === iv);
  });
  // Update download button tooltips with dynamic candle counts
  const dlMap = { 'dl-btn-1y': 365, 'dl-btn-3y': 1095, 'dl-btn-5y': 1825 };
  Object.entries(dlMap).forEach(([id, days]) => {
    const btn = document.getElementById(id);
    if (btn) btn.title = `Download ${candleCountLabel(days, iv)} (${days}d)`;
  });
  // Update test download button tooltip too
  const testBtn = document.getElementById('bt-dl-test-btn');
  if (testBtn) testBtn.title = `Download ${candleCountLabel(14, iv)} starting from selected date (held-out test window)`;
  // Refresh data status for new interval
  if (typeof loadDataStatus === 'function') loadDataStatus();
}

function candleCountLabel(days, interval) {
  const perDay = _INTERVAL_CANDLES_PER_DAY[interval] || 96;
  const count = Math.round(days * perDay);
  const countStr = count >= 1000 ? `${(count/1000).toFixed(0)}k` : String(count);
  return `${days}d × ${interval} = ${countStr} candles`;
}

// ── Timezone helpers ────────────────────────────────────────────
const _moscowTZ = 'Europe/Moscow';

// Ensure a timestamp string is parsed as UTC (append Z if no timezone info)
function _toUtcDate(isoStr) {
  if (!isoStr) return new Date(NaN);
  const s = (typeof isoStr === 'string' && !isoStr.endsWith('Z') && !isoStr.includes('+')) ? isoStr + 'Z' : isoStr;
  return new Date(s);
}

function fmtMoscow(isoStr) {
  if (!isoStr) return '—';
  try {
    return _toUtcDate(isoStr).toLocaleString('ru-RU', {
      timeZone: _moscowTZ,
      day: '2-digit', month: '2-digit',
      hour: '2-digit', minute: '2-digit',
    });
  } catch { return isoStr; }
}

function fmtMoscowTime(d) {
  // Format a Date object as HH:MM in Moscow time
  return d.toLocaleTimeString('ru-RU', { timeZone: _moscowTZ, hour: '2-digit', minute: '2-digit' });
}

// Chart axis labels in Moscow time — HH:MM only
function fmtTime(d) {
  return fmtMoscowTime(d);
}

// ── Fetch helpers ───────────────────────────────────────────────
async function get(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json();
}

async function post(url) {
  const r = await fetch(url, { method: 'POST' });
  return r.json();
}

async function put(url, body) {
  const r = await fetch(url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return { ok: r.ok, status: r.status, data: await r.json() };
}

async function patch(url, body) {
  const r = await fetch(url, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return { ok: r.ok, status: r.status, data: await r.json() };
}

async function postJson(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return { ok: r.ok, status: r.status, data: await r.json() };
}

// ── General formatters ──────────────────────────────────────────
function fmt(n) {
  if (n === null || n === undefined) return '0.00';
  return Math.abs(n) >= 1000
    ? n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : n.toFixed(2);
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
