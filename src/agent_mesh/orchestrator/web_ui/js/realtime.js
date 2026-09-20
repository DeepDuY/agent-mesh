// Live dashboard updates over Server-Sent Events.
//
// We parse the SSE stream from a `fetch` response (instead of EventSource) so
// the normal Authorization header is used — a token in the query string would
// end up in access logs. The legacy interval polling stays active as a
// fallback (files/skills/templates/config, and while the stream reconnects).

let realtimeController = null;
let realtimeRetry = 0;
const realtimeDebounce = {};

function startRealtime() {
  stopRealtime();
  realtimeRetry = 0;
  connectRealtime();
}

function stopRealtime() {
  if (realtimeController) {
    realtimeController.abort();
    realtimeController = null;
  }
}

async function connectRealtime() {
  if (!TOKEN) return;
  const controller = new AbortController();
  realtimeController = controller;
  try {
    const res = await fetch('/api/realtime', { headers, signal: controller.signal });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
    realtimeRetry = 0;
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buffer.indexOf('\n\n')) >= 0) {
        handleSseFrame(buffer.slice(0, sep));
        buffer = buffer.slice(sep + 2);
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') return; // deliberate stop/disconnect
  }
  if (realtimeController !== controller) return; // superseded by a newer connection
  realtimeController = null;
  // Reconnect with capped backoff; interval polling covers the gap meanwhile.
  realtimeRetry = Math.min(realtimeRetry + 1, 6);
  setTimeout(connectRealtime, 1000 * realtimeRetry);
}

function handleSseFrame(frame) {
  for (const line of frame.split('\n')) {
    if (!line.startsWith('data:')) continue; // ignore comments/keepalives
    let event;
    try { event = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }
    if (event.type === 'tasks_changed') scheduleRealtimeReload('tasks');
    else if (event.type === 'agents_changed') scheduleRealtimeReload('agents');
    else if (event.type === 'task_log') {
      // Only refresh the log stream if its task is currently open.
      if (typeof taskLogTaskId !== 'undefined' && taskLogTaskId === event.task_id) {
        pollTaskLogs();
      }
    }
  }
}

function scheduleRealtimeReload(key) {
  if (realtimeDebounce[key]) return;
  realtimeDebounce[key] = setTimeout(() => {
    realtimeDebounce[key] = null;
    if (key === 'tasks') loadTasks();
    else if (key === 'agents') loadAgents();
  }, 250);
}
