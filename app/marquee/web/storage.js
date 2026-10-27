/* Marquee storage UI: the request sheet's quality picker with size estimates, Settings > Storage (dashboard,
   cleanup rules, deletion log), the Home "Leaving soon" row, the delete-by-hand sheet and the smart-request line.
   Talks to marquee/storage_api.py. Self-contained: exposes window.MarqueeStorage, uses app.css tokens + storage.css. */
(() => {
'use strict';

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const ico = (id, size = 16) => `<svg width="${size}" height="${size}" aria-hidden="true"><use href="#i-${id}"/></svg>`;
const reduced = matchMedia('(prefers-reduced-motion: reduce)');

async function api(path, body, method) {
  const opt = body !== undefined ? { method: method || 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {};
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty */ }
  if (!r.ok || data.ok === false) { const err = new Error(data.message || r.statusText || 'Something went wrong'); err.status = r.status; throw err; }
  return data;
}

function size(b) {
  if (b === null || b === undefined) return '?';
  if (b < 2 ** 30) return b >= 2 ** 20 ? `${Math.max(1, Math.round(b / 2 ** 20))} MB` : '0 MB';
  const g = b / 2 ** 30;
  if (g >= 1024) return `${(g / 1024).toFixed(1)} TB`;
  return g < 10 ? `${g.toFixed(1)} GB` : `${Math.round(g)} GB`;
}
const day = t => new Date((typeof t === 'number' ? t * 1000 : t)).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
function when(t) {
  const d = Math.round((t * 1000 - Date.now()) / 864e5);
  if (d <= 0) return 'today';
  if (d === 1) return 'tomorrow';
  return d < 7 ? `on ${new Date(t * 1000).toLocaleDateString(undefined, { weekday: 'long' })}` : `on ${day(t)}`;
}
const poster = id => id ? `/img/Items/${encodeURIComponent(id)}/Images/Primary?fillHeight=600&quality=90` : '';

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

/* ================================================================== request sheet: quality + size */
/* mountQuality(el, {kind: 'movie'|'tv', tmdb, seasons: 'all'|'latest'|'smart'|[n...], anime?, onChange?})
   Draws the quality choices in plain words with a size estimate each, the 4K/server switch when there's a choice,
   the root folder when there's more than one, free space and the storage limit, and a warning when it won't fit.
   Returns a controller: value() -> {server, profile, is4k, root, blocked}, setSeasons(s), refresh(). */
function mountQuality(el, opts) {
  const st = { opts: { ...opts }, data: null, server: null, profile: null, root: null, more: false, seq: 0 };
  el.classList.add('mqs-quality');
  el.innerHTML = `<div class="mqs-skel"></div>`;
  const ctl = {
    value: () => ({ server: st.server ? st.server.id : null, profile: st.profile, is4k: st.server ? !!st.server.is4k : false,
      root: st.root, blocked: !!(st.fit && st.fit.blocked) }),
    setSeasons(s) { st.opts.seasons = s; return load(); },
    refresh: () => load(),
  };
  async function load() {
    const seq = ++st.seq;
    const s = st.opts.seasons;
    const q = s === undefined ? '' : `?seasons=${encodeURIComponent(Array.isArray(s) ? s.join(',') : s)}`;
    let data;
    try { data = await api(`/api/requests/options/${st.opts.anime && st.opts.kind === 'tv' ? 'anime' : st.opts.kind}/${st.opts.tmdb}${q}`); }
    catch (e) { if (seq === st.seq) el.innerHTML = `<p class="mqs-note">${esc(e.message)}</p>`; return; }
    if (seq !== st.seq) return;
    const keep = st.data ? ctl.value() : null;
    st.data = data;
    const pick = keep || data.choice || {};
    st.server = data.servers.find(x => x.id === pick.server && (!keep || x.is4k === keep.is4k)) || data.servers[0] || null;
    st.profile = st.server && st.server.profiles.some(p => p.id === pick.profile) ? pick.profile : (st.server ? st.server.default_profile : null);
    st.root = pick.root || (st.server && st.server.default_root) || null;
    draw();
  }
  function prof() { return st.server && st.server.profiles.find(p => p.id === st.profile); }
  function fit() {
    const d = st.data, p = prof(), e = p && p.estimate, u = d.storage || {};
    if (!e) return null;
    const root = st.server && st.server.root_folders.find(r => r.path === st.root);
    const free = root && root.free != null ? root.free : u.disk_free;
    const limit = u.limit, used = u.projected || u.used || 0;
    const out = { free, limit, used, level: 'ok', msg: '' };
    out.blocked = !!(limit && used + e.typical > limit && u.enforce === 'block');
    if (free != null && e.typical >= free) { out.level = 'bad'; out.msg = `Won't fit: about ${size(e.typical)} needed, ${size(free)} free on that disk.`; }
    else if (out.blocked) { out.level = 'bad'; out.msg = `Over your ${size(limit)} storage limit (this would make it ${size(used + e.typical)}). Free some space in Settings > Storage, or raise the limit.`; }
    else if (limit && used + e.typical > limit) { out.level = 'warn'; out.msg = (u.used || 0) + e.typical > limit ? `This takes your library over its ${size(limit)} limit.` : `With what's already on its way (${size(used - (u.used || 0))}), this takes your library over its ${size(limit)} limit.`; }
    else if (free != null && e.high >= free) { out.level = 'warn'; out.msg = `Tight: at its largest it could need ${size(e.high)}; ${size(free)} is free.`; }
    return out;
  }
  function draw() {
    const d = st.data;
    if (!d.servers.length) { el.innerHTML = `<p class="mqs-note">Seerr has no download server set up for this kind of title.</p>`; return; }
    const pool = d.servers.filter(x => !!x.is4k === !!st.server.is4k);
    const has4k = d.servers.some(x => x.is4k) && d.servers.some(x => !x.is4k);
    const featured = st.server.profiles.filter(p => p.featured || p.id === st.profile);
    const others = st.server.profiles.filter(p => !featured.includes(p));
    const choice = p => {
      const e = p.estimate || {};
      return `<button class="mqs-choice" data-mqs="profile" data-v="${p.id}" aria-pressed="${p.id === st.profile}">
        <span class="mqs-choice-top"><b>${esc(p.label)}</b>${p.default ? '<span class="mqs-tag">Default</span>' : ''}${d.choice && d.choice.remembered && d.choice.profile === p.id ? '<span class="mqs-tag star" title="Your choice last time">★</span>' : ''}</span>
        <span class="mqs-size">${e.typical ? `≈ ${size(e.typical)}` : ''}<span class="mqs-sub-range">${e.low ? ` ${size(e.low)}–${size(e.high)}` : ''}</span></span>
        <span class="mqs-sub">${esc(p.name)}${p.from_defaults ? ' · rough' : ''}</span></button>`;
    };
    const p = prof(), e = (p && p.estimate) || {};
    st.fit = fit();
    const u = d.storage || {};
    const free = st.fit && st.fit.free;
    const meter = u.limit ? Math.min(1, (u.used || 0) / u.limit) : null;
    const add = u.limit && e.typical ? Math.min(1 - meter, e.typical / u.limit) : 0;
    el.innerHTML = `
      ${has4k ? `<div class="opt-group"><h4>Version</h4><div class="opt-row">
        <button class="opt" data-mqs="4k" data-v="0" aria-pressed="${!st.server.is4k}">Standard</button>
        <button class="opt" data-mqs="4k" data-v="1" aria-pressed="${!!st.server.is4k}">4K</button></div></div>` : ''}
      ${pool.length > 1 ? `<div class="opt-group"><h4>Server</h4><div class="opt-row">${pool.map(x => `<button class="opt" data-mqs="server" data-v="${x.id}" aria-pressed="${x.id === st.server.id}">${esc(x.name)}</button>`).join('')}</div></div>` : ''}
      <div class="opt-group"><h4>Quality</h4>
        <div class="mqs-choices">${featured.map(choice).join('')}</div>
        ${others.length ? `<button class="mqs-more" data-mqs="more" aria-expanded="${st.more}">${st.more ? 'Fewer choices' : `More quality profiles (${others.length})`}</button>
        ${st.more ? `<div class="mqs-choices small">${others.map(choice).join('')}</div>` : ''}` : ''}
      </div>
      ${st.server.root_folders.length > 1 ? `<div class="opt-group"><h4>Save to</h4><div class="opt-row">${st.server.root_folders.map(r => `<button class="opt" data-mqs="root" data-v="${esc(r.path)}" aria-pressed="${r.path === st.root}" title="${esc(r.path)}">${esc(r.path.split(/[\\/]/).pop())}<span class="mqs-dim"> · ${size(r.free)} free</span></button>`).join('')}</div></div>` : ''}
      <div class="mqs-estimate ${st.fit ? st.fit.level : ''}">
        <div class="mqs-est-main"><span class="mqs-big">${esc(e.typical ? '≈ ' + size(e.typical) : '—')}</span>
          <span class="mqs-range">${e.low ? `${size(e.low)} – ${size(e.high)}` : ''}${d.info && d.info.episodes ? ` · ${d.info.episodes} episodes` : ''}</span></div>
        ${e.upgrade_text ? `<p class="mqs-line">${ico('sparkle', 14)} ${esc(e.upgrade_text)}</p>` : ''}
        ${e.later_text ? `<p class="mqs-line">${ico('plus', 14)} ${esc(e.later_text)}</p>` : ''}
        ${meter !== null ? `<div class="mqs-bar small" role="img" aria-label="Library ${size(u.used)} of ${size(u.limit)}"><i style="--w:${(meter * 100).toFixed(1)}%"></i><i class="add" style="--l:${(meter * 100).toFixed(1)}%;--w:${(add * 100).toFixed(1)}%"></i></div>` : ''}
        <p class="mqs-line mqs-dim">${free != null ? `${size(free)} free on that disk` : ''}${u.limit ? ` · library ${size(u.used)} of ${size(u.limit)}` : ''}${e.calibrated ? ' · sized from your library' : ''}</p>
        ${st.fit && st.fit.msg ? `<p class="mqs-warn">${ico('info', 14)} ${esc(st.fit.msg)}</p>` : ''}
        ${d.explain ? `<p class="mqs-line mqs-dim">${esc(d.explain)}</p>` : ''}
      </div>`;
    if (st.opts.onChange) st.opts.onChange(ctl.value());
  }
  el.addEventListener('click', ev => {
    const b = ev.target.closest('[data-mqs]');
    if (!b || !st.data) return;
    const k = b.dataset.mqs, v = b.dataset.v;
    if (k === 'profile') st.profile = +v;
    else if (k === 'more') st.more = !st.more;
    else if (k === 'root') st.root = v;
    else if (k === 'server') { st.server = st.data.servers.find(x => String(x.id) === v); fixProfile(); }
    else if (k === '4k') {
      const want = v === '1';
      st.server = st.data.servers.find(x => !!x.is4k === want && x.default) || st.data.servers.find(x => !!x.is4k === want) || st.server;
      fixProfile();
    }
    draw();
  });
  function fixProfile() {
    if (!st.server.profiles.some(p => p.id === st.profile)) st.profile = st.server.default_profile;
    if (!st.server.root_folders.some(r => r.path === st.root)) st.root = st.server.default_root;
  }
  load();
  return ctl;
}

/* Season modes for the request sheet: Smart is the default for shows with 2+ seasons. */
function seasonModes(details, pref) {
  const n = (details.seasons || []).length;
  const modes = [];
  if (n >= 2) modes.push({ v: 'smart', label: 'Smart', title: 'Start with season 1; the next season is requested as you watch' });
  modes.push({ v: 'all', label: `All ${n || ''}`.trim() }, { v: 'latest', label: 'Latest' }, { v: 'pick', label: 'Pick…' });
  const remembered = pref && modes.some(m => m.v === pref.season_mode) ? pref.season_mode : null;
  return { modes, default: remembered || (n >= 2 ? 'smart' : 'all') };
}

/* The request itself (instead of POST /api/requests): checks the storage rule, passes rootFolder, registers
   smart shows. body = {type, id, profile, server, is4k, root, seasons, season_mode, anime, title}. */
const sendRequest = body => api('/api/storage/request', body);

/* ================================================================== Home: Leaving soon */
async function leavingSoonRow() {
  let items;
  try { items = (await api('/api/storage/leaving')).items || []; } catch (e) { return null; }
  if (!items.length) return null;
  const sec = document.createElement('section');
  sec.className = 'row mqs-leaving in';
  sec.dataset.key = 'leaving-soon';
  sec.innerHTML = `<div class="row-head"><h2>Leaving soon</h2><span class="count">${items.length}</span>
      <span class="mqs-row-why">to save space · tap Keep to hold on to one</span></div>
    <div class="scroller">${items.map(leaveCard).join('')}</div>`;
  sec.addEventListener('click', async ev => {
    const b = ev.target.closest('[data-mqs-keep]');
    if (!b) return;
    ev.stopPropagation();
    b.disabled = true;
    try {
      await api(`/api/storage/keep/${encodeURIComponent(b.dataset.mqsKeep)}`, { keep: true });
      const card = b.closest('.mqs-leave-card');
      card.classList.add('kept');
      toast(`Keeping ${card.dataset.title}`, { icon: 'check' });
      setTimeout(() => { card.remove(); if (!$('.mqs-leave-card', sec)) sec.remove(); }, reduced.matches ? 0 : 420);
    } catch (e) { b.disabled = false; toast(e.message); }
  });
  return sec;
}
function leaveCard(i) {
  const img = poster(i.series_id || i.id);
  const t = i.title + (i.subtitle && !/^\d{4}$/.test(i.subtitle) ? ` · ${i.subtitle}` : '');
  return `<div class="mqs-leave-card" data-title="${esc(t)}" ${i.id ? `data-id="${esc(i.series_id || i.id)}"` : ''}>
    <div class="mqs-leave-media">${img ? `<img src="${esc(img)}" alt="" loading="lazy" onerror="this.remove()">` : ''}</div>
    <div class="mqs-leave-info"><span class="mqs-leave-when">Leaves ${esc(when(i.leave_at))}</span><b title="${esc(t)}">${esc(t)}</b><span>${esc(i.reason || '')} · ${size(i.size)}</span>
      <button class="btn small ghost" data-mqs-keep="${esc(i.keep_key)}">${ico('heart', 15)} Keep</button></div></div>`;
}

/* ================================================================== delete by hand */
/* deleteSheet({id, name, type: 'Movie'|'Series'|'Season'|'Episode'}) -> Promise<log entry | null> */
function deleteSheet(item) {
  return new Promise(resolve => {
    const scopesFor = { Movie: ['movie'], Episode: ['episode', 'season', 'series'], Season: ['season', 'series'], Series: ['series'] };
    const label = { movie: 'This film', episode: 'This episode', season: 'This season', series: 'Whole show' };
    const st = { scope: (scopesFor[item.type] || ['movie'])[0], torrent: null, keep: false, prev: null, seq: 0 };
    const layer = document.createElement('div');
    layer.className = 'mqs-layer';
    layer.setAttribute('role', 'dialog');
    layer.setAttribute('aria-modal', 'true');
    layer.setAttribute('aria-label', `Delete ${item.name || ''}`);
    layer.innerHTML = `<div class="mqs-scrim" data-close></div><div class="mqs-sheet glass"><div class="mqs-sheet-body"></div></div>`;
    document.body.append(layer);
    document.documentElement.classList.add('locked');
    requestAnimationFrame(() => requestAnimationFrame(() => layer.classList.add('open')));
    const body = $('.mqs-sheet-body', layer);
    const close = result => {
      layer.classList.remove('open');
      document.removeEventListener('keydown', onKey);
      setTimeout(() => { layer.remove(); if (!$('.layer:not([hidden])') && !$('.mqs-layer')) document.documentElement.classList.remove('locked'); }, 260);
      resolve(result || null);
    };
    const onKey = e => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    async function load() {
      const seq = ++st.seq;
      body.innerHTML = head() + `<div class="mqs-skel"></div>`;
      const q = new URLSearchParams({ item_id: item.id, scope: st.scope, keep_monitored: st.keep ? '1' : '' });
      if (st.torrent !== null) q.set('also_torrent', st.torrent ? '1' : '0');
      try { st.prev = await api(`/api/storage/delete/preview?${q}`); }
      catch (e) { if (seq === st.seq) body.innerHTML = head() + `<p class="mqs-warn">${esc(e.message)}</p>`; return; }
      if (seq !== st.seq) return;
      if (st.torrent === null) st.torrent = st.prev.frees_after_torrent > 0 && !st.prev.torrent_holds;  // the setting's default
      draw();
    }
    function head() {
      return `<div class="mqs-sheet-head"><span class="mqs-trash">${ico('close', 20)}</span><div><span class="eyebrow orange">Delete from this PC</span>
        <h2>${esc(item.name || 'Delete')}</h2></div><button class="icon-btn small" data-close aria-label="Close">${ico('close', 18)}</button></div>`;
    }
    function draw() {
      const p = st.prev;
      const scopes = scopesFor[item.type] || ['movie'];
      body.innerHTML = head() + `
        ${scopes.length > 1 ? `<div class="opt-group"><h4>What to delete</h4><div class="opt-row">${scopes.map(s => `<button class="opt" data-scope="${s}" aria-pressed="${s === st.scope}">${label[s]}</button>`).join('')}</div></div>` : ''}
        <div class="mqs-estimate ${p.warnings.length ? 'warn' : ''}">
          <div class="mqs-est-main"><span class="mqs-big">Frees ${size(p.size_freed)}</span><span class="mqs-range">${p.files.length} file${p.files.length === 1 ? '' : 's'} · ${size(p.size)}</span></div>
          <p class="mqs-line">${esc(p.title)}${p.subtitle ? ` · ${esc(p.subtitle)}` : ''}</p>
          <p class="mqs-line mqs-dim">${p.via === 'Windows Recycle Bin' ? 'Goes to the Windows Recycle Bin.' : `Removed through ${esc(p.via)}${p.recycle_bin ? `, into its recycle bin (${esc(p.recycle_bin)})` : ''}.`}
            ${p.unmonitor ? ' It won\'t be downloaded again.' : ''} Your watch history stays.</p>
          ${p.warnings.map(w => `<p class="mqs-warn">${ico('info', 14)} ${esc(w)}</p>`).join('')}
        </div>
        <div class="mqs-toggles">
          ${p.via !== 'Windows Recycle Bin' ? `<label class="mqs-check"><input type="checkbox" data-opt="keep" ${st.keep ? 'checked' : ''}> Keep monitoring (it may download again)</label>` : ''}
          ${p.frees_after_torrent ? `<label class="mqs-check"><input type="checkbox" data-opt="torrent" ${st.torrent ? 'checked' : ''}> Also remove the seeding torrent (frees ${size(p.frees_after_torrent)} more)</label>` : ''}
        </div>
        <div class="mqs-sheet-foot"><button class="btn ghost" data-close>Cancel</button>
          <button class="btn mqs-hold danger" data-hold ${p.can_delete ? '' : 'disabled'}><i class="mqs-hold-fill"></i><span>${p.can_delete ? 'Hold to delete' : 'Can\'t delete'}</span></button></div>`;
    }
    layer.addEventListener('click', e => {
      if (e.target.closest('[data-close]')) return close();
      const sc = e.target.closest('[data-scope]');
      if (sc) { st.scope = sc.dataset.scope; load(); }
    });
    layer.addEventListener('change', e => {
      const o = e.target.dataset.opt;
      if (o === 'keep') { st.keep = e.target.checked; load(); }
      if (o === 'torrent') { st.torrent = e.target.checked; load(); }
    });
    // A deliberate tap: hold the button for a second (keyboard: hold Enter/Space).
    let timer = 0;
    const start = e => {
      const b = e.target.closest('[data-hold]');
      if (!b || b.disabled || timer) return;
      if (e.type === 'keydown' && e.key !== 'Enter' && e.key !== ' ') return;
      if (e.type === 'keydown') e.preventDefault();
      b.classList.add('holding');
      $('span', b).textContent = 'Keep holding…';
      timer = setTimeout(() => { timer = 0; b.classList.remove('holding'); go(b); }, 1000);
    };
    const stop = () => {
      if (!timer) return;
      clearTimeout(timer); timer = 0;
      const b = $('[data-hold]', layer);
      if (b) { b.classList.remove('holding'); $('span', b).textContent = 'Hold to delete'; }
    };
    layer.addEventListener('pointerdown', start);
    layer.addEventListener('keydown', start);
    ['pointerup', 'pointerleave', 'pointercancel', 'keyup'].forEach(n => layer.addEventListener(n, stop, true));
    async function go(b) {
      b.disabled = true;
      $('span', b).textContent = 'Deleting…';
      const req = { item_id: item.id, scope: st.scope, keep_monitored: st.keep };
      if (st.torrent !== null) req.also_torrent = st.torrent;
      try {
        const r = await api('/api/storage/delete', req);
        toast(r.message, { icon: 'check', ms: 5200 });
        close(r.entry);
      } catch (e) { toast(`Couldn't delete: ${e.message}`); b.disabled = false; $('span', b).textContent = 'Hold to delete'; }
    }
    load();
  });
}

/* ================================================================== details page: smart line */
/* smartLine(el, tmdb): "Smart: S2 requested · S3 when you're halfway through S2" with Get all / Stop buttons. */
async function smartLine(el, tmdb) {
  let s;
  try { s = await api(`/api/storage/smart/${tmdb}`); } catch (e) { el.innerHTML = ''; return; }
  if (!s.smart) { el.innerHTML = ''; return; }
  el.classList.add('mqs-smart');
  el.innerHTML = `<span class="mqs-smart-text">${ico('sparkle', 15)} ${esc(s.text)}</span>
    ${s.state !== 'stopped' && s.state !== 'done' ? `<button class="btn small ghost" data-smart="all">Get all remaining seasons now</button>
    <button class="btn small ghost" data-smart="stop">Stop smart requesting</button>` : ''}`;
  el.onclick = async e => {
    const b = e.target.closest('[data-smart]');
    if (!b) return;
    b.disabled = true;
    try {
      const r = await api(`/api/storage/smart/${tmdb}/${b.dataset.smart}`, {});
      toast(r.message || (b.dataset.smart === 'stop' ? 'Smart requesting stopped for this show' : 'Requested'), { icon: 'check' });
      smartLine(el, tmdb);
    } catch (err) { toast(err.message); b.disabled = false; }
  };
}

/* ================================================================== Settings > Storage */
function mountSettings(el, opts = {}) {
  el.classList.add('mqs-settings');
  const smartEl = opts.smartEl || null;   // Settings shows smart seasons with the request settings
  el.innerHTML = `<div class="mqs-skel tall"></div>`;
  const st = { data: null, plan: null, showLog: false };
  async function load(refresh) {
    try { st.data = await api(`/api/storage${refresh ? '?refresh=1' : ''}`); }
    catch (e) { el.innerHTML = `<p class="mqs-warn">${esc(e.message)}</p>`; return; }
    draw();
  }
  function bar(u) {
    const disk = u.disk || {};
    const cap = u.limit || (disk.total ? Math.min(disk.total, (u.used || 0) + (disk.free || 0)) : (u.used || 0) + (disk.free || 0)) || 1;
    const seg = (v, cls, label) => v > 0 ? `<i class="${cls}" style="--w:${Math.min(100, v / cap * 100).toFixed(2)}%" title="${esc(label)}: ${size(v)}"></i>` : '';
    const coming = Math.max(0, (u.projected || 0) - (u.used || 0));
    return `<div class="mqs-bar" role="img" aria-label="Library uses ${size(u.used)}${u.limit ? ` of a ${size(u.limit)} limit` : ''}">
        ${seg(u.by.movies, 'movies', 'Movies')}${seg(u.by.tv, 'tv', 'TV')}${seg(u.by.anime, 'anime', 'Anime')}${seg(u.by.other, 'other', 'Other')}${seg(Math.min(coming, Math.max(0, cap - u.used)), 'coming', 'On its way')}
      </div>
      <div class="mqs-legend">
        <span><i class="movies"></i>Movies ${size(u.by.movies)}</span><span><i class="tv"></i>TV ${size(u.by.tv)}</span>
        <span><i class="anime"></i>Anime ${size(u.by.anime)}</span>${u.by.other ? `<span><i class="other"></i>Other ${size(u.by.other)}</span>` : ''}
        ${coming ? `<span><i class="coming"></i>On its way ${size(coming)}</span>` : ''}</div>`;
  }
  function draw() {
    const d = st.data, u = d.usage, s = d.settings, pr = u.projection || {};
    const disk = u.disk || {};
    const enforce = s.storage_enforce || 'warn';
    const rulesOn = s.delete_after_watch || s.delete_unwatched_days || (enforce === 'auto' && s.storage_limit_gb);
    const smart = `
      ${sw('smart_request', 'Request the next season as you watch', 'Shows requested "Smart" start with season 1; the next one is requested once you\'re partway through.', s.smart_request !== false)}
      <div class="set"><div class="lbl"><b>Request the next season at</b><span>Earlier (30%) for 4K or very big seasons.</span></div>
        <div class="opt-row">${[30, 50, 75].map(v => `<button class="opt" data-mqs-seg="smart_request_at" data-v="${v}" aria-pressed="${+(s.smart_request_at || 50) === v}">${v}%</button>`).join('')}</div></div>
    `;
    if (smartEl) smartEl.innerHTML = smart;
    el.innerHTML = `
    <section class="panel glass mqs-dash" style="--i:0"><h2>${ico('film', 20)} Storage</h2>
      <div class="mqs-headline"><span class="mqs-big">${size(u.used)}</span><span class="mqs-range">${u.limit ? `of your ${size(u.limit)} limit` : 'in your library'}${disk.free != null ? ` · ${size(disk.free)} free on ${esc(disk.drive || 'the disk')}` : ''}</span>
        ${u.over_limit ? '<span class="chip orange">Over the limit</span>' : u.projected_over ? '<span class="chip orange">Will go over</span>' : ''}
        <button class="icon-btn small" data-mqs-act="refresh" title="Measure again" aria-label="Measure again">${ico('restart', 16)}</button></div>
      ${bar(u)}
      <p class="mqs-line mqs-dim">Projected: ${size(u.projected)} once what's on its way arrives (${pr.missing_episode_count || 0} episodes and ${pr.missing_movie_count || 0} films monitored but missing${pr.pending_count ? `, ${pr.pending_count} requests waiting for approval` : ''}). ${pr.queue_count ? `${pr.queue_count} downloading now, ${size(pr.downloading_left)} to go.` : ''}</p>
      <p class="mqs-line mqs-dim">${esc(u.note)}</p>
      ${!u.arr_available ? `<p class="mqs-warn">${ico('info', 14)} Radarr/Sonarr aren't connected, so sizes are estimates and nothing can be removed. Connect them below.</p>` : ''}
      ${d.why_not && rulesOn ? `<p class="mqs-warn">${ico('info', 14)} ${esc(d.why_not)}</p>` : ''}
      ${!d.deletes_allowed ? `<p class="mqs-line mqs-dim">Development run: removing is switched off; everything below is a preview.</p>` : ''}
    </section>
    <section class="panel glass" style="--i:1"><h2>${ico('sun', 20)} Limit</h2>
      <div class="set"><div class="lbl"><b>Storage limit</b><span>How much the library may take (0 = no limit).</span></div>
        <div class="mqs-inline"><input class="input mqs-num" type="number" min="0" step="50" data-mqs-set="storage_limit_gb" value="${+s.storage_limit_gb || 0}" aria-label="Storage limit in GB"><span>GB</span></div></div>
      <div class="set stack"><div class="lbl"><b>When it's over the limit</b></div>
        <div class="opt-row">${[['warn', 'Warn me'], ['block', 'Block new requests'], ['auto', 'Free space automatically']].map(([v, l]) => `<button class="opt" data-mqs-seg="storage_enforce" data-v="${v}" aria-pressed="${enforce === v}">${l}</button>`).join('')}</div>
        <span class="mqs-line mqs-dim">${enforce === 'auto' ? 'Removes watched titles, oldest-watched first, with a "Leaving soon" notice first.' : enforce === 'block' ? 'Requests that would go over the limit are refused, with a reason.' : 'You get a warning in the request sheet.'}</span></div>
    </section>
    <section class="panel glass" style="--i:2"><h2>${ico('restart', 20)} Cleanup</h2>
      <p>Everything here is off until you turn it on. Titles always show in "Leaving soon" on Home first, with a Keep button.</p>
      ${sw('delete_after_watch', 'Delete after watching', 'Once everyone who watches it has finished. Never favourites, My List, or anything still in progress.', s.delete_after_watch)}
      <div class="set" ${s.delete_after_watch ? '' : 'hidden'}><div class="lbl"><b>Wait before deleting</b></div>
        <div class="opt-row">${[1, 3, 7, 14].map(v => `<button class="opt" data-mqs-seg="delete_after_watch_days" data-v="${v}" aria-pressed="${+s.delete_after_watch_days === v}">${v} day${v > 1 ? 's' : ''}</button>`).join('')}</div></div>
      ${s.delete_after_watch ? sw('delete_whole_seasons', 'Whole seasons only', 'Shows: wait until a season is fully watched (off: watched episodes go one by one; never the next one up or the season being watched).', s.delete_whole_seasons) : ''}
      <div class="set stack"><div class="lbl"><b>Remove what nobody started</b><span>Titles nobody has started, a while after they arrived. Not ones requested recently.</span></div>
        <div class="opt-row">${[[0, 'Off'], [30, '30 days'], [60, '60 days'], [90, '90 days'], [180, '180 days']].map(([v, l]) => `<button class="opt" data-mqs-seg="delete_unwatched_days" data-v="${v}" aria-pressed="${+s.delete_unwatched_days === v}">${l}</button>`).join('')}</div></div>
      ${sw('delete_torrents', 'Also remove the seeding torrent', 'A file hardlinked to a torrent only frees its space once the torrent goes too.', s.delete_torrents)}
      <div class="mqs-actions"><button class="btn small ghost" data-mqs-act="preview">${ico('search', 15)} Preview cleanup</button></div>
      <div class="mqs-plan" ${st.plan ? '' : 'hidden'}>${st.plan ? planHTML(st.plan) : ''}</div>
    </section>
    ${smartEl ? '' : `<section class="panel glass" style="--i:3"><h2>${ico('sparkle', 20)} Smart requests</h2>${smart}</section>`}
    ${listPanel('Leaving soon', d.leaving, i => `${esc(i.title)}${i.subtitle && !/^\d{4}$/.test(i.subtitle) ? ` · ${esc(i.subtitle)}` : ''}`, i => `${esc(i.reason || '')} · leaves ${esc(when(i.leave_at))}`, i => `<button class="btn small ghost" data-keep="${esc(i.keep_key)}">Keep</button>`, 4)}
    ${listPanel(`Watched, could be removed · ${size(d.could_size)}`, d.could, i => `${esc(i.title)}${i.subtitle && !/^\d{4}$/.test(i.subtitle) ? ` · ${esc(i.subtitle)}` : ''}`, i => esc(i.reason || ''), i => `<span class="mqs-dim">${size(i.size)}</span>`, 5)}
    ${listPanel('Largest', u.largest, i => `${esc(i.title)}${i.year ? ` (${i.year})` : ''}`, i => `${i.kind === 'series' ? `${i.files || ''} episodes · ` : ''}${esc({ movies: 'Movies', tv: 'TV', anime: 'Anime' }[i.group] || '')}`, i => `<b>${size(i.size)}</b>`, 6)}
    <section class="panel glass" style="--i:7"><h2>${ico('info', 20)} Deletion log</h2>
      ${d.log.length ? `<div class="mqs-list">${d.log.slice(0, st.showLog ? 100 : 6).map(e => `<div class="mqs-li"><div><b>${esc(e.title)}${e.subtitle && !/^\d{4}$/.test(e.subtitle) ? ` · ${esc(e.subtitle)}` : ''}</b>
        <span>${esc(day(e.at))} · ${esc(e.rule === 'manual' ? 'deleted by hand' : e.reason || e.rule || '')}${e.still_seeding ? ` · ${size(e.still_seeding)} still seeding` : ''}</span></div><span class="mqs-dim">${size(e.freed || e.size)}</span></div>`).join('')}</div>
        ${d.log.length > 6 ? `<button class="mqs-more" data-mqs-act="log">${st.showLog ? 'Show less' : `Show all ${d.log.length}`}</button>` : ''}` : '<p>Nothing has been removed yet.</p>'}
    </section>
    <section class="panel glass" style="--i:8"><h2>${ico('gear', 20)} Radarr &amp; Sonarr</h2>
      <p class="mqs-arr-status">${d.arrs.length ? `Connected: ${d.arrs.map(a => `${esc(a.name)}${a.source === 'seerr' ? ' (through Seerr)' : ''}`).join(', ')}.` : 'Not connected. Marquee asks Seerr for these when you\'re a Seerr admin; otherwise enter them here.'}</p>
      <details class="setx-adv" data-adv="storage-arr"${d.arrs.length ? '' : ' open'}><summary>Advanced</summary><div class="setx-adv-body">${['radarr', 'sonarr'].map(k => `<div class="set stack"><div class="lbl"><b>${k === 'radarr' ? 'Radarr' : 'Sonarr'}</b></div>
        <div class="fields mqs-arr-fields"><input class="input wide" data-arr="${k}_url" value="${esc(s[k + '_url'] || '')}" placeholder="http://localhost:${k === 'radarr' ? 7878 : 8989}" spellcheck="false" aria-label="${k} address">
        <input class="input wide" type="password" data-arr="${k}_key" placeholder="${s[k + '_key_set'] ? 'API key saved (type to replace)' : 'API key'}" autocomplete="off" aria-label="${k} API key"></div></div>`).join('')}</div></details>
    </section>`;
  }
  const sw = (key, title, sub, on) => `<div class="set"><div class="lbl"><b>${esc(title)}</b><span>${esc(sub)}</span></div>
    <button class="switch" role="switch" data-sw="${key}" aria-checked="${!!on}" aria-label="${esc(title)}"></button></div>`;
  function listPanel(title, items, a, b, c, i) {
    if (!items || !items.length) return '';
    return `<section class="panel glass" style="--i:${i}"><h2>${esc(title)}</h2><div class="mqs-list">${items.slice(0, 12).map(x => `<div class="mqs-li"><div><b>${a(x)}</b><span>${b(x)}</span></div>${c(x)}</div>`).join('')}</div></section>`;
  }
  function planHTML(p) {
    const t = p.totals || {};
    const rows = (p.run && p.run.actions && p.run.actions.length ? p.run.actions.map(a => ({ text: a.text, when: 'now' })) : [])
      .concat((p.items || []).filter(i => !i.due).map(i => ({ text: i.text, when: when(i.leave_at), reason: i.reason })));
    return `<p class="mqs-line"><b>${t.count || 0} title${t.count === 1 ? '' : 's'}</b> would go, freeing ${size(t.frees_now)}${t.frees_after_torrent ? ` (+${size(t.frees_after_torrent)} with their torrents)` : ''}.</p>
      ${p.why_not ? `<p class="mqs-warn">${ico('info', 14)} ${esc(p.why_not)}</p>` : ''}
      <div class="mqs-list">${rows.slice(0, 40).map(r => `<div class="mqs-li"><div><b>${esc(r.text)}</b><span>${esc(r.reason || '')}</span></div><span class="mqs-dim">${esc(r.when)}</span></div>`).join('') || '<p class="mqs-dim">Nothing would be removed with these settings.</p>'}</div>
      ${(p.skipped || []).length ? `<details class="mqs-skipped"><summary>Kept on purpose (${p.skipped.length})</summary>${p.skipped.slice(0, 40).map(x => `<div class="mqs-li"><div><b>${esc(x.title)}${x.subtitle && !/^\d{4}$/.test(x.subtitle) ? ` · ${esc(x.subtitle)}` : ''}</b><span>${esc(x.why)}</span></div></div>`).join('')}</details>` : ''}`;
  }
  async function save(changes) {
    try {
      const r = await api('/api/storage/settings', changes);
      Object.assign(st.data.settings, r.settings);
      toast('Saved', { icon: 'check', ms: 1400 });
      if (st.plan) st.plan = await api('/api/storage/plan?run=1');
      load();
    } catch (e) { toast(`Couldn't save: ${e.message}`); }
  }
  const onClick = async e => {
    const t = e.target.closest('button');
    if (!t || !st.data) return;
    if (t.dataset.sw) { const on = t.getAttribute('aria-checked') !== 'true'; t.setAttribute('aria-checked', on); return save({ [t.dataset.sw]: on }); }
    if (t.dataset.mqsSeg) { const v = isNaN(+t.dataset.v) ? t.dataset.v : +t.dataset.v; $$(`[data-mqs-seg="${t.dataset.mqsSeg}"]`, t.closest('.opt-row') || el).forEach(b => b.setAttribute('aria-pressed', b === t)); return save({ [t.dataset.mqsSeg]: v }); }
    if (t.dataset.keep) { t.disabled = true; try { await api(`/api/storage/keep/${encodeURIComponent(t.dataset.keep)}`, { keep: true }); toast('Kept', { icon: 'check' }); load(); } catch (err) { toast(err.message); t.disabled = false; } return; }
    if (t.dataset.mqsAct === 'refresh') { t.disabled = true; return load(true); }
    if (t.dataset.mqsAct === 'log') { st.showLog = !st.showLog; return draw(); }
    if (t.dataset.mqsAct === 'preview') {
      t.disabled = true;
      try { st.plan = await api('/api/storage/plan?run=1'); draw(); } catch (err) { toast(err.message); t.disabled = false; }
    }
  };
  el.addEventListener('click', onClick);
  if (smartEl) smartEl.addEventListener('click', onClick);
  el.addEventListener('change', e => {
    const t = e.target;
    if (t.dataset.mqsSet === 'storage_limit_gb') save({ storage_limit_gb: Math.max(0, +t.value || 0) });
    if (t.dataset.arr) { const v = t.value.trim(); if (t.dataset.arr.endsWith('_key') && !v) return; save({ [t.dataset.arr]: v }); if (t.type === 'password') t.value = ''; }
  });
  load();
}

/* ================================================================== events from Python (app.notify_ui) */
function onEvent(name, data) {
  data = data || {};
  if (name === 'storage-leaving') { toast(data.message || 'Some titles leave tomorrow to save space', { ms: 8000, action: 'Review', onAction: () => { location.hash = '#/settings/storage'; } }); return true; }
  if (name === 'storage-removed') { const n = (data.items || []).length; toast(`${n} title${n === 1 ? '' : 's'} removed to save space. Watch history is kept.`, { icon: 'check', ms: 6000 }); return true; }
  if (name === 'smart-request') { toast(data.message || 'Next season requested', { icon: 'sparkle', ms: 7000 }); return true; }
  return false;
}

window.MarqueeStorage = { mountQuality, seasonModes, sendRequest, leavingSoonRow, deleteSheet, smartLine, mountSettings, onEvent, size };
})();
