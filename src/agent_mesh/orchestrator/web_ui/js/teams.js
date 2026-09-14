let currentTeams = [];

function _teamUserTeamMap() {
  const map = {};
  (currentTeams || []).forEach(t => (t.members || []).forEach(uid => { map[uid] = t; }));
  return map;
}

async function loadTeams() {
  try {
    const res = await fetch('/api/teams', { headers });
    if (!res.ok) return;
    currentTeams = (await res.json()).teams || [];
    renderTeams();
  } catch (e) {
    console.error('load teams failed', e);
  }
}

function renderTeams() {
  document.getElementById('teams-body').innerHTML = currentTeams.map(t => {
    const members = (t.members || [])
      .map(uid => { const u = (currentUsers || []).find(x => x.user_id === uid); return u ? u.username : uid; })
      .join('、');
    return `
      <tr>
        <td>${escHtml(t.name)}</td>
        <td>${t.description ? escHtml(t.description) : '-'}</td>
        <td>${members ? escHtml(members) : '-'}</td>
        <td>
          <span class="actions">
            <button class="btn" onclick="openTeamModal('${t.team_id}')">编辑</button>
            <button class="btn btn-danger" onclick="deleteTeam('${t.team_id}')">删除</button>
          </span>
        </td>
      </tr>`;
  }).join('') || '<tr><td colspan="4" class="empty">暂无团队</td></tr>';
}

function openTeamModal(teamId) {
  const t = teamId ? currentTeams.find(x => x.team_id === teamId) : null;
  document.getElementById('team-modal-title').textContent = t ? '编辑团队' : '新建团队';
  document.getElementById('team-id').value = t ? t.team_id : '';
  document.getElementById('team-name').value = t ? (t.name || '') : '';
  document.getElementById('team-desc').value = t ? (t.description || '') : '';
  const selected = new Set(t ? (t.members || []) : []);
  const teamByUser = _teamUserTeamMap();
  document.getElementById('team-members').innerHTML = (currentUsers || []).map(u => {
    const other = teamByUser[u.user_id];
    const note = other && other.team_id !== teamId
      ? ` <span class="muted">（现属 ${escHtml(other.name)}）</span>` : '';
    return `<label style="display:block;margin:2px 0">
      <input type="checkbox" value="${escHtml(u.user_id)}" ${selected.has(u.user_id) ? 'checked' : ''}> ${escHtml(u.username)}${note}
    </label>`;
  }).join('') || '<span class="muted">暂无用户</span>';
  document.getElementById('team-status').textContent = '';
  const modal = document.getElementById('team-modal');
  modal.classList.remove('hidden');
  modal.style.display = 'flex';
}

function closeTeamModal() {
  const modal = document.getElementById('team-modal');
  modal.classList.add('hidden');
  modal.style.display = 'none';
}

function onTeamBackdrop(e) {
  if (e.target.id === 'team-modal') closeTeamModal();
}

async function saveTeam() {
  const statusEl = document.getElementById('team-status');
  const id = document.getElementById('team-id').value;
  const name = document.getElementById('team-name').value.trim();
  const description = document.getElementById('team-desc').value.trim();
  if (!name) {
    statusEl.textContent = '请填写团队名称';
    statusEl.style.color = 'var(--danger)';
    return;
  }
  try {
    let teamId = id;
    if (id) {
      const res = await fetch(`/api/teams/${id}`, {
        method: 'PATCH',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, description }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
    } else {
      const res = await fetch('/api/teams', {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, description }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      teamId = data.team.team_id;
    }
    // Sync membership (add newly checked, remove unchecked).
    const checked = new Set(Array.from(
      document.querySelectorAll('#team-members input:checked')).map(c => c.value));
    const before = new Set((currentTeams.find(t => t.team_id === teamId) || {}).members || []);
    for (const uid of checked) {
      if (!before.has(uid)) {
        await fetch(`/api/teams/${teamId}/members`, {
          method: 'POST',
          headers: { ...headers, 'Content-Type': 'application/json' },
          body: JSON.stringify({ user_id: uid }),
        });
      }
    }
    for (const uid of before) {
      if (!checked.has(uid)) {
        await fetch(`/api/teams/${teamId}/members/${uid}`, { method: 'DELETE', headers });
      }
    }
    closeTeamModal();
    await loadTeams();
    if (isAdmin()) await loadUsers();
  } catch (e) {
    statusEl.textContent = '保存失败：' + e.message;
    statusEl.style.color = 'var(--danger)';
  }
}

async function deleteTeam(teamId) {
  const t = currentTeams.find(x => x.team_id === teamId);
  if (!confirm(`确认删除团队 ${t ? t.name : teamId}？成员的团队归属会被清除。`)) return;
  try {
    const res = await fetch(`/api/teams/${teamId}`, { method: 'DELETE', headers });
    if (!res.ok) {
      alert(`删除失败：HTTP ${res.status}`);
      return;
    }
    await loadTeams();
    if (isAdmin()) await loadUsers();
  } catch (e) {
    alert('删除失败：' + e.message);
  }
}
