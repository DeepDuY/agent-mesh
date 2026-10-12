// Reusable searchable multi-select (chips + dropdown). Keeps layout stable no
// matter how many options there are, and stays usable with a search box.
// All top-level names are prefixed with `ms` because every page script shares
// one global scope (a later definition silently clobbers an earlier one).

let msInstances = {};

function msSameSet(a, b) {
  if (a.size !== b.size) return false;
  for (const v of a) if (!b.has(v)) return false;
  return true;
}

function msCreate(container, opts) {
  if (!container) return null;
  if (container.id && msInstances[container.id]) return msInstances[container.id];
  opts = opts || {};
  const inst = {
    container,
    options: [],
    selected: new Set(opts.selected || []),
    placeholder: opts.placeholder || '请选择',
    threshold: opts.searchThreshold == null ? 8 : opts.searchThreshold,
    query: '',
  };
  container.classList.add('ms');
  container.innerHTML = `
    <div class="ms-control" tabindex="0">
      <div class="ms-chips"></div>
      <span class="ms-caret">▾</span>
    </div>
    <div class="ms-menu hidden">
      <div class="ms-search-wrap"><input class="ms-search" type="text" placeholder="搜索" autocomplete="off"></div>
      <div class="ms-options"></div>
      <div class="ms-actions">
        <button type="button" class="ms-all">全选</button>
        <button type="button" class="ms-clear">清空</button>
      </div>
    </div>`;
  const control = container.querySelector('.ms-control');
  const menu = container.querySelector('.ms-menu');
  const searchEl = container.querySelector('.ms-search');
  const optionsEl = container.querySelector('.ms-options');

  inst.getSelected = () => Array.from(inst.selected);
  inst.setSelected = (values) => {
    const next = new Set(
      (values || []).filter(v => inst.options.some(o => o.value === v)));
    if (msSameSet(inst.selected, next)) return;
    inst.selected = next;
    msRender(inst);
  };
  inst.setOptions = (options) => {
    const list = (options || []).map(o => ({ value: String(o.value), label: String(o.label) }));
    const sig = list.map(o => o.value + '\u0001' + o.label).join('\u0002');
    if (sig === inst._optSig) return;
    inst._optSig = sig;
    inst.options = list;
    const valid = new Set(inst.options.map(o => o.value));
    inst.selected = new Set(Array.from(inst.selected).filter(v => valid.has(v)));
    msRender(inst);
  };

  control.addEventListener('click', (e) => {
    if (e.target.closest('.ms-chip-remove')) return;
    const willOpen = !container.classList.contains('open');
    msCloseAll();
    if (willOpen) {
      container.classList.add('open');
      menu.classList.remove('hidden');
      if (inst.options.length > inst.threshold) searchEl.focus();
    }
  });

  searchEl.addEventListener('input', () => {
    inst.query = searchEl.value.trim().toLowerCase();
    msRenderOptions(inst);
  });

  optionsEl.addEventListener('change', (e) => {
    const cb = e.target.closest('input[type=checkbox]');
    if (!cb) return;
    if (cb.checked) inst.selected.add(cb.value); else inst.selected.delete(cb.value);
    msRenderChips(inst);
  });

  container.querySelector('.ms-all').addEventListener('click', (e) => {
    e.preventDefault();
    msVisibleOptions(inst).forEach(o => inst.selected.add(o.value));
    msRender(inst);
  });
  container.querySelector('.ms-clear').addEventListener('click', (e) => {
    e.preventDefault();
    inst.selected.clear();
    msRender(inst);
  });

  control.addEventListener('click', (e) => {
    const btn = e.target.closest('.ms-chip-remove');
    if (!btn) return;
    e.stopPropagation();
    e.preventDefault();
    inst.selected.delete(btn.dataset.value);
    msRenderChips(inst);
    msRenderOptions(inst);
  });

  if (container.id) msInstances[container.id] = inst;
  inst.setOptions(opts.options || []);
  return inst;
}

function msRender(inst) {
  msRenderChips(inst);
  const wrap = inst.container.querySelector('.ms-search-wrap');
  const showSearch = inst.options.length > inst.threshold;
  wrap.classList.toggle('hidden', !showSearch);
  if (!showSearch) {
    inst.query = '';
    inst.container.querySelector('.ms-search').value = '';
  }
  msRenderOptions(inst);
}

function msRenderChips(inst) {
  const chipsEl = inst.container.querySelector('.ms-chips');
  if (!inst.selected.size) {
    chipsEl.innerHTML = `<span class="ms-placeholder">${escHtml(inst.placeholder)}</span>`;
    return;
  }
  chipsEl.innerHTML = inst.options
    .filter(o => inst.selected.has(o.value))
    .map(o => `<span class="ms-chip">${escHtml(o.label)}` +
      `<button type="button" class="ms-chip-remove" data-value="${escHtml(o.value)}" title="移除">×</button></span>`)
    .join('');
}

function msVisibleOptions(inst) {
  if (!inst.query) return inst.options;
  return inst.options.filter(o => o.label.toLowerCase().includes(inst.query));
}

function msRenderOptions(inst) {
  const el = inst.container.querySelector('.ms-options');
  const list = msVisibleOptions(inst);
  if (!list.length) {
    el.innerHTML = `<div class="ms-empty">${inst.options.length ? '无匹配项' : '暂无可选项'}</div>`;
    return;
  }
  el.innerHTML = list.map(o =>
    `<label class="ms-option"><input type="checkbox" value="${escHtml(o.value)}" ${inst.selected.has(o.value) ? 'checked' : ''}> <span>${escHtml(o.label)}</span></label>`
  ).join('');
}

function msCloseAll() {
  document.querySelectorAll('.ms.open').forEach(el => {
    el.classList.remove('open');
    const m = el.querySelector('.ms-menu');
    if (m) m.classList.add('hidden');
  });
}

document.addEventListener('click', (e) => {
  document.querySelectorAll('.ms.open').forEach(el => {
    if (!el.contains(e.target)) {
      el.classList.remove('open');
      const m = el.querySelector('.ms-menu');
      if (m) m.classList.add('hidden');
    }
  });
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') msCloseAll();
});
