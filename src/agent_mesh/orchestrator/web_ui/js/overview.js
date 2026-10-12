// Multi-dimensional overview dashboard. Aggregates task / node statistics over
// a selectable time range and renders them with lightweight CSS + inline SVG
// (no external chart library, so it works on air-gapped intranets).

const OV_STATUS_LABELS = {
  queued: '排队', assigned: '已分配', working: '执行中', completed: '完成',
  failed: '失败', timed_out: '超时', cancelled: '已终止', denied: '已拦截',
  pending_review: '待审核',
};
const OV_STATUS_ORDER = ['queued', 'pending_review', 'assigned', 'working', 'completed', 'failed', 'timed_out', 'cancelled', 'denied'];
const OV_MODE_ORDER = ['llm', 'command'];
const OV_MODE_LABELS = { llm: 'LLM', command: '命令' };
// Heatmap palette: 5 discrete levels, light-yellow -> orange -> red.
const OV_HEAT_COLORS = ['#fef9c3', '#fcd34d', '#fb923c', '#ea580c', '#dc2626'];

// Map an intensity ratio (v/max, 0..1) to one of the 5 heat levels. sqrt keeps
// low counts from saturating; v==0 is handled by the caller (neutral cell).
function ovHeatColor(ratio) {
  const t = Math.sqrt(Math.max(0, Math.min(1, ratio)));
  const idx = Math.min(5, Math.max(1, Math.ceil(t * 5)));
  return OV_HEAT_COLORS[idx - 1];
}

let ovRange = '7d';

function onOverviewRangeChange() {
  ovRange = document.getElementById('ov-range').value;
  const custom = ovRange === 'custom';
  document.getElementById('ov-from').style.display = custom ? '' : 'none';
  document.getElementById('ov-to').style.display = custom ? '' : 'none';
  document.getElementById('ov-sep').style.display = custom ? '' : 'none';
  loadOverview(true);
}

function ovQueryParams() {
  const params = new URLSearchParams({ range: ovRange });
  if (ovRange === 'custom') {
    const from = document.getElementById('ov-from').value;
    const to = document.getElementById('ov-to').value;
    if (from) params.append('created_after', new Date(from).toISOString());
    if (to) params.append('created_before', new Date(to).toISOString());
  }
  return params;
}

// Named `ovPct` (not `fmtPct`) — agents.js also defines a global `fmtPct` and
// loads after this file, so a shared name would be silently overridden.
function ovPct(frac) {
  if (frac == null) return '-';
  return `${Math.round(frac * 1000) / 10}%`;
}

function ovPad(n) { return String(n).padStart(2, '0'); }

// A dashboard bucket string is UTC ("YYYY-MM-DDTHH"); convert it into the
// viewer's local day/hour so the numbers match the times shown elsewhere.
function ovLocalBucket(bucket) {
  if (!bucket) return null;
  const iso = bucket.length === 13 ? `${bucket}:00:00Z` : `${bucket}T00:00:00Z`;
  const d = new Date(iso);
  if (isNaN(d.getTime())) return null;
  return {
    d,
    dayKey: `${d.getFullYear()}-${ovPad(d.getMonth() + 1)}-${ovPad(d.getDate())}`,
    dayLabel: `${ovPad(d.getMonth() + 1)}-${ovPad(d.getDate())}`,
    hour: d.getHours(),
  };
}

function ovBarList(el, items) {
  if (!items.length) { setHtmlIfChanged(el, '<p class="muted">暂无数据</p>'); return; }
  const max = Math.max(...items.map(i => i.total), 1);
  const html = items.map(i => {
    const pct = Math.max(2, Math.round((i.total / max) * 100));
    const failPct = i.total > 0 && i.failed ? Math.round((i.failed / i.total) * 100) : 0;
    const failSeg = failPct > 0 ? `<div class="bar-fill-fail" style="width:${failPct}%"></div>` : '';
    const title = `${i.label}：${i.total} 个任务${i.failed ? `，其中失败 ${i.failed}` : ''}`;
    return `<div class="bar-row" title="${escHtml(title)}">
      <span class="bar-label">${escHtml(i.label)}</span>
      <div class="bar-track"><div class="bar-fill ${i.cls || ''}" style="width:${pct}%">${failSeg}</div></div>
      <span class="bar-val">${i.total}</span>
    </div>`;
  }).join('');
  setHtmlIfChanged(el, html);
}

function ovRenderKpis(el, s) {
  const cards = [
    { label: '任务总数', value: s.total, cls: '' },
    { label: '成功率', value: ovPct(s.success_rate), cls: 'kpi-ok' },
    { label: '执行中', value: s.active, cls: 'kpi-active' },
    { label: '排队中', value: s.queued, cls: '' },
    { label: '待审核', value: s.pending_review || 0, cls: 'kpi-review' },
    { label: '失败', value: s.failed + s.denied, cls: 'kpi-fail' },
    { label: '平均耗时', value: s.avg_duration_ms != null ? fmtDuration(s.avg_duration_ms) : '-', cls: '' },
    { label: '高峰时段', value: s.peak_hour != null ? `${ovPad(s.peak_hour)}:00` : '-', cls: '' },
  ];
  setHtmlIfChanged(el, cards.map(c =>
    `<div class="kpi ${c.cls}"><div class="kpi-value">${escHtml(String(c.value))}</div><div class="kpi-label">${c.label}</div></div>`
  ).join(''));
}

// Time series (local days) derived from the hourly heatmap so the line, the
// axis and the heatmap all agree on the viewer's timezone.
function ovDailySeries(heatmap) {
  const byDay = {};
  for (const row of heatmap) {
    const loc = ovLocalBucket(row.bucket);
    if (!loc) continue;
    const d = byDay[loc.dayKey] || (byDay[loc.dayKey] = { dayKey: loc.dayKey, total: 0, failed: 0 });
    d.total += row.total;
    d.failed += row.failed;
  }
  return Object.keys(byDay).sort().map(k => ({ bucket: k, total: byDay[k].total, failed: byDay[k].failed }));
}

function ovRenderTrend(el, data) {
  if (!data.length) { setHtmlIfChanged(el, '<p class="muted">暂无数据</p>'); return; }
  const W = Math.max(el.clientWidth || 720, 320), H = 190, PADL = 34, PADR = 14, PADT = 12, PADB = 26;
  const max = Math.max(...data.map(d => d.total), 1);
  const n = data.length;
  const x = i => PADL + (n === 1 ? (W - PADL - PADR) / 2 : (i / (n - 1)) * (W - PADL - PADR));
  const y = v => H - PADB - (v / max) * (H - PADT - PADB);
  const totalPts = data.map((d, i) => `${x(i).toFixed(1)},${y(d.total).toFixed(1)}`).join(' ');
  const failedPts = data.map((d, i) => `${x(i).toFixed(1)},${y(d.failed).toFixed(1)}`).join(' ');
  const area = `${PADL},${H - PADB} ${totalPts} ${x(n - 1).toFixed(1)},${H - PADB}`;
  const grid = [0, 0.5, 1].map(f => {
    const yy = y(f * max);
    return `<line x1="${PADL}" y1="${yy.toFixed(1)}" x2="${W - PADR}" y2="${yy.toFixed(1)}" class="trend-grid"/>
            <text x="${PADL - 6}" y="${(yy + 4).toFixed(1)}" class="trend-axis" text-anchor="end">${Math.round(f * max)}</text>`;
  }).join('');
  const step = Math.max(1, Math.ceil(n / 8));
  const labels = data.map((d, i) => (i % step === 0 || i === n - 1)
    ? `<text x="${x(i).toFixed(1)}" y="${H - 8}" class="trend-axis" text-anchor="middle">${escHtml(d.bucket.slice(5))}</text>`
    : '').join('');
  const svg = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" class="trend-svg">
    ${grid}
    <polygon points="${area}" class="trend-area"/>
    <polyline points="${totalPts}" class="trend-line-total"/>
    <polyline points="${failedPts}" class="trend-line-failed"/>
    ${labels}
  </svg>
  <div class="trend-legend">
    <span><i class="dot dot-total"></i>总数</span>
    <span><i class="dot dot-failed"></i>失败</span>
  </div>`;
  setHtmlIfChanged(el, svg);
}

// Day (columns) x hour (rows, local) calendar heatmap built from the hourly
// buckets, covering every day in the selected range.
function ovRenderHeatmap(el, heatmap, after, before) {
  const counts = {};
  let max = 1;
  for (const row of heatmap) {
    const loc = ovLocalBucket(row.bucket);
    if (!loc) continue;
    const k = `${loc.dayKey}#${loc.hour}`;
    counts[k] = (counts[k] || 0) + row.total;
    if (counts[k] > max) max = counts[k];
  }
  const start = new Date(after), end = new Date(before);
  const days = [];
  if (!isNaN(start.getTime()) && !isNaN(end.getTime())) {
    const cur = new Date(start.getFullYear(), start.getMonth(), start.getDate());
    const last = new Date(end.getFullYear(), end.getMonth(), end.getDate());
    for (; cur <= last; cur.setDate(cur.getDate() + 1)) {
      days.push({ key: `${cur.getFullYear()}-${ovPad(cur.getMonth() + 1)}-${ovPad(cur.getDate())}`, label: `${ovPad(cur.getMonth() + 1)}-${ovPad(cur.getDate())}` });
    }
  }
  if (!days.length) { setHtmlIfChanged(el, '<p class="muted">暂无数据</p>'); return; }
  const labelStep = Math.max(1, Math.ceil(days.length / 15));

  let html = '<div class="heat-cal" style="--cols:' + days.length + '">';
  html += '<div class="heat-corner"></div>';
  for (let di = 0; di < days.length; di++) {
    html += `<div class="heat-daylabel">${di % labelStep === 0 ? escHtml(days[di].label.slice(3)) : ''}</div>`;
  }
  for (let h = 0; h < 24; h++) {
    html += `<div class="heat-rowlabel">${h % 3 === 0 ? ovPad(h) : ''}</div>`;
    for (const day of days) {
      const v = counts[`${day.key}#${h}`] || 0;
      const bg = v > 0 ? ` style="background:${ovHeatColor(v / max)}"` : '';
      html += `<div class="heat-cell" title="${day.label} ${ovPad(h)}:00 — ${v} 个任务"${bg}></div>`;
    }
  }
  html += '</div>';
  html += '<div class="heat-legend"><span>少</span>' +
    OV_HEAT_COLORS.map(c => `<i style="background:${c}"></i>`).join('') +
    '<span>多</span></div>';
  setHtmlIfChanged(el, html);
}

function ovRenderAgents(el, a) {
  if (!a.total) { setHtmlIfChanged(el, '<p class="muted">暂无可见节点</p>'); return; }
  const pct = Math.round((a.online / a.total) * 100);
  setHtmlIfChanged(el, `<div class="ns">
    <div class="ns-big"><span class="ns-on">${a.online}</span><span class="ns-slash">/</span><span class="ns-total">${a.total}</span></div>
    <div class="ns-cap">节点在线 <span class="ns-pct">${pct}%</span></div>
    <div class="ns-bar"><div class="ns-bar-on" style="width:${pct}%"></div></div>
    <div class="ns-legend">
      <span class="ns-li"><i class="ns-dot on"></i>在线 ${a.online}</span>
      <span class="ns-li"><i class="ns-dot off"></i>离线 ${a.offline}</span>
    </div>
  </div>`);
}

async function loadOverview(force) {
  if (force && ovRange === 'custom') {
    const from = document.getElementById('ov-from').value;
    if (!from) return; // wait until a start time is chosen
  }
  let res;
  try {
    res = await fetch(`/api/stats/overview?${ovQueryParams()}`, { headers });
  } catch (e) {
    return;
  }
  if (!res.ok) { reportApiError('加载概览失败', res); return; }
  const data = await res.json();
  const heatmap = data.heatmap || [];
  const daily = ovDailySeries(heatmap);

  // Peak hour in the viewer's local time (the backend buckets are UTC).
  const hourTotals = new Array(24).fill(0);
  for (const row of heatmap) {
    const loc = ovLocalBucket(row.bucket);
    if (loc) hourTotals[loc.hour] += row.total;
  }
  const peakHour = hourTotals.some(v => v > 0)
    ? hourTotals.indexOf(Math.max(...hourTotals)) : null;

  ovRenderKpis(document.getElementById('ov-kpis'), { ...(data.summary || {}), peak_hour: peakHour });
  const byTotal = (a, b) => b.total - a.total;
  ovBarList(document.getElementById('ov-status'),
    OV_STATUS_ORDER.filter(s => data.by_status[s]).map(s => ({
      label: OV_STATUS_LABELS[s] || s, total: data.by_status[s],
      cls: s === 'completed' ? 'bar-ok' : (['failed', 'timed_out', 'denied'].includes(s) ? 'bar-fail' : ''),
    })).sort(byTotal));
  ovBarList(document.getElementById('ov-mode'),
    OV_MODE_ORDER.filter(m => data.by_mode[m]).map(m => ({
      label: OV_MODE_LABELS[m], total: data.by_mode[m],
    })).sort(byTotal));
  ovRenderAgents(document.getElementById('ov-agents'), data.agents || { total: 0, online: 0, offline: 0 });
  ovRenderHeatmap(document.getElementById('ov-hourly'), heatmap, data.range.created_after, data.range.created_before);
  ovRenderTrend(document.getElementById('ov-trend'), daily);

  const withFail = (rows, labelKey) => rows.map(r => ({
    label: labelKey(r), total: r.total, failed: r.failed || 0,
  }));
  ovBarList(document.getElementById('ov-by-agent'), withFail(data.by_agent || [], r => agentDisplayName(r.agent_id)));
  ovBarList(document.getElementById('ov-by-user'), withFail(data.by_user || [], r => r.user));
  ovBarList(document.getElementById('ov-by-team'), withFail(data.by_team || [], r => r.name));
}
