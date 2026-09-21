let currentSchedules = [];

function fmtLocal(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? escHtml(iso) : d.toLocaleString();
}

async function loadSchedules() {
  try {
    const res = await fetch('/api/schedules', { headers });
    if (!res.ok) {
      reportApiError('加载定时任务', res);
      return;
    }
    currentSchedules = (await res.json()).schedules || [];
    renderSchedules();
  } catch (e) {
    console.error('load schedules failed', e);
  }
}

function renderSchedules() {
  const rows = currentSchedules.map(s => `
    <tr>
      <td>${escHtml(s.name)}</td>
      <td class="mono">${escHtml(s.cron)}</td>
      <td>${s.timezone ? escHtml(s.timezone) : '系统'}</td>
      <td>${escHtml(agentDisplayName(s.agent_id))}</td>
      <td>${s.mode === 'command' ? '命令' : 'LLM'}</td>
      <td class="col-desc">${escHtml(s.instruction)}</td>
      <td>${s.enabled ? '启用' : '<span class="muted">停用</span>'}${s.last_status ? ` <span class="muted">${escHtml(s.last_status)}</span>` : ''}</td>
      <td>${s.enabled ? fmtLocal(s.next_run_at) : '-'}</td>
      <td>${fmtLocal(s.last_run_at)}</td>
      <td>
        <span class="actions">
          <button class="btn" onclick="runScheduleNow(${s.id})">立即运行</button>
          <button class="btn" onclick="toggleSchedule(${s.id}, ${s.enabled ? 'false' : 'true'})">${s.enabled ? '停用' : '启用'}</button>
          <button class="btn" onclick="openScheduleModal(${s.id})">编辑</button>
          <button class="btn btn-danger" onclick="deleteSchedule(${s.id})">删除</button>
        </span>
      </td>
    </tr>
  `).join('') || '<tr><td colspan="10" class="empty">暂无定时任务</td></tr>';
  setHtmlIfChanged(document.getElementById('schedules-body'), rows);
}

function renderScheduleAgentOptions(current) {
  const select = document.getElementById('sch-agent');
  const nodes = currentAgents || [];
  select.innerHTML =
    '<option value="">（选择节点）</option>' +
    nodes.map(a => {
      const key = a.device_id || a.agent_id;
      return `<option value="${escHtml(key)}" ${key === current ? 'selected' : ''}>#${a.id} ${escHtml(hostLabel(a))}${a.alias ? ' · ' + escHtml(a.alias) : ''}</option>`;
    }).join('');
}

function renderScheduleModelOptions(current) {
  const select = document.getElementById('sch-model');
  const models = (window.AVAILABLE_MODELS || []).slice();
  if (current && !models.includes(current)) models.unshift(current);
  select.innerHTML =
    '<option value="">（用节点默认）</option>' +
    models.map(m => `<option value="${escHtml(m)}" ${m === current ? 'selected' : ''}>${escHtml(m)}</option>`).join('');
}

function openScheduleModal(scheduleId) {
  const s = scheduleId ? currentSchedules.find(x => x.id === scheduleId) : null;
  document.getElementById('schedule-modal-title').textContent = s ? '编辑定时任务' : '新建定时任务';
  document.getElementById('sch-id').value = s ? s.id : '';
  document.getElementById('sch-name').value = s ? (s.name || '') : '';
  document.getElementById('sch-cron').value = s ? (s.cron || '') : '';
  document.getElementById('sch-timezone').value = s ? (s.timezone || '') : '';
  const constraints = (s && s.constraints) || {};
  renderScheduleAgentOptions(s ? s.agent_id : '');
  document.getElementById('sch-mode').value = s ? (s.mode || 'llm') : 'llm';
  document.getElementById('sch-instruction').value = s ? (s.instruction || '') : '';
  document.getElementById('sch-timeout').value = constraints.timeout_s || 300;
  renderScheduleModelOptions(constraints.model || '');
  document.getElementById('sch-enabled').checked = s ? !!s.enabled : true;
  const statusEl = document.getElementById('sch-status');
  statusEl.textContent = '';
  const modal = document.getElementById('schedule-modal');
  modal.classList.remove('hidden');
  modal.style.display = 'flex';
}

function closeScheduleModal() {
  const modal = document.getElementById('schedule-modal');
  modal.classList.add('hidden');
  modal.style.display = 'none';
}

function onScheduleBackdrop(e) {
  if (e.target.id === 'schedule-modal') closeScheduleModal();
}

async function saveSchedule() {
  const statusEl = document.getElementById('sch-status');
  const id = document.getElementById('sch-id').value;
  const name = document.getElementById('sch-name').value.trim();
  const cron = document.getElementById('sch-cron').value.trim();
  const timezone = document.getElementById('sch-timezone').value.trim();
  const agent_id = document.getElementById('sch-agent').value;
  const mode = document.getElementById('sch-mode').value;
  const instruction = document.getElementById('sch-instruction').value.trim();
  const timeout_s = parseInt(document.getElementById('sch-timeout').value, 10) || 300;
  const model = document.getElementById('sch-model').value || null;
  const enabled = document.getElementById('sch-enabled').checked;
  if (!name) return _schError('请填写名称');
  if (!cron) return _schError('请填写 cron 表达式');
  if (!agent_id) return _schError('请选择目标节点');
  if (!instruction) return _schError('请填写指令');
  const body = { name, cron, timezone: timezone || null, agent_id, mode, instruction, timeout_s, model, enabled };
  try {
    const res = await fetch(id ? `/api/schedules/${id}` : '/api/schedules', {
      method: id ? 'PATCH' : 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    closeScheduleModal();
    loadSchedules();
  } catch (e) {
    _schError('保存失败：' + e.message);
  }
}

function _schError(msg) {
  const statusEl = document.getElementById('sch-status');
  statusEl.textContent = msg;
  statusEl.style.color = 'var(--danger)';
}

async function toggleSchedule(scheduleId, enabled) {
  try {
    const res = await fetch(`/api/schedules/${scheduleId}`, {
      method: 'PATCH',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`操作失败：${err.detail || res.status}`);
      return;
    }
    loadSchedules();
  } catch (e) {
    alert('操作失败：' + e.message);
  }
}

async function runScheduleNow(scheduleId) {
  try {
    const res = await fetch(`/api/schedules/${scheduleId}/run`, { method: 'POST', headers });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    alert(`已派发任务 ${data.task_id}`);
    loadSchedules();
    loadTasks();
  } catch (e) {
    alert('运行失败：' + e.message);
  }
}

async function deleteSchedule(scheduleId) {
  const s = currentSchedules.find(x => x.id === scheduleId);
  if (!confirm(`确认删除定时任务「${s ? s.name : scheduleId}」？`)) return;
  try {
    const res = await fetch(`/api/schedules/${scheduleId}`, { method: 'DELETE', headers });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`删除失败：${err.detail || res.status}`);
      return;
    }
    loadSchedules();
  } catch (e) {
    alert('删除失败：' + e.message);
  }
}
