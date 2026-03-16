// ── Shared state ───────────────────────────────────────────────
const API = '/api';
let selectedBot = null;
let portfolioChart = null;
let _paramsCache = {};         // param_name → {value, default, type, ...}
let _settingsOpen = false;
let _backtestOpen = false;
let _backtestChart = null;
let _lastOptResult = null;     // cached optimization result for "Apply" button
let _botsCache = [];           // last known bots list [{name, symbol, is_running}]
let _statsMode = 'all';        // 'all' | '24h' | '3h' — period for stats grid + chart
let _portfolioData = null;     // cached portfolio state + trade stats for current bot
let _historyData = null;       // cached {snaps, trades} for chart re-render on mode switch
let _globalStatsData = null;   // cached {portfolios, bots, coinData, obStatus, periodStats}
                               // periodStats: { botId: {h24: {...}, h3: {...}} }

// Tracks the last test-window download so "Use test window" can pre-fill backtest dates
let _lastTestWindow = null;    // { start: "YYYY-MM-DD", end: "YYYY-MM-DD" }

let _llmEnabled = false;
let _logPanelOpen = false;
let _logErrorCount = 0;
let _refreshInterval = null;
let _refreshSeconds = 60;

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
