let currentTemplates = [];

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
  document.getElementById('templates-body').innerHTML = currentTemplates.map(t => `
    <tr>
      <td>${escHtml(t.name)}</td>
      <td>${t.description ? escHtml(t.description) : '-'}</td>
      <td class="mono">${t.llm_model ? escHtml(t.llm_model) : '-'}</td>
      <td class="col-desc">${t.system_prompt ? escHtml(t.system_prompt) : '-'}</td>
      <td>${templateUsage(t.id)}</td>
      <td>
        <span class="actions">
          <button class="btn" onclick="openTemplateModal(${t.id})">编辑</button>
          <button class="btn btn-danger" onclick="deleteTemplate(${t.id})">删除</button>
        </span>
      </td>
    </tr>
  `).join('') || '<tr><td colspan="6" class="empty">暂无模板</td></tr>';
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
  renderTemplateModelOptions(t ? (t.llm_model || '') : '');
  document.getElementById('tpl-system-prompt').value = t ? (t.system_prompt || '') : '';
  document.getElementById('tpl-allowed-tools').value =
    t && t.allowed_tools ? JSON.stringify(t.allowed_tools) : '';
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
  const llm_model = document.getElementById('tpl-model').value.trim();
  const system_prompt = document.getElementById('tpl-system-prompt').value;
  const allowedToolsRaw = document.getElementById('tpl-allowed-tools').value.trim();
  if (!name) {
    statusEl.textContent = '请填写模板名称';
    statusEl.style.color = 'var(--danger)';
    return;
  }
  let allowed_tools = null;
  if (allowedToolsRaw) {
    try {
      allowed_tools = JSON.parse(allowedToolsRaw);
      if (!Array.isArray(allowed_tools)) throw new Error('必须是数组');
    } catch (e) {
      statusEl.textContent = 'allowed_tools 需为 JSON 数组，如 ["bash"]';
      statusEl.style.color = 'var(--danger)';
      return;
    }
  }
  const body = {
    name,
    description: description || null,
    llm_model: llm_model || null,
    system_prompt: system_prompt || null,
    allowed_tools,
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
