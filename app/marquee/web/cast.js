/* Atlas casting: the Cast button on title pages, the device picker (a liquid-glass sheet that searches the home
   network), the "Casting to <TV>" bar that stays at the bottom of every page while a TV plays, and Settings > Casting.
   Talks to marquee/cast/blueprint.py (/api/cast/*); the backend pushes "cast-state" events while a cast runs.
   Self-contained: exposes window.MarqueeCast, uses app.css tokens + cast.css. */
(() => {
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const ico = (id, size = 18) => `<svg width="${size}" height="${size}" aria-hidden="true"><use href="#i-${id}"/></svg>`;
const reduced = matchMedia('(prefers-reduced-motion: reduce)');

async function api(path, body, method) {
  const opt = body !== undefined ? { method: method || 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {};
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty */ }
  if (!r.ok || data.ok === false) { const err = new Error(data.message || r.statusText || 'Something went wrong'); err.status = r.status; err.data = data; throw err; }
  return data;
}
function toast(msg, opts = {}) {
  const box = $('#toasts');
  if (!box) return;
  const t = document.createElement('div');
  t.className = 'toast';
  t.innerHTML = `${opts.icon ? ico(opts.icon, 18) : ''}<span>${esc(msg)}</span>${opts.action ? `<button class="btn small on-dark">${esc(opts.action)}</button>` : ''}`;
  const dismiss = () => { t.classList.add('out'); setTimeout(() => t.remove(), 300); };
  if (opts.action) t.querySelector('button').onclick = () => { opts.onAction(); dismiss(); };
  box.append(t);
  setTimeout(dismiss, opts.ms || 4200);
  while (box.children.length > 3) box.firstElementChild.remove();
}
const clock = s => {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${x}` : `${m}:${x}`;
};
const titleOf = it => !it ? '' : it.type === 'Episode' ? (it.series || it.name) : it.name;
const subOf = it => !it ? '' : it.type === 'Episode'
  ? [it.season != null && it.episode != null ? `S${it.season} · E${it.episode}` : '', it.name].filter(Boolean).join(' · ') : String(it.year || '');

/* Icons this file needs (added to the page's sprite once). */
const SPRITE = `
  <symbol id="i-cast" viewBox="0 0 24 24"><path d="M3 8.5V7a2.5 2.5 0 0 1 2.5-2.5h13A2.5 2.5 0 0 1 21 7v10a2.5 2.5 0 0 1-2.5 2.5H14"/><path d="M3 12.2a7.3 7.3 0 0 1 7.3 7.3M3 15.8a3.7 3.7 0 0 1 3.7 3.7"/><circle cx="3.4" cy="19.1" r="1" fill="currentColor" stroke="none"/></symbol>
  <symbol id="i-tv" viewBox="0 0 24 24"><rect x="2.5" y="4.5" width="19" height="13" rx="2.5"/><path d="M8 20.5h8"/></symbol>
  <symbol id="i-tvbox" viewBox="0 0 24 24"><rect x="3" y="9" width="18" height="7.5" rx="3"/><circle cx="7" cy="12.75" r=".9" fill="currentColor" stroke="none"/><path d="M11 12.75h6"/></symbol>
  <symbol id="i-hub" viewBox="0 0 24 24"><path d="M4.5 5.5h15a1.5 1.5 0 0 1 1.5 1.5v8.5a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 15.5V7a1.5 1.5 0 0 1 1.5-1.5Z"/><path d="M6 17l-1 3h14l-1-3"/></symbol>
  <symbol id="i-apple" viewBox="0 0 24 24"><path d="M15.6 3c.1 1.2-.4 2.4-1.1 3.2-.8.8-1.9 1.4-3 1.3-.1-1.2.4-2.4 1.1-3.1.8-.8 2-1.4 3-1.4ZM19.4 16.6c-.5 1.2-.8 1.7-1.5 2.8-.9 1.4-2.2 3.1-3.8 3.1-1.4 0-1.8-.9-3.7-.9s-2.4.9-3.8.9c-1.6 0-2.8-1.6-3.7-3C.7 15.6.4 11 2 8.6c1.1-1.7 2.9-2.7 4.6-2.7s2.8.9 4.2.9c1.4 0 2.2-.9 4.2-.9 1.5 0 3.1.8 4.2 2.2-3.7 2-3.1 7.3.2 8.5Z" fill="currentColor" stroke="none"/></symbol>
  <symbol id="i-pause" viewBox="0 0 24 24"><rect x="6.5" y="4.5" width="4" height="15" rx="1.4" fill="currentColor" stroke="none"/><rect x="13.5" y="4.5" width="4" height="15" rx="1.4" fill="currentColor" stroke="none"/></symbol>
  <symbol id="i-back10" viewBox="0 0 24 24"><path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3M4.5 4v3.4h3.4"/><text x="12" y="15.6" text-anchor="middle" font-size="7.6" font-weight="700" fill="currentColor" stroke="none" font-family="Google Sans, Segoe UI, sans-serif">10</text></symbol>
  <symbol id="i-fwd10" viewBox="0 0 24 24"><path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4v3.4h-3.4"/><text x="12" y="15.6" text-anchor="middle" font-size="7.6" font-weight="700" fill="currentColor" stroke="none" font-family="Google Sans, Segoe UI, sans-serif">10</text></symbol>
  <symbol id="i-cc" viewBox="0 0 24 24"><rect x="2.5" y="5" width="19" height="14" rx="3"/><path d="M10.5 10.2a2.6 2.6 0 1 0 0 3.6M17.5 10.2a2.6 2.6 0 1 0 0 3.6"/></symbol>
  <symbol id="i-audio" viewBox="0 0 24 24"><path d="M3.5 12h2M7.5 8v8M11.5 5v14M15.5 9v6M19.5 11v2"/></symbol>
  <symbol id="i-stop" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2.5" fill="currentColor" stroke="none"/></symbol>
  <symbol id="i-skip" viewBox="0 0 24 24"><path d="M5 5.5v13l9-6.5-9-6.5ZM18.5 5.5v13" fill="none"/></symbol>
  <symbol id="i-shield" viewBox="0 0 24 24"><path d="M12 3 4.5 6v5.5c0 4.6 3.1 8.2 7.5 9.5 4.4-1.3 7.5-4.9 7.5-9.5V6L12 3Z"/><path d="M12 8v4.5M12 15.8v.2"/></symbol>`;
function sprite() {
  const defs = $('svg defs');
  if (defs && !$('#i-cast')) defs.insertAdjacentHTML('beforeend', SPRITE);
}

/* ================================================================== state */
const C = {
  st: null,             // last cast state from the PC
  base: { pos: 0, at: 0 },
  shownEnd: null,       // the cast id whose ending message was already shown
  poll: 0,
  picker: null,         // {target, timer, seq, data, busy}
  dragging: false,
  volDrag: 0,
  menu: null,
};
const active = () => !!(C.st && C.st.active);
const devIcon = d => !d ? 'tv' : d.kind === 'airplay' ? (/Apple TV/.test(d.type) ? 'apple' : 'tv') : /Hub/.test(d.type) ? 'hub' : /TV|BRAVIA/i.test(d.type) && !/Google TV/.test(d.type) ? 'tv' : 'tvbox';

function position() {
  const st = C.st;
  if (!st) return 0;
  if (st.state !== 'playing') return C.base.pos;
  return Math.min(st.duration || Infinity, C.base.pos + (performance.now() - C.base.at) / 1000);
}

function apply(st) {
  const prev = C.st;
  C.st = st || { active: false };
  if (!C.dragging && st) C.base = { pos: st.position || 0, at: performance.now() };
  if (st && !st.active && st.message && st.id && C.shownEnd !== st.id && (prev && prev.active)) {
    C.shownEnd = st.id;
    const where = st.device ? st.device.name : 'the TV';
    if (st.state === 'error') toast(st.message, { icon: 'cast', ms: 9000, ...(/[Ff]irewall/.test(st.message) ? { action: 'Settings', onAction: () => { location.hash = '#/settings/phone'; setTimeout(() => $('#cast-settings')?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 500); } } : {}) });
    else if (st.state === 'ended') toast(`Finished on ${where}`, { icon: 'check' });
    else toast(st.message === 'Stopped casting' ? `Stopped casting to ${where}` : st.message, { icon: 'cast' });
  }
  renderBar();
  syncButtons();
  clearTimeout(C.poll);
  if (active()) C.poll = setTimeout(refresh, 2500);   // events carry the state; this is the safety net
}
async function refresh() {
  try { apply((await api('/api/cast/state')).cast); } catch (e) { if (active()) C.poll = setTimeout(refresh, 4000); }
}

/* ================================================================== title page button */
function button(id, label) {
  const on = active();
  return `<button class="btn round on-dark cast-btn${on ? ' is-on' : ''}" data-cast-id="${esc(id)}" data-cast-label="${esc(label || '')}" title="${on ? `Casting to ${esc(C.st.device.name)}` : 'Cast to a TV'}" aria-label="Cast to a TV">${ico('cast', 19)}</button>`;
}
function syncButtons() {
  for (const b of $$('.cast-btn')) {
    b.classList.toggle('is-on', active());
    b.title = active() ? `Casting to ${C.st.device.name}` : 'Cast to a TV';
  }
}
/* Play a title on the TV that's already casting (the page's Play buttons call this while casting). */
async function castItem(id, start, label) {
  if (!active()) return openPicker({ id, start, label });
  try {
    const r = await api('/api/cast/start', { device: C.st.device.id, id, start: start ?? null });
    apply(r.cast);
    toast(`Playing ${label || titleOf(r.cast.item) || 'it'} on ${C.st.device.name}`, { icon: 'cast' });
  } catch (e) { toast(e.message); }
}

/* ================================================================== picker */
function layer() {
  let el = $('#cast-picker');
  if (el) return el;
  el = document.createElement('div');
  el.className = 'layer cast-layer';
  el.id = 'cast-picker';
  el.hidden = true;
  el.setAttribute('role', 'dialog');
  el.setAttribute('aria-modal', 'true');
  el.setAttribute('aria-label', 'Cast to a TV');
  el.innerHTML = `<div class="scrim" data-cp="close"></div>
    <div class="cast-sheet glass">
      <header class="cp-head"><span class="cp-badge">${ico('cast', 22)}</span>
        <div class="cp-titles"><h2>Cast to a TV</h2><p class="cp-what"></p></div>
        <button class="icon-btn small" data-cp="close" aria-label="Close">${ico('close', 16)}</button></header>
      <div class="cp-body"></div>
      <footer class="cp-foot"><span class="cp-hint">The TV and this PC need to be on the same network.</span>
        <button class="btn small ghost" data-cp="rescan">${ico('restart', 15)} Look again</button></footer>
    </div>`;
  document.body.append(el);
  return el;
}
function openPicker(target) {
  sprite();
  const el = layer();
  C.picker = { target: target || null, seq: 0, data: null, busy: null, opened: performance.now() };
  $('.cp-what', el).textContent = target && target.label ? target.label : (target ? 'Pick a TV' : 'Pick a TV');
  drawPicker();
  el.hidden = false;
  document.documentElement.classList.add('locked');
  requestAnimationFrame(() => el.classList.add('open'));
  loadDevices();
  setTimeout(() => $('.cp-dev, [data-cp="close"]', el)?.focus({ preventScroll: true }), 60);
}
function closePicker() {
  const el = $('#cast-picker');
  if (!el || el.hidden) return;
  clearTimeout(C.picker && C.picker.timer);
  C.picker = null;
  el.classList.remove('open');
  el.classList.add('closing');
  setTimeout(() => { el.hidden = true; el.classList.remove('closing'); if (!$('.layer.open:not(#cast-picker)')) document.documentElement.classList.remove('locked'); }, 260);
}
async function loadDevices(fresh) {
  const P = C.picker;
  if (!P) return;
  const seq = ++P.seq;
  clearTimeout(P.timer);
  let d;
  try { d = await api('/api/cast/devices' + (fresh ? '?fresh=1' : '')); } catch (e) { d = { error: e.message, devices: [] }; }
  if (C.picker !== P || seq !== P.seq) return;
  P.data = d;
  drawPicker();
  const again = d.searching || (d.firewall && d.firewall.pending) || performance.now() - P.opened < 30000;
  P.timer = setTimeout(() => loadDevices(), again ? 1200 : 4000);
}
function fwCard(fw, compact) {
  if (!fw || !fw.checked || fw.allowed) return '';
  const where = `${fw.category === 'Public' ? 'public' : fw.category === 'Domain' ? 'work' : 'private'} network${fw.interface ? ` (${esc(fw.interface)})` : ''}`;
  const why = fw.blocked_by && fw.blocked_by.length
    ? `A Windows Firewall rule (${esc(fw.blocked_by.join(', '))}) blocks Atlas on this ${where}. Turn it off in Windows Security › Firewall › Allow an app, then look again.`
    : `Windows Firewall doesn’t let TVs on this ${where} reach Atlas${!fw.mdns ? ', so they can’t be found either' : ''}. Allowing adds one rule for Atlas and the casting port, for devices on your local network only.`;
  return `<div class="cp-fw${compact ? ' compact' : ''}">${ico('shield', 20)}<div><b>Windows Firewall is in the way</b><p>${why}</p></div>
    ${fw.blocked_by && fw.blocked_by.length ? '' : `<button class="btn small" data-cast-fw>Allow Atlas</button>`}</div>`;
}
function drawPicker() {
  const el = $('#cast-picker'), P = C.picker;
  if (!el || !P) return;
  const body = $('.cp-body', el), d = P.data;
  if (d && d.enabled === false) {
    P.html = null;
    body.innerHTML = `<div class="cp-empty">${ico('cast', 30)}<b>Casting is off</b><p>Turn it on in Settings › Casting.</p>
      <button class="btn small" data-cp="settings">Open Settings</button></div>`;
    return;
  }
  if (P.busy && P.busy.kind === 'airplay-download') {
    const ins = P.busy.install || {};
    const p = ins.total ? ins.done / ins.total : 0;
    P.html = null;
    body.innerHTML = `<div class="cp-empty">${ico('apple', 28)}<b>AirPlay needs a one-time download</b>
      <p>About ${Math.max(1, Math.round((P.busy.size || 0) / 1e6))} MB of AirPlay support for Atlas. It stays on this PC.</p>
      ${ins.state === 'working' ? `<div class="cp-progress"><i style="--p:${p.toFixed(3)}"></i></div><p class="cp-small">${esc(ins.step || 'Downloading…')}</p>`
        : ins.state === 'error' ? `<p class="cp-small">Download failed: ${esc(ins.error || '')}</p><button class="btn small" data-cp="airplay-dl">Try again</button>`
        : `<div class="cp-row-btns"><button class="btn small ghost" data-cp="back">Back</button><button class="btn small" data-cp="airplay-dl">Download</button></div>`}</div>`;
    return;
  }
  const devs = (d && d.devices) || [];
  const searching = !d || d.searching;
  const cur = active() ? C.st.device.id : null;
  P.seen = P.seen || new Set();
  const rows = devs.map((v, k) => `<button class="cp-dev${v.id === cur ? ' is-current' : ''}${P.seen.has(v.id) ? '' : ' fresh'}" data-cp-dev="${esc(v.id)}" style="--k:${k}" ${P.busy ? 'disabled' : ''}>
      <span class="cp-dev-ico">${ico(devIcon(v), 22)}</span>
      <span class="cp-dev-txt"><b>${esc(v.name)}</b><span>${esc(v.type || (v.kind === 'airplay' ? 'AirPlay' : 'Chromecast'))}${v.busy && v.id !== cur ? ' · in use' : ''}</span></span>
      ${v.id === cur ? '<span class="chip solid">Casting</span>' : v.four_k ? '<span class="chip">4K</span>' : ''}
      ${P.busy && P.busy.dev === v.id ? '<span class="cp-spin" aria-label="Connecting"></span>' : ico('right', 16)}</button>`).join('');
  const search = searching ? `<div class="cp-searching" role="status"><span class="cp-radar" aria-hidden="true"><i></i><i></i><i></i>${ico('cast', 18)}</span>
      <span>Searching for TVs<span class="cp-dots"><i>.</i><i>.</i><i>.</i></span></span></div>` : '';
  const empty = !searching && !devs.length ? `<div class="cp-empty">${ico('tv', 30)}<b>No TVs found</b>
      <p>Turn the TV on, and check it’s on the same Wi-Fi as this PC. Chromecast, Google TV, Nest Hub, TVs with Chromecast built in, Apple TV and AirPlay TVs all work.</p></div>` : '';
  const err = d && (d.error || d.endpoint_error) ? `<p class="cp-small cp-err">${esc(d.error || d.endpoint_error)}</p>` : '';
  const stop = cur ? `<button class="cp-dev cp-stop" data-cp="stop"><span class="cp-dev-ico">${ico('stop', 18)}</span><span class="cp-dev-txt"><b>Stop casting</b><span>The TV goes back to its home screen</span></span></button>` : '';
  const html = `${fwCard(d && d.firewall)}${rows ? `<div class="cp-list">${rows}</div>` : ''}${search}${empty}${err}${stop}`;
  devs.forEach(v => P.seen.add(v.id));
  if (html.replace(/ fresh"/g, '"') === P.html) return;   // nothing changed: leave the rows (and their focus) alone
  P.html = html.replace(/ fresh"/g, '"');
  body.innerHTML = html;
}
async function pick(devId) {
  const P = C.picker;
  if (!P || P.busy) return;
  P.busy = { dev: devId };
  drawPicker();
  const t = P.target || {};
  try {
    const r = await api('/api/cast/start', { device: devId, id: t.id || null, start: t.start ?? null });
    P.busy = null;
    closePicker();
    apply(r.cast);
  } catch (e) {
    if (e.data && e.data.reason === 'airplay-download') {
      P.busy = { kind: 'airplay-download', dev: devId, size: e.data.size };
      return drawPicker();
    }
    P.busy = null;
    drawPicker();
    toast(e.message);
  }
}
async function airplayDownload() {
  const P = C.picker;
  if (!P || !P.busy) return;
  const dev = P.busy.dev;
  try { await api('/api/cast/airplay/install', {}); } catch (e) { toast(e.message); return; }
  for (;;) {
    await new Promise(r => setTimeout(r, 700));
    if (C.picker !== P) return;
    let st;
    try { st = await api('/api/cast/airplay'); } catch (e) { continue; }
    P.busy.install = st.install;
    drawPicker();
    if (st.ready || st.install.state === 'done') { P.busy = null; return pick(dev); }
    if (st.install.state === 'error') return;
  }
}

/* ================================================================== casting bar */
function bar() {
  let el = $('#cast-bar');
  if (el) return el;
  sprite();
  el = document.createElement('div');
  el.className = 'cast-bar glass';
  el.id = 'cast-bar';
  el.hidden = true;
  el.setAttribute('role', 'region');
  el.setAttribute('aria-label', 'Casting');
  el.innerHTML = `
    <div class="cb-skip" hidden><button class="btn light small" data-cb="skip">${ico('skip', 15)}<span>Skip intro</span></button></div>
    <div class="cb-main">
      <button class="cb-art" data-cb="open" title="Open the title page"><img alt=""></button>
      <div class="cb-info">
        <span class="cb-eyebrow"><i class="cb-dot"></i>${ico('cast', 14)}<span class="cb-to"></span></span>
        <button class="cb-title" data-cb="open"></button>
        <span class="cb-sub"></span>
      </div>
      <div class="cb-ctrls">
        <button class="cb-btn" data-cb="back" title="Back 10 seconds" aria-label="Back 10 seconds">${ico('back10', 22)}</button>
        <button class="cb-btn cb-play" data-cb="toggle" aria-label="Play or pause">${ico('pause', 22)}</button>
        <button class="cb-btn" data-cb="fwd" title="Forward 10 seconds" aria-label="Forward 10 seconds">${ico('fwd10', 22)}</button>
      </div>
      <div class="cb-vol" title="TV volume">
        <button class="cb-btn small" data-cb="mute" aria-label="Mute the TV">${ico('sound', 18)}</button>
        <input class="cb-range" type="range" min="0" max="100" step="1" value="50" aria-label="TV volume" data-cb-vol>
      </div>
      <div class="cb-menus">
        <button class="cb-btn small" data-cb="subs" title="Subtitles" aria-label="Subtitles" aria-haspopup="menu">${ico('cc', 19)}</button>
        <button class="cb-btn small" data-cb="audio" title="Audio" aria-label="Audio track" aria-haspopup="menu">${ico('audio', 19)}</button>
      </div>
      <button class="btn small ghost cb-stop" data-cb="stop">${ico('stop', 13)} Stop casting</button>
    </div>
    <div class="cb-seek">
      <span class="cb-t cb-pos">0:00</span>
      <div class="cb-track" data-cb-seek role="slider" tabindex="0" aria-label="Position"><i class="cb-buf"></i><i class="cb-fill"></i><i class="cb-knob"></i><span class="cb-tip" hidden></span></div>
      <span class="cb-t cb-dur">0:00</span>
    </div>
    <div class="cb-pop glass" role="menu" hidden></div>`;
  document.body.append(el);
  wireSeek($('[data-cb-seek]', el));
  const vol = $('[data-cb-vol]', el);
  let vt = 0;
  vol.addEventListener('input', () => {
    C.volDrag = performance.now() + 2500;
    vol.style.setProperty('--v', vol.value + '%');
    clearTimeout(vt);
    vt = setTimeout(() => cmd('volume', vol.value / 100), 120);
  });
  return el;
}
const STATE_LABEL = { connecting: 'Connecting…', loading: 'Loading on the TV…', buffering: 'Buffering…', paused: 'Paused' };
function renderBar() {
  const st = C.st;
  const el = active() ? bar() : $('#cast-bar');
  document.body.classList.toggle('casting', active());
  if (!el) return;
  if (!active()) {
    if (!el.hidden) { el.classList.add('leaving'); setTimeout(() => { if (!active()) { el.hidden = true; el.classList.remove('leaving'); } }, 280); }
    closeMenu();
    return;
  }
  el.hidden = false;
  const it = st.item || {};
  const art = it.type === 'Episode' ? (it.thumb || it.backdrop || it.poster) : (it.poster || it.backdrop);
  const img = $('.cb-art img', el);
  if (art && img.dataset.src !== art) { img.dataset.src = art; img.src = art; }
  el.classList.toggle('wide-art', it.type === 'Episode');
  $('.cb-to', el).textContent = `Casting to ${st.device.name}`;
  $('.cb-title', el).textContent = titleOf(it) || 'Loading…';
  const stLabel = STATE_LABEL[st.state] || '';
  $('.cb-sub', el).textContent = [subOf(it), stLabel].filter(Boolean).join(' · ');
  el.dataset.state = st.state;
  const can = new Set(st.can || []);
  const ready = !['connecting', 'loading'].includes(st.state);
  const play = $('[data-cb="toggle"]', el);
  play.innerHTML = ico(st.state === 'paused' ? 'play' : 'pause', 22);
  play.disabled = !ready || (!can.has('pause') && st.state !== 'paused');
  play.setAttribute('aria-label', st.state === 'paused' ? 'Play' : 'Pause');
  for (const b of $$('[data-cb="back"], [data-cb="fwd"]', el)) b.disabled = !ready || !can.has('seek');
  const vol = $('[data-cb-vol]', el);
  $('.cb-vol', el).hidden = !can.has('volume');
  if (performance.now() > C.volDrag && st.volume != null) { vol.value = Math.round(st.volume * 100); vol.style.setProperty('--v', vol.value + '%'); }
  $('[data-cb="mute"]', el).innerHTML = ico(st.muted ? 'muted' : 'sound', 18);
  $('[data-cb="mute"]', el).classList.toggle('is-on', !!st.muted);
  const subsOn = !!st.sub;
  const sb = $('[data-cb="subs"]', el);
  sb.classList.toggle('is-on', subsOn);
  sb.disabled = !(st.subs || []).length || !ready;
  $('[data-cb="audio"]', el).disabled = (st.audios || []).length < 2 || !ready;
  const seg = st.segment;
  const skip = $('.cb-skip', el);
  const showSkip = !!seg && ready && (seg.kind === 'intro' || st.next || seg.kind === 'credits');
  skip.hidden = !showSkip;
  if (showSkip) $('span', skip).textContent = seg.kind === 'intro' ? 'Skip intro' : st.next ? 'Next episode' : 'Skip credits';
  $('.cb-dur', el).textContent = clock(st.duration);
  drawSeek();
  if (C.menu) drawMenu(C.menu);
}
function drawSeek(p) {
  const el = $('#cast-bar');
  if (!el || el.hidden || !C.st) return;
  const d = C.st.duration || 0;
  const pos = p ?? position();
  const f = d ? Math.min(1, Math.max(0, pos / d)) : 0;
  const tr = $('.cb-track', el);
  tr.style.setProperty('--f', f.toFixed(4));
  tr.setAttribute('aria-valuenow', Math.round(pos));
  tr.setAttribute('aria-valuemax', Math.round(d));
  $('.cb-pos', el).textContent = clock(pos);
}
setInterval(() => { if (active() && !C.dragging) drawSeek(); }, 250);
function wireSeek(tr) {
  const at = e => { const r = tr.getBoundingClientRect(); return Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)) * (C.st.duration || 0); };
  const tip = $('.cb-tip', tr);
  tr.addEventListener('pointermove', e => {
    if (!C.st || !C.st.duration) return;
    const t = at(e), r = tr.getBoundingClientRect();
    tip.hidden = false;
    tip.textContent = clock(t);
    tip.style.left = `${Math.min(r.width - 24, Math.max(24, e.clientX - r.left))}px`;
    if (C.dragging) drawSeek(t);
  });
  tr.addEventListener('pointerleave', () => { if (!C.dragging) tip.hidden = true; });
  tr.addEventListener('pointerdown', e => {
    if (!C.st || !C.st.duration || !(C.st.can || []).includes('seek')) return;
    C.dragging = true;
    tr.setPointerCapture(e.pointerId);
    tr.classList.add('drag');
    drawSeek(at(e));
  });
  const end = e => {
    if (!C.dragging) return;
    C.dragging = false;
    tr.classList.remove('drag');
    tip.hidden = true;
    const t = at(e);
    C.base = { pos: t, at: performance.now() };
    drawSeek(t);
    cmd('seek', t);
  };
  tr.addEventListener('pointerup', end);
  tr.addEventListener('pointercancel', () => { C.dragging = false; tr.classList.remove('drag'); });
  tr.addEventListener('keydown', e => {
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); seekBy(e.key === 'ArrowLeft' ? -10 : 10); }
  });
}
function seekBy(sec) {
  const t = Math.max(0, position() + sec);
  C.base = { pos: t, at: performance.now() };
  drawSeek(t);
  cmd('seek', t);
}
async function cmd(action, value) {
  try {
    const r = await api('/api/cast/cmd', { action, value: value ?? null });
    if (r.cast && action !== 'volume') apply(r.cast);
  } catch (e) { toast(e.message); }
}

/* subtitle / audio menus (a small glass menu above the button) */
function openMenu(kind, btn) {
  if (C.menu === kind) return closeMenu();
  C.menu = kind;
  drawMenu(kind, btn);
}
function drawMenu(kind, btn) {
  const el = $('#cast-bar'), pop = el && $('.cb-pop', el), st = C.st;
  if (!pop || !st) return;
  btn = btn || $(`[data-cb="${kind}"]`, el);
  let items;
  if (kind === 'subs') {
    items = [{ v: 'off', label: 'Off', on: !st.sub }, ...(st.subs || []).map(s => ({ v: s.id, label: s.label, on: st.sub === s.id, tag: s.kind === 'image' ? 'burned in' : '' }))];
  } else {
    items = (st.audios || []).map(a => ({ v: String(a.index), label: a.label, on: String(st.audio) === String(a.index) }));
  }
  pop.innerHTML = `<p class="cb-pop-h">${kind === 'subs' ? 'Subtitles' : 'Audio'}</p>` + items.map(x =>
    `<button role="menuitemradio" aria-checked="${x.on}" data-cb-pick="${kind}" data-v="${esc(x.v)}">${x.on ? ico('check', 15) : '<i></i>'}<span>${esc(x.label)}</span>${x.tag ? `<em>${esc(x.tag)}</em>` : ''}</button>`).join('');
  pop.hidden = false;
  const br = btn.getBoundingClientRect(), er = el.getBoundingClientRect();
  const w = pop.offsetWidth;
  pop.style.left = `${Math.max(8, Math.min(er.width - w - 8, br.left - er.left + br.width / 2 - w / 2))}px`;
}
function closeMenu() {
  C.menu = null;
  const pop = $('#cast-bar .cb-pop');
  if (pop) pop.hidden = true;
}

function openTitle() {
  const it = C.st && C.st.item;
  if (!it || !window.MarqueeUI || !MarqueeUI.open) return;
  MarqueeUI.open(it.type === 'Episode' && it.series_id ? it.series_id : it.id);
}

/* ================================================================== events */
document.addEventListener('click', e => {
  const t = e.target;
  if (!t.closest) return;
  const cb = t.closest('.cast-btn');
  if (cb) {
    e.stopPropagation();
    if (active() && C.st.item && cb.dataset.castId === C.st.item.id) return openPicker({ id: cb.dataset.castId, label: cb.dataset.castLabel });
    return openPicker({ id: cb.dataset.castId, label: cb.dataset.castLabel });
  }
  const cp = t.closest('[data-cp], [data-cp-dev], [data-cast-fw]');
  if (cp) {
    if (cp.dataset.cpDev) return pick(cp.dataset.cpDev);
    if ('castFw' in cp.dataset) return allowFirewall(cp);
    const a = cp.dataset.cp;
    if (a === 'close') return closePicker();
    if (a === 'rescan') { if (C.picker) { C.picker.data = null; C.picker.opened = performance.now(); drawPicker(); } return loadDevices(true); }
    if (a === 'stop') { closePicker(); return cmd('stop'); }
    if (a === 'settings') { closePicker(); location.hash = '#/settings/phone'; return; }
    if (a === 'airplay-dl') return airplayDownload();
    if (a === 'back') { if (C.picker) C.picker.busy = null; return drawPicker(); }
    return;
  }
  const pk = t.closest('[data-cb-pick]');
  if (pk) {
    const kind = pk.dataset.cbPick, v = pk.dataset.v;
    closeMenu();
    return cmd(kind === 'subs' ? 'sub' : 'audio', kind === 'audio' ? +v : v);
  }
  const b = t.closest('[data-cb]');
  if (b && b.closest('#cast-bar')) {
    const a = b.dataset.cb;
    if (a !== 'subs' && a !== 'audio') closeMenu();
    if (a === 'toggle') {
      C.base = { pos: position(), at: performance.now() };
      C.st = { ...C.st, state: C.st.state === 'paused' ? 'playing' : 'paused' };
      renderBar();
      return cmd('toggle');
    }
    if (a === 'back') return seekBy(-10);
    if (a === 'fwd') return seekBy(10);
    if (a === 'mute') return cmd('mute');
    if (a === 'stop') return cmd('stop');
    if (a === 'skip') return cmd('skip');
    if (a === 'open') return openTitle();
    if (a === 'subs' || a === 'audio') return openMenu(a, b);
    return;
  }
  if (C.menu && !t.closest('.cb-pop')) closeMenu();
}, true);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (C.menu) { closeMenu(); e.stopPropagation(); return; }
    if (C.picker) { closePicker(); e.stopPropagation(); }
  }
}, true);

async function allowFirewall(btn) {
  btn.disabled = true;
  try {
    const r = await api('/api/cast/firewall', {});
    toast(r.message || 'Windows will ask for permission', { icon: 'shield', ms: 6000 });
    // check again once the person had time to answer Windows
    setTimeout(async () => {
      try { await api('/api/cast/firewall?fresh=1'); } catch (e) { /* ignore */ }
      if (C.picker) loadDevices();
      const box = $('#cast-settings');
      if (box) loadSettings(box);
    }, 9000);
  } catch (e) { toast(e.message); btn.disabled = false; }
}

function onEvent(name, data) {
  if (name === 'cast-state') { apply(data || {}); if (C.picker) drawPicker(); return true; }
  return false;
}

/* ================================================================== Settings > Casting */
function mountSettings(el, s) {
  sprite();
  s = s || {};
  const q = s.cast_quality || 'auto';
  el.innerHTML = `<section class="panel glass cast-panel" id="cast-settings" style="--i:6"><h2>${ico('cast', 20)} Casting</h2>
    <p>Play on a TV with Chromecast or AirPlay, and use Atlas as the remote. The TV streams from this PC, so this PC has to stay on.</p>
    <div class="set"><div class="lbl"><b>Casting</b><span>Cast buttons on title pages and in the player.</span></div>
      <button class="switch" role="switch" data-switch="cast" aria-checked="${s.cast !== false}" aria-label="Casting"></button></div>
    <div class="set stack"><div class="lbl"><b>Quality</b><span>Auto sends the best the TV plays. Original never shrinks the video, so it needs strong Wi-Fi.</span></div>
      <div class="pill-toggle" role="group" aria-label="Casting quality">${[['auto', 'Auto'], ['original', 'Original'], ['1080', '1080p'], ['720', '720p']].map(([v, n]) => `<button data-seg="cast_quality" data-v="${v}" aria-pressed="${q === v}">${n}</button>`).join('')}</div></div>
    <div class="set"><div class="lbl"><b>4K and HDR when the TV supports it</b><span>Off: at most 1080p, with HDR turned into standard colour.</span></div>
      <button class="switch" role="switch" data-switch="cast_4k_hdr" aria-checked="${s.cast_4k_hdr !== false}" aria-label="4K and HDR when the TV supports it"></button></div>
    <div class="cast-set-net"><div class="skel dx-skel-line"></div></div></section>`;
  loadSettings(el);
}
async function loadSettings(el, n = 0) {
  const box = $('.cast-set-net', el);
  if (!box) return;
  let d;
  try { d = await api('/api/cast/devices'); } catch (e) { box.innerHTML = `<p class="cp-small">${esc(e.message)}</p>`; return; }
  if (!document.contains(box)) return;
  if (d.enabled === false) { box.innerHTML = ''; return; }
  const fw = d.firewall || {};
  const fwLine = fw.pending || !fw.checked ? 'Checking Windows Firewall…'
    : fw.allowed ? `TVs on this ${fw.category === 'Public' ? 'public' : (fw.category || 'private').toLowerCase()} network${fw.interface ? ` (${esc(fw.interface)})` : ''} can reach Atlas on port ${d.port}.` : '';
  const devs = d.devices || [];
  box.innerHTML = `${fw.checked && !fw.allowed ? fwCard(fw, true) : `<div class="set"><div class="lbl"><b>Network</b><span>${fwLine}</span></div></div>`}
    <div class="set stack"><div class="lbl"><b>TVs on your network</b><span>${d.searching ? 'Searching…' : devs.length ? '' : 'None found right now.'}</span></div>
      ${devs.length ? `<ul class="cast-set-devs">${devs.map(v => `<li>${ico(devIcon(v), 18)}<b>${esc(v.name)}</b><span>${esc(v.type)}</span></li>`).join('')}</ul>` : ''}</div>`;
  if ((d.searching || fw.pending) && n < 20) setTimeout(() => loadSettings(el, n + 1), 1500);
}

/* ================================================================== start */
sprite();
refresh();
window.MarqueeCast = { button, active, castItem, openPicker, onEvent, mountSettings, state: () => C.st };
})();
