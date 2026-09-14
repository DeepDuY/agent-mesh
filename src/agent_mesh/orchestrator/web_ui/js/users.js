let currentUsers = [];

function roleLabel(role) {
  return role === 'admin' ? '管理员' : '普通用户';
}

function formatTime(value) {
  if (!value) return '-';
  return String(value).replace('T', ' ').slice(0, 19);
}

async function loadUsers() {
  if (!isAdmin()) return;
  try {
    const res = await fetch('/api/auth/users', { headers });
    if (!res.ok) return;
    currentUsers = (await res.json()).users || [];
    renderUsers();
    if (typeof renderUserTeamOptions === 'function') renderUserTeamOptions();
  } catch (e) {
    console.error('load users failed', e);
  }
}

function renderUsers() {
  document.getElementById('users-body').innerHTML = currentUsers.map(u => `
    <tr>
      <td>${escHtml(u.username)}</td>
      <td>${roleLabel(u.role)}</td>
      <td>${u.team_name ? escHtml(u.team_name) : '<span class="muted">-</span>'}</td>
      <td>${u.disabled ? '<span class="offline">已禁用</span>' : '<span class="online">正常</span>'}</td>
      <td class="mono">${formatTime(u.created_at)}</td>
      <td class="mono">${u.last_login_at ? formatTime(u.last_login_at) : '-'}</td>
      <td class="mono">${u.token_created_at ? formatTime(u.token_created_at) : '-'}</td>
      <td>
        <span class="actions">
          <button class="btn" onclick="resetUserPassword('${escHtml(u.username)}')">重置密码</button>
          <button class="btn" onclick="rotateUserToken('${escHtml(u.username)}')">轮换 token</button>
          <button class="btn btn-danger" onclick="deleteUser('${escHtml(u.username)}')" ${u.username === 'admin' ? 'disabled' : ''}>删除</button>
        </span>
      </td>
    </tr>
  `).join('') || '<tr><td colspan="8" class="empty">暂无用户</td></tr>';
}

function showUserToken(token, message) {
  document.getElementById('new-user-token-value').textContent = token;
  document.getElementById('new-user-token-box').classList.remove('hidden');
  const statusEl = document.getElementById('create-user-status');
  statusEl.textContent = message;
  statusEl.style.color = 'var(--ok)';
}

async function createUser() {
  const statusEl = document.getElementById('create-user-status');
  const username = document.getElementById('new-user-username').value.trim();
  const password = document.getElementById('new-user-password').value;
  const role = document.getElementById('new-user-role').value;
  const team_id = document.getElementById('new-user-team').value;
  if (!username || !password) {
    statusEl.textContent = '请填写用户名和密码';
    statusEl.style.color = 'var(--danger)';
    return;
  }
  if (!team_id) {
    statusEl.textContent = '请先选择团队（新用户必须归属一个团队）';
    statusEl.style.color = 'var(--danger)';
    return;
  }
  try {
    const res = await fetch('/api/auth/users', {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, role, team_id }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    document.getElementById('new-user-username').value = '';
    document.getElementById('new-user-password').value = '';
    showUserToken(data.token, `用户 ${data.username} 创建成功`);
    loadUsers();
  } catch (e) {
    statusEl.textContent = '创建失败：' + e.message;
    statusEl.style.color = 'var(--danger)';
  }
}

async function deleteUser(username) {
  if (!confirm(`确认删除用户 ${username}？其 API token 将立即失效。`)) return;
  try {
    const res = await fetch(`/api/auth/users/${encodeURIComponent(username)}`, {
      method: 'DELETE',
      headers,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    loadUsers();
  } catch (e) {
    alert('删除失败：' + e.message);
  }
}

async function resetUserPassword(username) {
  const password = prompt(`为用户 ${username} 设置新密码（至少 6 位）`);
  if (password === null) return;
  if (password.length < 6) {
    alert('密码至少 6 位');
    return;
  }
  try {
    const res = await fetch(`/api/auth/users/${encodeURIComponent(username)}/password`, {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    alert(`用户 ${username} 的密码已重置`);
    loadUsers();
  } catch (e) {
    alert('重置失败：' + e.message);
  }
}

async function rotateUserToken(username) {
  if (!confirm(`确认轮换用户 ${username} 的 API token？旧 token 将立即失效。`)) return;
  try {
    const res = await fetch(`/api/auth/users/${encodeURIComponent(username)}/token`, {
      method: 'POST',
      headers,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    showUserToken(data.token, `用户 ${data.username} 的 token 已轮换`);
    loadUsers();
  } catch (e) {
    alert('轮换失败：' + e.message);
  }
}
