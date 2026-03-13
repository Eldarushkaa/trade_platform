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

// Moscow timezone formatter
const _moscowTZ = 'Europe/Moscow';
function fmtMoscow(isoStr) {
  if (!isoStr) return '—';
  try {
    return new Date(isoStr).toLocaleString('ru-RU', {
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

// ── Fetch helpers ──────────────────────────────────────────────
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

// ── Sidebar: list all bots ─────────────────────────────────────
async function loadBots() {
  try {
    const [bots, portfolios, coinData, obStatus] = await Promise.all([
      get(`${API}/bots`),
      get(`${API}/portfolio/all`).catch(() => []),
      get(`${API}/portfolio/coin-positions`).catch(() => null),
      get(`${API}/portfolio/orderbook-status`).catch(() => null),
    ]);

    // update mode badge from health
    try {
      const h = await get('/health');
      document.getElementById('mode-badge').textContent = h.mode;
      document.getElementById('market-badge').textContent = h.market_type || 'futures';
    } catch {}

    // Build portfolio lookup by bot_id
    const portMap = {};
    portfolios.forEach(p => { portMap[p.bot_id] = p; });

    const container = document.getElementById('bots-list');
    container.innerHTML = '';

    if (bots.length === 0) {
      container.innerHTML = '<div style="color:var(--muted);font-size:13px">No bots registered.<br>Add bots in main.py</div>';
      return;
    }

    bots.forEach(bot => {
      const p = portMap[bot.name];
      const returnPct = p ? p.return_pct : null;
      const totalVal = p ? p.total_value_usdt : null;
      const side = p ? (p.position_side || 'NONE') : 'NONE';

      const returnColor = returnPct == null ? 'var(--muted)'
        : returnPct >= 0 ? 'var(--green)' : 'var(--red)';
      const returnStr = returnPct == null ? '—'
        : (returnPct >= 0 ? '+' : '') + returnPct.toFixed(2) + '%';
      const totalStr = totalVal == null ? '—' : '$' + fmt(totalVal);

      let sideDot = '';
      if (side === 'LONG')  sideDot = '<span class="mini-side-dot long"></span>';
      else if (side === 'SHORT') sideDot = '<span class="mini-side-dot short"></span>';

      const card = document.createElement('div');
      card.className = 'bot-card' + (selectedBot === bot.name ? ' active' : '');
      card.dataset.name = bot.name;
      card.innerHTML = `
        <div class="bot-name">
          <span class="status-dot ${bot.is_running ? 'running' : 'stopped'}"></span>
          ${bot.name}
        </div>
        <div class="bot-symbol">${bot.symbol}</div>
        <div class="bot-mini-stats">
          <span style="color:${returnColor};font-weight:700">${returnStr}</span>
          <span style="color:var(--muted)">${totalStr}</span>
          <span>${sideDot}${side !== 'NONE' ? `<span style="font-size:10px;color:${side==='LONG'?'var(--green)':'var(--red)'}">${side}</span>` : ''}</span>
        </div>
        <div class="bot-actions">
          ${bot.is_running
            ? `<button class="btn btn-stop"  onclick="controlBot('${bot.name}','stop');event.stopPropagation()">■ Stop</button>`
            : `<button class="btn btn-start" onclick="controlBot('${bot.name}','start');event.stopPropagation()">▶ Start</button>`
          }
        </div>`;
      card.addEventListener('click', () => selectBot(bot.name));
      container.appendChild(card);
    });

    // Cache bots list for use in loadDataStatus (symbol lookup)
    _botsCache = bots;

    // Fetch per-bot period stats in parallel for the global stats bar period toggle
    const periodStats = {};
    await Promise.all(bots.map(async bot => {
      const [s24h, s3h] = await Promise.all([
        get(`${API}/trades/${bot.name}/stats?hours=24`).catch(() => null),
        get(`${API}/trades/${bot.name}/stats?hours=3`).catch(() => null),
      ]);
      periodStats[bot.name] = { h24: s24h, h3: s3h };
    }));

    // Cache for period toggle re-render
    _globalStatsData = { portfolios, bots, coinData, obStatus, periodStats };

    // Render global stats bar with current mode
    renderGlobalStats(portfolios, bots, coinData, obStatus, periodStats);
  } catch (e) {
    console.error('loadBots error', e);
  }
}

// ── Global stats bar ───────────────────────────────────────────
function renderGlobalStats(portfolios, bots, coinData, obStatus, periodStats) {
  const bar = document.getElementById('global-stats-bar');
  if (!bar || portfolios.length === 0) return;

  const totalUSDT = portfolios.reduce((s, p) => s + (p.usdt_balance || 0), 0);
  const totalValue = portfolios.reduce((s, p) => s + (p.total_value_usdt || 0), 0);
  const positiveCount = portfolios.filter(p => (p.return_pct || 0) > 0).length;
  const totalInitial = portfolios.reduce((s, p) => s + (p.total_value_usdt / (1 + (p.return_pct || 0) / 100)), 0);
  const overallReturn = totalInitial > 0 ? ((totalValue - totalInitial) / totalInitial * 100) : 0;
  const returnColor = overallReturn >= 0 ? 'var(--green)' : 'var(--red)';
  const periodLabel = _statsMode === '3h' ? 'Last 3h' : _statsMode === '24h' ? 'Last 24h' : 'All time';

  // Trades & fees: period-aware if period stats available
  let totalTrades, totalFees, totalPnl;
  if (_statsMode !== 'all' && periodStats) {
    const key = _statsMode === '3h' ? 'h3' : 'h24';
    totalTrades = Object.values(periodStats).reduce((s, ps) => s + ((ps[key] && ps[key].trade_count) || 0), 0);
    totalFees   = Object.values(periodStats).reduce((s, ps) => s + ((ps[key] && ps[key].total_fees_paid) || 0), 0);
    totalPnl    = Object.values(periodStats).reduce((s, ps) => s + ((ps[key] && ps[key].realized_pnl) || 0), 0);
  } else {
    totalTrades = portfolios.reduce((s, p) => s + (p.trade_count || 0), 0);
    totalFees   = portfolios.reduce((s, p) => s + (p.total_fees_paid || 0), 0);
    totalPnl    = portfolios.reduce((s, p) => s + (p.realized_pnl || 0), 0);
  }
  const pnlColor = totalPnl >= 0 ? 'var(--green)' : 'var(--red)';

  // Build 3×3 matrix: strategies × coins
  const strategies = [...new Set(bots.map(b => {
    const parts = b.name.split('_');
    return parts.slice(0, -1).join('_');
  }))].sort();
  const symbols = [...new Set(bots.map(b => {
    const parts = b.name.split('_');
    return parts[parts.length - 1].toUpperCase();
  }))].sort();

  // Build portfolio lookup
  const portMap = {};
  portfolios.forEach(p => { portMap[p.bot_id] = p; });

  // Matrix HTML — columns defined in CSS (.gs-matrix: auto repeat(3, 48px))
  // In period mode, show period trades instead of all-time return
  let matrixHTML = `<div class="gs-matrix">`;
  matrixHTML += `<div class="gs-matrix-cell gs-matrix-hdr"></div>`;
  symbols.forEach(sym => {
    matrixHTML += `<div class="gs-matrix-cell gs-matrix-hdr">${sym}</div>`;
  });
  strategies.forEach(strat => {
    const stratLabel = strat.toUpperCase();
    matrixHTML += `<div class="gs-matrix-cell gs-matrix-hdr" style="font-size:9px">${stratLabel}</div>`;
    symbols.forEach(sym => {
      const botId = `${strat}_${sym.toLowerCase()}`;
      const p = portMap[botId];
      let cellClass = 'gs-matrix-cell gs-cell-neutral';
      let retStr = '—';
      if (_statsMode !== 'all' && periodStats && periodStats[botId]) {
        const key = _statsMode === '3h' ? 'h3' : 'h24';
        const ps = periodStats[botId][key];
        if (ps) {
          const pnl = ps.realized_pnl || 0;
          cellClass = pnl >= 0 ? 'gs-matrix-cell gs-cell-green' : 'gs-matrix-cell gs-cell-red';
          retStr = (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(1);
        }
      } else if (p) {
        const ret = p.return_pct || 0;
        cellClass = ret >= 0 ? 'gs-matrix-cell gs-cell-green' : 'gs-matrix-cell gs-cell-red';
        retStr = (ret >= 0 ? '+' : '') + ret.toFixed(1) + '%';
      }
      matrixHTML += `<div class="${cellClass}" title="${botId}">${retStr}</div>`;
    });
  });
  matrixHTML += `</div>`;

  // Per-coin position blocks
  const coinPositions = coinData ? coinData.coin_positions : {};
  let coinBlocksHTML = '';
  if (Object.keys(coinPositions).length > 0) {
    coinBlocksHTML = `<div class="gs-coin-section">`;
    Object.entries(coinPositions).sort(([a], [b]) => a.localeCompare(b)).forEach(([sym, cp]) => {
      const asset = sym.replace('USDT', '');
      const sideClass = cp.net_side === 'LONG' ? 'gs-net-long' : cp.net_side === 'SHORT' ? 'gs-net-short' : 'gs-net-flat';
      const sideLabel = cp.net_side;
      const longLabel  = cp.total_long_qty > 0  ? `▲${cp.total_long_qty.toFixed(4)}` : '▲—';
      const shortLabel = cp.total_short_qty > 0 ? `▼${cp.total_short_qty.toFixed(4)}` : '▼—';
      const netLabel   = Math.abs(cp.net_qty) > 1e-8 ? `${cp.net_side === 'SHORT' ? '-' : '+'}${Math.abs(cp.net_qty).toFixed(4)}` : '0';
      coinBlocksHTML += `
        <div class="gs-coin-block">
          <div class="gs-coin-sym">${asset}</div>
          <div class="gs-coin-row"><span class="gs-long-qty">${longLabel}</span><span class="gs-short-qty">${shortLabel}</span></div>
          <div class="gs-coin-net ${sideClass}">${sideLabel} ${netLabel}</div>
        </div>`;
    });
    coinBlocksHTML += `</div>`;
  }

  // DOM (orderbook) status block
  let domHTML = '';
  if (obStatus && obStatus.orderbook && Object.keys(obStatus.orderbook).length > 0) {
    const ob = obStatus.orderbook;
    let domCells = '';
    Object.entries(ob).sort(([a],[b]) => a.localeCompare(b)).forEach(([sym, info]) => {
      const asset = sym.replace('USDT','');
      const count = info.count || 0;
      const last = info.last ? fmtMoscow(info.last) + ' МСК' : '—';
      const ibal = info.latest ? (info.latest.imbalance * 100).toFixed(1) + '%' : '—';
      const ibalColor = info.latest
        ? (info.latest.imbalance > 0.55 ? 'var(--green)' : info.latest.imbalance < 0.45 ? 'var(--red)' : 'var(--muted)')
        : 'var(--muted)';
      const spread = info.latest ? info.latest.spread.toFixed(2) : '—';
      domCells += `
        <div class="gs-dom-cell">
          <div class="gs-dom-sym">${asset}</div>
          <div class="gs-dom-rows">
            <span style="color:var(--muted);font-size:9px">${count} snaps</span>
            <span style="color:var(--muted);font-size:9px">last ${last}</span>
            <span style="font-size:10px">sprd <b>${spread}</b></span>
            <span style="font-size:10px;color:${ibalColor}">bal <b>${ibal}</b></span>
          </div>
        </div>`;
    });
    domHTML = `<div class="gs-dom-section" title="Orderbook collector data">
      <div class="gs-dom-label">📖 DOM</div>
      ${domCells}
    </div>`;
  } else {
    domHTML = `<div class="gs-dom-section gs-dom-offline" title="Run scripts/collect_orderbook.py to enable">
      <div class="gs-dom-label">📖 DOM</div>
      <div style="font-size:10px;color:var(--muted);padding:4px 6px">collector offline</div>
    </div>`;
  }

  bar.innerHTML = `
    <div class="gs-stat">
      <div class="gs-label">Free USDT</div>
      <div class="gs-value">$${fmt(totalUSDT)}</div>
    </div>
    <div class="gs-stat">
      <div class="gs-label">Total Value</div>
      <div class="gs-value">$${fmt(totalValue)}</div>
    </div>
    <div class="gs-stat">
      <div class="gs-label">Overall Return</div>
      <div class="gs-value" style="color:${returnColor}">${overallReturn >= 0 ? '+' : ''}${overallReturn.toFixed(2)}%</div>
    </div>
    <div class="gs-stat">
      <div class="gs-label">Trades (${periodLabel})</div>
      <div class="gs-value">${totalTrades}</div>
    </div>
    <div class="gs-stat">
      <div class="gs-label">P&L (${periodLabel})</div>
      <div class="gs-value" style="color:${pnlColor}">${totalPnl >= 0 ? '+' : ''}$${fmt(totalPnl)}</div>
    </div>
    <div class="gs-stat">
      <div class="gs-label">Profitable Bots</div>
      <div class="gs-value" style="color:${positiveCount > 0 ? 'var(--green)' : 'var(--muted)'}">${positiveCount} / ${portfolios.length}</div>
    </div>
    <div class="gs-stat">
      <div class="gs-label">Fees (${periodLabel})</div>
      <div class="gs-value" style="color:var(--yellow)">$${fmt(totalFees)}</div>
    </div>
    ${domHTML}
    ${coinBlocksHTML}
    ${matrixHTML}
  `;
}

async function controlBot(name, action) {
  await post(`${API}/bots/${name}/${action}`);
  await loadBots();
  if (selectedBot === name) await loadBotDetail(name);
}

// ── Main panel: bot detail ─────────────────────────────────────
function selectBot(name) {
  selectedBot = name;
  document.getElementById('no-bot').style.display = 'none';
  document.getElementById('bot-detail').style.display = 'block';
  document.getElementById('detail-title').textContent = name;

  // Reset backtest UI
  document.getElementById('bt-results').style.display = 'none';
  document.getElementById('bt-opt-results').style.display = 'none';
  document.getElementById('bt-status').textContent = '';
  document.getElementById('bt-metrics').innerHTML = '';
  if (_backtestChart) { _backtestChart.destroy(); _backtestChart = null; }
  _lastOptResult = null;

  loadBotDetail(name);
  // Mark active
  document.querySelectorAll('.bot-card').forEach(c =>
    c.classList.toggle('active', c.dataset.name === name));
}

async function loadBotDetail(name) {
  await Promise.all([
    loadPortfolio(name),
    loadHistory(name),
    loadTrades(name),
    loadParams(name),
  ]);
}

async function loadPortfolio(name) {
  try {
    const [p, stats24h, stats3h] = await Promise.all([
      get(`${API}/portfolio/${name}`),
      get(`${API}/trades/${name}/stats?hours=24`).catch(() => null),
      get(`${API}/trades/${name}/stats?hours=3`).catch(() => null),
    ]);

    // Cache for toggle re-render
    _portfolioData = { p, stats24h, stats3h };

    _renderPortfolio(p, stats24h, stats3h);
  } catch (e) { console.warn('loadPortfolio', e); }
}

function toggleStatsMode(mode) {
  _statsMode = mode;
  // Sync button active states
  ['all', '24h', '3h'].forEach(m => {
    const btn = document.getElementById(`ptbtn-${m}`);
    if (btn) btn.classList.toggle('active', m === mode);
  });
  // Re-render global stats bar with period-aware numbers
  if (_globalStatsData) {
    const { portfolios, bots, coinData, obStatus, periodStats } = _globalStatsData;
    renderGlobalStats(portfolios, bots, coinData, obStatus, periodStats);
  }
  // Re-render bot detail stats + chart
  if (_portfolioData) {
    _renderPortfolio(_portfolioData.p, _portfolioData.stats24h, _portfolioData.stats3h);
  }
  if (_historyData) {
    const windowMs = mode === '3h' ? 3 * 3600 * 1000
                   : mode === '24h' ? 24 * 3600 * 1000
                   : null;
    _renderChart(_historyData.snaps, _historyData.trades, windowMs);
  }
}

function _renderPortfolio(p, stats24h, stats3h) {
  const sign = v => v >= 0 ? 'positive' : 'negative';

  // ── Position info row ──────────────────────────────
  const posRow = document.getElementById('position-row');
  const side = p.position_side || 'NONE';
  const badgeClass = side === 'LONG' ? 'long' : side === 'SHORT' ? 'short' : 'none';
  const hasPosition = side !== 'NONE' && p.position_qty > 0;

  // Margin ratio color: green < 0.3, yellow 0.3-0.6, orange 0.6-0.8, red > 0.8
  const mr = p.margin_ratio || 0;
  let mrColor = 'var(--green)';
  if (mr >= 0.8) mrColor = 'var(--red)';
  else if (mr >= 0.6) mrColor = 'var(--orange)';
  else if (mr >= 0.3) mrColor = 'var(--yellow)';

  if (hasPosition) {
    posRow.style.display = 'grid';
    posRow.innerHTML = `
      <div>
        <div class="label">Position</div>
        <div class="val"><span class="pos-badge ${badgeClass}">${side}</span></div>
      </div>
      <div>
        <div class="label">Size</div>
        <div class="val">${p.position_qty.toFixed(6)}</div>
      </div>
      <div>
        <div class="label">Entry Price</div>
        <div class="val">$${fmt(p.entry_price)}</div>
      </div>
      <div>
        <div class="label">Leverage</div>
        <div class="val">${p.leverage}×</div>
      </div>
      <div>
        <div class="label">Margin Locked</div>
        <div class="val">$${fmt(p.margin_locked)}</div>
      </div>
      <div>
        <div class="label">Liq. Price</div>
        <div class="val" style="color:var(--red)">$${fmt(p.liquidation_price)}</div>
      </div>
      <div>
        <div class="label">Margin Ratio</div>
        <div class="val" style="color:${mrColor}">${(mr * 100).toFixed(1)}%</div>
        <div class="margin-bar-bg">
          <div class="margin-bar-fill" style="width:${Math.min(mr * 100, 100)}%;background:${mrColor}"></div>
        </div>
      </div>
      <div>
        <div class="label">Unrealized P&L</div>
        <div class="val ${sign(p.unrealized_pnl)}">${p.unrealized_pnl >= 0 ? '+' : ''}$${fmt(p.unrealized_pnl)}</div>
      </div>`;
  } else {
    posRow.style.display = 'grid';
    posRow.innerHTML = `
      <div>
        <div class="label">Position</div>
        <div class="val"><span class="pos-badge none">NONE</span></div>
      </div>
      <div>
        <div class="label">Leverage</div>
        <div class="val">${p.leverage || '—'}×</div>
      </div>
      <div>
        <div class="label">Status</div>
        <div class="val" style="color:var(--muted)">Waiting for signal...</div>
      </div>`;
  }

  // ── Stats grid ────────────────────────────────────────────────
  const grid = document.getElementById('stats-grid');
  const s = _statsMode === '3h' ? stats3h
          : _statsMode === '24h' ? stats24h
          : null;
  const periodLabel = _statsMode === '3h' ? '3h' : _statsMode === '24h' ? '24h' : '';

  if (s && _statsMode !== 'all') {
    // Time-windowed mode: show trade-based stats for the selected period
    const winRate = (s.win_count + s.loss_count) > 0
      ? (s.win_count / (s.win_count + s.loss_count) * 100).toFixed(1) + '%'
      : '—';
    grid.innerHTML = `
      <div class="stat-card">
        <div class="label">Realized P&L (${periodLabel})</div>
        <div class="value ${sign(s.realized_pnl)}">${s.realized_pnl >= 0 ? '+' : ''}$${fmt(s.realized_pnl)}</div>
      </div>
      <div class="stat-card">
        <div class="label">Net P&L after fees (${periodLabel})</div>
        <div class="value ${sign(s.realized_pnl - s.total_fees_paid)}">${(s.realized_pnl - s.total_fees_paid) >= 0 ? '+' : ''}$${fmt(s.realized_pnl - s.total_fees_paid)}</div>
      </div>
      <div class="stat-card">
        <div class="label">Trades (${periodLabel})</div>
        <div class="value neutral">${s.trade_count}</div>
      </div>
      <div class="stat-card">
        <div class="label">Win Rate (${periodLabel})</div>
        <div class="value ${s.win_count > s.loss_count ? 'positive' : s.win_count < s.loss_count ? 'negative' : 'neutral'}">${winRate}</div>
      </div>
      <div class="stat-card">
        <div class="label">Fees Paid (${periodLabel})</div>
        <div class="value" style="color:var(--yellow)">$${fmt(s.total_fees_paid)}</div>
      </div>
      <div class="stat-card">
        <div class="label">Free USDT</div>
        <div class="value neutral">$${fmt(p.usdt_balance)}</div>
      </div>
      <div class="stat-card">
        <div class="label">Total Value</div>
        <div class="value neutral">$${fmt(p.total_value_usdt)}</div>
      </div>
      <div class="stat-card">
        <div class="label">Return (all time)</div>
        <div class="value ${sign(p.return_pct)}">${p.return_pct >= 0 ? '+' : ''}${p.return_pct.toFixed(2)}%</div>
      </div>`;
  } else {
    // All-time mode
    grid.innerHTML = `
      <div class="stat-card">
        <div class="label">Free USDT</div>
        <div class="value neutral">$${fmt(p.usdt_balance)}</div>
      </div>
      <div class="stat-card">
        <div class="label">Total Value</div>
        <div class="value neutral">$${fmt(p.total_value_usdt)}</div>
      </div>
      <div class="stat-card">
        <div class="label">Realized P&L</div>
        <div class="value ${sign(p.realized_pnl)}">${p.realized_pnl >= 0 ? '+' : ''}$${fmt(p.realized_pnl)}</div>
      </div>
      <div class="stat-card">
        <div class="label">Net P&L (after fees)</div>
        <div class="value ${sign(p.net_pnl)}">${p.net_pnl >= 0 ? '+' : ''}$${fmt(p.net_pnl)}</div>
      </div>
      <div class="stat-card">
        <div class="label">Return</div>
        <div class="value ${sign(p.return_pct)}">${p.return_pct >= 0 ? '+' : ''}${p.return_pct.toFixed(2)}%</div>
      </div>
      <div class="stat-card">
        <div class="label">Trades</div>
        <div class="value neutral">${p.trade_count}</div>
      </div>
      <div class="stat-card">
        <div class="label">Fees Paid</div>
        <div class="value" style="color:var(--yellow)">$${fmt(p.total_fees_paid)}</div>
      </div>
      <div class="stat-card">
        <div class="label">Liquidations</div>
        <div class="value ${p.liquidation_count > 0 ? 'negative' : 'neutral'}">${p.liquidation_count}</div>
      </div>`;
  }
}

async function loadHistory(name) {
  try {
    const [snaps, trades] = await Promise.all([
      get(`${API}/portfolio/${name}/history?limit=1000`),
      get(`${API}/trades/${name}?limit=1000`).catch(() => []),
    ]);

    if (snaps.length === 0) {
      if (portfolioChart) { portfolioChart.destroy(); portfolioChart = null; }
      return;
    }

    // Cache for mode switching
    _historyData = { snaps, trades };

    // Render with current window
    const windowMs = _statsMode === '3h' ? 3 * 3600 * 1000
                   : _statsMode === '24h' ? 24 * 3600 * 1000
                   : null;
    _renderChart(snaps, trades, windowMs);
  } catch (e) { console.warn('loadHistory', e); }
}

function _renderChart(snaps, trades, windowMs) {
  // Apply time window filter
  let visSnaps = snaps;
  if (windowMs) {
    const cutoff = Date.now() - windowMs;
    visSnaps = snaps.filter(s => new Date(s.timestamp).getTime() >= cutoff);
    if (visSnaps.length === 0) visSnaps = snaps.slice(-10); // fallback: show last 10
  }

  // Detect gap threshold from visible snaps
  const deltas = [];
  for (let i = 1; i < Math.min(visSnaps.length, 11); i++) {
    deltas.push(new Date(visSnaps[i].timestamp) - new Date(visSnaps[i-1].timestamp));
  }
  deltas.sort((a,b)=>a-b);
  const medianDelta = deltas.length ? deltas[Math.floor(deltas.length/2)] : 30000;
  const gapThreshold = Math.max(medianDelta * 3, 120000);

  const labels = [];
  const values = [];
  const usdtValues = [];
  const coinValues = [];
  const prices = [];
  const snapTimestamps = [];

  for (let i = 0; i < visSnaps.length; i++) {
    const s = visSnaps[i];
    const d = new Date(s.timestamp);

    if (i > 0) {
      const prev = new Date(visSnaps[i-1].timestamp);
      if ((d - prev) > gapThreshold) {
        const midMs = prev.getTime() + (d - prev) / 2;
        labels.push(fmtTime(new Date(midMs)));
        values.push(null); usdtValues.push(null); coinValues.push(null);
        prices.push(null); snapTimestamps.push(midMs);
      }
    }

    const usdtBal = s.usdt_balance ?? s.total_value_usdt;
    const coinVal = s.total_value_usdt - usdtBal;
    labels.push(fmtTime(d));
    values.push(s.total_value_usdt);
    usdtValues.push(usdtBal);
    coinValues.push(coinVal > 0.01 ? coinVal : 0);
    prices.push(s.asset_price ?? null);
    snapTimestamps.push(d.getTime());
  }

  const hasPriceData = prices.some(p => p !== null);

  // Trade markers — only trades within visible time window
  const longData = new Array(labels.length).fill(null);
  const shortData = new Array(labels.length).fill(null);
  const longMeta = {};
  const shortMeta = {};
  const chartStart = snapTimestamps[0] || 0;
  const chartEnd = snapTimestamps[snapTimestamps.length - 1] || Infinity;

  if (trades.length > 0) {
    trades.forEach(t => {
      const tMs = new Date(t.timestamp).getTime();
      // Filter: only trades within the visible window (with a 5-min buffer either side)
      if (tMs < chartStart - 300000 || tMs > chartEnd + 300000) return;

      // Find nearest snapshot — always show the marker even if far from a snapshot
      let bestIdx = 0, bestDist = Infinity;
      for (let i = 0; i < snapTimestamps.length; i++) {
        const dist = Math.abs(snapTimestamps[i] - tMs);
        if (dist < bestDist) { bestDist = dist; bestIdx = i; }
      }
      // Only skip if snapshots array is empty or distance > 2 hours
      if (bestDist > 2 * 3600 * 1000) return;

      const action = (t.position_side || '').toUpperCase();
      const isLong = action.includes('LONG');
      const isOpen = action.startsWith('OPEN');

      if (isLong || action === 'BUY') {
        longData[bestIdx] = t.price;
        longMeta[bestIdx] = { action: action || 'BUY', price: t.price, qty: t.quantity, pnl: t.realized_pnl, open: isOpen };
      } else {
        shortData[bestIdx] = t.price;
        shortMeta[bestIdx] = { action: action || 'SELL', price: t.price, qty: t.quantity, pnl: t.realized_pnl, open: isOpen };
      }
    });
  }

  const hasTradeMarkers = longData.some(v => v !== null) || shortData.some(v => v !== null);
  const assetSym = visSnaps[0]?.asset_symbol || snaps[0]?.asset_symbol || 'Coin';
  document.getElementById('price-legend-label').textContent = assetSym + ' Price';
  document.getElementById('price-legend').style.display = hasPriceData ? 'flex' : 'none';
  document.getElementById('long-legend').style.display = hasTradeMarkers ? 'flex' : 'none';
  document.getElementById('short-legend').style.display = hasTradeMarkers ? 'flex' : 'none';

  const ctx = document.getElementById('portfolio-chart').getContext('2d');
  if (portfolioChart) portfolioChart.destroy();

  const dashedSegment = (ctx, def) =>
    (ctx.p0.skip || ctx.p1.skip) ? [4, 4] : def;

  portfolioChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Coin Value', data: values,
          borderColor: 'rgba(0,200,150,0.6)', backgroundColor: 'rgba(0,200,150,0.15)',
          borderWidth: 0, pointRadius: 0, fill: true, tension: 0.3, spanGaps: true,
          yAxisID: 'yPortfolio', order: 3,
        },
        {
          label: 'USDT Balance', data: usdtValues,
          borderColor: 'rgba(108,99,255,0.6)', backgroundColor: 'rgba(108,99,255,0.2)',
          borderWidth: 0, pointRadius: 0, fill: true, tension: 0.3, spanGaps: true,
          yAxisID: 'yPortfolio', order: 2,
        },
        {
          label: 'Total Value', data: values,
          borderColor: '#6c63ff', backgroundColor: 'transparent',
          borderWidth: 2, pointRadius: 0, fill: false, tension: 0.3, spanGaps: true,
          yAxisID: 'yPortfolio', order: 1,
          segment: {
            borderDash: ctx => dashedSegment(ctx, []),
            borderColor: ctx => (ctx.p0.skip || ctx.p1.skip) ? 'rgba(108,99,255,0.3)' : '#6c63ff',
          }
        },
        {
          label: 'Coin Price', data: prices,
          borderColor: '#f5a623', backgroundColor: 'rgba(245,166,35,0.05)',
          borderWidth: 1.5, pointRadius: 0, fill: false, tension: 0.2, spanGaps: true,
          yAxisID: 'yPrice',
          segment: {
            borderDash: ctx => dashedSegment(ctx, []),
            borderColor: ctx => (ctx.p0.skip || ctx.p1.skip) ? 'rgba(245,166,35,0.3)' : '#f5a623',
          }
        },
        {
          label: 'Long', data: longData,
          borderColor: '#00c896', backgroundColor: '#00c896',
          pointRadius: longData.map(v => v !== null ? 7 : 0),
          pointHoverRadius: longData.map(v => v !== null ? 9 : 0),
          pointStyle: 'triangle', pointRotation: 0,
          borderWidth: 2, showLine: false, fill: false,
          yAxisID: 'yPrice', order: -1,
        },
        {
          label: 'Short', data: shortData,
          borderColor: '#ff4d6d', backgroundColor: '#ff4d6d',
          pointRadius: shortData.map(v => v !== null ? 7 : 0),
          pointHoverRadius: shortData.map(v => v !== null ? 9 : 0),
          pointStyle: 'triangle', pointRotation: 180,
          borderWidth: 2, showLine: false, fill: false,
          yAxisID: 'yPrice', order: -1,
        }
      ]
    },
    options: {
      responsive: true,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          filter: item => item.parsed.y !== null,
          callbacks: {
            label: tooltipCtx => {
              const v = tooltipCtx.parsed.y;
              if (v === null || v === undefined) return null;
              const dsLabel = tooltipCtx.dataset.label;
              const idx = tooltipCtx.dataIndex;
              const fmtUsd = n => '$' + n.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
              if (dsLabel === 'Total Value') return `Total: ${fmtUsd(v)}`;
              if (dsLabel === 'USDT Balance') return `USDT: ${fmtUsd(v)}`;
              if (dsLabel === 'Coin Value') return `Coin: ${fmtUsd(coinValues[idx] ?? 0)}`;
              if (dsLabel === 'Coin Price') return `${assetSym}: ${fmtUsd(v)}`;
              const meta = dsLabel === 'Long' ? longMeta[idx] : shortMeta[idx];
              if (!meta) return null;
              let line = `${meta.action.replace('_',' ')} @ $${fmt(meta.price)} × ${meta.qty.toFixed(6)}`;
              if (meta.pnl != null) line += ` | P&L: ${meta.pnl >= 0 ? '+' : ''}$${fmt(meta.pnl)}`;
              return line;
            }
          }
        }
      },
      scales: {
        x: {
          display: true,
          ticks: { color: '#8892a4', maxTicksLimit: 10, maxRotation: 0 },
          grid: { color: '#2a2d3a' }
        },
        yPortfolio: {
          type: 'linear', position: 'left',
          ticks: { color: '#8892a4', callback: v => '$' + v.toLocaleString('en-US',{maximumFractionDigits:0}) },
          grid: { color: '#2a2d3a' }
        },
        yPrice: {
          type: 'linear', position: 'right',
          display: hasPriceData || hasTradeMarkers,
          ticks: { color: '#f5a623', callback: v => '$' + v.toLocaleString('en-US',{maximumFractionDigits:0}) },
          grid: { drawOnChartArea: false }
        }
      }
    }
  });
}

function fmtTime(d) {
  // Chart axis labels in Moscow time
  return fmtMoscowTime(d);
}

async function loadTrades(name) {
  try {
    const trades = await get(`${API}/trades/${name}?limit=50`);
    const tbody = document.getElementById('trades-body');
    tbody.innerHTML = '';

    if (trades.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" style="color:var(--muted);text-align:center;padding:20px">No trades yet</td></tr>';
      return;
    }

    trades.forEach(t => {
      const pnl = t.realized_pnl;
      const pnlStr = pnl != null
        ? `<span class="${pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}">${pnl >= 0 ? '+' : ''}$${fmt(pnl)}</span>`
        : '—';

      // Action badge (OPEN_LONG, CLOSE_LONG, OPEN_SHORT, CLOSE_SHORT)
      const action = t.position_side || t.side;
      const actionClass = action.replace('_', '-').toLowerCase();
      const actionLabel = action.replace('_', ' ');

      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>#${t.id}</td>
        <td><span class="action-badge ${actionClass}">${actionLabel}</span></td>
        <td class="${t.side.toLowerCase()}">${t.side}</td>
        <td>$${fmt(t.price)}</td>
        <td>${t.quantity.toFixed(6)}</td>
        <td>${pnlStr}</td>
        <td style="color:var(--muted)">${fmtMoscow(t.timestamp)} МСК</td>`;
      tbody.appendChild(tr);
    });
  } catch (e) { console.warn('loadTrades', e); }
}

// ── Helpers ────────────────────────────────────────────────────
function fmt(n) {
  if (n === null || n === undefined) return '0.00';
  return Math.abs(n) >= 1000
    ? n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : n.toFixed(2);
}

// ── Settings panel ─────────────────────────────────────────────
async function loadParams(name) {
  try {
    const data = await get(`${API}/bots/${name}/params`);
    _paramsCache = data.params || {};
    const wrap = document.getElementById('settings-wrap');
    const grid = document.getElementById('settings-grid');
    const label = document.getElementById('settings-strategy-label');

    const keys = Object.keys(_paramsCache);
    if (keys.length === 0) {
      wrap.style.display = 'none';
      return;
    }

    wrap.style.display = 'block';
    label.textContent = `(${data.strategy})`;

    grid.innerHTML = '';
    keys.forEach(key => {
      const p = _paramsCache[key];
      const step = p.type === 'int' ? '1' : '0.0001';
      const isChanged = p.value !== p.default;
      const div = document.createElement('div');
      div.className = 'param-group';
      div.innerHTML = `
        <label>${key.replace(/_/g, ' ')}</label>
        <input type="number" id="param-${key}" data-key="${key}" data-default="${p.default}"
               value="${p.value}" min="${p.min}" max="${p.max}" step="${step}"
               class="${isChanged ? 'changed' : ''}"
               oninput="onParamInput(this)" />
        <div class="param-range">${p.min} – ${p.max} · default: ${p.default}</div>
        <div class="param-desc">${p.description || ''}</div>`;
      grid.appendChild(div);
    });

    document.getElementById('btn-save-params').disabled = true;
    hideToast();
  } catch (e) {
    console.warn('loadParams', e);
    document.getElementById('settings-wrap').style.display = 'none';
  }
}

function onParamInput(input) {
  const key = input.dataset.key;
  const defVal = parseFloat(input.dataset.default);
  const curVal = parseFloat(input.value);
  input.classList.toggle('changed', curVal !== defVal);
  document.getElementById('btn-save-params').disabled = false;
  hideToast();
}

function toggleSettings() {
  _settingsOpen = !_settingsOpen;
  document.getElementById('settings-body').classList.toggle('open', _settingsOpen);
  document.getElementById('settings-chevron').classList.toggle('open', _settingsOpen);
}

async function saveParams() {
  if (!selectedBot) return;
  const inputs = document.querySelectorAll('#settings-grid input[data-key]');
  const updates = {};
  inputs.forEach(inp => {
    const key = inp.dataset.key;
    const schema = _paramsCache[key];
    if (!schema) return;
    const val = schema.type === 'int' ? parseInt(inp.value) : parseFloat(inp.value);
    if (val !== schema.value) updates[key] = val;
  });

  if (Object.keys(updates).length === 0) {
    showToast('No changes to save', 'error');
    return;
  }

  const btn = document.getElementById('btn-save-params');
  btn.disabled = true;
  btn.textContent = '⏳ Saving...';

  try {
    const resp = await put(`${API}/bots/${selectedBot}/params`, updates);
    if (resp.ok) {
      showToast(`✓ Saved ${Object.keys(resp.data.applied).length} param(s)`, 'success');
      // Reload to refresh cached values
      await loadParams(selectedBot);
    } else {
      showToast(`✗ ${resp.data.detail || 'Error'}`, 'error');
    }
  } catch (e) {
    showToast(`✗ ${e.message}`, 'error');
  }
  btn.textContent = '💾 Save';
}

async function resetParams() {
  if (!selectedBot) return;
  const defaults = {};
  Object.entries(_paramsCache).forEach(([key, p]) => {
    defaults[key] = p.default;
  });

  try {
    const resp = await put(`${API}/bots/${selectedBot}/params`, defaults);
    if (resp.ok) {
      showToast('✓ Reset to defaults', 'success');
      await loadParams(selectedBot);
    } else {
      showToast(`✗ ${resp.data.detail || 'Error'}`, 'error');
    }
  } catch (e) {
    showToast(`✗ ${e.message}`, 'error');
  }
}

function showToast(msg, type) {
  const el = document.getElementById('settings-toast');
  el.textContent = msg;
  el.className = `settings-toast ${type}`;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.className = 'settings-toast'; }, 4000);
}

function hideToast() {
  const el = document.getElementById('settings-toast');
  el.className = 'settings-toast';
}

// ── Backtest & Optimization ────────────────────────────────────

function toggleBacktest() {
  _backtestOpen = !_backtestOpen;
  document.getElementById('backtest-body').classList.toggle('open', _backtestOpen);
  document.getElementById('bt-chevron').classList.toggle('open', _backtestOpen);
  if (_backtestOpen) loadDataStatus();
}

async function loadDataStatus() {
  try {
    let obStatus = null;
    try { obStatus = await get(`${API}/portfolio/orderbook-status`); } catch(e) { console.warn('ob-status fetch failed:', e); }

    // Update orderbook snapshot count in sidebar
    const obInfoEl = document.getElementById('ob-data-info');
    if (obInfoEl) {
      const ob = (obStatus && obStatus.orderbook) ? obStatus.orderbook : {};
      const syms = Object.keys(ob);
      if (syms.length > 0) {
        const totalSnaps = syms.reduce((s, sym) => s + (ob[sym].count || 0), 0);
        const perCoin = syms.length > 0 ? Math.round(totalSnaps / syms.length) : 0;
        const lastSym = Object.values(ob).find(v => v.last);
        const lastStr = lastSym ? lastSym.last.slice(11,16) + ' UTC' : '';
        obInfoEl.textContent = `${perCoin}/coin${lastStr ? ' · ' + lastStr : ''}`;
        obInfoEl.style.color = 'var(--green)';
      } else {
        obInfoEl.textContent = obStatus === null ? 'fetch error' : 'no data yet';
        obInfoEl.style.color = 'var(--muted)';
      }
    }

    const [d] = await Promise.all([
      get(`${API}/backtest/data-status`),
    ]);

    const info = document.getElementById('bt-data-info');
    const badge = document.getElementById('bt-data-badge');
    const total = d.total_candles || 0;

    // Determine if the selected bot is OB-driven (ob_wall uses OB snapshots as primary data)
    const isObBot = selectedBot && selectedBot.includes('ob_wall');

    if (isObBot) {
      // For OB-driven bots, show OB snapshot count instead of candle count
      const botInfo = _botsCache.find(b => b.name === selectedBot);
      const symbol = botInfo ? botInfo.symbol : null;
      const obCounts = d.ob_snapshots || {};
      const obCount = symbol ? (obCounts[symbol] || 0) : Object.values(obCounts).reduce((s, v) => s + v, 0);
      if (obCount > 0) {
        info.innerHTML = `${obCount} OB snapshots · ${symbol || 'all coins'}<br><span style="color:var(--muted);font-size:9px">Backtest uses OB data range</span>`;
        badge.textContent = `(${obCount} OB snaps)`;
        document.getElementById('bt-run-btn').disabled = false;
        document.getElementById('bt-opt-btn').disabled = false;
      } else {
        info.innerHTML = 'No OB data — run collect_orderbook.py first';
        badge.textContent = '';
        document.getElementById('bt-run-btn').disabled = true;
        document.getElementById('bt-opt-btn').disabled = true;
      }
    } else if (total > 0) {
      const syms = Object.entries(d.symbols).filter(([,v]) => v.count > 0);
      const coinCount = syms.length;
      // Find most recent candle end date across all symbols
      let latestEnd = null;
      syms.forEach(([, v]) => {
        if (v.end) {
          const d = new Date(v.end);
          if (!latestEnd || d > latestEnd) latestEnd = d;
        }
      });
      // Show per-coin candle count (total / coins) + last date
      const perCoin = coinCount > 0 ? Math.round(total / coinCount) : total;
      const dateStr = latestEnd
        ? `${latestEnd.toLocaleDateString('en-US', {month:'short', day:'numeric'})} ${String(latestEnd.getHours()).padStart(2,'0')}:${String(latestEnd.getMinutes()).padStart(2,'0')}`
        : '';
      info.innerHTML = `${perCoin} candles/coin · ${coinCount} coin${coinCount > 1 ? 's' : ''}<br><span style="color:var(--muted);font-size:9px">Last: ${dateStr}</span>`;
      badge.textContent = `(${perCoin}/coin, ${dateStr})`;
      document.getElementById('bt-run-btn').disabled = false;
      document.getElementById('bt-opt-btn').disabled = false;
    } else {
      info.innerHTML = 'No data — download first';
      badge.textContent = '';
      document.getElementById('bt-run-btn').disabled = true;
      document.getElementById('bt-opt-btn').disabled = true;
    }
  } catch (e) { console.warn('loadDataStatus', e); }
}

async function downloadHistory(days) {
  const btns = document.querySelectorAll('.bt-btn-dl');
  btns.forEach(b => { b.disabled = true; });
  document.getElementById('bt-data-info').textContent = `Downloading ${days}d of data...`;

  try {
    const resp = await postJson(`${API}/backtest/download`, { days });
    if (resp.ok) {
      const total = resp.data.results.reduce((s, r) => s + (r.candles_downloaded || 0), 0);
      document.getElementById('bt-data-info').textContent = `✓ Downloaded ${total} candles`;
    } else {
      document.getElementById('bt-data-info').textContent = `✗ ${resp.data.detail || 'Error'}`;
    }
  } catch (e) {
    document.getElementById('bt-data-info').textContent = `✗ ${e.message}`;
  }

  btns.forEach(b => { b.disabled = false; });
  await loadDataStatus();
}

async function runBacktest() {
  if (!selectedBot) return;
  const btn = document.getElementById('bt-run-btn');
  const status = document.getElementById('bt-status');
  btn.disabled = true;
  status.textContent = '⏳ Running backtest...';
  document.getElementById('bt-results').style.display = 'none';
  document.getElementById('bt-opt-results').style.display = 'none';

  try {
    const resp = await postJson(`${API}/backtest/run`, { bot_id: selectedBot });
    if (!resp.ok) {
      status.textContent = `✗ ${resp.data.detail || 'Error'}`;
      btn.disabled = false;
      return;
    }
    const r = resp.data;
    status.textContent = `✓ ${r.candles_processed} candles in ${r.duration_seconds}s`;

    renderBacktestResults(r);
  } catch (e) {
    status.textContent = `✗ ${e.message}`;
  }
  btn.disabled = false;
}

function renderBacktestResults(r) {
  document.getElementById('bt-results').style.display = 'block';

  // Metrics grid
  const sign = v => v >= 0 ? 'positive' : 'negative';
  const metricsEl = document.getElementById('bt-metrics');
  metricsEl.innerHTML = `
    <div class="bt-metric">
      <div class="label">Return</div>
      <div class="value ${sign(r.return_pct)}">${r.return_pct >= 0 ? '+' : ''}${r.return_pct.toFixed(2)}%</div>
    </div>
    <div class="bt-metric">
      <div class="label">Net P&L</div>
      <div class="value ${sign(r.net_pnl)}">${r.net_pnl >= 0 ? '+' : ''}$${fmt(r.net_pnl)}</div>
    </div>
    <div class="bt-metric">
      <div class="label">Sharpe Ratio</div>
      <div class="value ${r.sharpe_ratio >= 1 ? 'positive' : r.sharpe_ratio >= 0 ? 'neutral' : 'negative'}">${r.sharpe_ratio.toFixed(2)}</div>
    </div>
    <div class="bt-metric">
      <div class="label">Max Drawdown</div>
      <div class="value negative">-${r.max_drawdown_pct.toFixed(2)}%</div>
    </div>
    <div class="bt-metric">
      <div class="label">Trades (total/closed)</div>
      <div class="value neutral">${r.total_trades || r.trade_count} / ${r.trade_count}</div>
    </div>
    <div class="bt-metric">
      <div class="label">Win Rate</div>
      <div class="value ${r.win_rate >= 50 ? 'positive' : 'negative'}">${r.win_rate.toFixed(1)}%</div>
    </div>
    <div class="bt-metric">
      <div class="label">Profit Factor</div>
      <div class="value ${r.profit_factor >= 1 ? 'positive' : 'negative'}">${r.profit_factor === Infinity ? '∞' : r.profit_factor.toFixed(2)}</div>
    </div>
    <div class="bt-metric">
      <div class="label">Fees Paid</div>
      <div class="value" style="color:var(--yellow)">$${fmt(r.total_fees)}</div>
    </div>
    <div class="bt-metric">
      <div class="label">Liquidations</div>
      <div class="value ${r.liquidations > 0 ? 'negative' : 'neutral'}">${r.liquidations}</div>
    </div>
    <div class="bt-metric">
      <div class="label">Win Streak</div>
      <div class="value positive">${r.longest_win_streak}</div>
    </div>
    <div class="bt-metric">
      <div class="label">Loss Streak</div>
      <div class="value negative">${r.longest_loss_streak}</div>
    </div>`;

  // Equity chart
  renderBacktestChart(r.equity_curve);
}

function renderBacktestChart(curve) {
  if (!curve || curve.length === 0) return;

  const labels = curve.map(p => {
    const d = new Date(p.time);
    return `${String(d.getMonth()+1).padStart(2,'0')}/${String(d.getDate()).padStart(2,'0')} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
  });
  const values = curve.map(p => p.value);
  const usdtValues = curve.map(p => p.usdt ?? p.value);
  const coinValues = curve.map((p, i) => {
    const cv = (p.value || 0) - (p.usdt ?? p.value);
    return cv > 0.01 ? cv : 0;
  });
  const prices = curve.map(p => p.price);
  const sides = curve.map(p => p.side || 'NONE');

  // Color helpers based on position side
  const sideColor = (s) => s === 'LONG' ? 'rgba(0,200,120,0.8)' : s === 'SHORT' ? 'rgba(255,80,80,0.8)' : 'rgba(130,130,160,0.5)';
  const sideBg   = (s) => s === 'LONG' ? 'rgba(0,200,120,0.12)' : s === 'SHORT' ? 'rgba(255,80,80,0.12)' : 'rgba(130,130,160,0.05)';

  const ctx = document.getElementById('bt-chart').getContext('2d');
  if (_backtestChart) _backtestChart.destroy();

  _backtestChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        // Position area fill — colored by LONG/SHORT
        {
          label: 'Position',
          data: values,
          borderWidth: 0, pointRadius: 0, fill: true, tension: 0.3,
          yAxisID: 'yEq',
          order: 4,
          segment: {
            backgroundColor: ctx => sideBg(sides[ctx.p0DataIndex]),
          },
          backgroundColor: 'rgba(130,130,160,0.05)',
        },
        // USDT balance area (bottom)
        {
          label: 'USDT Balance',
          data: usdtValues,
          borderColor: 'rgba(108,99,255,0.6)',
          backgroundColor: 'rgba(108,99,255,0.18)',
          borderWidth: 0, pointRadius: 0, fill: true, tension: 0.3,
          yAxisID: 'yEq',
          order: 3,
        },
        // Total value line — colored by position side
        {
          label: 'Total Value',
          data: values,
          backgroundColor: 'transparent',
          borderWidth: 2, pointRadius: 0, fill: false, tension: 0.3,
          yAxisID: 'yEq',
          order: 1,
          segment: {
            borderColor: ctx => sideColor(sides[ctx.p0DataIndex]),
          },
          borderColor: 'rgba(130,130,160,0.5)',
        },
        {
          label: 'Coin Price',
          data: prices,
          borderColor: '#f5a623',
          borderWidth: 1.5, pointRadius: 0, fill: false, tension: 0.2,
          yAxisID: 'yPr',
          order: 2,
        }
      ]
    },
    options: {
      responsive: true,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: tooltipCtx => {
              const v = tooltipCtx.parsed.y;
              if (v === null || v === undefined) return null;
              const dsLabel = tooltipCtx.dataset.label;
              const idx = tooltipCtx.dataIndex;
              const fmtUsd = n => '$' + n.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
              if (dsLabel === 'Total Value') {
                const side = sides[idx];
                const tag = side === 'LONG' ? ' 🟢 LONG' : side === 'SHORT' ? ' 🔴 SHORT' : '';
                return `Total: ${fmtUsd(v)}${tag}`;
              }
              if (dsLabel === 'USDT Balance') return `USDT: ${fmtUsd(v)}`;
              if (dsLabel === 'Position') return `Coin: ${fmtUsd(coinValues[idx] ?? 0)}`;
              if (dsLabel === 'Coin Price') return `Price: ${fmtUsd(v)}`;
              return null;
            }
          }
        }
      },
      scales: {
        x: {
          ticks: { color: '#8892a4', maxTicksLimit: 8, maxRotation: 0 },
          grid: { color: '#2a2d3a' }
        },
        yEq: {
          type: 'linear', position: 'left',
          ticks: { color: '#8892a4', callback: v => '$'+v.toLocaleString('en-US',{maximumFractionDigits:0}) },
          grid: { color: '#2a2d3a' }
        },
        yPr: {
          type: 'linear', position: 'right',
          ticks: { color: '#f5a623', callback: v => '$'+v.toLocaleString('en-US',{maximumFractionDigits:0}) },
          grid: { drawOnChartArea: false }
        }
      }
    }
  });
}

async function runOptimize() {
  if (!selectedBot) return;
  const btn = document.getElementById('bt-opt-btn');
  const status = document.getElementById('bt-status');
  const iters = parseInt(document.getElementById('bt-opt-iters').value) || 200;
  btn.disabled = true;
  btn.textContent = '⏳ Optimizing...';
  status.textContent = `Optimization running (${iters} iterations)...`;
  document.getElementById('bt-opt-results').style.display = 'none';

  try {
    // Start optimization (returns task_id)
    const startResp = await postJson(`${API}/backtest/optimize`, { bot_id: selectedBot, iterations: iters });
    if (!startResp.ok) {
      status.textContent = `✗ ${startResp.data.detail || 'Error'}`;
      btn.disabled = false;
      btn.textContent = '🧠 Optimize';
      return;
    }

    const taskId = startResp.data.task_id;

    // Poll for completion
    let done = false;
    while (!done) {
      await new Promise(r => setTimeout(r, 2000));
      try {
        const poll = await get(`${API}/backtest/status?task_id=${taskId}`);
        status.textContent = `⏳ ${poll.progress?.msg || 'Running...'}`;
        if (poll.status === 'completed') {
          done = true;
          renderOptResults(poll.result);
          status.textContent = `✓ Optimization complete in ${poll.result.duration_seconds}s`;
        } else if (poll.status === 'error') {
          done = true;
          status.textContent = `✗ ${poll.error}`;
        }
      } catch (e) {
        console.warn('poll error', e);
      }
    }
  } catch (e) {
    status.textContent = `✗ ${e.message}`;
  }

  btn.disabled = false;
  btn.textContent = '🧠 Optimize';
}

function renderOptResults(r) {
  _lastOptResult = r;
  const wrap = document.getElementById('bt-opt-results');
  wrap.style.display = 'block';

  const sign = v => v >= 0 ? 'positive' : 'negative';
  const imp = r.improvement || {};
  const ga = r.ga_stats || {};

  // Build param comparison table
  let paramRows = '';
  const allKeys = new Set([...Object.keys(r.current_params || {}), ...Object.keys(r.best_params || {})]);
  allKeys.forEach(key => {
    const cur = r.current_params?.[key];
    const best = r.best_params?.[key];
    const changed = cur !== best;
    paramRows += `
      <div>${key.replace(/_/g, ' ')}</div>
      <div>${cur ?? '—'}</div>
      <div class="${changed ? 'changed' : ''}">${best ?? '—'}</div>
      <div class="${changed ? 'changed' : ''}">${changed ? '⚡' : '✓'}</div>`;
  });

  wrap.innerHTML = `
    <div class="bt-opt-results">
      <h4>🧬 GA Optimization Results</h4>
      <div class="bt-metrics" style="margin-bottom:14px">
        <div class="bt-metric">
          <div class="label">Best Sharpe</div>
          <div class="value ${sign(r.best_sharpe)}">${r.best_sharpe.toFixed(2)}</div>
        </div>
        <div class="bt-metric">
          <div class="label">Current Sharpe</div>
          <div class="value ${sign(r.current_sharpe)}">${r.current_sharpe.toFixed(2)}</div>
        </div>
        <div class="bt-metric">
          <div class="label">Sharpe Δ</div>
          <div class="value ${sign(imp.sharpe_delta || 0)}">${(imp.sharpe_delta||0) >= 0 ? '+' : ''}${(imp.sharpe_delta||0).toFixed(2)}</div>
        </div>
        <div class="bt-metric">
          <div class="label">Best Return</div>
          <div class="value ${sign(r.best_return_pct)}">${r.best_return_pct >= 0 ? '+' : ''}${r.best_return_pct.toFixed(2)}%</div>
        </div>
        <div class="bt-metric">
          <div class="label">Max Drawdown</div>
          <div class="value negative">-${r.best_max_drawdown.toFixed(2)}%</div>
        </div>
        <div class="bt-metric">
          <div class="label">Win Rate</div>
          <div class="value ${r.best_win_rate >= 50 ? 'positive' : 'negative'}">${r.best_win_rate.toFixed(1)}%</div>
        </div>
        <div class="bt-metric">
          <div class="label">Profit Factor</div>
          <div class="value ${(r.best_profit_factor||0) >= 1 ? 'positive' : 'negative'}">${(r.best_profit_factor||0).toFixed(2)}</div>
        </div>
        <div class="bt-metric">
          <div class="label">Trades</div>
          <div class="value neutral">${r.best_trade_count || '—'}</div>
        </div>
      </div>

      <div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px;padding:8px 10px;background:rgba(108,99,255,0.07);border-radius:8px;font-size:11px;color:var(--muted)">
        <span>🧬 <b>${ga.generations || '?'}</b> generations</span>
        <span>👥 pop <b>${ga.population_size || '?'}</b></span>
        <span>⚡ <b>${r.iterations_run}/${r.max_iterations}</b> evals</span>
        <span>🔄 <b>${ga.stagnation_restarts || 0}</b> restarts</span>
        <span>🧵 ×<b>${ga.concurrency || 1}</b> parallel</span>
        <span>⏱ <b>${r.duration_seconds}s</b></span>
      </div>

      <div style="margin-bottom:12px;font-size:12px;font-weight:600;color:var(--muted)">PARAMETER COMPARISON</div>
      <div class="bt-param-compare">
        <div class="hdr">Parameter</div>
        <div class="hdr">Current</div>
        <div class="hdr">Optimized</div>
        <div class="hdr"></div>
        ${paramRows}
      </div>

      <div style="display:flex;gap:10px;margin-top:12px">
        <button class="bt-btn bt-btn-apply" onclick="applyOptParams()">✅ Apply Optimized Params</button>
        <button class="bt-btn bt-btn-run" onclick="runBacktestWithOpt()" style="font-size:11px;padding:5px 12px">▶ Backtest with Optimized</button>
      </div>
    </div>`;
}

async function applyOptParams() {
  if (!_lastOptResult || !selectedBot) return;
  const params = _lastOptResult.best_params;
  try {
    const resp = await put(`${API}/bots/${selectedBot}/params`, params);
    if (resp.ok) {
      showToast(`✓ Applied ${Object.keys(resp.data.applied).length} optimized param(s)`, 'success');
      await loadParams(selectedBot);
    } else {
      showToast(`✗ ${resp.data.detail || 'Error'}`, 'error');
    }
  } catch (e) {
    showToast(`✗ ${e.message}`, 'error');
  }
}

async function runBacktestWithOpt() {
  if (!_lastOptResult || !selectedBot) return;
  const btn = document.getElementById('bt-run-btn');
  const status = document.getElementById('bt-status');
  btn.disabled = true;
  status.textContent = '⏳ Running backtest with optimized params...';

  try {
    const resp = await postJson(`${API}/backtest/run`, {
      bot_id: selectedBot,
      params: _lastOptResult.best_params,
    });
    if (resp.ok) {
      status.textContent = `✓ ${resp.data.candles_processed} candles in ${resp.data.duration_seconds}s (optimized params)`;
      renderBacktestResults(resp.data);
    } else {
      status.textContent = `✗ ${resp.data.detail || 'Error'}`;
    }
  } catch (e) {
    status.textContent = `✗ ${e.message}`;
  }
  btn.disabled = false;
}

// ── LLM Agent panel ────────────────────────────────────────────
let _llmEnabled = false;

async function loadLLMStatus() {
  try {
    const s = await get(`${API}/llm/status`);
    _llmEnabled = s.enabled;

    // Status dot
    const dot = document.getElementById('llm-dot');
    dot.className = `llm-status-dot ${s.enabled ? 'on' : 'off'}`;

    // Text fields
    document.getElementById('llm-status-text').textContent =
      s.enabled ? 'Active' : (s.config_enabled ? 'Paused' : 'Disabled');
    document.getElementById('llm-model').textContent = s.model || '—';
    document.getElementById('llm-interval').textContent = s.interval_minutes ? `${s.interval_minutes} min` : '—';
    document.getElementById('llm-mode').textContent = s.dry_run ? '🧪 Dry-run' : '🔴 Live';

    // Toggle button
    const btn = document.getElementById('llm-toggle-btn');
    if (s.enabled) {
      btn.textContent = '⏸ Disable';
      btn.className = 'btn btn-stop';
      btn.style.cssText = 'font-size:11px;padding:5px 12px';
    } else {
      btn.textContent = '▶ Enable';
      btn.className = 'btn btn-start';
      btn.style.cssText = 'font-size:11px;padding:5px 12px';
    }

    // Trigger button — only if API key present
    document.getElementById('llm-trigger-btn').disabled = !s.has_api_key;

    // Last decision
    const lastWrap = document.getElementById('llm-last-decision');
    if (s.last_decision) {
      lastWrap.style.display = 'block';
      document.getElementById('llm-last-ts').textContent = new Date(s.last_decision.timestamp).toLocaleString();

      // Parse response
      let reasoning = '';
      let actionsText = '';
      try {
        const resp = JSON.parse(s.last_decision.response_json || '{}');
        reasoning = resp.reasoning || '';
        const acts = resp.actions || [];
        actionsText = acts.length > 0
          ? acts.map(a => `${a.type}: ${a.bot_id}${a.params ? ' → ' + JSON.stringify(a.params) : ''}`).join('; ')
          : 'No actions taken';
      } catch {
        reasoning = s.last_decision.error_message || 'Error parsing response';
        actionsText = '';
      }
      document.getElementById('llm-last-reasoning').textContent = reasoning;
      document.getElementById('llm-last-actions').textContent = actionsText;

      // Show log section
      document.getElementById('llm-log-wrap').style.display = 'block';
    } else {
      lastWrap.style.display = 'none';
      document.getElementById('llm-log-wrap').style.display = s.enabled ? 'block' : 'none';
    }
  } catch (e) {
    console.warn('loadLLMStatus', e);
  }
}

async function toggleLLM() {
  const action = _llmEnabled ? 'disable' : 'enable';
  try {
    await post(`${API}/llm/${action}`);
  } catch (e) {
    console.error('toggleLLM', e);
  }
  await loadLLMStatus();
}

async function triggerLLM() {
  const btn = document.getElementById('llm-trigger-btn');
  btn.disabled = true;
  btn.textContent = '⏳ Running…';
  try {
    await post(`${API}/llm/trigger`);
  } catch (e) {
    console.error('triggerLLM', e);
  }
  btn.textContent = '⚡ Run Now';
  btn.disabled = false;
  await loadLLMStatus();
}

async function loadLLMLog() {
  try {
    const entries = await get(`${API}/llm/log?limit=10`);
    const container = document.getElementById('llm-log-entries');
    container.innerHTML = '';

    if (entries.length === 0) {
      container.innerHTML = '<div style="color:var(--muted);font-size:11px;padding:8px">No decisions yet</div>';
      return;
    }

    entries.forEach(d => {
      let reasoning = '';
      let actionsText = '';
      let success = d.success;

      if (d.error_message) {
        reasoning = '❌ ' + d.error_message;
      } else {
        try {
          const resp = JSON.parse(d.response_json || '{}');
          reasoning = resp.reasoning || '';
          const acts = resp.actions || [];
          actionsText = acts.length > 0
            ? acts.map(a => `${a.type}: ${a.bot_id}${a.params ? ' → ' + JSON.stringify(a.params) : ''}`).join('; ')
            : 'No actions';
        } catch {
          reasoning = 'Parse error';
        }
      }

      // Parse action results
      let resultsText = '';
      try {
        const results = JSON.parse(d.actions_taken || '[]');
        if (results.length > 0) resultsText = results.join(' | ');
      } catch {}

      const entry = document.createElement('div');
      entry.className = 'llm-log-entry';
      entry.innerHTML = `
        <div class="ts">${new Date(d.timestamp).toLocaleString()} · ${success ? '✅' : '❌'}</div>
        <div class="reasoning">${reasoning}</div>
        ${actionsText ? `<div class="actions">${actionsText}</div>` : ''}
        ${resultsText ? `<div class="actions" style="color:var(--accent);margin-top:2px">${resultsText}</div>` : ''}`;
      container.appendChild(entry);
    });
  } catch (e) {
    console.warn('loadLLMLog', e);
  }
}

// ── Auto-refresh loop ──────────────────────────────────────────
let _refreshInterval = null;
let _refreshSeconds = 60;

async function refresh() {
  await loadBots();
  await loadLLMStatus();
  if (selectedBot) await loadBotDetail(selectedBot);
}

function changeRefreshRate(seconds) {
  _refreshSeconds = parseInt(seconds) || 60;
  if (_refreshInterval) clearInterval(_refreshInterval);
  _refreshInterval = setInterval(refresh, _refreshSeconds * 1000);
}

// ── Reset all bots ─────────────────────────────────────────────

async function resetAllBots() {
  if (!confirm('⚠️ Reset ALL bots to default balance?\n\nThis will DELETE all trades and snapshots.\nBot parameters and historical data are kept.\n\nContinue?')) return;
  try {
    const resp = await post(`${API}/bots/reset-all`);
    showToast(`✓ ${resp.message || 'All bots reset'}`, 'success');
    await refresh();
  } catch (e) {
    showToast(`✗ ${e.message}`, 'error');
  }
}

async function resetBot(name) {
  if (!confirm(`Reset "${name}" to default balance?\n\nDeletes trades & snapshots, keeps params.`)) return;
  try {
    const resp = await post(`${API}/bots/${name}/reset`);
    showToast(`✓ ${resp.message || 'Bot reset'}`, 'success');
    await refresh();
    if (selectedBot === name) await loadBotDetail(name);
  } catch (e) {
    showToast(`✗ ${e.message}`, 'error');
  }
}

// ── Bootstrap ──────────────────────────────────────────────────
refresh();
loadDataStatus();  // show data status in sidebar on load
_refreshInterval = setInterval(refresh, _refreshSeconds * 1000);
