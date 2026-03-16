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
