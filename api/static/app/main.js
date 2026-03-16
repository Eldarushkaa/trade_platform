// ── Auto-refresh loop ──────────────────────────────────────────
async function refresh() {
  await loadBots();
  if (selectedBot) await loadBotDetail(selectedBot);
  loadLogs();  // refresh log badge count silently
}

function changeRefreshRate(seconds) {
  _refreshSeconds = parseInt(seconds) || 60;
  if (_refreshInterval) clearInterval(_refreshInterval);
  _refreshInterval = setInterval(refresh, _refreshSeconds * 1000);
}

// ── Reset all / single bot ──────────────────────────────────────
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

// ── Moscow clock ───────────────────────────────────────────────
function _tickMskClock() {
  const el = document.getElementById('msk-clock');
  if (!el) return;
  const now = new Date();
  const time = now.toLocaleTimeString('ru-RU', {
    timeZone: 'Europe/Moscow',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
  el.textContent = `🕐 ${time} МСК`;
}
_tickMskClock();
setInterval(_tickMskClock, 1000);

// ── Bootstrap ──────────────────────────────────────────────────
refresh();
loadDataStatus();       // show data status in sidebar on load
loadLogs();             // populate log badge count on load
_refreshInterval = setInterval(refresh, _refreshSeconds * 1000);
