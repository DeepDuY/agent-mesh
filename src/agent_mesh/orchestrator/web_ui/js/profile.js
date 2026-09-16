function openProfileModal() {
  document.getElementById('profile-token-box').classList.add('hidden');
  document.getElementById('profile-token-status').textContent = '';
  document.getElementById('profile-token-days').value = '';
  document.getElementById('profile-skill-status').textContent = '';
  const modal = document.getElementById('profile-modal');
  modal.classList.remove('hidden');
  modal.style.display = 'flex';
  loadProfile();
}

function closeProfileModal() {
  const modal = document.getElementById('profile-modal');
  modal.classList.add('hidden');
  modal.style.display = 'none';
}

function onProfileBackdrop(e) {
  if (e.target.id === 'profile-modal') closeProfileModal();
}

async function loadProfile() {
  try {
    const res = await fetch('/api/auth/me', { headers });
    if (!res.ok) return;
    const me = await res.json();
    renderProfileInfo(me);
    renderProfileToken(me);
  } catch (e) {
    console.error('load profile failed', e);
  }
}

function fmtProfileTime(value) {
  if (!value) return '-';
  return String(value).replace('T', ' ').slice(0, 19);
}

function renderProfileInfo(me) {
  const el = document.getElementById('profile-info');
  if (!el) return;
  const rows = [
    ['用户名', me.username || '-'],
    ['角色', me.role === 'admin' ? '管理员' : '普通用户'],
    ['团队', me.team_name || '-'],
    ['最近登录', fmtProfileTime(me.last_login_at)],
  ];
  el.innerHTML = rows.map(([k, v]) =>
    `<div class="detail-item"><label>${k}</label><span>${escHtml(v)}</span></div>`
  ).join('');
}

function renderProfileToken(me) {
  const el = document.getElementById('profile-token-info');
  if (!el) return;
  const created = me.token_created_at ? fmtProfileTime(me.token_created_at) : '-';
  const expiry = me.token_expires_at ? fmtProfileTime(me.token_expires_at) : '永久';
  el.innerHTML =
    `<div class="detail-item"><label>生成时间</label><span>${escHtml(created)}</span></div>` +
    `<div class="detail-item"><label>有效期</label><span>${escHtml(expiry)}</span></div>`;
}

async function rotateOwnToken() {
  const statusEl = document.getElementById('profile-token-status');
  const daysRaw = document.getElementById('profile-token-days').value.trim();
  let body = {};
  if (daysRaw !== '') {
    const days = parseInt(daysRaw, 10);
    if (!Number.isFinite(days) || days < 1 || days > 36500) {
      statusEl.textContent = '有效期需为 1–36500 天，留空表示永久';
      statusEl.style.color = 'var(--danger)';
      return;
    }
    body.expires_in_days = days;
  }
  const msg = daysRaw === ''
    ? '确认生成新的永久 API token？旧 token 将立即失效。'
    : `确认生成有效期 ${daysRaw} 天的 API token？旧 token 将立即失效。`;
  if (!confirm(msg)) return;

  try {
    const res = await fetch('/api/auth/token', {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);

    // Switch the current session over to the freshly issued API token.
    TOKEN = data.token;
    localStorage.setItem('agent_mesh_token', TOKEN);
    headers['Authorization'] = `Bearer ${TOKEN}`;

    document.getElementById('profile-token-value').textContent = data.token;
    document.getElementById('profile-token-box').classList.remove('hidden');
    document.getElementById('profile-token-days').value = '';
    statusEl.textContent = '已生成新 token（请立即保存）';
    statusEl.style.color = 'var(--ok)';
    loadProfile();
  } catch (e) {
    statusEl.textContent = '生成失败：' + e.message;
    statusEl.style.color = 'var(--danger)';
  }
}

async function downloadSkillPack() {
  const statusEl = document.getElementById('profile-skill-status');
  try {
    const res = await fetch('/api/skill-pack/agent-mesh', { headers });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'agent-mesh.zip';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    statusEl.textContent = '已开始下载 agent-mesh.zip';
    statusEl.style.color = '';
  } catch (e) {
    statusEl.textContent = '下载失败：' + e.message;
    statusEl.style.color = 'var(--danger)';
  }
  setTimeout(() => statusEl.textContent = '', 3000);
}
