// Shared helpers for the AgentHub pages. Identity lives in this browser only.
function identity(){
  return {
    me: localStorage.getItem('hub.me') || 'human@hub',
    token: localStorage.getItem('hub.token') || '',
  };
}

async function api(path, method = 'GET', body = null){
  const { token } = identity();
  const headers = {};
  if (token) headers['Authorization'] = 'Bearer ' + token;
  if (body) headers['Content-Type'] = 'application/json';
  const r = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : null });
  const text = await r.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text }; }
  if (!r.ok || data.error) throw new Error(data.error || ('HTTP ' + r.status));
  return data;
}

// Live updates. One stream per page, shared by every subscriber: a browser allows only
// six connections to the hub, and each open stream holds one. Reconnects on drop.
const listeners = [];
let stream = null;
function subscribe(onEvent){
  listeners.push(onEvent);
  if (stream) return;
  const open = () => {
    stream = new EventSource('/events');
    ['message', 'task', 'stop', 'resume', 'doc'].forEach(name =>
      stream.addEventListener(name, e => {
        let data;
        try { data = JSON.parse(e.data); } catch { return; }
        listeners.forEach(fn => { try { fn({ type: name, data }); } catch {} });
      }));
    stream.onerror = () => { stream.close(); setTimeout(open, 2000); };
  };
  open();
}

function stateDot(state){
  return '<span class="dot ' + state + '" title="' + state + '"></span>';
}

function escapeHtml(s){
  return (s ?? '').toString().replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

// A red bar while any stop is in force, and the human STOP NOW control.
async function paintStops(){
  let bar = document.getElementById('stopbar');
  if (!bar) {
    bar = document.createElement('section');
    bar.id = 'stopbar';
    bar.hidden = true;
    document.querySelector('header').after(bar);
  }
  let stops = [];
  try { stops = (await api('/api/stops')).stops || []; } catch { return; }
  bar.hidden = !stops.length;
  bar.innerHTML = stops.map(s =>
    '<div class="stop"><b>' + (s.scope === 'all' ? 'STOP NOW' : 'STOP ' + escapeHtml(s.target)) +
    '</b> <span class="sid">#' + s.id + '</span> by ' + escapeHtml(s.issuer) +
    (s.domain ? ' <span class="topic">' + escapeHtml(s.domain) + '</span>' : '') +
    ': ' + escapeHtml(s.reason) +
    ' <button class="ghost resume" data-id="' + s.id + '">lift</button></div>').join('');
}

document.addEventListener('click', async e => {
  const lift = e.target.closest('#stopbar .resume');
  if (lift) {
    try { await api('/api/resume', 'POST', { as: identity().me, id: +lift.dataset.id }); }
    catch (err) { alert(err.message); }
    paintStops();
    return;
  }
  if (e.target.id === 'stopnow') {
    const reason = prompt('STOP NOW halts every agent.\nReason (required):');
    if (!reason || !reason.trim()) return;
    try { await api('/api/stop', 'POST', { as: identity().me, reason: reason.trim() }); }
    catch (err) { alert(err.message); }
    paintStops();
  }
});

// Identity editor, shared by both pages.
document.addEventListener('DOMContentLoaded', () => {
  const hdr = document.querySelector('header');
  if (hdr && !document.getElementById('stopnow')) {
    const b = document.createElement('button');
    b.id = 'stopnow';
    b.title = 'Halt every agent (humans only)';
    b.textContent = 'STOP NOW';
    hdr.appendChild(b);
  }
  paintStops();
  setInterval(paintStops, 15000);
  subscribe(ev => { if (ev.type === 'stop' || ev.type === 'resume') paintStops(); });
  const bar = document.getElementById('idbar');
  const btn = document.getElementById('idbtn');
  const me = document.getElementById('me');
  const tok = document.getElementById('tok');
  const save = document.getElementById('save');
  if (!bar || !btn) return;
  const id = identity();
  if (me) me.value = id.me;
  if (tok) tok.value = id.token;
  btn.onclick = () => { bar.hidden = !bar.hidden; if (!bar.hidden && me) me.focus(); };
  if (save) save.onclick = () => {
    localStorage.setItem('hub.me', (me.value || 'human@hub').trim());
    localStorage.setItem('hub.token', tok.value.trim());
    bar.hidden = true;
  };
});

// In the AgentHub Viewer app (its own window, no browser around it) the pages drop the
// parts that only make sense in a browser tab: see html.app in hub.css.
if (navigator.userAgent.includes('AgentHubViewer')) document.documentElement.classList.add('app');

// A task in a window of its own (task.html), one window per task.
function openTask(id){
  window.open('task.html?id=' + id, 'task-' + id, 'popup,width=560,height=720');
}

// The right-click menu on a task (any element with class card or sub and data-id inside
// `root`): open it, or change its status. `after` runs once a change is saved.
function taskMenu(root, after){
  const close = () => document.querySelectorAll('.ctxmenu').forEach(m => m.remove());
  root.addEventListener('contextmenu', e => {
    const el = e.target.closest('.sub[data-id], .card[data-id]');
    if (!el) return;
    e.preventDefault();
    close();
    const id = +el.dataset.id;
    const cur = ['active', 'planning', 'blocked', 'pending', 'done', 'elevated'].find(c => el.classList.contains(c));
    const closed = cur === 'done' || cur === 'elevated';
    const items = [['Open in window', () => openTask(id)], null];
    if (closed) items.push(['Reopen', 'pending']);
    else {
      if (cur !== 'active') items.push(['Start', 'active']);
      if (cur !== 'blocked') items.push(['Block', 'blocked']);
      if (cur !== 'pending') items.push(['Set pending', 'pending']);
      items.push(['Done', 'done']);
    }
    const m = document.createElement('div');
    m.className = 'ctxmenu';
    m.innerHTML = '<div class="ch">#' + id + ' · ' + escapeHtml(cur || '') + '</div>' + items.map((it, i) =>
      it ? '<button data-i="' + i + '">' + escapeHtml(it[0]) + '</button>' : '<hr>').join('');
    document.body.appendChild(m);
    const r = m.getBoundingClientRect();
    m.style.left = Math.max(4, Math.min(e.clientX, innerWidth - r.width - 4)) + 'px';
    m.style.top = Math.max(4, Math.min(e.clientY, innerHeight - r.height - 4)) + 'px';
    m.addEventListener('click', async ev => {
      const b = ev.target.closest('button');
      if (!b) return;
      const act = items[+b.dataset.i][1];
      close();
      if (typeof act === 'function') return act();
      try { await api('/api/task/update', 'POST', { as: identity().me, id, status: act }); }
      catch (err) { alert(err.message); }
      if (after) after();
    });
  });
  if (!taskMenu.wired) {
    taskMenu.wired = true;
    document.addEventListener('mousedown', e => { if (!e.target.closest('.ctxmenu')) close(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
    addEventListener('blur', close);
    document.addEventListener('scroll', close, true);
  }
}
