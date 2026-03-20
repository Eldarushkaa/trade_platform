// ── Sidebar: list all bots ─────────────────────────────────────
async function loadBots() {
  try {
    const [bots, portfolios] = await Promise.all([
      get(`${API}/bots`),
      get(`${API}/portfolio/all`).catch(() => []),
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

      const liveEnabled = !!bot.live_enabled;

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
          <label class="live-toggle" title="${liveEnabled ? 'Live ON — bot will auto-start on server restart' : 'Live OFF — bot stays paused on restart'}" onclick="event.stopPropagation()">
            <input type="checkbox" ${liveEnabled ? 'checked' : ''} onchange="toggleBotLive('${bot.name}', this.checked)" />
            <span class="live-toggle-slider"></span>
            <span class="live-toggle-label">${liveEnabled ? 'Live' : 'Off'}</span>
          </label>
        </div>`;
      card.addEventListener('click', () => selectBot(bot.name));
      container.appendChild(card);
    });

    // Cache bots list for use in loadDataStatus (symbol lookup)
    _botsCache = bots;

    // Populate the standalone backtest panel bot selector
    if (typeof populateBtBotSelect === 'function') populateBtBotSelect(bots);

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
    _globalStatsData = { portfolios, bots, periodStats };

    // Render global stats bar with current mode
    renderGlobalStats(portfolios, bots, periodStats);
  } catch (e) {
    console.error('loadBots error', e);
  }
}

// ── Global stats bar ───────────────────────────────────────────
// Design contract:
//   • "Matrix" cells always show return_pct (current bot state, same as sidebar — never period-filtered)
//   • "Overall Return %" is period-specific:
//       all-time → avg(return_pct)   [authoritative server value, includes unrealized & fees]
//       24h / 3h → avg( (period_realized - period_fees + unrealized) / initial_balance × 100 )
//   • "P&L" uses the SAME formula across all modes:
//       period_realized - period_fees + unrealized
//       (all-time: period = all trades = portfolio.realized_pnl, portfolio.total_fees_paid)
//   • "Trades" and "Fees" are period sums
function renderGlobalStats(portfolios, bots, periodStats) {
  const bar = document.getElementById('global-stats-bar');
  if (!bar || portfolios.length === 0) return;

  // --- Helper: initial balance from current value and all-time return_pct ---
  function initialBal(p) {
    const rp = p.return_pct || 0;
    return rp !== -100
      ? (p.total_value_usdt || 0) / (1 + rp / 100)
      : (p.total_value_usdt || 0);
  }

  // Portfolio lookup
  const portMap = {};
  portfolios.forEach(p => { portMap[p.bot_id] = p; });

  const totalUSDT  = portfolios.reduce((s, p) => s + (p.usdt_balance || 0), 0);
  const totalValue = portfolios.reduce((s, p) => s + (p.total_value_usdt || 0), 0);
  // Unrealized is always current (live from portfolio state)
  const totalUnrealized = portfolios.reduce((s, p) => s + (p.unrealized_pnl || 0), 0);
  const periodLabel = _statsMode === '3h' ? 'Last 3h' : _statsMode === '24h' ? 'Last 24h' : 'All time';
  const key = _statsMode === '3h' ? 'h3' : _statsMode === '24h' ? 'h24' : null;

  // --- Period-specific totals ---
  let totalTrades, totalFees, totalPeriodRealized;
  if (key && periodStats) {
    totalTrades         = portfolios.reduce((s, p) => s + (periodStats[p.bot_id]?.[key]?.trade_count || 0), 0);
    totalFees           = portfolios.reduce((s, p) => s + (periodStats[p.bot_id]?.[key]?.total_fees_paid || 0), 0);
    totalPeriodRealized = portfolios.reduce((s, p) => s + (periodStats[p.bot_id]?.[key]?.realized_pnl || 0), 0);
  } else {
    totalTrades         = portfolios.reduce((s, p) => s + (p.trade_count || 0), 0);
    totalFees           = portfolios.reduce((s, p) => s + (p.total_fees_paid || 0), 0);
    totalPeriodRealized = portfolios.reduce((s, p) => s + (p.realized_pnl || 0), 0);
  }

  // Net P&L: same formula all modes — realized(period) - fees(period) + unrealized(current)
  const totalPnl = totalPeriodRealized - totalFees + totalUnrealized;

  // Overall Return %: period-specific average across bots
  let overallReturn;
  if (key && periodStats) {
    const returns = portfolios.map(p => {
      const ps = periodStats[p.bot_id]?.[key];
      const realized = ps?.realized_pnl || 0;
      const fees     = ps?.total_fees_paid || 0;
      const unrealized = p.unrealized_pnl || 0;
      const init = initialBal(p);
      return init > 0 ? ((realized - fees + unrealized) / init * 100) : 0;
    });
    overallReturn = returns.reduce((s, r) => s + r, 0) / returns.length;
  } else {
    overallReturn = portfolios.reduce((s, p) => s + (p.return_pct || 0), 0) / portfolios.length;
  }

  // Profitable bots = return_pct > 0 (always current snapshot, matches sidebar)
  const positiveCount = portfolios.filter(p => (p.return_pct || 0) > 0).length;

  const returnColor = overallReturn >= 0 ? 'var(--green)' : 'var(--red)';
  const pnlColor    = totalPnl >= 0 ? 'var(--green)' : 'var(--red)';

  // --- Matrix: strategy × coin grid ---
  const strategies = [...new Set(bots.map(b => b.name.split('_').slice(0,-1).join('_')))].sort();
  const symbols    = [...new Set(bots.map(b => b.name.split('_').at(-1).toUpperCase()))].sort();

  let matrixHTML = `<div class="gs-matrix">`;
  matrixHTML += `<div class="gs-matrix-cell gs-matrix-hdr"></div>`;
  symbols.forEach(sym => {
    matrixHTML += `<div class="gs-matrix-cell gs-matrix-hdr">${sym}</div>`;
  });
  strategies.forEach(strat => {
    matrixHTML += `<div class="gs-matrix-cell gs-matrix-hdr" style="font-size:9px">${strat.toUpperCase()}</div>`;
    symbols.forEach(sym => {
      const p = portMap[`${strat}_${sym.toLowerCase()}`];
      let cellClass = 'gs-matrix-cell gs-cell-neutral';
      let retStr = '—';
      if (p) {
        const ret = p.return_pct || 0;
        cellClass = `gs-matrix-cell ${ret >= 0 ? 'gs-cell-green' : 'gs-cell-red'}`;
        retStr = (ret >= 0 ? '+' : '') + ret.toFixed(1) + '%';
      }
      matrixHTML += `<div class="${cellClass}" title="${strat}_${sym.toLowerCase()}">${retStr}</div>`;
    });
  });
  matrixHTML += `</div>`;

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
      <div class="gs-value" style="color:${positiveCount > 0 ? 'var(--green)' : 'var(--muted)'}">
        ${positiveCount} / ${portfolios.length}
      </div>
    </div>
    <div class="gs-stat">
      <div class="gs-label">Fees (${periodLabel})</div>
      <div class="gs-value" style="color:var(--yellow)">$${fmt(totalFees)}</div>
    </div>
    ${matrixHTML}
  `;
}

// ── Bot control ────────────────────────────────────────────────
async function controlBot(name, action) {
  await post(`${API}/bots/${name}/${action}`);
  await loadBots();
  if (selectedBot === name) await loadBotDetail(name);
}

// ── Live enable/disable toggle ─────────────────────────────────
async function toggleBotLive(name, enabled) {
  try {
    await patch(`${API}/bots/${name}/live`, { enabled });
    // Update label without full reload for snappy UX
    const card = document.querySelector(`.bot-card[data-name="${name}"]`);
    if (card) {
      const label = card.querySelector('.live-toggle-label');
      if (label) label.textContent = enabled ? 'Live' : 'Off';
      const toggle = card.querySelector('.live-toggle');
      if (toggle) toggle.title = enabled
        ? 'Live ON — bot will auto-start on server restart'
        : 'Live OFF — bot stays paused on restart';
    }
  } catch (e) {
    console.error('toggleBotLive error', e);
    await loadBots(); // refresh on error to restore correct state
  }
}

// ── Main panel: bot detail ─────────────────────────────────────
function selectBot(name) {
  selectedBot = name;
  document.getElementById('no-bot').style.display = 'none';
  document.getElementById('bot-detail').style.display = 'block';
  document.getElementById('detail-title').textContent = name;

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

// ── Period toggle ──────────────────────────────────────────────
function toggleStatsMode(mode) {
  _statsMode = mode;
  // Sync button active states
  ['all', '24h', '3h'].forEach(m => {
    const btn = document.getElementById(`ptbtn-${m}`);
    if (btn) btn.classList.toggle('active', m === mode);
  });
  // Re-render global stats bar with period-aware numbers
  if (_globalStatsData) {
    const { portfolios, bots, periodStats } = _globalStatsData;
    renderGlobalStats(portfolios, bots, periodStats);
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
