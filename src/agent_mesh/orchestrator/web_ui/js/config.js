async function loadConfig() {
  try {
    const res = await fetch('/api/settings', { headers });
    if (!res.ok) return;
    const settings = (await res.json()).settings;

    const publicUrlInput = document.getElementById('public-url');
    if (document.activeElement !== publicUrlInput) {
      publicUrlInput.value = settings.public_url || '';
    }

    const modelInput = document.getElementById('llm-model');
    if (document.activeElement !== modelInput) modelInput.value = settings.llm_model || '';

    const keyInput = document.getElementById('llm-api-key');
    if (document.activeElement !== keyInput) keyInput.value = settings.llm_api_key || '';

    const baseInput = document.getElementById('llm-base-url');
    if (document.activeElement !== baseInput) baseInput.value = settings.llm_base_url || '';

    const modelsInput = document.getElementById('llm-models');
    if (document.activeElement !== modelsInput) modelsInput.value = settings.llm_models || '';
    // Expose the configured model ids for the template model dropdown.
    window.AVAILABLE_MODELS = (settings.llm_models || '')
      .split(/[,\n]/)
      .map(s => s.trim())
      .filter(Boolean);

    const autoUp = document.getElementById('auto-upgrade');
    autoUp.checked = (settings.auto_upgrade ?? '1') !== '0';

    const mcInput = document.getElementById('max-concurrent');
    if (document.activeElement !== mcInput) mcInput.value = settings.max_concurrent || '2';

    const pollInput = document.getElementById('task-log-poll');
    if (document.activeElement !== pollInput) pollInput.value = settings.task_log_poll_interval || '5';

    window.TASK_LOG_POLL_S = parseInt(settings.task_log_poll_interval, 10) || 5;
  } catch (e) {
    console.error('load config failed', e);
  }
}

async function savePublicUrl() {
  const url = document.getElementById('public-url').value.trim();
  const statusEl = document.getElementById('public-url-status');
  try {
    await patchSettings({ public_url: url });
    statusEl.textContent = '已保存';
    statusEl.style.color = '';
  } catch (e) {
    statusEl.textContent = '保存失败：' + e.message;
    statusEl.style.color = 'var(--danger)';
  }
  setTimeout(() => statusEl.textContent = '', 3000);
}

async function saveLlmConfig() {
  const statusEl = document.getElementById('llm-config-status');
  try {
    await patchSettings({
      llm_model: document.getElementById('llm-model').value.trim(),
      llm_api_key: document.getElementById('llm-api-key').value.trim(),
      llm_base_url: document.getElementById('llm-base-url').value.trim(),
    });
    statusEl.textContent = '已保存';
    statusEl.style.color = '';
  } catch (e) {
    statusEl.textContent = '保存失败：' + e.message;
    statusEl.style.color = 'var(--danger)';
  }
  setTimeout(() => statusEl.textContent = '', 3000);
}

async function saveLlmModels() {
  const statusEl = document.getElementById('llm-models-status');
  try {
    await patchSettings({ llm_models: document.getElementById('llm-models').value.trim() });
    statusEl.textContent = '已保存，将随心跳同步到节点';
    statusEl.style.color = '';
  } catch (e) {
    statusEl.textContent = '保存失败：' + e.message;
    statusEl.style.color = 'var(--danger)';
  }
  setTimeout(() => statusEl.textContent = '', 3000);
}

async function patchSettings(body) {
  const res = await fetch('/api/settings', {
    method: 'PATCH',
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (data && data.detail) detail = String(data.detail);
    } catch (_) { /* ignore body parse errors */ }
    throw new Error(detail);
  }
  return res.json();
}

async function saveAutoUpgrade(checkbox) {
  const el = document.getElementById('auto-upgrade-status');
  try {
    await patchSettings({ auto_upgrade: checkbox.checked ? '1' : '0' });
    el.textContent = checkbox.checked ? '已开启自动升级' : '已关闭自动升级';
    el.style.color = '';
  } catch (e) {
    el.textContent = '保存失败：' + e.message;
    el.style.color = 'var(--danger)';
  }
  setTimeout(() => el.textContent = '', 3000);
}

async function saveMaxConcurrent() {
  const value = parseInt(document.getElementById('max-concurrent').value, 10);
  const n = Number.isFinite(value) ? Math.max(1, Math.min(32, value)) : 2;
  const el = document.getElementById('max-concurrent-status');
  try {
    await patchSettings({ max_concurrent: String(n) });
    document.getElementById('max-concurrent').value = String(n);
    el.textContent = '已保存，将随下一次心跳同步到节点';
    el.style.color = '';
  } catch (e) {
    el.textContent = '保存失败：' + e.message;
    el.style.color = 'var(--danger)';
  }
  setTimeout(() => el.textContent = '', 3000);
}

async function saveTaskLogPoll() {
  const value = parseInt(document.getElementById('task-log-poll').value, 10);
  const n = Number.isFinite(value) ? Math.max(1, Math.min(60, value)) : 5;
  const el = document.getElementById('task-log-poll-status');
  try {
    await patchSettings({ task_log_poll_interval: String(n) });
    document.getElementById('task-log-poll').value = String(n);
    window.TASK_LOG_POLL_S = n;
    el.textContent = '已保存（刷新间隔 ' + n + ' 秒）';
    el.style.color = '';
  } catch (e) {
    el.textContent = '保存失败：' + e.message;
    el.style.color = 'var(--danger)';
  }
  setTimeout(() => el.textContent = '', 3000);
}

async function downloadAgentMeshSkill() {
  const statusEl = document.getElementById('skill-doc-status');
  try {
    const res = await fetch('/api/skill-doc/agent-mesh', { headers });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'agent-mesh-SKILL.md';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    statusEl.textContent = '已开始下载 SKILL.md';
    statusEl.style.color = '';
  } catch (e) {
    statusEl.textContent = '下载失败：' + e.message;
    statusEl.style.color = 'var(--danger)';
  }
  setTimeout(() => statusEl.textContent = '', 3000);
}
