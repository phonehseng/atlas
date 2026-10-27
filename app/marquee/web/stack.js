/* Your stack: the Media Hub's dashboard inside Atlas (window.MarqueeStack.mount(page)).
   The hub keeps doing the work in the background; this page reads it through /api/stack (marquee/stack_api.py) and
   presses the hub's own buttons. Without a hub it shows Atlas's read-only checks. Needs taste.js (MarqueeKit). */
(() => {
'use strict';
const K = () => window.MarqueeKit;
const ST = { page: null, data: null, timer: 0, seq: 0, open: new Set(['problems']) };
const $ = (s, r) => (r || ST.page).querySelector(s);
const $$ = (s, r) => [...(r || ST.page).querySelectorAll(s)];
const LEVEL = { ok: 'ok', warn: 'warn', error: 'bad', down: 'bad', setup: 'setup', unknown: 'unknown' };
const LABEL = { ok: 'Running', warn: 'Warning', error: 'Problem', down: 'Down', setup: 'Needs setup', unknown: 'Unknown' };

function ago(t) {
  if (!t) return 'never';
  const d = Date.now() / 1000 - t;
  if (d < 60) return 'just now';
  if (d < 3600) return `${Math.round(d / 60)} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`;
  return `${Math.round(d / 86400)} days ago`;
}
function eta(sec) {
  if (sec == null || sec < 0 || sec >= 8640000) return '';
  if (sec < 90) return 'under 2 min';
  if (sec < 3600) return `${Math.round(sec / 60)} min`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} h ${String(Math.round(sec % 3600 / 60)).padStart(2, '0')} min`;
  return `${Math.round(sec / 86400)} days`;
}
function timeleft(s) {
  const m = /^(?:(\d+)\.)?(\d+):(\d+):(\d+)$/.exec(s || '');
  return m ? (((+(m[1] || 0) * 24 + +m[2]) * 60 + +m[3]) * 60 + +m[4]) : null;
}
const gb = n => n == null ? '?' : n >= 1024 ? `${(n / 1024).toFixed(1)} TB` : `${n} GB`;
const mb = b => b == null ? '' : b >= 2 ** 30 ? `${(b / 2 ** 30).toFixed(1)} GB` : `${Math.max(1, Math.round(b / 2 ** 20))} MB`;

/* ================================================================== confirm (shared with the chat) */
function confirmSheet(title, text, okLabel = 'Confirm', danger = false) {
  const { esc } = K();
  return new Promise(resolve => {
    const layer = document.createElement('div');
    layer.className = 'stk-layer';
    layer.setAttribute('role', 'dialog');
    layer.setAttribute('aria-modal', 'true');
    layer.setAttribute('aria-label', title);
    layer.innerHTML = `<div class="stk-scrim" data-no></div><div class="stk-confirm glass"><h3>${esc(title)}</h3><p>${esc(text)}</p>
      <div class="stk-confirm-btns"><button class="btn ghost small" data-no>Cancel</button><button class="btn small${danger ? ' danger' : ''}" data-yes>${esc(okLabel)}</button></div></div>`;
    document.body.append(layer);
    requestAnimationFrame(() => layer.classList.add('open'));
    const done = v => { layer.classList.remove('open'); setTimeout(() => layer.remove(), 250); removeEventListener('keydown', key, true); resolve(v); };
    const key = e => { if (e.key === 'Escape') { e.stopPropagation(); done(false); } };
    addEventListener('keydown', key, true);
    layer.addEventListener('click', e => { if (e.target.closest('[data-yes]')) done(true); else if (e.target.closest('[data-no]')) done(false); });
    setTimeout(() => layer.querySelector('[data-yes]').focus(), 60);
  });
}
async function runAction(action, arg, label, confirm) {
  const { api, toast } = K();
  if (confirm && !(await confirmSheet(label + '?', action === 'restart'
    ? 'The Media Hub restarts it now. Anything playing from it may stop for a moment.' : 'The Media Hub does this now.', label))) return null;
  try {
    const r = await api('/api/stack/action', { action, arg: arg || '' });
    toast(r.message || 'Done', { icon: 'check' });
    if (ST.page && document.contains(ST.page)) setTimeout(() => load(true), 2500);
    return r;
  } catch (e) { toast(e.message); return null; }
}

/* ================================================================== the page */
async function mount(page) {
  ST.page = page;
  page.classList.add('page-stack');
  page.innerHTML = `<div class="page-head"><div class="grow"><span class="eyebrow">Your stack</span><h1 class="stk-title">Checking your stack…</h1>
      <p class="stk-sub">Asking the Media Hub…</p></div>
      <div class="stk-head-acts"><button class="btn small ghost" data-sact="ask">${K().icon('sparkle', 15)} Ask Atlas</button>
      <button class="btn small ghost" data-sact="refresh">${K().icon('restart', 15)} Refresh</button></div></div>
    <div class="stk-update" hidden></div>
    <div class="stk-body"><div class="stk-grid">${'<div class="skel stk-skel"></div>'.repeat(6)}</div></div>`;
  window.MarqueeUpdate && MarqueeUpdate.mountStack($('.stk-update'));
  page.addEventListener('click', onClick);
  page.addEventListener('change', onChange);
  await load();
  clearInterval(ST.timer);
  ST.timer = setInterval(() => { if (!document.contains(page)) return clearInterval(ST.timer); if (!document.hidden) load(); }, 30000);
}

async function load(force) {
  const { api, esc } = K();
  const page = ST.page, seq = ++ST.seq;
  let d;
  try { d = await api('/api/stack' + (force ? '?refresh=1' : '')); }
  catch (e) {
    if (seq !== ST.seq || !document.contains(page)) return;
    $('.stk-title').textContent = 'Couldn’t check your stack';
    $('.stk-sub').textContent = e.message;
    return;
  }
  if (seq !== ST.seq || !document.contains(page)) return;
  ST.data = d;
  draw(d);
}

function draw(d) {
  const { esc, icon } = K();
  const s = d.summary || {};
  $('.stk-title').innerHTML = `<span class="stk-dot lv-${LEVEL[s.level] || 'unknown'}" aria-hidden="true"></span>${esc(s.text || '')}`;
  $('.stk-sub').innerHTML = (d.source === 'hub'
    ? `From the Media Hub at <b>${esc(d.hub.url.replace(/^https?:\/\//, ''))}</b>, which keeps checking in the background. `
    : `Atlas's own read-only checks: no Media Hub was found, so there's no watchdog, request doctor or backups here. `)
    + `<span class="stk-when">Checked ${esc(ago(d.checked))}.</span>` + (d.hub_error ? ` <span class="stk-err">${esc(d.hub_error)}</span>` : '');
  const keep = new Set($$('details[data-sec]').filter(x => x.open).map(x => x.dataset.sec));
  const sections = [
    problemsHTML(d), appsHTML(d), `<div class="stk-two">${doctorHTML(d)}${downloadsHTML(d)}</div>`,
    `<div class="stk-two">${serverHTML(d)}${healthHTML(d)}</div>`, `<div class="stk-two">${activityHTML(d)}${backupsHTML(d)}</div>`,
    `<div class="stk-two">${phoneHTML(d)}${connectHTML(d)}</div>`,
  ];
  $('.stk-body').innerHTML = sections.join('');
  $$('details[data-sec]').forEach(x => { if (keep.has(x.dataset.sec)) x.open = true; });
}

function fixBtn(f, cls = 'btn small') {
  const { esc, icon } = K();
  if (!f) return '';
  return `<button class="${cls}" data-sact="fix" data-action="${esc(f.action)}" data-arg="${esc(f.arg || '')}" data-label="${esc(f.label)}" data-confirm="${f.confirm ? 1 : ''}">${icon('restart', 14)} ${esc(f.label)}</button>`;
}

function problemsHTML(d) {
  const { esc, icon } = K();
  const probs = (d.problems || []).filter(p => p.level !== 'setup');
  const setup = (d.problems || []).filter(p => p.level === 'setup');
  if (!probs.length && !setup.length) return `<section class="panel glass stk-ok" style="--i:0"><span class="stk-okmark">${icon('check', 22)}</span><div><h2>Nothing needs you</h2><p>Every check came back fine.</p></div></section>`;
  return `<section class="panel glass stk-problems" style="--i:0"><h2>${icon('info')} ${probs.length ? `${probs.length} thing${probs.length === 1 ? '' : 's'} to look at` : 'Waiting for setup'}</h2>
    <ul class="stk-plist">${probs.map(p => `<li class="lv-${LEVEL[p.level] || 'unknown'}"><span class="stk-dot lv-${LEVEL[p.level] || 'unknown'}"></span>
      <div class="grow"><b>${esc(p.name)}</b><span>${esc(p.text)}${p.link ? ` <a class="link" href="${esc(p.link)}" target="_blank" rel="noopener">Why?</a>` : ''}</span></div>${fixBtn(p.fix)}</li>`).join('')}
      ${setup.map(p => `<li class="lv-setup"><span class="stk-dot lv-setup"></span><div class="grow"><b>${esc(p.name)}</b><span>${esc(p.text)}</span></div></li>`).join('')}</ul></section>`;
}

function appsHTML(d) {
  const { esc, icon } = K();
  const apps = d.apps || [];
  return `<section class="stk-apps" aria-label="Apps">${apps.map((a, k) => {
    const lv = LEVEL[a.status] || 'unknown';
    const ms = a.ms != null ? `<span class="stk-ms ${a.ms > 1500 ? 'slow' : ''}" title="How long its page took to answer just now">${a.ms} ms</span>` : '';
    return `<article class="stk-app glass lv-${lv}" style="--i:${k}">
      <div class="stk-app-top"><span class="stk-dot lv-${lv}" aria-hidden="true"></span><b>${esc(a.name)}</b><span class="stk-state">${esc(LABEL[a.status] || a.status)}</span></div>
      <span class="stk-role">${esc(a.role || '')}</span>
      <p class="stk-sum">${esc(a.summary || '')}</p>
      <div class="stk-app-foot"><span class="stk-ver">${a.version ? 'v' + esc(String(a.version).replace(/^v/, '')) : ''}</span>${ms}
        ${(a.fixes || []).map(f => fixBtn(f, 'stk-mini')).join('')}
        ${a.open ? `<a class="stk-mini" href="${esc(a.open)}" target="_blank" rel="noopener" title="Open ${esc(a.name)} in your browser">Open ↗</a>` : ''}</div></article>`;
  }).join('')}</section>`;
}

const DOC_STATE = { downloading: 'Downloading', waiting: 'Not out yet', airing: 'Airing', searching: 'Searching', available: 'Arrived',
  pending: 'Needs approval', partial: 'Partly here', error: 'Couldn’t check', stuck: 'Stuck' };
function doctorHTML(d) {
  const { esc, icon } = K();
  const doc = d.doctor;
  const hub = d.source === 'hub';
  const head = `<h2>${icon('plus')} Requests on the way</h2>`;
  if (!doc) return `<section class="panel glass" style="--i:2">${head}<p>${hub ? 'The request doctor hasn’t written a report yet.' : 'Seerr isn’t connected, so Atlas can’t list requests.'}</p></section>`;
  const items = doc.items || [];
  return `<section class="panel glass stk-doc" style="--i:2">${head}
    <p>${doc.local ? 'What Atlas can see from Seerr and the download queue.' : `Why each request isn't here yet, from the request doctor (${esc(ago(doc.time))}${doc.running ? ', checking again now' : ''}).`}</p>
    ${items.length ? `<ul class="stk-list">${items.map(i => `<li><div class="grow"><b>${esc(i.title)}${i.year ? ` <span class="stk-y">(${esc(i.year)})</span>` : ''}</b>
      <span>${esc(i.reason || '')}</span>${(i.fixed || []).length ? `<span class="stk-fixed">${icon('check', 12)} ${esc(i.fixed.join('; '))}</span>` : ''}</div>
      <span class="stk-tag st-${esc(i.state)}">${esc(DOC_STATE[i.state] || i.state)}</span></li>`).join('')}</ul>` : '<p class="stk-none">Every request has arrived.</p>'}
    ${hub ? `<div class="stk-acts"><button class="btn small ghost" data-sact="fix" data-action="requests" data-label="Check every request again">${icon('restart', 14)} Check every request again</button></div>` : ''}</section>`;
}

function downloadsHTML(d) {
  const { esc, icon } = K();
  const dl = d.downloads || {};
  const items = [];
  const byTitle = new Map();
  for (const q of dl.queue || []) {
    if (byTitle.has(q.title)) { byTitle.get(q.title).n++; continue; }   // a season pack: one row, not one per episode
    const it = { title: q.title, p: q.progress || 0, eta: eta(timeleft(q.timeleft)), st: q.status, warn: q.warning, n: 1 };
    byTitle.set(q.title, it); items.push(it);
  }
  const seen = new Set(items.map(i => (i.title || '').toLowerCase()));
  for (const t of dl.torrents || []) if (t.progress < 100 && !seen.has((t.name || '').toLowerCase())) items.push({ title: t.name, p: t.progress || 0, eta: eta(t.eta), st: t.state, stalled: /stalled|meta/i.test(t.state || '') && !t.dlspeed });
  items.sort((a, b) => b.p - a.p);
  const vpn = d.system && d.system.vpn;
  const conn = dl.connection;
  return `<section class="panel glass stk-dl" style="--i:3"><h2>${icon('cloud-off')} Downloads</h2>
    <p>${esc(dl.summary || (items.length ? '' : 'Nothing downloading.'))}${conn ? ` · qBittorrent ${esc(conn)}` : ''}${vpn === true ? ' · through the VPN' : vpn === false ? ' · <b class="stk-err">VPN off</b>' : ''}</p>
    ${items.length ? `<ul class="stk-list">${items.slice(0, 10).map(i => `<li class="stk-dlrow"><div class="grow"><b class="stk-clip">${esc(i.title)}</b>
      <div class="stk-bar"><i style="width:${Math.max(2, Math.min(100, i.p))}%"></i></div>
      <span>${Math.round(i.p)}%${i.n > 1 ? ` · ${i.n} episodes` : ''}${i.eta ? ' · ' + esc(i.eta) + ' left' : ''}${i.stalled ? ' · stalled (no one sharing right now)' : ''}${i.warn ? ' · ' + esc(i.warn) : ''}</span></div></li>`).join('')}</ul>
      ${items.length > 10 ? `<p class="stk-none">and ${items.length - 10} more</p>` : ''}` : ''}</section>`;
}

function serverHTML(d) {
  const { esc, icon } = K();
  const sys = d.system || {};
  const disk = sys.disk;
  const used = disk && disk.total_gb ? Math.round(100 * (1 - disk.free_gb / disk.total_gb)) : null;
  const vc = sys.vpncheck;
  const vpnTxt = sys.vpn === true ? 'Connected' : sys.vpn === false ? 'Disconnected: torrents are paused' : 'Not checked here';
  const w = d.hub && d.hub.watchdog;
  return `<section class="panel glass" style="--i:4"><h2>${icon('film')} This server</h2>
    <div class="set"><div class="lbl"><b>Library disk</b><span>${disk ? `${gb(disk.free_gb)} free${disk.total_gb ? ' of ' + gb(disk.total_gb) : ''}` : 'Not checked'}</span></div>
      ${used != null ? `<div class="stk-ring" style="--p:${used}" title="${used}% used"><span>${used}%</span></div>` : ''}</div>
    <div class="set"><div class="lbl"><b>VPN</b><span>${esc(vpnTxt)}</span></div><span class="stk-dot lv-${sys.vpn === true ? 'ok' : sys.vpn === false ? 'bad' : 'unknown'}"></span></div>
    ${vc ? `<div class="set"><div class="lbl"><b>VPN leak check</b><span>${esc(vc.text || vc.status)} · ${esc(ago(vc.time))}</span></div><span class="stk-tag st-${vc.status === 'ok' ? 'available' : 'error'}">${esc(vc.status === 'ok' ? 'No leak' : vc.status)}</span></div>` : ''}
    ${w ? `<div class="set"><div class="lbl"><b>Watchdog</b><span>${w.on ? `Restarts apps that go down; checks every ${esc(w.minutes)} min, last ${esc(ago(w.checked))}` : 'Off'}</span></div></div>` : ''}</section>`;
}

function healthHTML(d) {
  const { esc, icon } = K();
  const h = d.health;
  const hub = d.source === 'hub';
  const run = hub ? `<div class="stk-acts"><button class="btn small ghost" data-sact="fix" data-action="healthcheck" data-label="Run the health check now">${icon('restart', 14)} Run it now</button></div>` : '';
  if (!h) return `<section class="panel glass" style="--i:5"><h2>${icon('check')} Health check</h2><p>${hub ? 'No report yet. The health check runs every hour and fixes what is safe to fix.' : 'The hourly health check comes with the Media Hub.'}</p>${run}</section>`;
  const list = (arr, cls, ico) => (arr || []).map(x => `<li class="${cls}">${icon(ico, 13)}<span>${esc(x)}</span></li>`).join('');
  return `<section class="panel glass stk-health" style="--i:5"><h2>${icon('check')} Health check</h2>
    <p>${esc(h.title || `${(h.attention || []).length} need you, ${(h.fixed || []).length} fixed`)} · ${esc(ago(h.time))}</p>
    <ul class="stk-hl">${list(h.attention, 'need', 'info')}${list(h.fixed, 'fixed', 'check')}</ul>
    ${h.source === 'event' && h.message && !(h.attention || []).length ? `<p class="stk-none">${esc(h.message)}</p>` : ''}
    ${(h.ok || []).length ? `<details data-sec="health-ok"><summary>${(h.ok || []).length} checks passed</summary><ul class="stk-hl">${list(h.ok, 'ok', 'check')}</ul></details>` : ''}${run}</section>`;
}

function activityHTML(d) {
  const { esc, icon } = K();
  const inc = d.incidents;
  const ev = (d.events || []).filter(e => e.level !== 'info').slice(0, 10);
  const recent = (d.events || []).slice(0, 8);
  const incHTML = inc && inc.length ? `<ul class="stk-list">${inc.slice(0, 8).map(i => `<li><div class="grow"><b>${esc(i.name)} ${i.resolved ? 'was down' : 'is down'}</b>
      <span>${esc(i.time.replace('T', ' ').slice(0, 16))}${i.resolved ? ' – ' + esc(String(i.resolved).slice(11, 16)) : ''} · ${esc(i.action || '')}</span><span>${esc(i.diagnosis || '')}</span></div>
      <span class="stk-tag st-${i.resolved ? 'available' : 'error'}">${i.resolved ? 'Recovered' : 'Open'}</span></li>`).join('')}</ul>` : '';
  const evHTML = (list) => `<ul class="stk-list">${list.map(e => `<li><span class="stk-dot lv-${e.level === 'error' ? 'bad' : e.level === 'warn' ? 'warn' : 'ok'}"></span><div class="grow"><b>${esc(e.title)}</b><span>${esc(e.message || '')}</span></div><span class="stk-y">${esc(ago(e.time))}</span></li>`).join('')}</ul>`;
  let body;
  if (inc && inc.length) body = `<p>When an app went down, what the watchdog did, and why.</p>${incHTML}`;
  else if (ev.length) body = `<p>${inc ? 'No incidents. ' : ''}Recent warnings from the apps and the health check.</p>${evHTML(ev)}`;
  else if (recent.length) body = `<p>No incidents or warnings lately. The latest activity:</p>${evHTML(recent)}`;
  else body = `<p>${d.source === 'hub' ? 'Nothing has happened lately.' : 'Activity and incidents come from the Media Hub.'}</p>`;
  return `<section class="panel glass stk-activity" style="--i:6"><h2>${icon('restart')} Recent incidents</h2>${body}</section>`;
}

function backupsHTML(d) {
  const { esc, icon } = K();
  const b = d.backups;
  const head = `<h2>${icon('cloud-off')} Backups</h2>`;
  if (!b) return `<section class="panel glass" style="--i:7">${head}<p>${d.source === 'hub' ? 'This hub doesn’t report backups.' : 'Nightly backups of every app’s settings come with the Media Hub.'}</p></section>`;
  const items = b.items || [];
  return `<section class="panel glass stk-backups" style="--i:7">${head}<p>Every app's settings, saved nightly. Restoring puts them back (a safety backup is made first).</p>
    ${b.error ? `<p class="stk-err">${esc(b.error)}</p>` : ''}
    ${items.length ? `<ul class="stk-list">${items.slice(0, 8).map(x => `<li><div class="grow"><b>${esc(new Date(x.made * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }))}</b>
      <span>${esc(x.label || 'manual')} · ${esc(x.size_mb)} MB · ${esc((x.apps || []).length)} apps${x.problems ? ` · ${esc(x.problems)} problem(s)` : ''}</span></div>
      ${b.can_restore ? `<button class="stk-mini" data-sact="restore" data-name="${esc(x.name)}" data-when="${esc(new Date(x.made * 1000).toLocaleString())}">Restore…</button>` : ''}</li>`).join('')}</ul>` : '<p class="stk-none">No backups yet.</p>'}
    <div class="stk-acts"><button class="btn small ghost" data-sact="fix" data-action="backup" data-label="Back up now">${icon('check', 14)} Back up now</button></div></section>`;
}

function phoneHTML(d) {
  const { esc, icon } = K();
  const ph = d.phone;
  const head = `<h2>${icon('phone')} Phone alerts</h2>`;
  if (!ph) return `<section class="panel glass" style="--i:8">${head}<p>Alerts to your phone (ready to watch, problems, VPN leaks) are sent by the Media Hub through the free ntfy app.</p></section>`;
  return `<section class="panel glass stk-phone" style="--i:8">${head}<p>“Ready to watch”, problems and VPN leaks, sent to the free <b>ntfy</b> app on your phone. Only titles and short messages leave this server.</p>
    <div class="set"><div class="lbl"><b>Send alerts to my phone</b></div><button class="switch" role="switch" aria-checked="${!!ph.on}" data-sact="phone-toggle" aria-label="Phone alerts"></button></div>
    ${ph.on && ph.topic ? `<div class="set stack"><div class="lbl"><b>Subscribe in ntfy</b><span>Open ntfy, tap +, and enter this topic${ph.server && !/ntfy\.sh$/.test(ph.server) ? ` with the server ${esc(ph.server)}` : ''}. The topic is the only key, so keep it to yourself.</span></div>
      <div class="stk-topic"><code class="stk-secret" data-sact="reveal" title="Click to show" tabindex="0">${esc(ph.topic)}</code><button class="stk-mini" data-sact="copy" data-v="${esc(ph.topic)}">Copy</button></div></div>
      <div class="stk-acts"><button class="btn small ghost" data-sact="phone-test">${icon('phone', 14)} Send a test</button></div>` : ''}
    ${ph.can_notify !== false ? `<div class="set"><div class="lbl"><b>Desktop notifications on the server</b></div><button class="switch" role="switch" aria-checked="${!!ph.notifications}" data-sact="notif-toggle" aria-label="Desktop notifications"></button></div>` : ''}
    ${d.hub && (d.hub.features || []).includes('info') ? `<div class="set"><div class="lbl"><b>Media Hub web page</b><span>With Atlas, the hub's own page can just point here. Its API and background jobs keep running either way.</span></div>
      <div class="pill-toggle" role="group" aria-label="Media Hub web page"><button data-sact="hub-ui" data-v="marquee" aria-pressed="${ph.ui === 'marquee'}">Point to Atlas</button><button data-sact="hub-ui" data-v="hub" aria-pressed="${ph.ui !== 'marquee'}">Full dashboard</button></div></div>` : ''}</section>`;
}

function connectHTML(d) {
  const { esc, icon } = K();
  const s = d.settings || {};
  return `<section class="panel glass stk-connect" style="--i:9"><h2>${icon('gear')} Connection</h2>
    <p>${s.found_hub ? `Using the Media Hub at ${esc(s.found_hub)}${s.hub_configured ? '' : ' (found by itself)'}.` : 'No Media Hub found. Atlas looks at this PC (port 8585) unless you give it an address.'}</p>
    <form class="fields stk-form" data-sform>
      <label>Media Hub address<input class="input wide" name="hub_url" value="${esc(s.hub_url || '')}" placeholder="http://127.0.0.1:8585" spellcheck="false"></label>
      <label>Stack settings file (mediastack.json)<input class="input wide" name="stack_config" value="${esc(s.stack_config || '')}" placeholder="C:\\MediaStack\\config\\shared\\mediastack.json" spellcheck="false"></label>
      <label>Hub token (only for a hub on another computer)<input class="input wide" name="hub_token" type="password" placeholder="${s.hub_token_set ? 'Saved (type to replace)' : 'From the hub: /api/token'}" autocomplete="off"></label>
      <div><button class="btn small">Save</button></div></form></section>`;
}

/* ================================================================== clicks */
async function onClick(e) {
  const b = e.target.closest('[data-sact]');
  if (!b) return;
  const { api, toast } = K();
  const act = b.dataset.sact;
  if (act === 'refresh') { b.disabled = true; await load(true); b.disabled = false; }
  else if (act === 'ask') { location.hash = '#/chat'; setTimeout(() => window.MarqueeChat && window.MarqueeChat.send('Is everything ok?'), 700); }
  else if (act === 'fix') { b.disabled = true; await runAction(b.dataset.action, b.dataset.arg, b.dataset.label, !!b.dataset.confirm); b.disabled = false; }
  else if (act === 'restore') {
    if (!(await confirmSheet('Restore this backup?', `Every app's settings go back to ${b.dataset.when}. The apps restart (a safety backup of today's settings is made first).`, 'Restore', true))) return;
    try { const r = await api('/api/stack/restore', { name: b.dataset.name, confirm: true }); toast(r.message || 'Restoring', { icon: 'check', ms: 6000 }); }
    catch (err) { toast(err.message); }
  }
  else if (act === 'phone-toggle' || act === 'notif-toggle') {
    const on = b.getAttribute('aria-checked') !== 'true';
    b.setAttribute('aria-checked', on);
    try { await api('/api/stack/phone', { changes: { [act === 'phone-toggle' ? 'phone' : 'notifications']: on } }); load(true); }
    catch (err) { b.setAttribute('aria-checked', !on); toast(err.message); }
  }
  else if (act === 'phone-test') { try { toast((await api('/api/stack/phone', { test: true })).message, { icon: 'phone' }); } catch (err) { toast(err.message); } }
  else if (act === 'hub-ui') {
    try { await api('/api/stack/phone', { changes: { ui: b.dataset.v } }); $$('[data-sact="hub-ui"]').forEach(x => x.setAttribute('aria-pressed', x === b)); toast('Saved', { icon: 'check' }); }
    catch (err) { toast(err.message); }
  }
  else if (act === 'reveal') b.classList.toggle('shown');
  else if (act === 'copy') { try { await navigator.clipboard.writeText(b.dataset.v); toast('Copied', { icon: 'check' }); } catch (err) { toast('Couldn’t copy'); } }
}
async function onChange() {}
document.addEventListener('submit', async e => {
  const f = e.target.closest && e.target.closest('[data-sform]');
  if (!f) return;
  e.preventDefault();
  const { api, toast } = K();
  try {
    await api('/api/stack/settings', { hub_url: f.hub_url.value, stack_config: f.stack_config.value, hub_token: f.hub_token.value });
    toast('Saved', { icon: 'check' });
    load(true);
  } catch (err) { toast(err.message); }
});

window.MarqueeStack = { mount, runAction, confirm: confirmSheet };
})();
