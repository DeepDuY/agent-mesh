// AI review gate: the "审核" module (pending decisions + audit history) and the
// review-policy settings in the config tab. All top-level names are prefixed
// with `review`/`r`-specific names to avoid clobbering other page scripts.

let currentReviews = [];
let reviewHistoryPage = 0;
const reviewHistoryPageSize = 20;
let reviewHistoryTotal = 0;

function reviewActionLabel(action) {
  return { allowed: '放行', pending_review: '待审核', denied: '拦截' }[action] || action || '-';
}

function reviewSourceLabel(source) {
  return {
    dispatch: '派发',
    approve: '人工通过',
    reject: '人工拒绝',
    expire: '超时',
  }[source] || source || '-';
}

// Review records store the agent reference the caller supplied, which may be a
// numeric id, a device_id or an agent_id (and differ between the dispatch and
// the decision rows of the same task). Resolve it to the agent so both审核
// tables can show a consistent {id, hostname} pair.
function reviewNodeInfo(key) {
  const k = key == null ? '' : String(key);
  const agents = currentAgents || [];
  const a = agents.find(x => x.device_id && x.device_id === k) ||
    agents.find(x => String(x.id) === k) ||
    agents.find(x => x.agent_id === k);
  if (!a) {
    // Numeric fallback id only when the key really is a number; otherwise the
    // key is an opaque identifier and belongs in the id column verbatim.
    return { id: k || '-', hostname: '-' };
  }
  return {
    id: a.id != null ? String(a.id) : '-',
    hostname: hostLabel(a),
  };
}

function openReviewTask(taskId) {
  if (typeof switchTab === 'function') switchTab('tasks');
  if (typeof showTaskDetail === 'function') showTaskDetail(taskId);
}

async function loadReviews() {
  if (!isAdmin()) return;
  const actionEl = document.getElementById('review-action-filter');
  const searchEl = document.getElementById('review-history-search');
  const action = actionEl ? actionEl.value : '';
  const search = searchEl ? searchEl.value.trim() : '';
  const params = new URLSearchParams({
    limit: String(reviewHistoryPageSize),
    offset: String(reviewHistoryPage * reviewHistoryPageSize),
  });
  if (action) params.append('action', action);
  if (search) params.append('search', search);
  try {
    const [pendRes, histRes] = await Promise.all([
      fetch('/api/tasks?status=pending_review&limit=200', { headers }),
      fetch(`/api/reviews?${params}`, { headers }),
    ]);
    if (pendRes.ok) renderReviewPending(((await pendRes.json()).tasks) || []);
    if (histRes.ok) {
      const data = await histRes.json();
      reviewHistoryTotal = data.total || 0;
      const totalPages = Math.max(1, Math.ceil(reviewHistoryTotal / reviewHistoryPageSize));
      if (reviewHistoryPage >= totalPages) {
        reviewHistoryPage = totalPages - 1;
        return loadReviews();
      }
      renderReviewHistory(data.reviews || []);
      setTextIfChanged(
        document.getElementById('review-page-info'),
        `第 ${reviewHistoryPage + 1} / ${totalPages} 页（共 ${reviewHistoryTotal} 条）`
      );
    }
  } catch (e) {
    console.error('load reviews failed', e);
  }
}

function applyReviewFilters() {
  reviewHistoryPage = 0;
  loadReviews();
}

function clearReviewFilters() {
  const s = document.getElementById('review-history-search');
  if (s) s.value = '';
  const a = document.getElementById('review-action-filter');
  if (a) a.value = '';
  reviewHistoryPage = 0;
  loadReviews();
}

function prevReviewPage() {
  if (reviewHistoryPage <= 0) return;
  reviewHistoryPage -= 1;
  loadReviews();
}

function nextReviewPage() {
  const totalPages = Math.max(1, Math.ceil(reviewHistoryTotal / reviewHistoryPageSize));
  if (reviewHistoryPage >= totalPages - 1) return;
  reviewHistoryPage += 1;
  loadReviews();
}

function renderReviewPending(tasks) {
  const rows = tasks.map(t => {
    const node = reviewNodeInfo(t.agent_id);
    return `
    <tr>
      <td class="mono"><a href="#" class="link" onclick="event.preventDefault();openReviewTask('${escHtml(t.task_id)}')">${escHtml(t.task_id)}</a></td>
      <td class="mono">${escHtml(node.id)}</td>
      <td>${escHtml(node.hostname)}</td>
      <td>${modeBadge(t.mode)}</td>
      <td>${reviewScoreCell(t.review_score)}</td>
      <td title="${escHtml(t.review_reason || '')}">${escHtml((t.review_reason || '').slice(0, 60))}${(t.review_reason || '').length > 60 ? '...' : ''}</td>
      <td>${escHtml(t.dispatched_by || '-')}</td>
      <td class="mono">${formatTime(t.created_at)}</td>
      <td>
        <span class="actions">
          <button class="btn btn-success" onclick="approveReviewedTask('${escHtml(t.task_id)}')">通过</button>
          <button class="btn btn-danger" onclick="rejectReviewedTask('${escHtml(t.task_id)}')">拒绝</button>
        </span>
      </td>
    </tr>
  `;
  }).join('') || '<tr><td colspan="9" class="empty">暂无待审核任务</td></tr>';
  setHtmlIfChanged(document.getElementById('review-pending-body'), rows);
}

function renderReviewHistory(reviews) {
  currentReviews = reviews;
  const rows = reviews.map(r => {
    const node = reviewNodeInfo(r.agent_id);
    return `
    <tr>
      <td class="mono">${formatTime(r.created_at)}</td>
      <td class="mono"><a href="#" class="link" onclick="event.preventDefault();openReviewTask('${escHtml(r.task_id)}')">${escHtml(r.task_id)}</a></td>
      <td class="mono">${escHtml(node.id)}</td>
      <td>${escHtml(node.hostname)}</td>
      <td>${escHtml(reviewActionLabel(r.action))}</td>
      <td>${reviewScoreCell(r.score)}</td>
      <td title="${escHtml(r.reason || '')}">${escHtml((r.reason || '').slice(0, 60))}${(r.reason || '').length > 60 ? '...' : ''}</td>
      <td>${escHtml(reviewSourceLabel(r.source))}</td>
      <td>${escHtml(r.actor || '-')}</td>
    </tr>
  `;
  }).join('') || '<tr><td colspan="9" class="empty">暂无审核记录</td></tr>';
  setHtmlIfChanged(document.getElementById('review-history-body'), rows);
}

async function approveReviewedTask(taskId) {
  if (!confirm(`通过任务 ${taskId}？通过后将进入排队执行。`)) return;
  await reviewAct(taskId, 'approve');
}

async function rejectReviewedTask(taskId) {
  const reason = prompt(`拒绝任务 ${taskId}，可填写原因：`);
  if (reason === null) return;
  await reviewAct(taskId, 'reject', reason);
}

async function reviewAct(taskId, action, reason) {
  try {
    const res = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/${action}`, {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: reason || '' }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    if (typeof loadTasks === 'function') loadTasks();
    if (typeof loadReviews === 'function') loadReviews();
  } catch (e) {
    alert('操作失败：' + e.message);
  }
}

// ---------------------------------------------------------------------------
// Review-policy settings (config tab)
// ---------------------------------------------------------------------------

let reviewScopeSelection = { agents: [], teams: [], users: [] };

function parseReviewPolicy(raw) {
  let d = {};
  if (typeof raw === 'string' && raw.trim()) {
    try { d = JSON.parse(raw) || {}; } catch (_) { d = {}; }
  } else if (raw && typeof raw === 'object') {
    d = raw;
  }
  const arr = k => (Array.isArray(d[k]) ? d[k].map(String) : []);
  const int = (v, def, min, max) => {
    const n = Math.trunc(Number(v));
    return Number.isFinite(n) ? Math.max(min, Math.min(max, n)) : def;
  };
  return {
    enabled: !!d.enabled,
    threshold: int(d.threshold, 60, 0, 100),
    human_review: d.human_review !== false,
    bypass_admin: d.bypass_admin !== false,
    model: d.model || '',
    rubric: d.rubric || '',
    expire_s: int(d.expire_s, 1800, 0, 86400),
    api_style: d.api_style || 'auto',
    agents: arr('agents'),
    teams: arr('teams'),
    users: arr('users'),
  };
}

function reviewScopeInstance(id) {
  const el = document.getElementById(id);
  if (!el) return null;
  return msInstances[id] || msCreate(el, { placeholder: '不限制（全部）', searchThreshold: 5 });
}

// Node scope labels combine the node id (as shown in the 节点 module), the
// hostname and the IP so same-named hosts (e.g. several "localhost") stay
// distinguishable. ip_address falls back to alias for nodes that report no IP.
function reviewAgentLabel(a) {
  const id = a.id != null ? a.id : '-';
  const ip = a.ip_address || a.alias || '-';
  return `${id} · ${hostLabel(a)} · ${ip}`;
}

function openReviewSettings() {
  if (typeof pauseRefresh === 'function') pauseRefresh();
  if (typeof loadConfig === 'function') loadConfig();
  const modal = document.getElementById('review-settings-modal');
  if (modal) {
    modal.classList.remove('hidden');
    modal.style.display = 'flex';
  }
}

function closeReviewSettings() {
  const modal = document.getElementById('review-settings-modal');
  if (modal) {
    modal.classList.add('hidden');
    modal.style.display = 'none';
  }
  if (typeof resumeRefresh === 'function') resumeRefresh();
}

function onReviewSettingsBackdrop(e) {
  if (e.target.id === 'review-settings-modal') closeReviewSettings();
}

function renderReviewScopeOptions() {
  const a = reviewScopeInstance('review-scope-agents');
  if (a) {
    a.setOptions((currentAgents || []).map(x => ({
      value: x.device_id || x.agent_id || String(x.id),
      label: reviewAgentLabel(x),
    })));
    a.setSelected(reviewScopeSelection.agents);
  }
  const t = reviewScopeInstance('review-scope-teams');
  if (t) {
    t.setOptions((currentTeams || []).map(x => ({ value: x.team_id, label: x.name })));
    t.setSelected(reviewScopeSelection.teams);
  }
  const u = reviewScopeInstance('review-scope-users');
  if (u) {
    u.setOptions((currentUsers || []).map(x => ({ value: x.user_id, label: x.username })));
    u.setSelected(reviewScopeSelection.users);
  }
}

function loadReviewPolicy(raw) {
  if (!document.getElementById('review-enabled')) return;
  const p = parseReviewPolicy(raw);
  reviewScopeSelection = { agents: p.agents, teams: p.teams, users: p.users };
  const setVal = (id, val) => {
    const el = document.getElementById(id);
    if (el && document.activeElement !== el) el.value = val;
  };
  const setCheck = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.checked = val;
  };
  setCheck('review-enabled', p.enabled);
  setCheck('review-human', p.human_review);
  setCheck('review-bypass-admin', p.bypass_admin);
  setVal('review-threshold', p.threshold);
  setVal('review-model', p.model);
  setVal('review-expire', p.expire_s);
  setVal('review-rubric', p.rubric);
  const style = document.getElementById('review-api-style');
  if (style) style.value = p.api_style || 'auto';
  renderReviewScopeOptions();
}

async function saveReviewPolicy() {
  const el = document.getElementById('review-status');
  const int = (id, def, min, max) => {
    const raw = document.getElementById(id).value;
    const n = Math.trunc(Number(raw));
    return Number.isFinite(n) && raw !== '' ? Math.max(min, Math.min(max, n)) : def;
  };
  const a = reviewScopeInstance('review-scope-agents');
  const t = reviewScopeInstance('review-scope-teams');
  const u = reviewScopeInstance('review-scope-users');
  const policy = {
    enabled: document.getElementById('review-enabled').checked,
    threshold: int('review-threshold', 60, 0, 100),
    human_review: document.getElementById('review-human').checked,
    bypass_admin: document.getElementById('review-bypass-admin').checked,
    model: document.getElementById('review-model').value.trim(),
    expire_s: int('review-expire', 1800, 0, 86400),
    rubric: document.getElementById('review-rubric').value,
    api_style: (document.getElementById('review-api-style') || {}).value || 'auto',
    agents: a ? a.getSelected() : [],
    teams: t ? t.getSelected() : [],
    users: u ? u.getSelected() : [],
  };
  reviewScopeSelection = { agents: policy.agents, teams: policy.teams, users: policy.users };
  document.getElementById('review-threshold').value = String(policy.threshold);
  document.getElementById('review-expire').value = String(policy.expire_s);
  try {
    await patchSettings({ review_policy: JSON.stringify(policy) });
    el.textContent = '已保存';
    el.style.color = '';
  } catch (e) {
    el.textContent = '保存失败：' + e.message;
    el.style.color = 'var(--danger)';
  }
  setTimeout(() => el.textContent = '', 3000);
}
