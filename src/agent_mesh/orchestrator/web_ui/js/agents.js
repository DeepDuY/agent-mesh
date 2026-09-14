function fmtPct(x) {
  return x == null ? '-' : Number(x).toFixed(2) + '%';
}

async function loadAgents() {
  const res = await fetch('/api/agents', { headers });
  if (!res.ok) return;
  currentAgents = (await res.json()).agents;
  document.getElementById('agents-body').innerHTML = currentAgents.map(a => {
    const version = a.upgrade_requested
      ? `<span title="升级到 ${a.upgrade_version}" style="color:var(--warn)">${a.version || '-'} → ${a.upgrade_version}</span>`
      : (a.version || '-');
    return `
    <tr>
      <td class="mono">${a.id}</td>
      <td title="${escHtml(a.hostname || '')}">${hostLabel(a)}</td>
      <td>${a.alias || '-'}</td>
      <td>${version}</td>
      <td>${a.runtime || '-'}</td>
      <td>${a.distro || a.os || '-'}</td>
      <td>${a.cpu_percent != null ? `CPU ${fmtPct(a.cpu_percent)} · 内存 ${fmtPct(a.mem_percent)}` : '-'}</td>
      <td class="${a.online ? 'online' : 'offline'}">${a.online ? '在线' : '离线'}</td>
      <td class="mono">${a.current_task_id || '-'}</td>
      <td>
        <span class="actions">
          <button class="btn" onclick="showAgentDetail(${a.id})">详情</button>
          <button class="btn" onclick="requestUpgrade(${a.id})">升级</button>
          <button class="btn btn-danger" onclick="deleteAgent(${a.id})">删除</button>
        </span>
      </td>
    </tr>
  `}).join('') || '<tr><td colspan="10" class="empty">暂无节点</td></tr>';
}

async function showAgentDetail(agentId) {
  const res = await fetch(`/api/agents/${agentId}/detail`, { headers });
  if (!res.ok) return;
  const a = (await res.json()).agent;
  const lastSeen = a.last_seen ? new Date(a.last_seen).toLocaleString() : '-';
  const tasks = a.tasks || [];
  const memPct = a.mem_percent != null ? a.mem_percent : null;
  const memInfo = (a.mem_used_mb != null && a.mem_total_mb != null)
    ? `${a.mem_used_mb} / ${a.mem_total_mb} MB` : '-';
  document.getElementById('modal-title').textContent = '节点详情';
  // System prompt / template binding are admin-only (control-plane) — hide for
  // non-admins; the backend also enforces admin role (403).
  const adminBlock = isAdmin() ? `
    <h3>节点 System Prompt <span class="help" data-tip="拼接在该节点每个 llm 任务提示词中（模板提示词之前）；留空则只用模板/内置提示词。保存后随心跳同步到节点。">?</span></h3>
    <form onsubmit="setAgentSystemPrompt(event, ${a.id})">
      <textarea name="system_prompt" class="modal-textarea" placeholder="例如：你是运维专员，只允许操作 /opt 下的目录，禁止改动系统文件。" onfocus="pauseRefresh()" onblur="resumeRefresh()">${escHtml(a.system_prompt || '')}</textarea>
      <div class="sub-actions"><button class="btn" type="submit">保存 System Prompt</button></div>
    </form>

    <h3>应用模板 <span class="help" data-tip="绑定模板后继承模板的提示词/默认模型/权限；节点自身配置优先/叠加。改模板会自动同步到绑定节点。">?</span></h3>
    <form class="form-row" onsubmit="applyTemplate(event, ${a.id})">
      <select name="template_id">
        <option value="">（不绑定模板）</option>
        ${(currentTemplates || []).map(t => `<option value="${t.id}" ${a.template_id === t.id ? 'selected' : ''}>${escHtml(t.name)}</option>`).join('')}
      </select>
      <button type="submit">应用</button>
    </form>
  ` : '';
  document.getElementById('agent-detail').innerHTML = `
    <div class="detail-grid">
      <div class="detail-item"><label>数字 ID</label><span class="mono">${a.id}</span></div>
      <div class="detail-item"><label>设备标识</label><span class="mono">${a.device_id || '-'}</span></div>
      <div class="detail-item"><label>节点标识</label><span class="mono">${a.agent_id}</span></div>
      <div class="detail-item"><label>显示名</label><span>${a.display_name}</span></div>
      <div class="detail-item"><label>别名</label><span>${a.alias || '-'}</span></div>
      <div class="detail-item"><label>节点描述</label><span>${a.effective_description ? escHtml(a.effective_description) : '-'}</span></div>
      <div class="detail-item"><label>模板</label><span>${escHtml(a.template_name || (currentTemplates || []).find(t => t.id === a.template_id)?.name || '-')}</span></div>
      <div class="detail-item"><label>主机名</label><span>${a.hostname || '-'}</span></div>
      <div class="detail-item"><label>探针版本</label><span class="mono">${a.version || '-'}</span></div>
      <div class="detail-item"><label>系统</label><span>${a.distro || a.os || '-'} ${a.arch || ''}</span></div>
      <div class="detail-item"><label>运行时</label><span>${a.runtime || '-'}</span></div>
      <div class="detail-item"><label>状态</label><span class="${a.online ? 'online' : 'offline'}">${a.online ? '在线' : '离线'}</span></div>
      <div class="detail-item"><label>最后在线</label><span>${lastSeen}</span></div>
      <div class="detail-item"><label>当前任务</label><span class="mono">${a.current_task_id || '-'}</span></div>
    </div>

    <h3>系统资源</h3>
    <div class="detail-grid">
      <div class="detail-item"><label>CPU 使用率</label><span>${fmtPct(a.cpu_percent)}</span></div>
      <div class="detail-item"><label>内存使用率</label><span>${fmtPct(memPct)}</span></div>
      <div class="detail-item"><label>内存使用量</label><span>${memInfo}</span></div>
    </div>
    ${a.cpu_percent != null ? `<div class="meter"><div class="meter-bar" style="width:${Math.min(a.cpu_percent, 100)}%"></div></div><p class="refresh-hint" style="margin:0">CPU</p>` : ''}
    ${memPct != null ? `<div class="meter"><div class="meter-bar" style="width:${Math.min(memPct, 100)}%"></div></div><p class="refresh-hint" style="margin:0">内存</p>` : ''}

    <h3>修改别名</h3>
    <form class="form-row" onsubmit="setAlias(event, ${a.id})">
      <input name="alias" value="${a.alias || ''}" placeholder="设置别名" onfocus="pauseRefresh()" onblur="resumeRefresh()">
      <button type="submit">保存</button>
    </form>

    <h3>节点描述（本节点） <span class="help" data-tip="该节点是做什么的，主 Agent 通过 list_agents 的 effective_description 看到。本节点填写优先；留空则用其绑定模板的「节点描述」。仅元数据，不影响执行。">?</span></h3>
    <form onsubmit="setAgentDescription(event, ${a.id})">
      <textarea name="description" class="modal-textarea" placeholder="留空则使用绑定模板的节点描述；例如：生产 Web 服务器，只跑部署类命令" onfocus="pauseRefresh()" onblur="resumeRefresh()">${escHtml(a.description || '')}</textarea>
      <div class="sub-actions"><button class="btn" type="submit">保存描述</button></div>
    </form>

    ${adminBlock}

    <h3>最近任务</h3>
    <table>
      <thead><tr><th>ID</th><th>状态</th><th>摘要</th><th>耗时</th></tr></thead>
      <tbody>
        ${tasks.length ? tasks.map(t => `<tr><td class="mono">${t.task_id}</td><td>${badge(t.status)}</td><td>${t.result ? t.result.summary : '-'}</td><td>${fmtDuration(t.result && t.result.duration_ms)}</td></tr>`).join('') : '<tr><td colspan="4" class="empty">无任务</td></tr>'}
      </tbody>
    </table>
  `;
  openModal();
}

async function requestUpgrade(agentId) {
  const a = currentAgents.find(x => x.id === agentId);
  const name = a ? (a.display_name || a.agent_id) : agentId;
  if (a && a.upgrade_requested) {
    alert(`节点 ${name} 已有待执行的升级（目标版本 ${a.upgrade_version}），请等待节点空闲后自动升级。`);
    return;
  }
  if (!confirm(`确认升级节点 ${name}？\n将在该节点空闲时自动下载新版安装包并重启（失败会自动回滚）。`)) return;
  try {
    const res = await fetch(`/api/agents/${agentId}/upgrade`, {
      method: 'POST',
      headers
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(`升级请求失败：${data.detail || res.status}`);
      return;
    }
    if (!data.requested) {
      alert(`升级未发起：${data.error || '未知原因'}`);
      return;
    }
    alert(`已下发升级请求（目标版本 ${data.version}），节点空闲后会自动执行。`);
    loadAll();
  } catch (e) {
    alert('升级请求失败：' + e.message);
  }
}

async function deleteAgent(agentId) {
  const a = currentAgents.find(x => x.id === agentId);
  const name = a ? (a.display_name || a.agent_id) : agentId;
  if (!confirm(`确认删除节点 ${name}？\n将下发自毁命令让该节点卸载 agent（删除安装目录和 systemd 服务），并立即从列表移除。`)) return;
  try {
    const res = await fetch(`/api/agents/${agentId}`, {
      method: 'DELETE',
      headers
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`删除失败：${err.detail || res.status}`);
      return;
    }
    await res.json();
    alert(`节点 ${name} 已删除。`);
    loadAll();
  } catch (e) {
    alert('删除失败：' + e.message);
  }
}

async function showInstallCommand() {
  const publicUrl = document.getElementById('public-url').value.trim() || window.location.origin;
  // The installed edge needs a LONG-LIVED credential in edge.env (the login
  // session token expires, default 24h). Admins get the global token auto-filled
  // from /api/settings; other users must paste their user API token or ask an admin.
  let installToken = '<全局token或你的用户API token>';
  try {
    const res = await fetch('/api/settings', { headers });
    if (res.ok) {
      const st = (await res.json()).settings;
      if (st.agent_mesh_token) installToken = st.agent_mesh_token;
    }
  } catch (e) { /* keep placeholder */ }
  const cmd = `TOKEN='${installToken}' bash <(curl -fsSL -H "Authorization: Bearer ${TOKEN}" ${publicUrl}/api/bootstrap/install.sh)`;
  document.getElementById('modal-title').textContent = '安装新节点';
  document.getElementById('agent-detail').innerHTML = `
    <p>在目标机器上执行以下命令安装节点：</p>
    <p style="color:var(--danger)">注意：安装 token 必须长期有效（全局 token / 用户 API token），不要使用登录 session token（24h 过期）。</p>
    <pre style="white-space:pre-wrap;">${cmd}</pre>
    <button class="btn" onclick="copyText(this.parentElement.querySelector('pre').textContent)">复制命令</button>
  `;
  openModal();
}

async function setAlias(e, agentId) {
  e.preventDefault();
  const alias = e.target.alias.value;
  await fetch(`/api/agents/${agentId}/alias`, {
    method: 'PATCH',
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify({ alias: alias || null })
  });
  closeAgentModal();
  loadAll();
}

async function setAgentDescription(e, agentId) {
  e.preventDefault();
  const description = e.target.description.value;
  const res = await fetch(`/api/agents/${agentId}/description`, {
    method: 'PATCH',
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify({ description: description || null })
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(`保存失败：${err.detail || res.status}`);
    return;
  }
  closeAgentModal();
  loadAll();
}

async function setAgentSystemPrompt(e, agentId) {
  e.preventDefault();
  const system_prompt = e.target.system_prompt.value;
  const res = await fetch(`/api/agents/${agentId}/system_prompt`, {
    method: 'PATCH',
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify({ system_prompt: system_prompt || null })
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(`保存失败：${err.detail || res.status}`);
    return;
  }
  closeAgentModal();
  loadAll();
}

async function applyTemplate(e, agentId) {
  e.preventDefault();
  const raw = e.target.template_id.value;
  const template_id = raw ? parseInt(raw, 10) : null;
  const res = await fetch(`/api/agents/${agentId}/template`, {
    method: 'PATCH',
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify({ template_id })
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(`应用失败：${err.detail || res.status}`);
    return;
  }
  closeAgentModal();
  loadAll();
}
