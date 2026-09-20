let taskPage = 0;
let taskPageSize = 20;
let taskTotal = 0;
let taskFilter = '';      // status
let taskMode = '';
let taskSearch = '';
let taskTimeOp = '';      // '' | 'after' | 'between' | 'before'
let taskTimeFrom = '';
let taskTimeTo = '';
const selectedTasks = new Set();
let selectAllPages = false;
let taskLogTimer = null;
let taskLogNextId = 0;
let taskLogTaskId = null;

function readTaskFilters() {
  taskFilter = document.getElementById('task-status-filter').value;
  taskMode = document.getElementById('task-mode-filter').value;
  taskSearch = document.getElementById('task-search').value.trim();
  taskTimeOp = document.getElementById('task-time-op').value;
  taskTimeFrom = document.getElementById('task-time-from').value;
  taskTimeTo = document.getElementById('task-time-to').value;
}

function applyTaskFilters() {
  readTaskFilters();
  taskPage = 0;
  selectedTasks.clear();
  selectAllPages = false;
  loadTasks();
}

function clearTaskFilters() {
  document.getElementById('task-search').value = '';
  document.getElementById('task-status-filter').value = '';
  document.getElementById('task-mode-filter').value = '';
  document.getElementById('task-time-op').value = '';
  document.getElementById('task-time-from').value = '';
  document.getElementById('task-time-to').value = '';
  toggleTimeInputs();
  applyTaskFilters();
}

function toggleTimeInputs() {
  const op = document.getElementById('task-time-op').value;
  const from = document.getElementById('task-time-from');
  const to = document.getElementById('task-time-to');
  const sep = document.getElementById('task-time-sep');
  from.style.display = (op === 'after' || op === 'between') ? '' : 'none';
  to.style.display = (op === 'before' || op === 'between') ? '' : 'none';
  sep.style.display = op === 'between' ? '' : 'none';
}

function toIso(local) {
  if (!local) return null;
  const d = new Date(local);
  return isNaN(d.getTime()) ? null : d.toISOString();
}

function taskQueryParams() {
  const params = new URLSearchParams({
    limit: taskPageSize,
    offset: taskPage * taskPageSize,
  });
  if (taskFilter) params.append('status', taskFilter);
  if (taskMode) params.append('mode', taskMode);
  if (taskSearch) params.append('search', taskSearch);
  const after = toIso(taskTimeFrom);
  const before = toIso(taskTimeTo);
  if (taskTimeOp === 'after' && after) params.append('started_after', after);
  if (taskTimeOp === 'before' && before) params.append('started_before', before);
  if (taskTimeOp === 'between') {
    if (after) params.append('started_after', after);
    if (before) params.append('started_before', before);
  }
  return params;
}

function modeBadge(mode) {
  const label = mode === 'command' ? 'shell' : 'llm';
  return `<span class="badge-mode mode-${mode || 'llm'}">${label}</span>`;
}

function formatTime(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '-';
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function selectedCount() {
  return selectAllPages ? taskTotal : selectedTasks.size;
}

function updateTaskSelectionUI() {
  const count = selectedCount();
  document.getElementById('tasks-selected-count').textContent =
    selectAllPages ? `已选全部 ${count} 项` : `已选 ${count} 项`;
  document.getElementById('tasks-batch-delete-btn').disabled = count === 0;
  const all = document.getElementById('tasks-select-all');
  const pageChecked = Array.from(document.querySelectorAll('.task-checkbox')).every(cb => cb.checked);
  const anyChecked = Array.from(document.querySelectorAll('.task-checkbox')).some(cb => cb.checked);
  all.checked = pageChecked && document.querySelectorAll('.task-checkbox').length > 0;
  all.indeterminate = anyChecked && !pageChecked;
  document.getElementById('tasks-select-all-pages').checked = selectAllPages;
}

async function loadTasks() {
  const params = taskQueryParams();

  const res = await fetch(`/api/tasks?${params}`, { headers });
  if (!res.ok) { reportApiError('加载任务失败', res); return; }
  const data = await res.json();
  const tasks = data.tasks || [];
  taskTotal = data.total || 0;
  const totalPages = Math.max(1, Math.ceil(taskTotal / taskPageSize));
  if (taskTotal === 0) {
    taskPage = 0;
  } else if (taskPage >= totalPages) {
    taskPage = totalPages - 1;
    return loadTasks();
  }
  document.getElementById('tasks-body').innerHTML = tasks.map(t => {
    const cancellable = ['queued', 'assigned', 'working'].includes(t.status);
    const checked = selectAllPages || selectedTasks.has(t.task_id);
    return `
    <tr style="cursor:pointer" onclick="showTaskDetail('${t.task_id}')">
      <td class="col-select" onclick="event.stopPropagation()">
        <input type="checkbox" class="task-checkbox" ${checked ? 'checked' : ''} onclick="toggleTaskSelect('${t.task_id}')">
      </td>
      <td class="mono">${t.task_id}</td>
      <td>${escHtml(agentDisplayName(t.agent_id))}</td>
      <td>${escHtml(t.dispatched_by || '-')}</td>
      <td title="${escHtml(t.instruction)}">${escHtml(t.instruction.slice(0, 60))}${t.instruction.length > 60 ? '...' : ''}</td>
      <td>${modeBadge(t.mode)}</td>
      <td>${badge(t.status)}</td>
      <td>${formatTime(t.started_at)}</td>
      <td>${t.result ? fmtDuration(t.result.duration_ms) : '-'}</td>
      <td>${cancellable ? `<span class="actions"><button class="btn btn-danger" onclick="event.stopPropagation();cancelTask('${t.task_id}')">终止</button></span>` : ''}</td>
    </tr>
  `}).join('') || '<tr><td colspan="10" class="empty">暂无任务</td></tr>';

  document.getElementById('task-page-info').textContent = `第 ${taskPage + 1} / ${totalPages} 页（共 ${taskTotal} 条）`;
  updateTaskSelectionUI();
}

function toggleTaskSelect(taskId) {
  if (selectAllPages) return;
  if (selectedTasks.has(taskId)) selectedTasks.delete(taskId);
  else selectedTasks.add(taskId);
  updateTaskSelectionUI();
}

function toggleSelectAllPage() {
  if (selectAllPages) {
    selectAllPages = false;
    selectedTasks.clear();
    updateTaskSelectionUI();
    loadTasks();
    return;
  }
  const rows = Array.from(document.querySelectorAll('.task-checkbox'));
  const allChecked = rows.every(cb => cb.checked);
  document.querySelectorAll('.task-checkbox').forEach(cb => {
    const taskId = cb.closest('tr').querySelector('.mono').textContent;
    if (allChecked) selectedTasks.delete(taskId);
    else selectedTasks.add(taskId);
  });
  updateTaskSelectionUI();
  loadTasks();
}

function toggleSelectAllPages() {
  selectAllPages = document.getElementById('tasks-select-all-pages').checked;
  if (selectAllPages) selectedTasks.clear();
  updateTaskSelectionUI();
  loadTasks();
}

async function batchDeleteTasks() {
  const count = selectedCount();
  if (count === 0) return alert('请先选择要删除的任务');
  if (!confirm(`确认删除 ${count} 条任务？此操作不可恢复。`)) return;
  const body = selectAllPages
    ? {
        all_matching: true,
        status: taskFilter || null,
        mode: taskMode || null,
        search: taskSearch || null,
        started_after: toIso(taskTimeFrom),
        started_before: toIso(taskTimeTo),
        agent_id: null,
      }
    : { task_ids: Array.from(selectedTasks) };
  try {
    const res = await fetch('/api/tasks/batch-delete', {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`删除失败：${err.detail || res.status}`);
      return;
    }
    const data = await res.json();
    alert(`已删除 ${data.deleted} 条任务`);
    selectedTasks.clear();
    selectAllPages = false;
    loadTasks();
  } catch (e) {
    alert('删除失败：' + e.message);
  }
}

function nextTaskPage() {
  taskPage++;
  loadTasks();
}

function prevTaskPage() {
  if (taskPage > 0) {
    taskPage--;
    loadTasks();
  }
}

function stopTaskLogPolling() {
  if (taskLogTimer) {
    clearInterval(taskLogTimer);
    taskLogTimer = null;
  }
  taskLogTaskId = null;
}

function renderLogLine(log) {
  const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  if (log.kind === 'error') return `<p class="task-log-error">${esc(log.content)}</p>`;
  if (log.kind === 'complete') return `<p class="task-log-complete">${esc(log.content)}</p>`;
  return `<p>${esc(log.content)}</p>`;
}

async function pollTaskLogs() {
  if (!taskLogTaskId) return;
  try {
    const res = await fetch(`/api/tasks/${taskLogTaskId}/logs?after_id=${taskLogNextId}`, { headers });
    if (!res.ok) return;
    const data = await res.json();
    if (!data.logs || !data.logs.length) return;
    const box = document.getElementById('task-live-log');
    if (!box) return;
    box.innerHTML += data.logs.map(renderLogLine).join('');
    taskLogNextId = data.next_id;
    box.scrollTop = box.scrollHeight;
  } catch (e) {
    /* transient poll failure; retry next tick */
  }
}

async function showTaskDetail(taskId) {
  stopTaskLogPolling();
  const res = await fetch(`/api/tasks/${taskId}`, { headers });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(`加载失败：${err.detail || res.status}`);
    return;
  }
  const t = (await res.json()).task;
  const r = t.result || {};
  let artifacts = '';
  if (r.artifacts && r.artifacts.length) {
    artifacts = '<p><strong>产物：</strong></p><ul>' + r.artifacts.map(a => {
      const name = String(a.filename ?? '');
      // The filename is user-controlled: escape it for display and for the
      // inline-JS string argument.
      const jsName = name.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
      return `<li><a href="#" style="color:var(--primary)" onclick="event.preventDefault();downloadArtifact('${t.task_id}','${a.artifact_id}','${jsName}')">${escHtml(name)}</a> (${Number(a.size) || 0} 字节)</li>`;
    }).join('') + '</ul>';
  }
  const cancellable = ['queued', 'assigned', 'working'].includes(t.status);
  const isLive = t.status === 'working' || t.status === 'assigned';
  document.getElementById('modal-title').textContent = '任务详情';
  document.getElementById('agent-detail').innerHTML = `
    <div class="detail-grid">
      <div class="detail-item"><label>任务 ID</label><span class="mono">${escHtml(t.task_id)}</span></div>
      <div class="detail-item"><label>节点</label><span>${escHtml(agentDisplayName(t.agent_id))}</span></div>
      <div class="detail-item"><label>来源用户</label><span>${escHtml(t.dispatched_by || '-')}</span></div>
      <div class="detail-item"><label>模式</label><span>${modeBadge(t.mode)}</span></div>
      <div class="detail-item"><label>状态</label>${badge(t.status)}</div>
      <div class="detail-item"><label>创建</label><span>${formatTime(t.created_at)}</span></div>
      <div class="detail-item"><label>开始</label><span>${formatTime(t.started_at)}</span></div>
      <div class="detail-item"><label>结束</label><span>${formatTime(t.finished_at)}</span></div>
      <div class="detail-item"><label>重试</label><span>${t.retry_count || 0} / ${t.max_retries || 0}</span></div>
      <div class="detail-item"><label>耗时</label><span>${fmtDuration(r.duration_ms)}</span></div>
      ${t.constraints && t.constraints.session_id ? `<div class="detail-item"><label>复用会话</label><span class="mono">${escHtml(t.constraints.session_id)}</span></div>` : ''}
      ${r.session_id ? `<div class="detail-item"><label>会话 ID</label><span class="mono">${escHtml(r.session_id)}</span></div>` : ''}
      ${t.depends_on && t.depends_on.length ? `<div class="detail-item"><label>依赖任务</label><span class="mono">${t.depends_on.map(escHtml).join(', ')}</span></div>` : ''}
    </div>
    ${cancellable ? `<p style="margin:.5rem 0"><button class="btn btn-danger" onclick="cancelTask('${t.task_id}')">终止此任务</button></p>` : ''}
    <p><strong>指令：</strong></p><pre>${escHtml(t.instruction)}</pre>
    <p><strong>摘要：</strong> ${escHtml(r.summary || '-')}</p>
    ${artifacts}
    <p><strong>审计事件：</strong></p><div id="task-events" class="muted">加载中...</div>
    ${isLive ? `<p><strong>实时输出：</strong></p><div id="task-live-log" class="task-live-log"><p class="muted">等待输出...</p></div>` : ''}
    <p><strong>标准输出：</strong></p><pre>${escHtml(r.stdout_tail || '(空)')}</pre>
    <p><strong>标准错误：</strong></p><pre>${escHtml(r.stderr_tail || '(空)')}</pre>
  `;
  openModal();
  loadTaskEvents(taskId);
  if (isLive) {
    taskLogTaskId = taskId;
    taskLogNextId = 0;
    document.getElementById('task-live-log').innerHTML = '';
    pollTaskLogs();
    const pollS = window.TASK_LOG_POLL_S || 5;
    taskLogTimer = setInterval(pollTaskLogs, pollS * 1000);
  }
}

async function loadTaskEvents(taskId) {
  const box = document.getElementById('task-events');
  if (!box) return;
  try {
    const res = await fetch(`/api/tasks/${taskId}/events`, { headers });
    if (!res.ok) { box.textContent = ''; return; }
    const events = (await res.json()).events || [];
    if (!events.length) { box.textContent = '(无)'; return; }
    box.classList.remove('muted');
    box.innerHTML = '<ul class="task-events">' + events.map(e => {
      const detail = e.details && Object.keys(e.details).length
        ? ' ' + escHtml(JSON.stringify(e.details)) : '';
      return `<li><span class="mono">${formatTime(e.created_at)}</span> ` +
        `<strong>${escHtml(e.event_type)}</strong>${detail}</li>`;
    }).join('') + '</ul>';
  } catch (e) {
    box.textContent = '';
  }
}

async function downloadArtifact(taskId, artifactId, filename) {
  const timeoutS = window.ARTIFACT_TIMEOUT_S || 300;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutS * 1000);
  try {
    const res = await fetch(`/api/artifacts/${taskId}/${artifactId}`, {
      headers,
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(`下载失败：HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    if (e.name === 'AbortError') {
      alert(`下载超时（${timeoutS}s）；可在「配置 → 上传大小限制」调整产物传输超时`);
    } else {
      alert(e.message || '下载失败');
    }
  } finally {
    clearTimeout(timer);
  }
}

async function cancelTask(taskId) {
  if (!confirm(`确认终止任务 ${taskId}？`)) return;
  try {
    const res = await fetch(`/api/tasks/${taskId}/cancel`, {
      method: 'POST',
      headers
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`终止失败：${err.detail || res.status}`);
      return;
    }
    const data = await res.json();
    if (!data.accepted) {
      alert(`终止失败：${data.error || '任务已处于终态'}`);
      return;
    }
    alert(`任务 ${taskId} 已终止。`);
    closeAgentModal();
    loadTasks();
  } catch (e) {
    alert('终止失败：' + e.message);
  }
}
