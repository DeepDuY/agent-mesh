let currentFiles = [];
let filesSearch = '';
const selectedFiles = new Set();

async function loadFiles() {
  const params = new URLSearchParams();
  if (filesSearch) params.append('search', filesSearch);
  const qs = params.toString();
  const res = await fetch(`/api/files${qs ? '?' + qs : ''}`, { headers });
  if (!res.ok) { reportApiError('加载文件失败', res); return; }
  currentFiles = (await res.json()).files || [];
  const rows = currentFiles.map(f => `
    <tr>
      <td class="col-select"><input type="checkbox" class="file-checkbox" ${selectedFiles.has(f.file_id) ? 'checked' : ''} onclick="toggleFileSelect('${f.file_id}', this)"></td>
      <td class="mono">${f.file_id}</td>
      <td>${escHtml(f.filename)}</td>
      <td>${formatBytes(f.size)}</td>
      <td class="mono">${f.md5 || '-'}</td>
      <td class="mono">${(f.created_at || '').replace('T', ' ').slice(0, 19)}</td>
      <td>
        <span class="actions">
          <button class="btn" onclick="downloadLibraryFile('${f.file_id}')">下载</button>
          <button class="btn btn-danger" onclick="deleteLibraryFile('${f.file_id}')">删除</button>
        </span>
      </td>
    </tr>
  `).join('') || '<tr><td colspan="7" class="empty">暂无文件</td></tr>';
  setHtmlIfChanged(document.getElementById('files-body'), rows);
  updateFileSelectionUI();
}

async function uploadFiles(input) {
  const files = input.files;
  input.value = '';
  const statusEl = document.getElementById('files-upload-status');
  if (!files.length) return;
  const form = new FormData();
  for (const f of files) form.append('files', f);
  try {
    const res = await fetch('/api/files', {
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
    statusEl.textContent = `已上传 ${data.files.length} 个文件（重复内容自动去重）。`;
    statusEl.style.color = '';
    selectedFiles.clear();
    loadFiles();
  } catch (e) {
    statusEl.textContent = '上传失败：' + e.message;
    statusEl.style.color = 'var(--danger)';
  }
}

function applyFileSearch() {
  filesSearch = document.getElementById('files-search').value.trim();
  selectedFiles.clear();
  loadFiles();
}

function clearFileSearch() {
  document.getElementById('files-search').value = '';
  filesSearch = '';
  selectedFiles.clear();
  loadFiles();
}

function toggleFileSelect(fileId, cb) {
  if (cb.checked) selectedFiles.add(fileId);
  else selectedFiles.delete(fileId);
  updateFileSelectionUI();
}

function toggleSelectAllFiles(cb) {
  document.querySelectorAll('.file-checkbox').forEach(c => c.checked = cb.checked);
  if (cb.checked) currentFiles.forEach(f => selectedFiles.add(f.file_id));
  else currentFiles.forEach(f => selectedFiles.delete(f.file_id));
  updateFileSelectionUI();
}

function updateFileSelectionUI() {
  const count = selectedFiles.size;
  document.getElementById('files-selected-count').textContent = `已选 ${count} 项`;
  document.getElementById('files-batch-delete-btn').disabled = count === 0;
  document.getElementById('files-batch-download-btn').disabled = count === 0;
  const all = document.getElementById('files-select-all');
  const boxes = Array.from(document.querySelectorAll('.file-checkbox'));
  all.checked = boxes.length > 0 && boxes.every(c => c.checked);
  all.indeterminate = boxes.some(c => c.checked) && !all.checked;
}

async function batchDeleteFiles() {
  const count = selectedFiles.size;
  if (!count) return;
  if (!confirm(`确认删除选中的 ${count} 个文件？\n被任务引用的文件删除后，相关任务执行时会因下载失败而标记失败。`)) return;
  try {
    const res = await fetch('/api/files/batch-delete', {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_ids: Array.from(selectedFiles) }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`删除失败：${err.detail || res.status}`);
      return;
    }
    const data = await res.json();
    alert(`已删除 ${data.deleted} 个文件`);
    selectedFiles.clear();
    loadFiles();
  } catch (e) {
    alert('删除失败：' + e.message);
  }
}

async function batchDownloadFiles() {
  const count = selectedFiles.size;
  if (!count) return;
  try {
    const res = await fetch('/api/files/batch-download', {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_ids: Array.from(selectedFiles) }),
    });
    if (!res.ok) throw new Error(`下载失败：HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'files.zip';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert(e.message || '下载失败');
  }
}

async function deleteLibraryFile(fileId) {
  const f = (currentFiles || []).find(x => x.file_id === fileId);
  const filename = f ? f.filename : fileId;
  if (!confirm(`确认删除文件 ${filename}？\n被任务引用的文件删除后，相关任务执行时会因下载失败而标记失败。`)) return;
  try {
    const res = await fetch(`/api/files/${fileId}`, { method: 'DELETE', headers });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`删除失败：${err.detail || res.status}`);
      return;
    }
    selectedFiles.delete(fileId);
    loadFiles();
  } catch (e) {
    alert('删除失败：' + e.message);
  }
}

async function downloadLibraryFile(fileId) {
  try {
    const res = await fetch(`/api/files/${fileId}`, { headers });
    if (!res.ok) throw new Error(`下载失败：HTTP ${res.status}`);
    const blob = await res.blob();
    const cd = res.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)/i);
    const name = m ? decodeURIComponent(m[1]) : fileId;
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert(e.message || '下载失败');
  }
}

function formatBytes(n) {
  if (!n && n !== 0) return '-';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}
