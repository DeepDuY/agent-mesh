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

    const permInput = document.getElementById('default-permission');
    if (document.activeElement !== permInput) {
      permInput.value = settings.default_permission || '';
    }

    const mcInput = document.getElementById('max-concurrent');
    if (document.activeElement !== mcInput) mcInput.value = settings.max_concurrent || '2';

    const pollInput = document.getElementById('task-log-poll');
    if (document.activeElement !== pollInput) pollInput.value = settings.task_log_poll_interval || '5';

    window.TASK_LOG_POLL_S = parseInt(settings.task_log_poll_interval, 10) || 5;

    const fileMax = document.getElementById('file-max-size-mb');
    if (fileMax && document.activeElement !== fileMax) fileMax.value = settings.file_max_size_mb || '100';
    const artMax = document.getElementById('artifact-max-size-mb');
    if (artMax && document.activeElement !== artMax) artMax.value = settings.artifact_max_size_mb || '100';
    const artTask = document.getElementById('artifact-task-total-mb');
    if (artTask && document.activeElement !== artTask) artTask.value = settings.artifact_task_total_mb || '100';
    const artTotal = document.getElementById('artifact-total-mb');
    if (artTotal && document.activeElement !== artTotal) artTotal.value = settings.artifact_total_mb || '200';
    const evictEl = document.getElementById('artifact-evict-oldest');
    if (evictEl) evictEl.checked = (settings.artifact_evict_oldest ?? '1') !== '0';
    const artTimeout = document.getElementById('artifact-timeout-s');
    if (artTimeout && document.activeElement !== artTimeout) artTimeout.value = settings.artifact_timeout_s || '300';
    window.ARTIFACT_TIMEOUT_S = parseInt(settings.artifact_timeout_s, 10) || 300;

    loadVersionInfo();
  } catch (e) {
    console.error('load config failed', e);
  }
}

async function loadVersionInfo() {
  try {
    const res = await fetch('/api/bootstrap/info', { headers });
    if (!res.ok) return;
    const data = await res.json();
    const serverEl = document.getElementById('server-version');
    const probeEl = document.getElementById('probe-version');
    if (serverEl) serverEl.textContent = data.server_version || '-';
    if (probeEl) probeEl.textContent = data.probe_version || '(未发布)';
  } catch (e) {
    /* ignore transient errors */
  }
}

async function uploadProbe(input) {
  const statusEl = document.getElementById('probe-upload-status');
  const file = input.files && input.files[0];
  if (!file) return;
  if (!confirm(`确认上传并发布探针包 ${file.name}？\n新节点安装与节点升级将使用该版本。`)) {
    input.value = '';
    return;
  }
  statusEl.textContent = '上传中…（大文件请稍候）';
  statusEl.style.color = '';
  const form = new FormData();
  form.append('file', file);
  try {
    const res = await fetch('/api/bootstrap', { method: 'POST', headers, body: form });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    statusEl.textContent = `已发布 ${data.filename}（版本 ${data.probe_version}）`;
    statusEl.style.color = 'var(--ok)';
    loadVersionInfo();
  } catch (e) {
    statusEl.textContent = '上传失败：' + e.message;
    statusEl.style.color = 'var(--danger)';
  } finally {
    input.value = '';
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
    statusEl.textContent = '已保存';
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
    el.textContent = '已保存';
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

function applyDefaultPermissionPreset(name) {
  if (!name) return;
  document.getElementById('default-permission').value =
    JSON.stringify(PERMISSION_PRESETS[name], null, 2);
}

async function saveDefaultPermission() {
  const el = document.getElementById('default-permission-status');
  const raw = document.getElementById('default-permission').value.trim();
  if (raw) {
    try {
      const obj = JSON.parse(raw);
      if (typeof obj !== 'object' || Array.isArray(obj) || obj === null) {
        throw new Error('必须是对象');
      }
    } catch (e) {
      el.textContent = 'permission 需为 JSON 对象，如 {"*":"deny"}';
      el.style.color = 'var(--danger)';
      return;
    }
  }
  try {
    await patchSettings({ default_permission: raw });
    el.textContent = '已保存';
    el.style.color = '';
  } catch (e) {
    el.textContent = '保存失败：' + e.message;
    el.style.color = 'var(--danger)';
  }
  setTimeout(() => el.textContent = '', 3000);
}

async function saveUploadLimits() {
  const el = document.getElementById('upload-limits-status');
  const clamp = (id, def, min, max) => {
    const v = parseInt(document.getElementById(id).value, 10);
    const n = Number.isFinite(v) ? Math.max(min, Math.min(max, v)) : def;
    document.getElementById(id).value = String(n);
    return n;
  };
  try {
    await patchSettings({
      file_max_size_mb: String(clamp('file-max-size-mb', 100, 1, 102400)),
      artifact_max_size_mb: String(clamp('artifact-max-size-mb', 100, 1, 102400)),
      artifact_task_total_mb: String(clamp('artifact-task-total-mb', 100, 1, 1048576)),
      artifact_total_mb: String(clamp('artifact-total-mb', 200, 1, 1048576)),
      artifact_evict_oldest: document.getElementById('artifact-evict-oldest').checked ? '1' : '0',
      artifact_timeout_s: String(clamp('artifact-timeout-s', 300, 1, 86400)),
    });
    window.ARTIFACT_TIMEOUT_S = parseInt(document.getElementById('artifact-timeout-s').value, 10) || 300;
    el.textContent = '已保存';
    el.style.color = '';
  } catch (e) {
    el.textContent = '保存失败：' + e.message;
    el.style.color = 'var(--danger)';
  }
  setTimeout(() => el.textContent = '', 3000);
}
