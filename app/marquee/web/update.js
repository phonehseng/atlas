/* Marquee updates: the top-bar pill ("Update to 1.4.0"), the update sheet (notes, Update now / Later / Skip),
   Settings > About, the "Updated to 1.4.0" toast after a restart, and the quiet "newer Media Stack" line on Your stack.
   Talks to marquee/updater.py (/api/update/...). Self-contained: exposes window.MarqueeUpdate, styles in update.css. */
(() => {
'use strict';

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const $ = (s, r = document) => r.querySelector(s);
const ico = (id, size = 16) => `<svg width="${size}" height="${size}" aria-hidden="true"><use href="#i-${id}"/></svg>`;
const UP = size => `<svg width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 17V7.5M7.5 11.5 12 7l4.5 4.5"/><circle cx="12" cy="12" r="9.2"/></svg>`;

async function api(path, body) {
  const opt = body !== undefined ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {};
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty */ }
  if (!r.ok || data.ok === false) { const err = new Error(data.message || r.statusText || 'Something went wrong'); err.status = r.status; throw err; }
  return data;
}
function toast(msg, opts = {}) {
  let box = $('#toasts');
  if (!box) { box = document.createElement('div'); box.id = 'toasts'; box.className = 'toasts'; document.body.append(box); }
  const t = document.createElement('div');
  t.className = 'toast';
  t.innerHTML = `${opts.icon ? ico(opts.icon, 18) : ''}<span>${esc(msg)}</span>${opts.action ? `<button class="btn small on-dark">${esc(opts.action)}</button>` : ''}`;
  const dismiss = () => { t.classList.add('out'); setTimeout(() => t.remove(), 300); };
  if (opts.action) t.querySelector('button').onclick = () => { opts.onAction(); dismiss(); };
  box.append(t);
  setTimeout(dismiss, opts.ms || 4200);
  while (box.children.length > 3) box.firstElementChild.remove();
}
const mb = b => `${b ? Math.max(1, Math.round(b / 1e6)) : 0} MB`;
function ago(t) {
  if (!t) return 'never';
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(t * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

/* Release notes are short markdown: headings, bullets, **bold**, `code`, [links](https://...). */
function md(text) {
  const inline = s => esc(s)
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[([^\]]+)\]\((https:\/\/[^)\s]+)\)/g, '<a class="link" href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  const out = [];
  let list = null;
  for (const raw of String(text || '').split(/\r?\n/)) {
    const line = raw.trim();
    const li = line.match(/^[-*]\s+(.*)/);
    if (li) { if (!list) { list = []; } list.push(`<li>${inline(li[1])}</li>`); continue; }
    if (list) { out.push(`<ul>${list.join('')}</ul>`); list = null; }
    const h = line.match(/^#{1,4}\s+(.*)/);
    if (h) out.push(`<h4>${inline(h[1])}</h4>`);
    else if (line) out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push(`<ul>${list.join('')}</ul>`);
  return out.join('') || '<p class="upd-dim">No notes for this one.</p>';
}

const U = { st: null, sheet: null, mode: 'update', aboutEl: null, stackEl: null, polled: 0 };

/* ================================================================== status */
async function refresh() {
  try { U.st = await api('/api/update/status'); } catch (e) { return U.st; }
  drawAll();
  return U.st;
}
function drawAll() { drawPill(); drawSheet(); drawAbout(); drawStack(); }

const dlBusy = d => ['downloading', 'paused', 'verifying', 'unpacking'].includes(d && d.state);
function pct(d) { return d && d.total ? Math.min(100, Math.round(d.done / d.total * 100)) : 0; }

/* ================================================================== the pill */
function pillEl() {
  let p = $('#upd-pill');
  if (!p) {
    const acts = $('#topbar .top-actions');
    if (!acts) return null;
    p = document.createElement('button');
    p.id = 'upd-pill';
    p.className = 'upd-pill';
    p.hidden = true;
    p.addEventListener('click', () => openSheet('update'));
    acts.prepend(p);
  }
  return p;
}
function drawPill() {
  const p = pillEl(), s = U.st;
  if (!p) return;
  const a = s && s.available, d = (s && s.download) || {};
  const show = !!a && !s.skipped && !s.snoozed;
  p.hidden = !show;
  if (!show) return;
  const ready = d.state === 'ready' && d.version === a.version;
  const busy = dlBusy(d) && d.version === a.version;
  p.classList.toggle('ready', ready);
  p.classList.toggle('critical', !!a.critical);
  p.style.setProperty('--p', busy ? pct(d) / 100 : 0);
  const label = ready ? 'Restart to update' : busy ? (d.state === 'paused' ? `Update paused · ${pct(d)}%` : d.state === 'downloading' ? `Updating · ${pct(d)}%` : 'Checking update…') : `Update to ${a.version}`;
  p.innerHTML = `<span class="upd-pill-ico">${ready ? ico('restart', 15) : UP(16)}</span><span class="upd-pill-txt">${esc(label)}</span>${busy ? '<i class="upd-pill-bar" aria-hidden="true"></i>' : ''}`;
  p.title = ready ? `Marquee ${a.version} is ready. Restart to finish updating.` : `Marquee ${a.version} is available`;
  p.setAttribute('aria-label', label);
}

/* ================================================================== the sheet */
function openSheet(mode = 'update') {
  U.mode = mode;
  if (!U.sheet) {
    const layer = document.createElement('div');
    layer.className = 'upd-layer';
    layer.setAttribute('role', 'dialog');
    layer.setAttribute('aria-modal', 'true');
    layer.setAttribute('aria-label', 'Update Marquee');
    layer.innerHTML = `<div class="upd-scrim" data-upd="close"></div><div class="upd-sheet glass"><div class="upd-body"></div></div>`;
    document.body.append(layer);
    layer.addEventListener('click', onSheetClick);
    U.sheet = layer;
    document.documentElement.classList.add('locked');
    requestAnimationFrame(() => requestAnimationFrame(() => layer.classList.add('open')));
    document.addEventListener('keydown', onKey);
  }
  drawSheet();
}
function closeSheet() {
  const l = U.sheet;
  if (!l) return;
  U.sheet = null;
  l.classList.remove('open');
  document.removeEventListener('keydown', onKey);
  setTimeout(() => { l.remove(); if (!$('.layer:not([hidden])') && !$('.mqs-layer') && !$('.upd-layer')) document.documentElement.classList.remove('locked'); }, 260);
}
function onKey(e) { if (e.key === 'Escape') { e.stopPropagation(); closeSheet(); } }

function drawSheet() {
  if (!U.sheet) return;
  const body = $('.upd-body', U.sheet), s = U.st || {};
  if (U.mode === 'news') {
    const j = s.just_updated || U.news || {};
    body.innerHTML = `<div class="upd-head"><span class="upd-badge done">${ico('check', 22)}</span><div><span class="eyebrow">What’s new</span><h2>Marquee ${esc(j.to || s.version)}</h2></div>
        <button class="icon-btn small" data-upd="close" aria-label="Close">${ico('close', 18)}</button></div>
      <div class="upd-notes">${md(j.notes)}</div>
      <div class="upd-foot">${j.notes_url ? `<a class="btn ghost small" href="${esc(j.notes_url)}" target="_blank" rel="noopener noreferrer">On GitHub</a>` : ''}<button class="btn small" data-upd="close">Done</button></div>`;
    return;
  }
  const a = s.available;
  if (!a) {
    body.innerHTML = `<div class="upd-head"><span class="upd-badge done">${ico('check', 22)}</span><div><span class="eyebrow">Updates</span><h2>You’re up to date</h2></div>
        <button class="icon-btn small" data-upd="close" aria-label="Close">${ico('close', 18)}</button></div>
      <p class="upd-dim">Marquee ${esc(s.version || '')} is the newest version.</p>`;
    return;
  }
  const d = s.download || {}, mine = d.version === a.version;
  const ready = mine && d.state === 'ready', busy = mine && dlBusy(d), err = mine && d.state === 'error';
  const can = s.can_apply && a.os_ok !== false;
  const stateLine = ready ? `${ico('check', 14)} Downloaded and checked. Restart when you’re ready${s.playing ? ' (after the video)' : ''}.`
    : busy ? (d.state === 'paused' ? 'Paused while a video plays.' : d.state === 'verifying' ? 'Checking the download…' : d.state === 'unpacking' ? 'Unpacking…' : `Downloading · ${mb(d.done)} of ${mb(d.total)}`)
    : err ? esc(d.error) : '';
  body.innerHTML = `<div class="upd-head"><span class="upd-badge">${UP(24)}</span><div><span class="eyebrow${a.critical ? ' orange' : ''}">${a.critical ? 'Important update' : a.prerelease ? 'Beta update' : 'Update available'}</span>
      <h2>Marquee ${esc(a.version)}</h2><span class="upd-dim">You have ${esc(s.version)}${a.size ? ` · ${mb(a.size)}` : ''}${a.published ? ` · ${esc(new Date(a.published).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }))}` : ''}</span></div>
      <button class="icon-btn small" data-upd="close" aria-label="Close">${ico('close', 18)}</button></div>
    <div class="upd-notes">${md(a.notes)}</div>
    ${busy || ready || err ? `<div class="upd-progress${err ? ' bad' : ''}"><div class="index-bar"><i style="--p:${ready ? 1 : (pct(d) / 100).toFixed(3)}"></i></div><span>${stateLine}</span></div>` : ''}
    ${!can ? `<p class="upd-warn">${ico('info', 15)} ${esc(a.os_ok === false ? 'This version needs a newer Windows.' : s.reason)}</p>` : ''}
    <div class="upd-foot">
      ${!a.critical ? `<button class="btn ghost small upd-skip" data-upd="skip">Skip this version</button>` : ''}
      <span class="upd-grow"></span>
      <button class="btn ghost small" data-upd="later">Later</button>
      ${!can ? `<a class="btn small" href="${esc(s.releases_url)}" target="_blank" rel="noopener noreferrer">Open download page</a>`
        : ready ? `<button class="btn small" data-upd="apply" ${s.playing ? 'disabled title="Stop the video first"' : ''}>${ico('restart', 16)} Restart to update</button>`
        : `<button class="btn small" data-upd="download" ${busy ? 'disabled' : ''}>${busy ? 'Downloading…' : err ? 'Try again' : 'Update now'}</button>`}
    </div>`;
}

async function onSheetClick(e) {
  const b = e.target.closest('[data-upd]');
  if (!b) return;
  const what = b.dataset.upd, a = U.st && U.st.available;
  try {
    if (what === 'close') return closeSheet();
    if (what === 'later') { if (U.mode === 'update' && a) await api('/api/update/later', {}); closeSheet(); return refresh(); }
    if (what === 'skip') { await api('/api/update/skip', { version: a.version }); closeSheet(); toast(`Skipped ${a.version}. You’ll hear about the next one.`); return refresh(); }
    if (what === 'download') { b.disabled = true; U.st = await api('/api/update/download', {}); return drawAll(); }
    if (what === 'apply') { b.disabled = true; await api('/api/update/apply', {}); b.textContent = 'Restarting…'; toast('Restarting to update…', { icon: 'restart' }); }
  } catch (err) { toast(err.message); b.disabled = false; refresh(); }
}

/* ================================================================== Settings > About */
function mountAbout(el) { U.aboutEl = el; drawAbout(); refresh(); }
function drawAbout() {
  const el = U.aboutEl;
  if (!el || !document.contains(el)) { U.aboutEl = null; return; }
  const s = U.st;
  if (!s) { el.innerHTML = `<section class="panel glass" style="--i:8"><h2>${ico('info', 20)} About</h2><div class="skel dx-skel-line"></div></section>`; return; }
  const a = s.available, d = s.download || {};
  const line = s.status === 'checking' ? 'Checking…'
    : a ? `Marquee ${esc(a.version)} is available${s.skipped ? ' (skipped)' : ''}.`
    : s.status === 'unreachable' ? 'Can’t reach updates right now.'
    : s.status === 'error' ? esc(s.message)
    : s.checked ? 'You’re up to date.' : 'Not checked yet.';
  const seg = (key, opts, cur) => `<div class="pill-toggle" role="group">${opts.map(([v, n]) => `<button data-upd-set="${key}" data-v="${v}" aria-pressed="${cur === v}">${n}</button>`).join('')}</div>`;
  el.innerHTML = `<section class="panel glass upd-about" style="--i:8"><h2>${ico('info', 20)} About</h2>
    <div class="set"><div class="lbl"><b>Marquee ${esc(s.version)}</b><span>${line} <span class="upd-dim">Last checked ${esc(ago(s.checked))}.</span></span>
      ${a && dlBusy(d) ? `<div class="index-bar"><i style="--p:${(pct(d) / 100).toFixed(3)}"></i></div>` : ''}</div>
      ${a ? `<button class="btn small" data-upd-open>${d.state === 'ready' && d.version === a.version ? 'Restart to update' : 'See update'}</button>`
        : `<button class="btn small ghost" data-upd-check>${ico('restart', 15)} Check for updates</button>`}</div>
    <div class="set"><div class="lbl"><b>Updates</b><span>${s.mode === 'auto' ? 'Downloads in the background, asks before restarting.' : s.mode === 'notify' ? 'Tells you about new versions; you choose when to download.' : 'Never checks. You can still check here.'}</span></div>
      ${seg('update_check', [['auto', 'Auto'], ['notify', 'Notify'], ['off', 'Off']], s.mode)}</div>
    <div class="set"><div class="lbl"><b>Channel</b><span>${s.channel === 'beta' ? 'Also offers early test versions.' : 'Finished releases only.'}</span></div>
      ${seg('update_channel', [['stable', 'Stable'], ['beta', 'Beta']], s.channel)}</div>
    <div class="set"><div class="lbl"><b>Release notes</b><span title="${esc(s.installed_in ? `Installed in ${s.installed_in}` : 'Running from source')}">What changed in each version.</span></div>
      <div class="upd-links"><a class="link" href="${esc(s.releases_url)}/tag/v${esc(s.version)}" target="_blank" rel="noopener noreferrer">This version</a>
      <a class="link" href="${esc(s.releases_url)}" target="_blank" rel="noopener noreferrer">All releases ↗</a></div></div>
  </section>`;
}
document.addEventListener('click', async e => {
  const t = e.target.closest && e.target.closest('[data-upd-check], [data-upd-open], [data-upd-set], [data-upd-stack]');
  if (!t) return;
  e.stopPropagation();
  try {
    if ('updCheck' in t.dataset) {
      t.disabled = true;
      t.innerHTML = `${ico('restart', 15)} Checking…`;
      U.st = await api('/api/update/check', {});
      drawAll();
      if (U.st.available) openSheet('update');
      else if (U.st.status === 'ok') toast('Marquee is up to date', { icon: 'check' });
      else toast(U.st.message || 'Can’t reach updates right now');
      return;
    }
    if ('updOpen' in t.dataset) return openSheet('update');
    if (t.dataset.updSet) {
      await api('/api/settings', { [t.dataset.updSet]: t.dataset.v });
      toast('Saved', { icon: 'check', ms: 1600 });
      return refresh();
    }
    if ('updStack' in t.dataset) {
      t.disabled = true;
      await api('/api/update/stack/download', {});
      toast('Downloading Media Stack Setup…');
      return refresh();
    }
  } catch (err) { toast(err.message); t.disabled = false; }
}, true);

/* ================================================================== Your stack: newer Media Stack */
function mountStack(el) { U.stackEl = el; drawStack(); refresh(); }
function drawStack() {
  const el = U.stackEl;
  if (!el || !document.contains(el)) { U.stackEl = null; return; }
  const k = U.st && U.st.stack, sd = (U.st && U.st.stack_download) || {};
  if (!k) { el.hidden = true; el.innerHTML = ''; return; }
  el.hidden = false;
  const busy = sd.state === 'downloading';
  el.innerHTML = `<div class="upd-stack">${UP(16)}<span>A newer Media Stack is available (${esc(k.version)}, you have ${esc(k.current)}). Run the installer to update the stack apps.
      ${sd.state === 'launched' ? `<span class="upd-dim">Media Stack Setup was started.</span>` : sd.error ? `<span class="upd-dim">${esc(sd.error)}</span>` : ''}</span>
    ${k.has_installer ? `<button class="btn small ghost" data-upd-stack ${busy ? 'disabled' : ''}>${busy ? `Downloading… ${sd.total ? Math.round(sd.done / sd.total * 100) + '%' : ''}` : 'Update'}</button>`
      : `<a class="btn small ghost" href="${esc(k.notes_url)}" target="_blank" rel="noopener noreferrer">Get it</a>`}</div>`;
}

/* ================================================================== events from Python */
function onEvent(name, data) {
  if (!/^update-|^stack-update$/.test(name)) return false;
  if (name === 'update-progress' && U.st && U.st.download) {
    Object.assign(U.st.download, { state: data.state, done: data.done, total: data.total, version: data.version });
    drawPill(); drawSheet();
    if (U.aboutEl && Date.now() - U.polled > 1500) { U.polled = Date.now(); drawAbout(); }
    return true;
  }
  if (name === 'update-error') toast(data.message || 'The update failed');
  if (name === 'update-ready' && !U.sheet) toast(`Marquee ${data.version} is ready`, { icon: 'check', action: 'Restart', onAction: () => openSheet('update'), ms: 8000 });
  refresh();
  return true;
}

/* ================================================================== start */
async function start() {
  const s = await refresh();
  if (!s) return;
  if (s.just_updated) {
    U.news = s.just_updated;
    toast(`Updated to ${s.just_updated.to} ✓`, { icon: 'check', action: 'What’s new', onAction: () => openSheet('news'), ms: 12000 });
    api('/api/update/ack', {}).catch(() => {});
  } else if (s.update_failed) {
    toast(`Marquee ${s.update_failed.version || ''} didn’t start, so you’re still on ${s.version}.`, { icon: 'info', ms: 12000 });
    api('/api/update/ack', {}).catch(() => {});
  }
}
setTimeout(start, 1500);  // after the page has drawn

window.MarqueeUpdate = { onEvent, mountAbout, mountStack, openSheet, refresh };
})();
