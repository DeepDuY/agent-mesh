let currentTemplates = [];

// Built-in permission presets (mirror of agent_mesh.shared.permissions).
const PERMISSION_PRESETS = {
  build: { "*": "allow" },
  plan: {
    edit: "deny",
    bash: {
      "*": "deny",
      "ls": "allow", "ls *": "allow", "cat *": "allow", "head *": "allow",
      "tail *": "allow", "grep *": "allow", "rg *": "allow", "find *": "allow",
      "wc *": "allow", "stat *": "allow", "df *": "allow", "du *": "allow",
      "pwd": "allow", "whoami": "allow", "date": "allow", "echo *": "allow",
      "uname *": "allow", "git status": "allow", "git status *": "allow",
      "git log": "allow", "git log *": "allow", "git diff": "allow",
      "git diff *": "allow", "git show *": "allow", "git branch": "allow",
      "git branch *": "allow", "curl *": "allow",
    },
    webfetch: "allow",
    read: "allow",
    glob: "allow",
    grep: "allow",
  },
  readonly: { "*": "deny", read: "allow", glob: "allow", grep: "allow" },
};

function applyTemplatePermissionPreset(name) {
  if (!name) return;
  document.getElementById('tpl-permission').value =
    JSON.stringify(PERMISSION_PRESETS[name], null, 2);
}

async function loadTemplates() {
  try {
    const res = await fetch('/api/templates', { headers });
    if (!res.ok) return;
    currentTemplates = (await res.json()).templates || [];
    renderTemplates();
  } catch (e) {
    console.error('load templates failed', e);
  }
}

function templateUsage(templateId) {
  return (currentAgents || []).filter(a => a.template_id === templateId).length;
}

function renderTemplates() {
  const rows = currentTemplates.map(t => `
    <tr>
      <td>${escHtml(t.name)}</td>
      <td>${t.description ? escHtml(t.description) : '-'}</td>
      <td class="col-desc">${t.node_description ? escHtml(t.node_description) : '-'}</td>
      <td class="mono">${t.llm_model ? escHtml(t.llm_model) : '-'}</td>
      <td class="col-desc">${t.system_prompt ? escHtml(t.system_prompt) : '-'}</td>
      <td>${templateUsage(t.id)}</td>
      <td>
        <span class="actions">
          <button class="btn" onclick="openTemplateNodesModal(${t.id})">应用到节点</button>
          <button class="btn" onclick="openTemplateModal(${t.id})">编辑</button>
          <button class="btn btn-danger" onclick="deleteTemplate(${t.id})">删除</button>
        </span>
      </td>
    </tr>
  `).join('') || '<tr><td colspan="7" class="empty">暂无模板</td></tr>';
  setHtmlIfChanged(document.getElementById('templates-body'), rows);
}

function openTemplateNodesModal(templateId) {
  const t = currentTemplates.find(x => x.id === templateId);
  if (!t) return;
  document.getElementById('modal-title').textContent = `应用模板 - ${t.name}`;
  const nodes = currentAgents || [];
  document.getElementById('agent-detail').innerHTML = `
    <p class="refresh-hint" style="text-align:left">勾选要绑定模板「${escHtml(t.name)}」的节点；取消勾选会解除绑定（仅对当前绑定了该模板的节点生效）。</p>
    <div style="max-height:50vh;overflow:auto">
      ${nodes.map(a => `
        <label style="display:block;margin:2px 0">
          <input type="checkbox" name="tpl_node" value="${a.id}" ${a.template_id === templateId ? 'checked' : ''}>
          <span class="mono">#${a.id}</span> ${escHtml(hostLabel(a))}${a.alias ? ' · ' + escHtml(a.alias) : ''}
          <span class="muted">${a.template_name ? '（当前：' + escHtml(a.template_name) + '）' : ''}</span>
        </label>`).join('') || '<span class="muted">暂无节点</span>'}
    </div>
    <div id="tpl-nodes-status" class="status-msg"></div>
    <div class="sub-actions">
      <button class="btn" onclick="saveTemplateNodes(${templateId})">保存</button>
      <button class="btn btn-secondary" onclick="closeAgentModal()">取消</button>
    </div>
  `;
  openModal();
}

async function saveTemplateNodes(templateId) {
  const statusEl = document.getElementById('tpl-nodes-status');
  const checked = new Set(Array.from(document.querySelectorAll('input[name=tpl_node]:checked'))
    .map(c => parseInt(c.value, 10)));
  const toBind = Array.from(checked);
  // Unchecking a node that is currently bound to this template -> unbind it.
  const toUnbind = (currentAgents || [])
    .filter(a => a.template_id === templateId && !checked.has(a.id))
    .map(a => a.id);
  try {
    let applied = 0;
    for (const [ids, template_id] of [[toBind, templateId], [toUnbind, null]]) {
      if (!ids.length) continue;
      const res = await fetch('/api/agents/batch/template', {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent_ids: ids, template_id }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      applied += data.applied || 0;
    }
    closeAgentModal();
    await loadAll();
    alert(`已更新 ${applied} 个节点的模板绑定`);
  } catch (e) {
    statusEl.textContent = '保存失败：' + e.message;
    statusEl.style.color = 'var(--danger)';
  }
}

function renderTemplateModelOptions(current) {
  const select = document.getElementById('tpl-model');
  const models = (window.AVAILABLE_MODELS || []).slice();
  if (current && !models.includes(current)) models.unshift(current);
  select.innerHTML =
    '<option value="">（不设置，用全局默认）</option>' +
    models.map(m => `<option value="${escHtml(m)}" ${m === current ? 'selected' : ''}>${escHtml(m)}</option>`).join('');
}

function openTemplateModal(templateId) {
  const t = templateId ? currentTemplates.find(x => x.id === templateId) : null;
  document.getElementById('template-modal-title').textContent = t ? '编辑模板' : '新建模板';
  document.getElementById('tpl-id').value = t ? t.id : '';
  document.getElementById('tpl-name').value = t ? (t.name || '') : '';
  document.getElementById('tpl-description').value = t ? (t.description || '') : '';
  document.getElementById('tpl-node-description').value = t ? (t.node_description || '') : '';
  renderTemplateModelOptions(t ? (t.llm_model || '') : '');
  document.getElementById('tpl-system-prompt').value = t ? (t.system_prompt || '') : '';
  document.getElementById('tpl-permission').value =
    t && t.permission ? JSON.stringify(t.permission, null, 2) : '';
  document.getElementById('tpl-permission-preset').value = '';
  const statusEl = document.getElementById('tpl-status');
  statusEl.textContent = '';
  const modal = document.getElementById('template-modal');
  modal.classList.remove('hidden');
  modal.style.display = 'flex';
}

function closeTemplateModal() {
  const modal = document.getElementById('template-modal');
  modal.classList.add('hidden');
  modal.style.display = 'none';
}

function onTemplateBackdrop(e) {
  if (e.target.id === 'template-modal') closeTemplateModal();
}

async function saveTemplate() {
  const statusEl = document.getElementById('tpl-status');
  const id = document.getElementById('tpl-id').value;
  const name = document.getElementById('tpl-name').value.trim();
  const description = document.getElementById('tpl-description').value.trim();
  const node_description = document.getElementById('tpl-node-description').value.trim();
  const llm_model = document.getElementById('tpl-model').value.trim();
  const system_prompt = document.getElementById('tpl-system-prompt').value;
  const permissionRaw = document.getElementById('tpl-permission').value.trim();
  if (!name) {
    statusEl.textContent = '请填写模板名称';
    statusEl.style.color = 'var(--danger)';
    return;
  }
  let permission = null;
  if (permissionRaw) {
    try {
      permission = JSON.parse(permissionRaw);
      if (typeof permission !== 'object' || Array.isArray(permission) || permission === null) {
        throw new Error('必须是 JSON 对象');
      }
    } catch (e) {
      statusEl.textContent = 'permission 需为 JSON 对象，如 {"edit":"deny"}';
      statusEl.style.color = 'var(--danger)';
      return;
    }
  }
  const body = {
    name,
    description: description || null,
    node_description: node_description || null,
    llm_model: llm_model || null,
    system_prompt: system_prompt || null,
    permission,
    data: null,
  };
  try {
    const res = await fetch(id ? `/api/templates/${id}` : '/api/templates', {
      method: id ? 'PATCH' : 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    closeTemplateModal();
    loadAll();
  } catch (e) {
    statusEl.textContent = '保存失败：' + e.message;
    statusEl.style.color = 'var(--danger)';
  }
}

async function deleteTemplate(templateId) {
  const t = currentTemplates.find(x => x.id === templateId);
  const name = t ? t.name : templateId;
  const usage = templateUsage(templateId);
  const extra = usage ? `\n当前有 ${usage} 个节点绑定该模板，删除后这些节点将解除绑定。` : '';
  if (!confirm(`确认删除模板 ${name}？${extra}`)) return;
  try {
    const res = await fetch(`/api/templates/${templateId}`, {
      method: 'DELETE',
      headers,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`删除失败：${err.detail || res.status}`);
      return;
    }
    loadAll();
  } catch (e) {
    alert('删除失败：' + e.message);
  }
}
