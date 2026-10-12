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
