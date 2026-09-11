async function login() {
  const username = document.getElementById('username').value;
  const password = document.getElementById('password').value;
  const err = document.getElementById('login-error');
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password })
    });
    if (!res.ok) throw new Error('登录失败');
    const data = await res.json();
    TOKEN = data.token;
    localStorage.setItem('agent_mesh_token', TOKEN);
    localStorage.setItem('agent_mesh_user', data.username);
    localStorage.setItem('agent_mesh_role', data.role || 'user');
    USER_ROLE = data.role || 'user';
    headers['Authorization'] = `Bearer ${TOKEN}`;
    showApp();
  } catch (e) {
    err.textContent = e.message;
  }
}

function logout() {
  stopRefresh();
  localStorage.removeItem('agent_mesh_token');
  localStorage.removeItem('agent_mesh_user');
  localStorage.removeItem('agent_mesh_role');
  localStorage.removeItem('agent_mesh_public_url');
  location.reload();
}

function openPasswordModal() {
  document.getElementById('cp-old').value = '';
  document.getElementById('cp-new').value = '';
  document.getElementById('cp-confirm').value = '';
  const statusEl = document.getElementById('cp-status');
  statusEl.textContent = '';
  const modal = document.getElementById('password-modal');
  modal.classList.remove('hidden');
  modal.style.display = 'flex';
}

function closePasswordModal() {
  const modal = document.getElementById('password-modal');
  modal.classList.add('hidden');
  modal.style.display = 'none';
}

function onPasswordBackdrop(e) {
  if (e.target.id === 'password-modal') closePasswordModal();
}

async function submitChangePassword() {
  const statusEl = document.getElementById('cp-status');
  const oldPwd = document.getElementById('cp-old').value;
  const newPwd = document.getElementById('cp-new').value;
  const confirmPwd = document.getElementById('cp-confirm').value;
  if (!oldPwd || !newPwd) {
    statusEl.textContent = '请填写当前密码和新密码';
    statusEl.style.color = 'var(--danger)';
    return;
  }
  if (newPwd.length < 6) {
    statusEl.textContent = '新密码至少 6 位';
    statusEl.style.color = 'var(--danger)';
    return;
  }
  if (newPwd !== confirmPwd) {
    statusEl.textContent = '两次输入的新密码不一致';
    statusEl.style.color = 'var(--danger)';
    return;
  }
  try {
    const res = await fetch('/api/auth/change-password', {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ old_password: oldPwd, new_password: newPwd }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    statusEl.textContent = '密码已修改';
    statusEl.style.color = 'var(--ok)';
    setTimeout(closePasswordModal, 800);
  } catch (e) {
    statusEl.textContent = '修改失败：' + e.message;
    statusEl.style.color = 'var(--danger)';
  }
}
