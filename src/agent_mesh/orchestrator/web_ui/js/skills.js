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
          <button class="btn" onclick="openSkillEditor('${s.name}')">编辑</button>
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

// ---- page editor: create / edit SKILL.md ----

const SKILL_NAME_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/;

function _skillTemplate(name) {
  const n = name || 'my-skill';
  return (
    '---\n' +
    `name: ${n}\n` +
    'description: \n' +
    '---\n\n' +
    `# ${n}\n\n` +
    '说明这个技能做什么、何时使用，以及分步骤的操作指引。\n'
  );
}

function _skillEditorHtml(name, content, isNew) {
  return `
    <div class="sub-field">
      <label>技能名称（小写字母 / 数字 / 连字符）</label>
      <input id="skill-edit-name" type="text" value="${escHtml(name)}" ${isNew ? '' : 'readonly'}>
    </div>
    <div class="sub-field">
      <label>SKILL.md（YAML frontmatter + 正文；frontmatter 的 name 必须与上面一致）</label>
      <textarea id="skill-edit-content" class="modal-textarea skill-editor" spellcheck="false">${escHtml(content)}</textarea>
    </div>
    <div class="sub-actions">
      <button class="btn btn-success" onclick="saveSkillEditor()">${isNew ? '创建技能' : '保存修改'}</button>
      <button class="btn btn-secondary" onclick="closeAgentModal()">取消</button>
      <span id="skill-edit-status" class="status-msg"></span>
    </div>
  `;
}

function openNewSkill() {
  const name = 'my-skill';
  document.getElementById('modal-title').textContent = '新建技能';
  document.getElementById('agent-detail').innerHTML = _skillEditorHtml(name, _skillTemplate(name), true);
  const nameEl = document.getElementById('skill-edit-name');
  const bodyEl = document.getElementById('skill-edit-content');
  // Keep the frontmatter/title in sync with the name field until the user
  // starts editing the body themselves.
  window.__skillBodyTouched = false;
  bodyEl.addEventListener('input', () => { window.__skillBodyTouched = true; });
  nameEl.addEventListener('input', () => {
    if (window.__skillBodyTouched) return;
    bodyEl.value = _skillTemplate(nameEl.value.trim());
  });
  openModal();
  nameEl.focus();
  nameEl.select();
}

async function openSkillEditor(name) {
  const res = await fetch(`/api/skills/${encodeURIComponent(name)}/content`, { headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    alert('加载失败：' + (data.detail || res.status));
    return;
  }
  document.getElementById('modal-title').textContent = `编辑技能：${name}`;
  document.getElementById('agent-detail').innerHTML = _skillEditorHtml(name, data.content || '', false);
  openModal();
}

async function saveSkillEditor() {
  const statusEl = document.getElementById('skill-edit-status');
  const name = document.getElementById('skill-edit-name').value.trim();
  const content = document.getElementById('skill-edit-content').value;
  if (!SKILL_NAME_RE.test(name)) {
    statusEl.textContent = '名称需匹配 ^[a-z0-9]+(-[a-z0-9]+)*$';
    statusEl.style.color = 'var(--danger)';
    return;
  }
  statusEl.textContent = '保存中…';
  statusEl.style.color = '';
  try {
    const res = await fetch(`/api/skills/${encodeURIComponent(name)}`, {
      method: 'PUT',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    closeAgentModal();
    loadSkills();
  } catch (e) {
    statusEl.textContent = '保存失败：' + e.message;
    statusEl.style.color = 'var(--danger)';
  }
}
