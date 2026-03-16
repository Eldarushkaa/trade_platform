// ── Log viewer panel ───────────────────────────────────────────
function toggleLogPanel() {
  _logPanelOpen = !_logPanelOpen;
  const body = document.getElementById('log-panel-body');
  const chevron = document.getElementById('log-chevron');
  if (body) body.classList.toggle('open', _logPanelOpen);
  if (chevron) chevron.classList.toggle('open', _logPanelOpen);
  if (_logPanelOpen) loadLogs();
}

async function loadLogs() {
  const level = document.getElementById('log-level-select')?.value || 'WARNING';
  const container = document.getElementById('log-entries');
  if (!container) return;
  try {
    const resp = await get(`${API}/logs?level=${level}&limit=200`);
    const records = resp.records || [];

    // Update badge count (always based on WARNING+)
    const countBadge = document.getElementById('log-count-badge');
    if (countBadge) {
      if (records.length > 0) {
        countBadge.textContent = records.length;
        countBadge.style.display = 'inline';
      } else {
        countBadge.style.display = 'none';
      }
    }

    if (!_logPanelOpen) return;  // don't render DOM if collapsed

    if (records.length === 0) {
      container.innerHTML = '<div style="color:var(--muted);padding:8px 0">No warnings or errors recorded</div>';
      return;
    }

    // Show newest first
    const sorted = [...records].reverse();
    container.innerHTML = sorted.map(r => {
      const lvlClass = r.level === 'ERROR' || r.level === 'CRITICAL' ? 'log-err' : 'log-warn';
      const ts = r.ts ? r.ts.slice(11, 19) : '';  // HH:MM:SS
      const fullTs = r.ts ? new Date(r.ts).toLocaleString('ru-RU', {timeZone: 'Europe/Moscow'}) : '';
      // Strip the formatted prefix from message to avoid duplication, show raw part
      const parts = r.message.split(' | ');
      const msgPart = parts.length >= 4 ? parts.slice(3).join(' | ') : r.message;
      const srcPart = r.logger || '';
      return `<div class="log-entry" title="${fullTs}">
        <span class="log-ts">${ts}</span>
        <span class="${lvlClass}">${r.level}</span>
        <span class="log-src">[${srcPart}]</span>
        <span class="log-msg">${escapeHtml(msgPart)}</span>
      </div>`;
    }).join('');
  } catch (e) {
    if (container) container.innerHTML = `<div style="color:var(--red)">Failed to load logs: ${e.message}</div>`;
  }
}

async function clearLogs() {
  try {
    await fetch(`${API}/logs`, { method: 'DELETE' });
    await loadLogs();
  } catch (e) {
    console.warn('clearLogs', e);
  }
}
