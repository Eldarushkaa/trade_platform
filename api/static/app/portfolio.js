// ── Portfolio panel ────────────────────────────────────────────
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
    // Include current unrealized PnL in displayed figures
    const unrealized = p.unrealized_pnl || 0;
    const periodNetWithUnrealized = s.realized_pnl + unrealized;
    const periodAfterFeesWithUnrealized = s.realized_pnl - s.total_fees_paid + unrealized;
    const winRate = (s.win_count + s.loss_count) > 0
      ? (s.win_count / (s.win_count + s.loss_count) * 100).toFixed(1) + '%'
      : '—';
    grid.innerHTML = `
      <div class="stat-card">
        <div class="label">Net P&L (${periodLabel})</div>
        <div class="value ${sign(periodNetWithUnrealized)}">${periodNetWithUnrealized >= 0 ? '+' : ''}$${fmt(periodNetWithUnrealized)}</div>
      </div>
      <div class="stat-card">
        <div class="label">Realized (${periodLabel})</div>
        <div class="value ${sign(s.realized_pnl)}">${s.realized_pnl >= 0 ? '+' : ''}$${fmt(s.realized_pnl)}</div>
      </div>
      <div class="stat-card">
        <div class="label">Unrealized now</div>
        <div class="value ${sign(unrealized)}">${unrealized >= 0 ? '+' : ''}$${fmt(unrealized)}</div>
      </div>
      <div class="stat-card">
        <div class="label">After fees (${periodLabel})</div>
        <div class="value ${sign(periodAfterFeesWithUnrealized)}">${periodAfterFeesWithUnrealized >= 0 ? '+' : ''}$${fmt(periodAfterFeesWithUnrealized)}</div>
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

// ── History chart ──────────────────────────────────────────────
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

// Parse a snapshot/trade timestamp correctly as UTC regardless of whether it has a Z suffix
function _tsMs(ts) {
  if (!ts) return 0;
  // If string lacks timezone info, append 'Z' so JS parses it as UTC (not local time)
  const s = (typeof ts === 'string' && !ts.endsWith('Z') && !ts.includes('+')) ? ts + 'Z' : ts;
  return new Date(s).getTime();
}

function _renderChart(snaps, trades, windowMs) {
  // Apply time window filter — use _tsMs to avoid UTC vs local bug
  let visSnaps = snaps;
  if (windowMs) {
    const cutoff = Date.now() - windowMs;
    const filtered = snaps.filter(s => _tsMs(s.timestamp) >= cutoff);
    // Use filtered results even if few — don't fall back to old data outside the window
    visSnaps = filtered.length > 0 ? filtered : [];
  }

  // If nothing to show, clear chart and return
  if (visSnaps.length === 0) {
    const ctx = document.getElementById('portfolio-chart').getContext('2d');
    if (portfolioChart) portfolioChart.destroy();
    portfolioChart = null;
    // Draw a placeholder message
    ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
    ctx.fillStyle = '#8892a4';
    ctx.font = '14px Segoe UI, system-ui, sans-serif';
    ctx.textAlign = 'center';
    const windowLabel = windowMs === 3 * 3600000 ? '3 hours' : windowMs === 24 * 3600000 ? '24 hours' : '';
    ctx.fillText(`No data in the last ${windowLabel}`, ctx.canvas.width / 2, ctx.canvas.height / 2);
    return;
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
    const d = new Date(_tsMs(s.timestamp));

    if (i > 0) {
      const prev = new Date(_tsMs(visSnaps[i-1].timestamp));
      if ((d - prev) > gapThreshold) {
        const midMs = prev.getTime() + (d - prev) / 2;
        labels.push(fmtTime(new Date(midMs)));
        values.push(null); usdtValues.push(null); coinValues.push(null);
        prices.push(null); snapTimestamps.push(midMs);
      }
    }

    const total = s.total_value_usdt;
    const usdtBal = s.usdt_balance ?? total;
    // For futures: "coin value" = total - free_usdt (= margin locked + unrealized PnL).
    // Only show as positive fill when there's an open position (asset_balance > 0).
    // This avoids spikes at LONG→SHORT transitions where coinVal would otherwise
    // jump from positive unrealized to negative and back.
    const hasPosition = (s.asset_balance != null ? Math.abs(s.asset_balance) : 0) > 1e-8;
    const coinVal = hasPosition ? Math.max(0, total - usdtBal) : 0;

    labels.push(fmtTime(d));
    values.push(total);
    usdtValues.push(usdtBal);
    coinValues.push(coinVal);
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
      const tMs = _tsMs(t.timestamp);
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
          label: 'Margin / Unrealized', data: coinValues,
          borderColor: 'rgba(0,200,150,0.6)', backgroundColor: 'rgba(0,200,150,0.15)',
          borderWidth: 0, pointRadius: 0, fill: 'origin', tension: 0.3, spanGaps: true,
          yAxisID: 'yPortfolio', order: 3,
        },
        {
          label: 'USDT Balance', data: usdtValues,
          borderColor: 'rgba(108,99,255,0.6)', backgroundColor: 'rgba(108,99,255,0.2)',
          borderWidth: 0, pointRadius: 0, fill: 'origin', tension: 0.3, spanGaps: true,
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

// ── Trades table ───────────────────────────────────────────────
async function loadTrades(name) {
  const tbody = document.getElementById('trades-body');
  try {
    const trades = await get(`${API}/trades/${name}?limit=50`);
    tbody.innerHTML = '';

    if (!Array.isArray(trades) || trades.length === 0) {
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
      const actionClass = action.replace(/_/g, '-').toLowerCase();
      const actionLabel = action.replace(/_/g, ' ');

      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>#${t.id}</td>
        <td><span class="action-badge ${actionClass}">${actionLabel}</span></td>
        <td class="${t.side.toLowerCase()}">${t.side}</td>
        <td>$${fmt(t.price)}</td>
        <td>${(t.quantity || 0).toFixed(6)}</td>
        <td>${pnlStr}</td>
        <td style="color:var(--muted)">${fmtMoscow(t.timestamp)} МСК</td>`;
      tbody.appendChild(tr);
    });
  } catch (e) {
    console.error('loadTrades error:', e);
    if (tbody) {
      tbody.innerHTML = `<tr><td colspan="7" style="color:var(--red);text-align:center;padding:20px">Failed to load trades: ${e.message}</td></tr>`;
    }
  }
}
