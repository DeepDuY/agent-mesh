let TOKEN = localStorage.getItem('agent_mesh_token');
// Marks requests as coming from the bundled Web UI. Management endpoints
// (templates, node template binding) are only reachable with this header, so
// they are effectively absent from the API surface for external callers.
const headers = { 'X-Agent-Mesh-UI': '1' };
if (TOKEN) headers['Authorization'] = `Bearer ${TOKEN}`;
let refreshTimer = null;
let refreshPaused = false;
let currentAgents = [];
let USER_ROLE = localStorage.getItem('agent_mesh_role');

let sessionExpiredHandling = false;

function handleSessionExpired() {
  if (sessionExpiredHandling) return;
  sessionExpiredHandling = true;
  try { stopRefresh(); } catch (e) { /* ignore */ }
  sessionStorage.setItem('agent_mesh_expired', '1');
  localStorage.removeItem('agent_mesh_token');
  localStorage.removeItem('agent_mesh_user');
  localStorage.removeItem('agent_mesh_role');
  localStorage.removeItem('agent_mesh_public_url');
  location.reload();
}

// Any API call that comes back 401 means the session/token is no longer valid
// (expired session token, rotated/revoked API token, disabled user). Redirect
// to the login view automatically instead of showing empty pages.
(function installAuthFetchGuard() {
  const origFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const res = await origFetch(input, init);
    try {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      if (
        res.status === 401 &&
        url.includes('/api/') &&
        !url.includes('/api/auth/login')
      ) {
        handleSessionExpired();
      }
    } catch (e) { /* never break the caller */ }
    return res;
  };
})();

if (sessionStorage.getItem('agent_mesh_expired')) {
  sessionStorage.removeItem('agent_mesh_expired');
  const errEl = document.getElementById('login-error');
  if (errEl) errEl.textContent = '登录已过期，请重新登录';
}

if (TOKEN && localStorage.getItem('agent_mesh_user')) showApp();

function isAdmin() {
  return USER_ROLE === 'admin';
}

function applyRole() {
  document.querySelectorAll('.admin-only').forEach(el => {
    el.classList.toggle('hidden', !isAdmin());
  });
}

async function refreshMe() {
  try {
    const res = await fetch('/api/auth/me', { headers });
    if (res.status === 401) {
      // The fetch guard already triggers the redirect to the login view.
      return;
    }
    if (!res.ok) return;
    const me = await res.json();
    if (me.username) localStorage.setItem('agent_mesh_user', me.username);
    USER_ROLE = me.role || 'user';
    localStorage.setItem('agent_mesh_role', USER_ROLE);
    applyRole();
    document.getElementById('user-label').innerHTML =
      `<span class="user-dot"></span>${escHtml(localStorage.getItem('agent_mesh_user'))}`;
  } catch (e) {
    /* ignore transient errors */
  }
}

function escHtml(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function fmtDuration(ms) {
  if (ms == null) return '-';
  const s = ms / 1000;
  if (s < 60) return (Number.isInteger(s) ? String(s) : s.toFixed(1)) + 's';
  let total = Math.round(s);
  const h = Math.floor(total / 3600);
  total %= 3600;
  const m = Math.floor(total / 60);
  const sec = total % 60;
  return h > 0 ? `${h}h ${m}m ${sec}s` : `${m}m ${sec}s`;
}

function hostLabel(a) {
  // Short hostname (strip FQDN domain suffix) so nodes read cleanly.
  return (a.hostname || a.agent_id || '-').split('.')[0];
}

function showApp() {
  document.getElementById('login-view').classList.add('hidden');
  document.getElementById('app-view').classList.remove('hidden');
  document.getElementById('user-label').innerHTML =
    `<span class="user-dot"></span>${escHtml(localStorage.getItem('agent_mesh_user'))}`;
  applyRole();
  refreshMe();
  loadAll();
  startRefresh();
}

function startRefresh() {
  stopRefresh();
  refreshTimer = setInterval(() => {
    if (!refreshPaused) loadAll();
  }, 5000);
}

function stopRefresh() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

function pauseRefresh() {
  refreshPaused = true;
}

function resumeRefresh() {
  refreshPaused = false;
}

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  ['agents', 'tasks', 'files', 'skills', 'templates', 'users', 'teams', 'config'].forEach(n => {
    document.getElementById(`tab-${n}`).classList.toggle('hidden', n !== name);
  });
}

function badge(status) {
  const labels = { queued: '排队', assigned: '已分配', working: '执行中', completed: '完成', failed: '失败', timed_out: '超时', cancelled: '已终止', denied: '已拦截' };
  return `<span class="badge status-${status}">${labels[status] || status}</span>`;
}

function agentDisplayName(key) {
  const a = currentAgents.find(x => x.device_id === key || x.agent_id === key);
  return a ? hostLabel(a) : key;
}

async function loadAll() {
  await Promise.all([loadAgents(), loadTasks(), loadFiles(), loadSkills(), loadTemplates(), loadConfig(), loadProfile()]);
  if (isAdmin()) { await loadUsers(); await loadTeams(); }
}

function openModal() {
  const modal = document.getElementById('agent-modal');
  modal.classList.remove('hidden');
  modal.style.display = 'flex';
}

function closeAgentModal() {
  if (typeof stopTaskLogPolling === 'function') stopTaskLogPolling();
  const modal = document.getElementById('agent-modal');
  modal.classList.add('hidden');
  modal.style.display = 'none';
}

function onModalBackdrop(e) {
  if (e.target.id === 'agent-modal') closeAgentModal();
}

// User menu (header): open "个人中心" / change password / logout.
function toggleUserMenu(e) {
  if (e) e.stopPropagation();
  const dd = document.getElementById('user-menu-dropdown');
  if (dd) dd.classList.toggle('hidden');
}

function closeUserMenu() {
  const dd = document.getElementById('user-menu-dropdown');
  if (dd) dd.classList.add('hidden');
}

document.addEventListener('click', (e) => {
  const menu = document.querySelector('.user-menu');
  if (menu && !menu.contains(e.target)) closeUserMenu();
});
{
  const dd = document.getElementById('user-menu-dropdown');
  if (dd) dd.addEventListener('click', () => closeUserMenu());
}

function copyText(text) {
  if (!navigator.clipboard) {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    alert('已复制');
    return;
  }
  navigator.clipboard.writeText(text).then(() => alert('已复制')).catch(() => alert('复制失败'));
}
