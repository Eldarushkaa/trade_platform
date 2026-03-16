// ── Backtest & Optimization ────────────────────────────────────

function toggleBacktest() {
  _backtestOpen = !_backtestOpen;
  document.getElementById('backtest-body').classList.toggle('open', _backtestOpen);
  document.getElementById('bt-chevron').classList.toggle('open', _backtestOpen);
  if (_backtestOpen) loadDataStatus();
}

async function loadDataStatus() {
  try {
    const d = await get(`${API}/backtest/data-status`);

    const info = document.getElementById('bt-data-info');
    const badge = document.getElementById('bt-data-badge');
    const total = d.total_candles || 0;

    if (total > 0) {
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
      document.getElementById('bt-wfo-btn').disabled = false;
    } else {
      info.innerHTML = 'No data — download first';
      badge.textContent = '';
      document.getElementById('bt-run-btn').disabled = true;
      document.getElementById('bt-opt-btn').disabled = true;
      document.getElementById('bt-wfo-btn').disabled = true;
    }
  } catch (e) { console.warn('loadDataStatus', e); }
}

// ── Data download ──────────────────────────────────────────────
async function downloadHistory(days, startDate = null) {
  const btns = document.querySelectorAll('.bt-btn-dl');
  btns.forEach(b => { b.disabled = true; });
  const label = startDate ? `${days}d from ${startDate}` : `${days}d`;
  document.getElementById('bt-data-info').textContent = `Downloading ${label} of 5m data...`;

  try {
    const body = { days };
    if (startDate) body.start_date = startDate;
    const resp = await postJson(`${API}/backtest/download`, body);
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

async function downloadTestData() {
  const dateInput = document.getElementById('bt-test-start-date');
  const startDate = dateInput ? dateInput.value : '';
  if (!startDate) {
    alert('Please pick a start date for the 14-day test window.');
    return;
  }
  // Compute end date (start + 14 days) for display / pre-fill
  const start = new Date(startDate + 'T00:00:00Z');
  const end   = new Date(start.getTime() + 14 * 86400_000);
  const endDate = end.toISOString().slice(0, 10);
  _lastTestWindow = { start: startDate, end: endDate };

  await downloadHistory(14, startDate);
}

// ── Date range helpers ─────────────────────────────────────────
function clearBtDates() {
  const s = document.getElementById('bt-start-date');
  const e = document.getElementById('bt-end-date');
  if (s) s.value = '';
  if (e) e.value = '';
}

function fillTestDates() {
  if (!_lastTestWindow) {
    alert('No test window downloaded yet. Use "📥 14d Test from:" first.');
    return;
  }
  const s = document.getElementById('bt-start-date');
  const e = document.getElementById('bt-end-date');
  if (s) s.value = _lastTestWindow.start;
  if (e) e.value = _lastTestWindow.end;
}

function _btFeeRate() {
  // Read the fee % input and return it as a decimal (e.g. 0.07% → 0.0007).
  // Falls back to null (backend uses its default) if invalid.
  const el = document.getElementById('bt-fee-pct');
  if (!el) return null;
  const v = parseFloat(el.value);
  if (isNaN(v) || v <= 0) return null;
  return v / 100;
}

// ── Run backtest ───────────────────────────────────────────────
async function runBacktest() {
  if (!selectedBot) return;
  const btn = document.getElementById('bt-run-btn');
  const status = document.getElementById('bt-status');
  btn.disabled = true;
  status.textContent = '⏳ Running backtest...';
  document.getElementById('bt-results').style.display = 'none';
  document.getElementById('bt-opt-results').style.display = 'none';

  const feeRate = _btFeeRate();
  const body = { bot_id: selectedBot, fee_rate: feeRate };

  // Optional date range filter
  const startDate = (document.getElementById('bt-start-date') || {}).value;
  const endDate   = (document.getElementById('bt-end-date')   || {}).value;
  if (startDate) body.start_date = startDate;
  if (endDate)   body.end_date   = endDate;

  try {
    const resp = await postJson(`${API}/backtest/run`, body);
    if (!resp.ok) {
      status.textContent = `✗ ${resp.data.detail || 'Error'}`;
      btn.disabled = false;
      return;
    }
    const r = resp.data;
    const rangeLabel = (startDate || endDate)
      ? ` [${startDate || '…'} → ${endDate || '…'}]`
      : '';
    status.textContent = `✓ ${r.candles_processed} candles in ${r.duration_seconds}s${rangeLabel}`;

    renderBacktestResults(r);
  } catch (e) {
    status.textContent = `✗ ${e.message}`;
  }
  btn.disabled = false;
}

// ── Render backtest results ────────────────────────────────────
function renderBacktestResults(r) {
  document.getElementById('bt-results').style.display = 'block';

  // Safe number formatter: handles null, "Infinity", "-Infinity" strings from Python safe_round()
  const _n = (v, dec = 2, fallback = '—') => {
    const n = parseFloat(v);
    if (isNaN(n)) return fallback;
    if (!isFinite(n)) return n > 0 ? '∞' : '-∞';
    return n.toFixed(dec);
  };
  // Numeric value for comparisons (never NaN/Inf → use 0 as neutral)
  const _v = v => { const n = parseFloat(v); return isFinite(n) ? n : 0; };

  // Metrics grid
  const sign = v => _v(v) >= 0 ? 'positive' : 'negative';
  const metricsEl = document.getElementById('bt-metrics');
  metricsEl.innerHTML = `
    <div class="bt-metric">
      <div class="label">Return</div>
      <div class="value ${sign(r.return_pct)}">${_v(r.return_pct) >= 0 ? '+' : ''}${_n(r.return_pct)}%</div>
    </div>
    <div class="bt-metric">
      <div class="label">Net P&L</div>
      <div class="value ${sign(r.net_pnl)}">${_v(r.net_pnl) >= 0 ? '+' : ''}$${fmt(r.net_pnl)}</div>
    </div>
    <div class="bt-metric">
      <div class="label">Sharpe Ratio</div>
      <div class="value ${_v(r.sharpe_ratio) >= 1 ? 'positive' : _v(r.sharpe_ratio) >= 0 ? 'neutral' : 'negative'}">${_n(r.sharpe_ratio)}</div>
    </div>
    <div class="bt-metric">
      <div class="label">Max Drawdown</div>
      <div class="value negative">-${_n(r.max_drawdown_pct)}%</div>
    </div>
    <div class="bt-metric">
      <div class="label">Trades (total/closed)</div>
      <div class="value neutral">${r.total_trades || r.trade_count} / ${r.trade_count}</div>
    </div>
    <div class="bt-metric">
      <div class="label">Win Rate</div>
      <div class="value ${_v(r.win_rate) >= 50 ? 'positive' : 'negative'}">${_n(r.win_rate, 1)}%</div>
    </div>
    <div class="bt-metric">
      <div class="label">Profit Factor</div>
      <div class="value ${_v(r.profit_factor) >= 1 ? 'positive' : 'negative'}">${_n(r.profit_factor)}</div>
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
    return fmtMoscow(new Date(p.time).toISOString());
  });
  const values = curve.map(p => p.value);
  const usdtValues = curve.map(p => p.usdt ?? p.value);
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
        // USDT balance area fill (always flat bottom)
        {
          label: 'USDT Balance',
          data: usdtValues,
          borderColor: 'rgba(108,99,255,0.5)',
          backgroundColor: 'rgba(108,99,255,0.15)',
          borderWidth: 0, pointRadius: 0, fill: 'origin', tension: 0.3,
          yAxisID: 'yEq',
          order: 3,
        },
        // Total value line — colored by position side, fill between usdt and total
        {
          label: 'Total Value',
          data: values,
          backgroundColor: 'transparent',
          borderWidth: 2, pointRadius: 0, fill: false, tension: 0.3,
          yAxisID: 'yEq',
          order: 1,
          segment: {
            borderColor: ctx => sideColor(sides[ctx.p0DataIndex]),
            backgroundColor: ctx => sideBg(sides[ctx.p0DataIndex]),
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

// ── Optimization ───────────────────────────────────────────────
async function runOptimize() {
  if (!selectedBot) return;
  const btn = document.getElementById('bt-opt-btn');
  const status = document.getElementById('bt-status');
  const iters = parseInt(document.getElementById('bt-opt-iters').value) || 200;
  const feeRate = _btFeeRate();
  btn.disabled = true;
  btn.textContent = '⏳ Optimizing...';
  status.textContent = `Optimization running (${iters} iterations)...`;
  document.getElementById('bt-opt-results').style.display = 'none';
  document.getElementById('bt-wfo-results').style.display = 'none';

  try {
    // Start optimization (returns task_id)
    const startResp = await postJson(`${API}/backtest/optimize`, { bot_id: selectedBot, iterations: iters, fee_rate: feeRate });
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

// ── Walk-Forward Optimization ──────────────────────────────────
async function runWalkForward() {
  if (!selectedBot) return;
  const btn = document.getElementById('bt-wfo-btn');
  const status = document.getElementById('bt-status');
  const iters = parseInt(document.getElementById('bt-opt-iters').value) || 200;
  const folds = parseInt(document.getElementById('bt-wfo-folds').value) || 4;
  const testPct = parseFloat(document.getElementById('bt-wfo-testpct').value) || 10;
  const feeRate = _btFeeRate();

  btn.disabled = true;
  btn.textContent = '⏳ WFO Running...';
  status.textContent = `Walk-Forward: ${folds} folds × ${iters} iters/fold...`;
  document.getElementById('bt-opt-results').style.display = 'none';
  document.getElementById('bt-wfo-results').style.display = 'none';

  try {
    const startResp = await postJson(`${API}/backtest/walk-forward`, {
      bot_id: selectedBot,
      n_folds: folds,
      test_pct: testPct / 100,
      iterations: iters,
      fee_rate: feeRate,
    });
    if (!startResp.ok) {
      status.textContent = `✗ ${startResp.data.detail || 'Error'}`;
      btn.disabled = false;
      btn.textContent = '📊 Walk-Forward';
      return;
    }

    const taskId = startResp.data.task_id;

    // Poll for completion
    let done = false;
    while (!done) {
      await new Promise(r => setTimeout(r, 3000));
      try {
        const poll = await get(`${API}/backtest/status?task_id=${taskId}`);
        status.textContent = `⏳ ${poll.progress?.msg || 'Running...'}`;
        if (poll.status === 'completed') {
          done = true;
          renderWFOResults(poll.result);
          status.textContent = `✓ Walk-Forward complete in ${poll.result.duration_seconds}s`;
        } else if (poll.status === 'error') {
          done = true;
          status.textContent = `✗ ${poll.error}`;
        }
      } catch (e) {
        console.warn('wfo poll error', e);
      }
    }
  } catch (e) {
    status.textContent = `✗ ${e.message}`;
  }

  btn.disabled = false;
  btn.textContent = '📊 Walk-Forward';
}

function renderWFOResults(r) {
  _lastWFOResult = r;
  const wrap = document.getElementById('bt-wfo-results');
  wrap.style.display = 'block';

  const _n = (v, dec = 2, fallback = '—') => {
    const n = parseFloat(v);
    if (isNaN(n)) return fallback;
    if (!isFinite(n)) return n > 0 ? '∞' : '-∞';
    return n.toFixed(dec);
  };
  const _v = v => { const n = parseFloat(v); return isFinite(n) ? n : 0; };
  const sign = v => _v(v) >= 0 ? 'positive' : 'negative';

  // WFE colour coding
  const wfeClass = wfe => {
    const w = _v(wfe);
    if (w >= 0.6) return 'positive';
    if (w >= 0.3) return 'neutral';
    return 'negative';
  };
  const wfeLabel = wfe => {
    const w = _v(wfe);
    if (w >= 0.6) return '✅ Good';
    if (w >= 0.3) return '⚠️ Moderate';
    return '❌ Overfit';
  };

  // Folds table rows
  const fmtMs = ms => {
    if (!ms) return '—';
    return new Date(ms).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: '2-digit' });
  };

  let foldRows = '';
  (r.folds || []).forEach(f => {
    foldRows += `
      <tr>
        <td style="color:var(--muted)">#${f.fold}</td>
        <td style="font-size:10px;color:var(--muted)">${fmtMs(f.train_start_ms)}–${fmtMs(f.train_end_ms)}</td>
        <td style="font-size:10px;color:var(--muted)">${fmtMs(f.test_start_ms)}–${fmtMs(f.test_end_ms)}</td>
        <td class="${sign(f.is_return_pct)}">${_v(f.is_return_pct) >= 0 ? '+' : ''}${_n(f.is_return_pct)}%</td>
        <td class="${sign(f.oos_return_pct)}">${_v(f.oos_return_pct) >= 0 ? '+' : ''}${_n(f.oos_return_pct)}%</td>
        <td class="${sign(f.oos_sharpe)}">${_n(f.oos_sharpe)}</td>
        <td class="${sign(f.oos_profit_factor - 1)}">${_n(f.oos_profit_factor)}</td>
        <td style="color:var(--muted)">${f.oos_trade_count || 0}</td>
        <td class="${wfeClass(f.wfe)}">${_n(f.wfe, 2)} <span style="font-size:9px">${wfeLabel(f.wfe)}</span></td>
      </tr>`;
  });

  // Summary header metrics
  const avgWfe = _v(r.avg_wfe);
  const avgWfeClass = avgWfe >= 0.6 ? 'positive' : avgWfe >= 0.3 ? 'neutral' : 'negative';
  const avgWfeTip = avgWfe >= 0.6
    ? '✅ Strategy generalises well — low overfitting'
    : avgWfe >= 0.3
      ? '⚠️ Moderate overfitting — use with caution'
      : '❌ Heavy curve-fitting — params may not work live';

  wrap.innerHTML = `
    <div class="bt-opt-results">
      <h4>📊 Walk-Forward Optimization Results</h4>

      <div class="bt-metrics" style="margin-bottom:14px">
        <div class="bt-metric">
          <div class="label">Avg WFE</div>
          <div class="value ${avgWfeClass}" title="${avgWfeTip}">${_n(r.avg_wfe, 2)}</div>
        </div>
        <div class="bt-metric">
          <div class="label">Avg OOS Return</div>
          <div class="value ${sign(r.avg_oos_return_pct)}">${_v(r.avg_oos_return_pct) >= 0 ? '+' : ''}${_n(r.avg_oos_return_pct)}%</div>
        </div>
        <div class="bt-metric">
          <div class="label">Avg OOS Sharpe</div>
          <div class="value ${sign(r.avg_oos_sharpe)}">${_n(r.avg_oos_sharpe)}</div>
        </div>
        <div class="bt-metric">
          <div class="label">Total OOS Trades</div>
          <div class="value neutral">${r.total_oos_trades || 0}</div>
        </div>
        <div class="bt-metric">
          <div class="label">Final IS Return</div>
          <div class="value ${sign(r.final_is_return_pct)}">${_v(r.final_is_return_pct) >= 0 ? '+' : ''}${_n(r.final_is_return_pct)}%</div>
        </div>
        <div class="bt-metric">
          <div class="label">Final IS Sharpe</div>
          <div class="value ${sign(r.final_is_sharpe)}">${_n(r.final_is_sharpe)}</div>
        </div>
        <div class="bt-metric">
          <div class="label">Folds</div>
          <div class="value neutral">${(r.folds || []).length} / ${r.n_folds}</div>
        </div>
        <div class="bt-metric">
          <div class="label">Duration</div>
          <div class="value neutral">${r.duration_seconds}s</div>
        </div>
      </div>

      <div style="padding:8px 12px;border-radius:6px;font-size:11px;margin-bottom:14px;
        background:${avgWfe >= 0.6 ? 'rgba(0,200,120,0.08)' : avgWfe >= 0.3 ? 'rgba(245,166,35,0.08)' : 'rgba(255,80,80,0.08)'};
        border-left:3px solid ${avgWfe >= 0.6 ? 'var(--green)' : avgWfe >= 0.3 ? 'var(--yellow)' : 'var(--red)'}">
        <b>Walk-Forward Efficiency:</b> ${avgWfeTip}
        <br><span style="color:var(--muted);font-size:10px">WFE = OOS return / IS return. &gt;0.6 good, 0.3–0.6 moderate, &lt;0.3 overfit</span>
      </div>

      <div style="margin-bottom:8px;font-size:12px;font-weight:600;color:var(--muted)">FOLD-BY-FOLD RESULTS</div>
      <div style="overflow-x:auto;margin-bottom:14px">
        <table style="width:100%;font-size:11px;border-collapse:collapse">
          <thead>
            <tr style="color:var(--muted);border-bottom:1px solid #2a2d3a">
              <th style="text-align:left;padding:4px 8px">Fold</th>
              <th style="text-align:left;padding:4px 8px">Train period</th>
              <th style="text-align:left;padding:4px 8px">Test period</th>
              <th style="text-align:right;padding:4px 8px">IS Ret</th>
              <th style="text-align:right;padding:4px 8px">OOS Ret</th>
              <th style="text-align:right;padding:4px 8px">OOS Sharpe</th>
              <th style="text-align:right;padding:4px 8px">OOS PF</th>
              <th style="text-align:right;padding:4px 8px">Trades</th>
              <th style="text-align:right;padding:4px 8px">WFE</th>
            </tr>
          </thead>
          <tbody>${foldRows}</tbody>
        </table>
      </div>

      <div style="margin-bottom:8px;font-size:12px;font-weight:600;color:var(--muted)">STITCHED OOS EQUITY CURVE</div>
      <div style="position:relative;height:220px;margin-bottom:14px">
        <canvas id="bt-wfo-chart"></canvas>
      </div>

      <div style="margin-bottom:8px;font-size:12px;font-weight:600;color:var(--muted)">FINAL PARAMS (optimized on full dataset)</div>
      <div class="bt-param-compare" style="margin-bottom:12px">
        <div class="hdr">Parameter</div>
        <div class="hdr">Value</div>
        <div class="hdr"></div>
        <div class="hdr"></div>
        ${Object.entries(r.final_params || {}).map(([k, v]) => `
          <div>${k.replace(/_/g, ' ')}</div>
          <div class="changed">${v}</div>
          <div></div>
          <div></div>
        `).join('')}
      </div>

      <div style="display:flex;gap:10px;margin-top:12px">
        <button class="bt-btn bt-btn-apply" onclick="applyWFOParams()">✅ Apply Final Params</button>
        <button class="bt-btn bt-btn-run" onclick="runBacktestWithWFO()" style="font-size:11px;padding:5px 12px">▶ Backtest with Final Params</button>
      </div>
    </div>`;

  // Render OOS equity curve
  renderWFOChart(r.oos_equity_curve || []);
}

function renderWFOChart(curve) {
  if (!curve || curve.length === 0) return;
  const ctx = document.getElementById('bt-wfo-chart');
  if (!ctx) return;

  const labels = curve.map(p => fmtMoscow(new Date(p.time).toISOString()));
  const values = curve.map(p => p.value);

  if (window._wfoChart) window._wfoChart.destroy();

  window._wfoChart = new Chart(ctx.getContext('2d'), {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'OOS Equity',
        data: values,
        borderColor: '#6c63ff',
        backgroundColor: 'rgba(108,99,255,0.12)',
        borderWidth: 2,
        pointRadius: 0,
        fill: 'origin',
        tension: 0.3,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => `$${ctx.parsed.y.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
          }
        }
      },
      scales: {
        x: { ticks: { color: '#8892a4', maxTicksLimit: 8, maxRotation: 0 }, grid: { color: '#2a2d3a' } },
        y: {
          ticks: { color: '#8892a4', callback: v => '$' + v.toLocaleString('en-US', { maximumFractionDigits: 0 }) },
          grid: { color: '#2a2d3a' }
        }
      }
    }
  });
}

async function applyWFOParams() {
  if (!_lastWFOResult || !selectedBot) return;
  const params = _lastWFOResult.final_params;
  try {
    const resp = await put(`${API}/bots/${selectedBot}/params`, params);
    if (resp.ok) {
      showToast(`✓ Applied ${Object.keys(resp.data.applied).length} walk-forward param(s)`, 'success');
      await loadParams(selectedBot);
    } else {
      showToast(`✗ ${resp.data.detail || 'Error'}`, 'error');
    }
  } catch (e) {
    showToast(`✗ ${e.message}`, 'error');
  }
}

async function runBacktestWithWFO() {
  if (!_lastWFOResult || !selectedBot) return;
  const btn = document.getElementById('bt-run-btn');
  const status = document.getElementById('bt-status');
  btn.disabled = true;
  status.textContent = '⏳ Running backtest with WFO final params...';

  try {
    const feeRate = _btFeeRate();
    const resp = await postJson(`${API}/backtest/run`, {
      bot_id: selectedBot,
      params: _lastWFOResult.final_params,
      fee_rate: feeRate,
    });
    if (resp.ok) {
      status.textContent = `✓ ${resp.data.candles_processed} candles in ${resp.data.duration_seconds}s (WFO final params)`;
      renderBacktestResults(resp.data);
    } else {
      status.textContent = `✗ ${resp.data.detail || 'Error'}`;
    }
  } catch (e) {
    status.textContent = `✗ ${e.message}`;
  }
  btn.disabled = false;
}

function renderOptResults(r) {
  _lastOptResult = r;
  const wrap = document.getElementById('bt-opt-results');
  wrap.style.display = 'block';

  // Safe number helpers (same as renderBacktestResults)
  const _n = (v, dec = 2, fallback = '—') => {
    const n = parseFloat(v);
    if (isNaN(n)) return fallback;
    if (!isFinite(n)) return n > 0 ? '∞' : '-∞';
    return n.toFixed(dec);
  };
  const _v = v => { const n = parseFloat(v); return isFinite(n) ? n : 0; };
  const sign = v => _v(v) >= 0 ? 'positive' : 'negative';

  const imp = r.improvement || {};
  const ga = r.ga_stats || {};
  const sharpeDelta = _v(imp.sharpe_delta);

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
          <div class="value ${sign(r.best_sharpe)}">${_n(r.best_sharpe)}</div>
        </div>
        <div class="bt-metric">
          <div class="label">Current Sharpe</div>
          <div class="value ${sign(r.current_sharpe)}">${_n(r.current_sharpe)}</div>
        </div>
        <div class="bt-metric">
          <div class="label">Sharpe Δ</div>
          <div class="value ${sign(sharpeDelta)}">${sharpeDelta >= 0 ? '+' : ''}${_n(sharpeDelta)}</div>
        </div>
        <div class="bt-metric">
          <div class="label">Best Return</div>
          <div class="value ${sign(r.best_return_pct)}">${_v(r.best_return_pct) >= 0 ? '+' : ''}${_n(r.best_return_pct)}%</div>
        </div>
        <div class="bt-metric">
          <div class="label">Max Drawdown</div>
          <div class="value negative">-${_n(r.best_max_drawdown)}%</div>
        </div>
        <div class="bt-metric">
          <div class="label">Win Rate</div>
          <div class="value ${_v(r.best_win_rate) >= 50 ? 'positive' : 'negative'}">${_n(r.best_win_rate, 1)}%</div>
        </div>
        <div class="bt-metric">
          <div class="label">Profit Factor</div>
          <div class="value ${_v(r.best_profit_factor) >= 1 ? 'positive' : 'negative'}">${_n(r.best_profit_factor)}</div>
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
    const feeRate = _btFeeRate();
    const resp = await postJson(`${API}/backtest/run`, {
      bot_id: selectedBot,
      params: _lastOptResult.best_params,
      fee_rate: feeRate,
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
