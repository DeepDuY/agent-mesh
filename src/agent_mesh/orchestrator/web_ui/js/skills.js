async function loadSkills() {
  const res = await fetch('/api/skills', { headers });
  if (!res.ok) return;
  const skills = (await res.json()).skills || [];
  document.getElementById('skills-body').innerHTML = skills.map(s => `
    <tr>
      <td class="mono nowrap">${s.name}</td>
      <td class="col-desc" title="${escHtml(s.description)}">${escHtml(s.description) || '-'}</td>
      <td class="nowrap">v${s.version}</td>
      <td class="nowrap ${s.enabled ? 'online' : 'offline'}">${s.enabled ? '启用' : '停用'}</td>
      <td>
        <span class="actions">
          <button class="btn" onclick="downloadSkill('${s.name}')">下载</button>
          <button class="btn" onclick="toggleSkill('${s.name}', ${!s.enabled})">${s.enabled ? '停用' : '启用'}</button>
          <button class="btn btn-danger" onclick="deleteSkill('${s.name}')">删除</button>
        </span>
      </td>
    </tr>
  `).join('') || '<tr><td colspan="5" class="empty">暂无技能</td></tr>';
}

async function uploadSkill(input) {
  const file = input.files[0];
  input.value = '';
  const statusEl = document.getElementById('skill-upload-status');
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  try {
    const res = await fetch('/api/skills', {
      method: 'POST',
      headers,
      body: form,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      statusEl.textContent = `上传失败：${data.detail || res.status}`;
      statusEl.style.color = 'var(--danger)';
      return;
    }
    statusEl.textContent = `技能 ${data.skill.name} v${data.skill.version} 已上传。`;
    statusEl.style.color = '';
    loadSkills();
  } catch (e) {
    statusEl.textContent = '上传失败：' + e.message;
    statusEl.style.color = 'var(--danger)';
  }
}

async function toggleSkill(name, enabled) {
  const res = await fetch(`/api/skills/${name}`, {
    method: 'PATCH',
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled })
  });
  if (res.ok) loadSkills();
}

async function deleteSkill(name) {
  if (!confirm(`确认删除技能 ${name}？`)) return;
  const res = await fetch(`/api/skills/${name}`, { method: 'DELETE', headers });
  if (res.ok) loadSkills();
}

async function downloadSkill(name) {
  try {
    const res = await fetch(`/api/skills/${name}/download`, { headers });
    if (!res.ok) throw new Error(`下载失败：HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${name}.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert(e.message || '下载失败');
  }
}
