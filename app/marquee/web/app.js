/* Marquee browsing UI: a small single-page app over the local JSON API (see marquee/server.py).
   No framework: template strings + event delegation. Every animation is transform/opacity. */
(() => {
'use strict';

/* ================================================================== basics */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const finePointer = matchMedia('(hover: hover) and (pointer: fine)');
const root = document.documentElement;
const icon = (id, size = 18) => `<svg width="${size}" height="${size}" aria-hidden="true"><use href="#i-${id}"/></svg>`;

const S = {
  state: null, settings: {}, ai: { on: false }, user: '',
  items: new Map(),   // Jellyfin cards by id
  tmdb: new Map(),    // Seerr cards by "type:id"
  home: null, homeAt: 0, lib: {}, welcome: undefined, picks: undefined,
  route: '', renderSeq: 0,
  mylist: new Set(),  // My List keys: "item:<id>" for the library, "movie:<tmdb>" / "tv:<tmdb>" for titles to request
  mlData: null, index: undefined,
};

async function api(path, body, method) {
  const opt = body !== undefined
    ? { method: method || 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {};
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty body */ }
  if (!r.ok || data.ok === false) {
    const err = new Error(data.message || r.statusText || 'Something went wrong');
    err.status = r.status;
    throw err;
  }
  return data;
}

/* The desktop shell calls this when the native player closes. Defined first so it exists as early as possible. */
window.marquee = {
  onEvent(name, data) {
    try {
      if (window.MarqueeStorage && MarqueeStorage.onEvent(name, data)) return;
      if (window.MarqueeUpdate && MarqueeUpdate.onEvent(name, data)) return;
      if (name === 'playback-ended') { fetch('/api/taste/update', { method: 'POST' }).catch(() => {}); closeUpNext(); afterPlayback(data && data.id); }
      else if (name === 'library-changed') onLibraryChanged(data);
      else if (name === 'userdata-changed') onUserdataChanged(data);
      else if (name === 'connection') onConnection(data || {});
      else if (name === 'credits') openUpNext(data || {});
      else if (name === 'credits-closed') closeUpNext();
      else if (name === 'atlas-ending') onAtlasEnding(data || {});
      else if (name === 'atlas-answer') atlasToast(data || {});
      else if (name === 'share-progress') onShareProgress(data || {});
      else if (name === 'share-ready') onShareReady(data || {});
      else if (name === 'mobile-devices') { if (S.route === 'settings' && $('#remote-box')) loadRemote(true); }
      else if (name === 'share-auth') toast((data && data.message) || 'Google Drive needs you to sign in again', { action: 'Connect', onAction: () => shareConnect(), ms: 8000 });
    } catch (e) { console.error('event', name, e); }
  },
};

/* ================================================================== formatting */
function fmtRuntime(sec) {
  if (!sec) return '';
  const m = Math.round(sec / 60), h = Math.floor(m / 60);
  return h ? `${h}h ${m % 60 ? (m % 60) + 'm' : ''}`.trim() : `${m}m`;
}
function fmtClock(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}
const epCode = i => (i.season != null && i.episode != null) ? `S${i.season} · E${i.episode}` : (i.episode != null ? `E${i.episode}` : '');
const titleOf = i => (i.type === 'Episode' ? (i.series || i.name) : i.name) || 'Untitled';
function subOf(i) {
  if (i.type === 'Episode') return [epCode(i), i.name].filter(Boolean).join(' · ');
  if (i.type === 'Series') return [i.year, i.unplayed ? `${i.unplayed} unwatched` : 'Series'].filter(Boolean).join(' · ');
  return [i.year, fmtRuntime(i.runtime)].filter(Boolean).join(' · ');
}
const PALETTE = [['#23408f', '#6f8fd6'], ['#14203f', '#2f55b5'], ['#ff8a1f', '#ff5f6d'], ['#1f7a55', '#5fb8a0'], ['#5a3fa0', '#a07fe0'], ['#0d1738', '#ff8a1f'], ['#2f55b5', '#5fc6e0']];
function grad(name) {
  let h = 0;
  for (const c of String(name || '')) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  const [a, b] = PALETTE[h % PALETTE.length];
  return `linear-gradient(150deg, ${a}, ${b})`;
}
/* Tiny version of a Jellyfin/TMDB image for blurred placeholders. */
function tiny(url) {
  if (!url) return '';
  return url.replace(/fill(Width|Height)=\d+/, 'fillWidth=48').replace(/quality=\d+/, 'quality=40').replace('/w1280/', '/w300/').replace('/w500/', '/w92/');
}
function timeGreeting() {
  const h = new Date().getHours();
  if (h < 5) return ['Up late', 'The best films start after midnight.'];
  if (h < 12) return ['Good morning', 'Coffee, a blanket, something good.'];
  if (h < 17) return ['Good afternoon', 'A matinee never hurt anyone.'];
  if (h < 22) return ['Good evening', 'Lights down, phones away. What are we watching?'];
  return ['Good night', 'One more episode? We won’t tell.'];
}
const STATUS = { 2: ['Requested', 'solid', 'Requested and waiting to be picked up'], 3: ['Downloading', 'solid', 'On its way: downloading or being processed'],
  4: ['Partly here', 'orange', 'Some of it is in your library already, the rest is on its way'], 5: ['In library', 'green', 'Already on this PC'] };
/* Which glass a poster wears: clear = in your library, white = requested/on its way, smoky navy = can be requested. */
const availClass = status => status === 5 ? 'is-owned' : (status >= 2 && status <= 4) ? 'is-requested' : 'is-requestable';

/* ================================================================== images */
function imgTag(url, alt = '', cls = '', alt2 = '') {
  if (!url) return '';
  return `<img src="${esc(url)}" alt="${esc(alt)}" loading="lazy" decoding="async"${cls ? ` class="${cls}"` : ''}${alt2 ? ` data-alt="${esc(alt2)}"` : ''}>`;
}
document.addEventListener('load', e => {
  const t = e.target;
  if (t.tagName !== 'IMG') return;
  t.classList.add('loaded');
  const m = t.closest('.media');
  if (m) m.classList.add('ready');
}, true);
document.addEventListener('error', e => {
  const t = e.target;
  if (t.tagName !== 'IMG') return;
  if (t.dataset.alt) { const a = t.dataset.alt; delete t.dataset.alt; t.src = a; return; }
  if (t.classList.contains('logo-img')) { t.replaceWith(Object.assign(document.createElement('h1'), { className: t.dataset.cls || 'hero-title', textContent: t.alt })); return; }
  const m = t.closest('.media, .face, .r-poster-wrap');
  if (m) m.classList.add('noimg', 'ready');
  t.style.visibility = 'hidden';
}, true);

/* ================================================================== cards */
function reg(i) { if (i && i.id) S.items.set(i.id, { ...(S.items.get(i.id) || {}), ...i }); return i; }

function posterCard(i, n = 0, extra = '') {
  reg(i);
  const url = i.poster;
  const prog = !i.played && i.progress > 0.02 ? `<div class="progress"><i style="--p:${i.progress.toFixed(3)}"></i></div>` : '';
  const badge = i.played ? `<span class="tick" title="Watched">${icon('check')}</span>`
    : (i.type === 'Series' && i.unplayed ? `<span class="count-badge" title="${i.unplayed} unwatched">${i.unplayed}</span>` : '');
  /* Four glasses: clear = on this PC, white = requested / partly here, dark = can be requested,
     dark + check = watched, then removed to save space (state "removed_watched" from the storage manager). */
  const removed = i.state === 'removed_watched' || (i.removed && i.played);
  const glass = removed ? 'is-requestable is-removed' : 'is-owned';
  return `<div class="card poster ${glass}" role="button" tabindex="0" data-id="${esc(i.id)}" data-act="open" style="--i:${n}" aria-label="${esc(titleOf(i))}${removed ? ', watched and removed' : ''}">
    <div class="media${url ? '' : ' noimg ready'}" style="--fb:${grad(titleOf(i))}">${imgTag(url, '', '', i.thumb || i.backdrop)}
      <div class="fallback">${esc(titleOf(i))}</div><div class="badges">${badge}</div>${prog}${['Movie', 'Series', 'Episode'].includes(i.type) ? mlBtn(mlKeyOf(i), 'ml-btn') : ''}</div>
    <div class="label"><b>${esc(titleOf(i))}</b><span>${removed ? 'Watched · removed' : esc(subOf(i))}</span></div>${i.why ? `<div class="why">${esc(i.why)}</div>` : ''}${extra}</div>`;
}

function wideCard(i, n = 0) {
  reg(i);
  const url = i.thumb || i.backdrop;
  const left = i.runtime && i.position ? `${fmtRuntime(i.runtime - i.position)} left` : '';
  const sub = i.type === 'Episode' ? [epCode(i), i.name].filter(Boolean).join(' · ') : [left || i.year, i.age].filter(Boolean).join(' · ');
  const prog = !i.played && i.progress > 0.005 ? `<div class="progress"><i style="--p:${i.progress.toFixed(3)}"></i></div>` : '';
  return `<div class="card wide is-owned" role="button" tabindex="0" data-id="${esc(i.id)}" data-act="play" style="--i:${n}" aria-label="Play ${esc(titleOf(i))}">
    <div class="media${url ? '' : ' noimg ready'}" style="--fb:${grad(titleOf(i))}">${imgTag(url, '', '', i.backdrop !== url ? i.backdrop : '')}
      <div class="fallback">${esc(titleOf(i))}</div><div class="shade"></div>
      <div class="wide-info"><b>${esc(titleOf(i))}</b><span>${esc(sub)}${left && i.type === 'Episode' ? ' · ' + esc(left) : ''}</span></div>
      <span class="play-glyph">${icon('play')}</span>
      <button class="info-btn" data-act="info" aria-label="More info" title="More info">${icon('info', 16)}</button>${prog}</div></div>`;
}

function tmdbCard(c, n = 0, withBtn = false) {
  const key = `${c.type}:${c.id}`;
  S.tmdb.set(key, { ...(S.tmdb.get(key) || {}), ...c });
  const st = STATUS[c.status];
  const removed = c.state === 'removed_watched' || c.state === 'removed' || c.removed;
  const watchedGone = removed && (c.state === 'removed_watched' || c.played);
  const glass = removed && !(c.status >= 2 && c.status <= 5) ? 'is-requestable' + (watchedGone ? ' is-removed' : ' is-gone') : availClass(c.status);
  return `<div class="card poster tmdb ${glass}${withBtn ? ' req-card' : ''}" role="button" tabindex="0" data-tmdb="${key}" data-act="tmdb" style="--i:${n}" aria-label="${esc(c.title)}">
    <div class="media${c.poster ? '' : ' noimg ready'}" style="--fb:${grad(c.title)}">${imgTag(c.poster)}
      <div class="fallback">${esc(c.title)}</div>${!st && !removed ? `<span class="req-hint">${icon('plus', 14)} Request</span>` : ''}
      <div class="badges">${st && c.status !== 5 ? `<span class="chip glass-chip status" title="${st[2]}">${st[0]}</span>` : ''}${removed && !st ? `<span class="chip dark-glass removed-chip" title="Deleted to save space${c.removed_at ? ' on ' + esc(fmtDate(c.removed_at)) : ''}">${watchedGone ? 'Watched · removed' : 'Removed'}</span>` : ''}${watchedGone ? `<span class="tick" title="Watched">${icon('check')}</span>` : ''}${c.anime && !removed ? '<span class="chip dark-glass" style="margin-left:auto">Anime</span>' : ''}</div>${mlBtn(key, 'ml-btn')}</div>
    <div class="label"><b>${esc(c.title)}</b><span>${esc(removed ? [c.year, c.removed_at ? 'removed ' + fmtDate(c.removed_at, { month: 'short', day: 'numeric' }) : 'removed'].filter(Boolean).join(' · ') : [c.year, c.type === 'tv' ? 'Series' : 'Film'].filter(Boolean).join(' · '))}</span></div>${c.why ? `<div class="why">${esc(c.why)}</div>` : ''}
    </div>`;
}

function rowHTML(row, n) {
  const wide = row.style === 'wide';
  const cards = row.items.map((it, k) => (row.tmdb || row.style === 'request') ? tmdbCard(it, k) : (wide ? wideCard(it, k) : posterCard(it, k))).join('');
  return `<section class="row" style="--i:${n}" data-key="${esc(row.key)}">
    <div class="row-head"><h2>${row.ai ? `<span class="eyebrow orange" style="display:inline-flex;margin-right:10px">${esc(row.ai)}</span>` : ''}${esc(row.title)}</h2><span class="count">${row.items.length}</span></div>
    <button class="row-arrow l" data-action="row-l" aria-label="Scroll left">${icon('left', 20)}</button>
    <div class="scroller">${cards}</div>
    <button class="row-arrow r" data-action="row-r" aria-label="Scroll right">${icon('right', 20)}</button></section>`;
}

/* Rows animate in as they come into view; arrows show only when there's more to scroll. */
const rowObserver = new IntersectionObserver(entries => {
  for (const e of entries) if (e.isIntersecting) { e.target.classList.add('in'); rowObserver.unobserve(e.target); updateArrows(e.target); }
}, { rootMargin: '0px 0px -60px 0px', threshold: 0.15 });
function watchRows(scope) {
  let n = 0;
  for (const r of $$('.row:not(.in)', scope)) {
    r.style.setProperty('--i', n++ % 6);
    rowObserver.observe(r);
  }
}
function updateArrows(row) {
  const sc = $('.scroller', row);
  if (!sc) return;
  $('.row-arrow.l', row)?.classList.toggle('can', sc.scrollLeft > 8);
  $('.row-arrow.r', row)?.classList.toggle('can', sc.scrollLeft + sc.clientWidth < sc.scrollWidth - 8);
}

/* ================================================================== layers */
const layers = { search: $('#search'), modal: $('#modal'), sheet: $('#sheet'), person: $('#person') };
function openLayer(el) {
  closePop();
  el.hidden = false;
  el.classList.remove('closing');
  root.classList.add('locked');
  requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add('open')));
}
function closeLayer(el) {
  if (el.hidden || el.classList.contains('closing')) return;
  if (el === layers.modal) { stopHero(); closeLayer(layers.person); D.seq++; D.kind = null; }
  el.classList.remove('open');
  el.classList.add('closing');
  setTimeout(() => {
    if (!el.classList.contains('closing')) return;
    el.hidden = true;
    el.classList.remove('closing');
    if (!Object.values(layers).some(l => !l.hidden) && !$('.surprise')) root.classList.remove('locked');
    if (S.homeStale && !anyLayerOpen()) { S.homeStale = false; if (S.route === 'home') route(); }
  }, reduced.matches ? 10 : 260);
}
const anyLayerOpen = () => Object.values(layers).some(l => !l.hidden) || !!$('.surprise');

/* ================================================================== toasts, confetti, ripples */
function toast(msg, opts = {}) {
  const t = document.createElement('div');
  t.className = 'toast';
  t.innerHTML = `${opts.icon ? icon(opts.icon) : ''}<span>${esc(msg)}</span>${opts.action ? `<button class="btn small on-dark">${esc(opts.action)}</button>` : ''}`;
  if (opts.action) t.querySelector('button').onclick = () => { opts.onAction(); dismiss(); };
  $('#toasts').append(t);
  const dismiss = () => { t.classList.add('out'); setTimeout(() => t.remove(), 300); };
  setTimeout(dismiss, opts.ms || 3200);
  while ($('#toasts').children.length > 3) $('#toasts').firstElementChild.remove();
}
function confetti(x, y, n = 28) {
  if (reduced.matches) return;
  const colors = ['#ff8a1f', '#2f55b5', '#6f8fd6', '#ffd166', '#1f7a55', '#ff5f6d', '#ffffff'];
  for (let k = 0; k < n; k++) {
    const p = document.createElement('i');
    p.className = 'confetti';
    const a = Math.random() * Math.PI * 2, d = 80 + Math.random() * 160;
    p.style.cssText = `left:${x}px;top:${y}px;background:${colors[k % colors.length]};--dx:${Math.cos(a) * d}px;--dy:${Math.sin(a) * d - 60 + Math.random() * 160}px;--rot:${Math.random() * 720 - 360}deg;animation-delay:${Math.random() * 60}ms`;
    document.body.append(p);
    setTimeout(() => p.remove(), 1300);
  }
}
function heartFloat(btn) {
  if (reduced.matches) return;
  const r = btn.getBoundingClientRect();
  const f = document.createElement('i');
  f.className = 'favorite-float';
  f.innerHTML = icon('heart', 22);
  f.style.left = r.left + r.width / 2 + 'px';
  f.style.top = r.top + 'px';
  document.body.append(f);
  setTimeout(() => f.remove(), 900);
}
document.addEventListener('pointerdown', e => {
  const b = e.target.closest('.btn');
  if (!b || reduced.matches) return;
  const r = b.getBoundingClientRect(), s = Math.max(r.width, r.height) / 2;
  const rp = document.createElement('i');
  rp.className = 'ripple';
  rp.style.cssText = `width:${s}px;height:${s}px;left:${e.clientX - r.left - s / 2}px;top:${e.clientY - r.top - s / 2}px`;
  b.append(rp);
  setTimeout(() => rp.remove(), 650);
});

/* ================================================================== theme */
function applyTheme(t) {
  t = t === 'light' ? 'light' : 'dark';
  root.dataset.theme = t;
  try { localStorage.setItem('marquee-theme', t); } catch (e) { /* storage off */ }
}

/* ================================================================== playback */
async function play(id, start, label) {
  closePop();
  const it = S.items.get(id) || {};
  try {
    await api('/api/play', start == null ? { id } : { id, start });
    toast(`Starting ${label || titleOf(it)}${it.type === 'Episode' ? ' · ' + epCode(it) : ''}…`, { icon: 'film' });
  } catch (e) {
    toast(`Couldn't start playback: ${e.message}`);
  }
}
/* Series play their next-up episode (or the very first); films and episodes resume where you left off. */
async function playItem(id) {
  let it = S.items.get(id);
  if (!it || it.type !== 'Series') return play(id);
  try {
    const { item } = await api(`/api/item/${id}`);
    if (item.next_up) { reg(item.next_up); return play(item.next_up.id); }
    const first = item.seasons && item.seasons[0];
    if (!first) return toast('No episodes yet');
    const { items } = await api(`/api/episodes/${id}/${first.id}`);
    if (items[0]) { reg(items[0]); play(items[0].id, 0); }
  } catch (e) { toast(e.message); }
}
async function afterPlayback(id) {
  S.home = null;
  S.lib = {};
  if (S.route === 'home') refreshHomeRows();
  const open = layers.modal.dataset.item;
  if (!layers.modal.hidden && open && D.kind === 'item') {
    const it = S.items.get(id);
    if (open === id || (it && it.series_id === open)) openDetails(open, { refresh: true, season: D.season && D.season.number });
  }
}

/* ================================================================== hover preview */
const pv = { timer: 0, card: null, pop: null, lastClose: 0 };
function previewsOn() { return S.settings.previews !== false && finePointer.matches && !anyLayerOpen(); }

document.addEventListener('pointerover', e => {
  const card = e.target.closest('.card[data-id]');
  if (pv.pop && e.target.closest('.pop')) return;
  if (card === pv.card) return;
  clearTimeout(pv.timer);
  pv.card = card;
  if (!card || !previewsOn() || card.closest('.layer')) return;
  const quick = Date.now() - pv.lastClose < 900;   // browsing along a row: next preview comes sooner
  pv.timer = setTimeout(() => openPop(card), quick ? 550 : 1500);
});
document.addEventListener('pointerleave', () => { clearTimeout(pv.timer); pv.card = null; });
const cancelPreview = () => { clearTimeout(pv.timer); pv.card = null; closePop(); closeArrPop(); };
addEventListener('scroll', cancelPreview, { capture: true, passive: true });
addEventListener('wheel', e => { if (pv.pop || pv.timer) cancelPreview(); }, { passive: true });
addEventListener('blur', cancelPreview);

function openPop(card) {
  closePop(true);
  const id = card.dataset.id, i = S.items.get(id);
  if (!i || !document.contains(card)) return;
  const m = $('.media', card).getBoundingClientRect();
  const wide = card.classList.contains('wide');
  const W = Math.round(wide ? Math.min(Math.max(m.width * 1.3, 380), 540) : Math.min(Math.max(m.width * 2.05, 340), 470));
  const H = W * 9 / 16 + 178;
  const cx = m.left + m.width / 2, cy = m.top + m.height / 2;
  const left = Math.min(Math.max(cx - W / 2, 16), innerWidth - W - 16);
  const top = Math.min(Math.max(cy - H / 2, 84), innerHeight - H - 12);
  const el = document.createElement('div');
  el.className = 'pop';
  el.dataset.id = id;
  el.style.cssText = `left:${left}px;top:${top}px;width:${W}px;--ox:${cx - left}px;--oy:${cy - top}px;--s0:${(m.width / W).toFixed(3)}`;
  const art = i.backdrop || i.thumb || i.poster;
  const resume = !i.played && i.position > 30;
  const meta = [i.year, i.age && `<span class="chip">${esc(i.age)}</span>`, i.type === 'Series' ? 'Series' : fmtRuntime(i.runtime),
    i.rating && `<span style="color:var(--orange);font-weight:700">★ ${i.rating.toFixed(1)}</span>`].filter(Boolean);
  el.innerHTML = `<div class="pop-media">${art ? `<img src="${esc(art)}" alt="">` : `<div class="fallback" style="display:grid;--fb:${grad(titleOf(i))}">${esc(titleOf(i))}</div>`}
      <video muted playsinline preload="none" disablepictureinpicture></video>
      ${i.logo ? `<img class="pop-logo" src="${esc(i.logo)}" alt="" onerror="this.remove()">` : ''}<div class="pop-progress"><i></i></div></div>
    <div class="pop-body">
      <div class="pop-actions">
        <button class="btn small round" data-act="play-item" title="${resume ? 'Resume' : 'Play'}" aria-label="Play">${icon('play', 16)}</button>
        <button class="btn small round ghost${i.played ? ' is-on' : ''}" data-act="watched" title="${i.played ? 'Mark unwatched' : 'Mark watched'}" aria-label="Toggle watched">${icon('check', 16)}</button>
        ${mlBtn(mlKeyOf(i), 'btn small round ghost')}
        <span class="grow"></span>
        <button class="btn small round ghost" data-act="open" title="More info" aria-label="More info">${icon('info', 16)}</button>
      </div>
      <div class="pop-title">${esc(titleOf(i))}${i.type === 'Episode' ? ` <span style="color:var(--muted);font-weight:500">· ${esc(epCode(i))}</span>` : ''}</div>
      <div class="pop-meta">${meta.map((x, k) => `<span${k ? ' class="dot"' : ''}>${x}</span>`).join('')}</div>
      ${resume ? `<div class="pop-meta" style="margin-top:8px"><span style="flex:1;height:4px;border-radius:9px;background:var(--line);position:relative;overflow:hidden"><i style="position:absolute;inset:0;background:var(--orange);transform-origin:left;transform:scaleX(${i.progress})"></i></span><span>${fmtRuntime(i.runtime - i.position)} left</span></div>` : ''}
      ${i.overview ? `<p class="pop-overview">${esc(i.overview)}</p>` : ''}
      ${i.genres && i.genres.length ? `<div class="genres">${i.genres.slice(0, 3).map(esc).join(' · ')}</div>` : ''}
    </div>`;
  $('#pop-layer').append(el);
  el.addEventListener('pointerleave', e => { if (!e.relatedTarget || !el.contains(e.relatedTarget)) { pv.card = null; closePop(); } });
  const P = pv.pop = { el, id, video: $('video', el), stopAt: 0 };
  const realH = el.offsetHeight;   // layout size (the scale transform doesn't affect it)
  const top2 = Math.min(Math.max(cy - realH / 2, 84), innerHeight - realH - 12);
  if (Math.abs(top2 - top) > 1) { el.style.top = top2 + 'px'; el.style.setProperty('--oy', (cy - top2) + 'px'); }
  el.getBoundingClientRect();
  el.classList.add('open');
  if (['Movie', 'Series', 'Episode'].includes(i.type)) startClip(P);
}

async function startClip(P) {
  let res;
  try { res = await api(`/api/preview/${P.id}`); } catch (e) { return; }
  if (pv.pop !== P || !res.url) return;
  const v = P.video;
  const bar = $('.pop-progress i', P.el);
  v.addEventListener('playing', () => {
    if (pv.pop !== P) return;
    v.classList.add('playing');
    P.el.querySelector('.pop-media').classList.add('vid');
    if (!P.stopAt) P.stopAt = performance.now() + 30000;
  });
  v.addEventListener('timeupdate', () => {
    if (!P.stopAt) return;
    const left = P.stopAt - performance.now();
    bar.style.setProperty('--p', Math.min(1, 1 - left / 30000).toFixed(3));
    if (left <= 0) endClip(P);
  });
  v.addEventListener('error', () => endClip(P), { once: true });
  addPreviewSubs(v, res.subs);
  v.src = res.url;
  v.play().catch(() => {});
}
function endClip(P) {
  const v = P.video;
  v.classList.remove('playing');
  P.el.querySelector('.pop-media')?.classList.remove('vid');
  bar0(P);
  setTimeout(() => unloadVideo(v), 700);
}
function bar0(P) { $('.pop-progress i', P.el)?.style.setProperty('--p', 0); }
function unloadVideo(v) {
  try { v.pause(); v.querySelectorAll('track').forEach(t => t.remove()); v.removeAttribute('src'); v.load(); } catch (e) { /* already gone */ }
}
/* Previews start muted, so they show subtitles (the preview window's cues as WebVTT from the server). */
function addPreviewSubs(v, url) {
  v.controls = false; v.disableRemotePlayback = true; v.disablePictureInPicture = true;
  v.setAttribute('controlslist', 'nodownload nofullscreen noremoteplayback noplaybackrate');
  v.querySelectorAll('track').forEach(t => t.remove());
  if (!url) return;
  const t = document.createElement('track');
  t.kind = 'subtitles'; t.srclang = 'en'; t.label = 'Subtitles'; t.default = true; t.src = url;
  t.addEventListener('load', () => { try { t.track.mode = 'showing'; } catch (e) { /* gone */ } });
  v.append(t);
  try { t.track.mode = 'showing'; } catch (e) { /* not ready */ }
}
function closePop(instant) {
  const P = pv.pop;
  if (!P) return;
  pv.pop = null;
  pv.lastClose = Date.now();
  unloadVideo(P.video);
  if (instant) { P.el.remove(); return; }
  P.el.classList.add('closing');
  setTimeout(() => P.el.remove(), 260);
}

/* Poster tilt + sheen follows the pointer. */
let tiltCard = null, tiltRaf = 0;
document.addEventListener('pointermove', e => {
  if (reduced.matches || !finePointer.matches) return;
  const card = e.target.closest('.card');
  if (tiltCard && tiltCard !== card) resetTilt(tiltCard);
  tiltCard = card;
  if (!card) return;
  cancelAnimationFrame(tiltRaf);
  tiltRaf = requestAnimationFrame(() => {
    const r = card.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
    card.style.setProperty('--ry', ((x - .5) * 9).toFixed(2) + 'deg');
    card.style.setProperty('--rx', ((.5 - y) * 7).toFixed(2) + 'deg');
    card.style.setProperty('--mx', (x * 100).toFixed(1) + '%');
    card.style.setProperty('--my', (y * 100).toFixed(1) + '%');
  });
}, { passive: true });
function resetTilt(c) { c.style.setProperty('--rx', '0deg'); c.style.setProperty('--ry', '0deg'); }

/* ================================================================== router */
const ROUTES = { home: renderHome, movies: (p, s) => renderLibrary('movies', p, s), shows: (p, s) => renderLibrary('shows', p, s),
  anime: (p, s) => renderLibrary('anime', p, s), requests: renderRequests, settings: renderSettings, mylist: renderMyList,
  chat: page => window.MarqueeChat ? window.MarqueeChat.mount(page) : renderHome(page, S.renderSeq),
  stack: page => window.MarqueeStack ? window.MarqueeStack.mount(page) : renderHome(page, S.renderSeq) };

function currentRoute() {
  const r = location.hash.replace(/^#\/?/, '').split('/')[0];
  return ROUTES[r] ? r : 'home';
}
async function route() {
  if (!S.state || !S.state.server_ok) return;
  const r = currentRoute();
  closePop(true);
  closeLayer(layers.modal);
  closeLayer(layers.sheet);
  closeSearch();
  S.route = r;
  const seq = ++S.renderSeq;
  for (const a of $$('[data-route]')) a.toggleAttribute('aria-current', a.dataset.route === r) || a.removeAttribute('aria-current');
  $$('[data-route]').forEach(a => a.dataset.route === r ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current'));
  movePill();
  const view = $('#view');
  const old = view.firstElementChild;
  if (old && !reduced.matches) { old.classList.add('leaving'); await sleep(170); }
  if (seq !== S.renderSeq) return;
  scrollTo(0, 0);
  if (r !== 'home') setAmbient(null);
  const page = document.createElement('div');
  page.className = 'page page-' + r;
  view.replaceChildren(page);
  ROUTES[r](page, seq);
}
addEventListener('hashchange', route);
addEventListener('marquee:taste-changed', () => {
  S.home = null; S.picks = undefined; S.discoverAi = undefined;
  // a rating made on a title page: Home catches up when the page closes, instead of closing it now
  if (S.route === 'home') { if (anyLayerOpen()) S.homeStale = true; else route(); }
});

function closeNavMenu() {
  const m = $('#nav-menu');
  if (!m || m.hidden) return false;
  m.hidden = true;
  $('.nav-menu-btn')?.setAttribute('aria-expanded', 'false');
  return true;
}
function movePill() {
  const pill = $('#nav-pill'), a = $('#nav a[aria-current="page"]');
  if (!a) { pill.classList.remove('on'); return; }
  pill.style.width = a.offsetWidth + 'px';
  pill.style.transform = `translateX(${a.offsetLeft}px)`;
  pill.classList.add('on');
}
addEventListener('resize', movePill);
document.fonts && document.fonts.ready.then(movePill);

const stale = seq => seq !== S.renderSeq;

/* ================================================================== home */
function skeletonRows(n = 3) {
  return Array.from({ length: n }, () => `<div class="skel skel-line"></div><div class="skeleton-row">${'<i></i>'.repeat(10)}</div>`).join('');
}

async function renderHome(page, seq) {
  const [hello, quip] = timeGreeting();
  const name = S.user ? `, ${S.user}` : '';
  page.innerHTML = `<section class="greet"><span class="eyebrow">${esc(new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' }))}</span>
      <h1 style="margin-top:6px">${esc(hello)}${esc(name)} <span class="wave">👋</span></h1>
      <div class="ai-line" id="ai-line"><span>${esc(quip)}</span></div></section>
    <div class="hero skel" id="hero"></div>
    <div class="rows" id="rows">${skeletonRows(3)}</div>`;
  welcomeLine(page);
  let data = S.home;
  if (!data || Date.now() - S.homeAt > 5 * 60e3) {
    try { data = await api('/api/home'); S.home = data; S.homeAt = Date.now(); }
    catch (e) { if (!stale(seq)) $('#rows', page).innerHTML = errorBox(e); return; }
  }
  if (stale(seq)) return;
  if (data.user && !S.user) setUser(data.user);
  renderHero($('#hero', page), data.hero || []);
  const rows = $('#rows', page);
  rows.innerHTML = data.rows.length ? data.rows.map(rowHTML).join('')
    : `<div class="empty"><span class="big">🍿</span>Your library looks empty. Add some films in Jellyfin and they'll appear here.</div>`;
  watchRows(rows);
  addMyListRow(rows, seq);
  if (window.MarqueeStorage) MarqueeStorage.leavingSoonRow().then(el => {
    if (!el || stale(seq) || !document.contains(rows)) return;
    const after = $$('.row', rows).filter(r => ['continue', 'nextup'].includes(r.dataset.key)).pop();
    after ? after.after(el) : rows.prepend(el);
  }, () => {});
  addPicksRow(rows, seq);
  addTasteRow(rows, seq);
  addDiscoverRows(rows, seq);
  addAiDiscover(rows, seq);
  startArrivals();
}

/* A row that follows the taste profile, under the top rows. */
function addTasteRow(rows, seq) {
  api('/api/taste/home-row').then(r => {
    if (stale(seq) || !r.row || !document.contains(rows) || $('[data-key="taste"]', rows)) return;
    const tmp = document.createElement('div');
    tmp.innerHTML = rowHTML(r.row, 0);
    const el = tmp.firstElementChild;
    const top = $$('.row', rows).filter(x => ['continue', 'mylist', 'nextup'].includes(x.dataset.key));
    const after = top[top.length - 1];
    if (after) after.after(el); else rows.prepend(el);
    watchRows(rows);
  }, () => {});
}

/* Titles to request, woven between the library rows: sequels, the rest of collections, "more like X". */
async function addDiscoverRows(rows, seq) {
  if (!S.discover || Date.now() - S.discover.at > 30 * 60e3) {
    try { S.discover = { rows: (await api('/api/home/discover')).rows || [], at: Date.now() }; } catch (e) { return; }
  }
  if (stale(seq) || !document.contains(rows)) return;
  const anchors = $$('.row', rows).filter(r => r.dataset.key === 'recent' || r.dataset.key.startsWith('rec-'));
  S.discover.rows.filter(r => r.items && r.items.length).forEach((row, k) => {
    if ($(`.row[data-key="${CSS.escape('d-' + row.key)}"]`, rows)) return;
    const html = rowHTML({ ...row, key: 'd-' + row.key, style: 'request' }, 0);
    const at = anchors[k];
    if (at && document.contains(at)) at.insertAdjacentHTML('afterend', html);
    else rows.insertAdjacentHTML('beforeend', html);
  });
  watchRows(rows);
}

/* The assistant's curated request row: can take minutes the first time, so it slides in whenever it lands. */
async function addAiDiscover(rows, seq) {
  if (!S.ai.on) return;
  if (S.discoverAi === undefined) {
    S.discoverAi = api('/api/home/discover/ai').then(r => r.row || null, () => null);
  }
  const row = await S.discoverAi;
  if (stale(seq) || !row || !row.items || !row.items.length || !document.contains(rows) || $('[data-key="ai-discover"]', rows)) return;
  const tmp = document.createElement('div');
  tmp.innerHTML = rowHTML({ ...row, key: 'ai-discover', style: 'request', ai: S.ai.name || 'Assistant' }, 0);
  const el = tmp.firstElementChild;
  const top = $$('.row', rows).filter(r => ['continue', 'nextup', 'ai-picks'].includes(r.dataset.key));
  const after = top[top.length - 1];
  if (after) after.after(el); else rows.prepend(el);
  el.classList.add('in');
  if (!reduced.matches) {
    const h = el.offsetHeight + 18;
    el.animate([{ opacity: 0, transform: 'translateY(30px) scale(.98)' }, { opacity: 1, transform: 'none' }], { duration: 800, easing: 'cubic-bezier(.34,1.3,.64,1)' });
    let sib = el.nextElementSibling;
    for (; sib; sib = sib.nextElementSibling) {
      if (sib.getBoundingClientRect().top > innerHeight) break;
      sib.animate([{ transform: `translateY(${-h}px)` }, { transform: 'none' }], { duration: 650, easing: 'cubic-bezier(.2,.8,.2,1)' });
    }
  }
  updateArrows(el);
}

/* The time-of-day line shows at once; the assistant's line animates in if and when it arrives. */
async function welcomeLine(page) {
  if (!S.ai.on) return;
  if (S.welcome === undefined) {
    try { S.welcome = (await api('/api/ai/welcome')).text || null; } catch (e) { S.welcome = null; }
  }
  const line = $('#ai-line', page);
  if (!S.welcome || !line || !document.contains(line)) return;
  const words = S.welcome.split(/(\s+)/).filter(Boolean);
  line.innerHTML = `${icon('sparkle')}<span>${words.map((w, k) => `<span class="w" style="animation-delay:${Math.min(k, 80) * 22}ms">${esc(w)}</span>`).join('')}</span>`;
  line.title = `${S.ai.name || 'Assistant'} says`;
}

async function addPicksRow(rows, seq) {
  if (!S.ai.on) return;
  if (S.picks === undefined) {
    try { S.picks = (await api('/api/ai/picks')).items || []; } catch (e) { S.picks = []; }
  }
  if (stale(seq) || !S.picks.length || $('[data-key="ai-picks"]', rows)) return;
  const html = rowHTML({ key: 'ai-picks', title: 'Picked for you', ai: S.ai.name || 'Assistant', items: S.picks, style: 'poster' }, 0);
  const wideRows = $$('.row', rows).filter(r => ['continue', 'nextup'].includes(r.dataset.key));
  const after = wideRows[wideRows.length - 1];
  if (after) after.insertAdjacentHTML('afterend', html); else rows.insertAdjacentHTML('afterbegin', html);
  watchRows(rows);
}

/* ------------------------------------------------------------------ live refresh
   Rows are reconciled card by card: changed cards are swapped in place, new ones slide in, gone ones fold away.
   Rows Home shuffles on every load (genres, random picks) only get their cards' watched state refreshed. */
const STABLE_ROWS = new Set(['continue', 'nextup', 'recent', 'new-movies', 'new-shows', 'top']);
const sigOf = i => i ? [i.played, (i.progress || 0).toFixed(2), i.unplayed ?? '', i.favorite, i.name].join('|') : '';
let refreshing = null;
async function refreshHomeRows() {
  if (refreshing) return refreshing;
  refreshing = (async () => {
    let data;
    try { data = await api('/api/home'); } catch (e) { return; }
    S.home = data; S.homeAt = Date.now();
    const rows = $('#rows');
    if (!rows || S.route !== 'home') return;
    let prev = null;
    for (const fresh of data.rows) {
      const old = $(`.row[data-key="${CSS.escape(fresh.key)}"]`, rows);
      if (old) {
        reconcileRow(old, fresh, STABLE_ROWS.has(fresh.key));
        prev = old;
      } else if (STABLE_ROWS.has(fresh.key)) {
        const tmp = document.createElement('div');
        tmp.innerHTML = rowHTML(fresh, 0);
        const el = tmp.firstElementChild;
        if (prev) prev.after(el); else rows.prepend(el);
        el.classList.add('in');
        if (!reduced.matches) el.animate([{ opacity: 0, transform: 'translateY(20px)' }, { opacity: 1, transform: 'none' }], { duration: 600, easing: 'cubic-bezier(.34,1.3,.64,1)' });
        prev = el;
      }
    }
    const keys = new Set(data.rows.map(r => r.key));
    for (const r of $$('.row', rows)) if (!keys.has(r.dataset.key) && STABLE_ROWS.has(r.dataset.key)) foldAway(r);
  })();
  try { await refreshing; } finally { refreshing = null; }
}
function reconcileRow(rowEl, fresh, full) {
  const sc = $('.scroller', rowEl);
  const wide = fresh.style === 'wide';
  const make = i => { const t = document.createElement('div'); t.innerHTML = wide ? wideCard(i) : posterCard(i); return t.firstElementChild; };
  const byId = new Map($$('.card[data-id]', sc).map(c => [c.dataset.id, c]));
  const wanted = new Set(fresh.items.map(i => i.id));
  if (full) for (const [id, c] of byId) if (!wanted.has(id)) foldAway(c, true);
  let before = null;
  for (const it of fresh.items) {
    const cur = byId.get(it.id);
    const changed = sigOf(S.items.get(it.id)) !== sigOf({ ...(S.items.get(it.id) || {}), ...it });
    if (cur) {
      if (changed) { const n = make(it); n.style.animation = 'none'; cur.replaceWith(n); before = n; } else { reg(it); before = cur; }
    } else if (full) {
      const n = make(it);
      if (before) before.after(n); else sc.prepend(n);
      if (!reduced.matches) n.animate([{ opacity: 0, transform: 'translateX(-24px) scale(.9)' }, { opacity: 1, transform: 'none' }], { duration: 650, easing: 'cubic-bezier(.34,1.56,.64,1)' });
      before = n;
    }
  }
  const count = $('.count', rowEl);
  if (count && full) count.textContent = fresh.items.length;
  updateArrows(rowEl);
}
function foldAway(el, horizontal) {
  if (reduced.matches) return el.remove();
  el.style.pointerEvents = 'none';
  const a = el.animate(horizontal ? [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(.8)' }]
    : [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(-12px)' }], { duration: 320, easing: 'ease', fill: 'forwards' });
  a.onfinish = () => el.remove();
}

/* Events from the desktop app. Bursts are coalesced so a big import refreshes once. */
let liveTimer = 0;
function scheduleLive(ms = 1200) {
  clearTimeout(liveTimer);
  liveTimer = setTimeout(() => {
    S.lib = {};
    if (S.route === 'home') { refreshHomeRows(); syncArrivals(); } else S.home = null;
  }, ms);
}
function onLibraryChanged() { S.discover = null; scheduleLive(); }
function onUserdataChanged(data) {
  const list = (data && data.items) || [];
  const ids = [];
  for (const x of list) {
    const id = typeof x === 'string' ? x : (x && (x.id || x.Id || x.ItemId));
    if (!id) continue;
    ids.push(id);
    if (typeof x !== 'object') continue;
    const patch = { id };
    if ('played' in x || 'Played' in x) patch.played = !!(x.played ?? x.Played);
    if ('favorite' in x || 'IsFavorite' in x) patch.favorite = !!(x.favorite ?? x.IsFavorite);
    if (S.items.has(id)) reg(patch);
    if ('played' in patch) for (const b of $$(`.card[data-id="${CSS.escape(id)}"] .badges`)) b.innerHTML = patch.played ? `<span class="tick">${icon('check')}</span>` : '';
  }
  const open = layers.modal.dataset.item;
  if (!layers.modal.hidden && open && D.kind === 'item' && ids.some(id => id === open || (S.items.get(id) || {}).series_id === open)) openDetails(open, { refresh: true, season: D.season && D.season.number });
  if (data && data.items && data.items.some(x => typeof x === 'object' && x && ('IsFavorite' in x || 'favorite' in x))) { S.mlData = null; refreshMyListViews(); }
  scheduleLive(800);
}

/* ------------------------------------------------------------------ arrivals
   "Just arrived": finished downloads Jellyfin hasn't imported yet, playable straight away.
   "Arriving soon": still downloading, with a progress ring and ETA. Polled every 15 s while Home is showing. */
const ARR = { timer: 0, cards: new Map(), lastReady: new Set() };
const RING = 2 * Math.PI * 22;
function fmtEta(s) {
  if (s == null || s < 0) return '';
  if (s < 60) return 'under a minute';
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)}h ${m % 60 ? (m % 60) + 'm' : ''}`.trim();
}
const arrTitle = c => c.type === 'Episode' ? (c.series || c.name) : c.name;
const arrSub = c => c.type === 'Episode' ? [c.season != null && c.episode != null ? `S${c.season} · E${c.episode}` : '', c.name].filter(Boolean).join(' · ') : [c.year, c.quality].filter(Boolean).join(' · ');
const clamp01 = v => Math.max(0, Math.min(1, v || 0));
function arrivalCard(c, ready) {
  ARR.cards.set(c.key, c);
  const p = clamp01(c.progress);
  return `<div class="card poster arrival ${ready ? 'is-owned arrived' : 'is-requested coming'}" role="button" tabindex="0" data-arrival="${esc(c.key)}" data-act="arrival" aria-label="${esc(arrTitle(c))}">
    <div class="media${c.poster ? '' : ' noimg ready'}" style="--fb:${grad(arrTitle(c))}">${imgTag(c.poster, '', '', c.backdrop)}
      <div class="fallback">${esc(arrTitle(c))}</div>
      <div class="badges">${ready ? `<span class="chip glass-chip new-chip" title="Downloaded and ready to play">${icon('sparkle', 12)} New</span>` : `<span class="chip glass-chip status" title="Downloading now">${c.quality ? esc(c.quality) : 'Downloading'}</span>`}</div>
      ${ready ? `<span class="arr-play">${icon('play', 22)}</span>` : `<div class="arr-ring"><svg viewBox="0 0 52 52" aria-hidden="true"><circle cx="26" cy="26" r="22" class="trk"/><circle cx="26" cy="26" r="22" class="val" style="stroke-dasharray:${RING.toFixed(1)};stroke-dashoffset:${(RING * (1 - p)).toFixed(1)}"/></svg><b>${Math.round(p * 100)}%</b></div>
      <div class="arr-eta">${c.eta ? esc(fmtEta(c.eta)) + ' left' : 'Starting…'}</div>`}
    </div>
    <div class="label"><b>${esc(arrTitle(c))}</b><span>${esc(arrSub(c))}</span></div></div>`;
}
function startArrivals() {
  clearInterval(ARR.timer);
  syncArrivals();
  ARR.timer = setInterval(() => {
    if (S.route !== 'home') return clearInterval(ARR.timer);
    if (!document.hidden) syncArrivals();
  }, 15000);
}
async function syncArrivals() {
  let data;
  try { data = await api('/api/arrivals'); } catch (e) { return; }
  const rows = $('#rows');
  if (!rows || S.route !== 'home') return;
  const ready = data.ready || [], coming = data.coming || [];
  const rects = new Map($$('.card[data-arrival]', rows).map(c => [c.dataset.arrival, c.getBoundingClientRect()]));
  const cont = $('.row[data-key="continue"]', rows);
  const readyRow = arrRow(rows, 'arr-ready', 'Just arrived', ready, true, cont);
  arrRow(rows, 'arr-coming', 'Arriving soon', coming, false, readyRow || cont);
  if (!reduced.matches) for (const c of $$('.card[data-moved]', rows)) {
    delete c.dataset.moved;
    const a = rects.get(c.dataset.arrival);
    if (!a) continue;
    const b = c.getBoundingClientRect();
    c.animate([{ transform: `translate(${a.left - b.left}px, ${a.top - b.top}px) scale(.95)` }, { transform: 'none' }], { duration: 750, easing: 'cubic-bezier(.34,1.3,.64,1)' });
  }
  // A "Just arrived" card that vanished was imported by Jellyfin: refresh so its real card slides into the library rows.
  const nowReady = new Set(ready.map(c => c.key));
  const imported = [...ARR.lastReady].some(k => !nowReady.has(k));
  ARR.lastReady = nowReady;
  if (imported) scheduleLive(400);
}
function arrRow(rows, key, title, items, ready, after) {
  let row = $(`.row[data-key="${key}"]`, rows);
  if (!items.length) {
    if (row) { for (const c of $$('.card[data-arrival]', row)) ARR.cards.delete(c.dataset.arrival); foldAway(row); }
    return null;
  }
  if (!row) {
    const t = document.createElement('div');
    t.innerHTML = `<section class="row in arr-row" data-key="${key}"><div class="row-head"><h2>${ready ? '<span class="eyebrow orange" style="display:inline-flex;margin-right:10px">Fresh</span>' : ''}${title}</h2><span class="count"></span></div>
      <button class="row-arrow l" data-action="row-l" aria-label="Scroll left">${icon('left', 20)}</button><div class="scroller"></div>
      <button class="row-arrow r" data-action="row-r" aria-label="Scroll right">${icon('right', 20)}</button></section>`;
    row = t.firstElementChild;
    if (after && document.contains(after)) after.after(row); else rows.prepend(row);
    if (!reduced.matches) row.animate([{ opacity: 0, transform: 'translateY(18px)' }, { opacity: 1, transform: 'none' }], { duration: 600, easing: 'cubic-bezier(.34,1.3,.64,1)' });
  }
  const sc = $('.scroller', row);
  const wanted = new Set(items.map(c => c.key));
  for (const c of $$('.card[data-arrival]', sc)) if (!wanted.has(c.dataset.arrival)) foldAway(c, true);
  let prev = null;
  for (const c of items) {
    let el = $(`.card[data-arrival="${CSS.escape(c.key)}"]`, sc);
    if (el && !ready) {   // still downloading: move the ring and ETA in place
      const p = clamp01(c.progress);
      const v = $('.arr-ring .val', el);
      if (v) v.style.strokeDashoffset = (RING * (1 - p)).toFixed(1);
      const b = $('.arr-ring b', el); if (b) b.textContent = Math.round(p * 100) + '%';
      const eta = $('.arr-eta', el); if (eta) eta.textContent = c.eta ? fmtEta(c.eta) + ' left' : 'Starting…';
      ARR.cards.set(c.key, c);
    } else if (!el) {
      const elsewhere = $(`.card[data-arrival="${CSS.escape(c.key)}"]`, rows);
      const t = document.createElement('div');
      t.innerHTML = arrivalCard(c, ready);
      el = t.firstElementChild;
      if (elsewhere) { elsewhere.remove(); el.dataset.moved = '1'; }
      if (prev) prev.after(el); else sc.prepend(el);
      if (!elsewhere && !reduced.matches) el.animate([{ opacity: 0, transform: 'scale(.85)' }, { opacity: 1, transform: 'none' }], { duration: 600, easing: 'cubic-bezier(.34,1.56,.64,1)' });
      if (elsewhere && ready) setTimeout(() => { if (!document.contains(el)) return; const r = el.getBoundingClientRect(); confetti(r.left + r.width / 2, r.top + 30, 16); }, 600);
    } else ARR.cards.set(c.key, c);
    prev = el;
  }
  $('.count', row).textContent = items.length;
  updateArrows(row);
  return row;
}

/* Small glass popover for an arrival: what it is, and Play (or how long is left). */
function arrivalPop(cardEl) {
  closeArrPop(true);
  closePop(true);
  const c = ARR.cards.get(cardEl.dataset.arrival);
  if (!c) return;
  const ready = cardEl.classList.contains('arrived');
  const r = $('.media', cardEl).getBoundingClientRect();
  const W = 340;
  const p = clamp01(c.progress);
  const el = document.createElement('div');
  el.className = 'arr-pop glass';
  el.innerHTML = `<span class="eyebrow${ready ? ' orange' : ''}">${ready ? 'Just arrived' : 'Arriving soon'}</span>
    <h3>${esc(arrTitle(c))}</h3><div class="pop-meta">${esc(arrSub(c))}</div>
    ${c.overview ? `<p class="pop-overview">${esc(c.overview)}</p>` : ''}
    ${ready ? `<div class="m-actions" style="margin-top:14px"><button class="btn small" data-act="arrival-play" data-key="${esc(c.key)}">${icon('play', 16)} Play now</button>
      <span style="font-size:12px;color:var(--muted)">It joins your library shortly.</span></div>`
      : `<div class="pop-meta" style="margin-top:12px"><span style="flex:1;height:6px;border-radius:9px;background:var(--line);position:relative;overflow:hidden"><i style="position:absolute;inset:0;background:var(--orange);transform-origin:left;transform:scaleX(${p})"></i></span><span>${Math.round(p * 100)}%${c.eta ? ' · ' + esc(fmtEta(c.eta)) + ' left' : ''}</span></div>`}`;
  document.body.append(el);
  const h = el.offsetHeight;
  const left = Math.min(Math.max(r.left + r.width / 2 - W / 2, 16), innerWidth - W - 16);
  const below = r.bottom + 10 + h < innerHeight;
  el.style.left = left + 'px';
  el.style.top = (below ? r.bottom + 10 : Math.max(80, r.top - h - 10)) + 'px';
  el.style.width = W + 'px';
  el.style.transformOrigin = `${r.left + r.width / 2 - left}px ${below ? 0 : h}px`;
  setTimeout(() => el.classList.add('open'), 20);
}
function closeArrPop(instant) {
  const el = $('.arr-pop');
  if (!el) return false;
  if (instant) { el.remove(); return true; }
  el.classList.remove('open');
  setTimeout(() => el.remove(), 220);
  return true;
}
async function playArrival(key) {
  closeArrPop();
  const c = ARR.cards.get(key) || {};
  try {
    await api('/api/arrivals/play', { key });
    toast(`Starting ${arrTitle(c) || 'your download'}…`, { icon: 'film' });
  } catch (e) { toast(e.message); }
}

/* ------------------------------------------------------------------ hero carousel */
const hero = { timer: 0, idx: 0, n: 0, el: null, dur: 9000 };
function renderHero(el, items) {
  clearInterval(hero.timer);
  if (!items.length) { el.remove(); return; }
  el.classList.remove('skel');
  hero.el = el; hero.n = items.length; hero.idx = 0;
  el.innerHTML = items.map((i, k) => {
    reg(i);
    const meta = [i.year && `<span class="chip dark-glass">${i.year}</span>`, i.age && `<span class="chip dark-glass">${esc(i.age)}</span>`,
      i.rating && `<span class="chip dark-glass">★ ${i.rating.toFixed(1)}</span>`,
      `<span class="chip dark-glass">${i.type === 'Series' ? 'Series' : esc(fmtRuntime(i.runtime))}</span>`,
      ...(i.genres || []).slice(0, 2).map(g => `<span class="chip dark-glass">${esc(g)}</span>`)].filter(Boolean).join('');
    const resume = !i.played && i.position > 30;
    return `<div class="hero-slide${k === 0 ? ' on' : ''}" data-id="${esc(i.id)}">
      <div class="hero-art"><div class="hero-zoom"><img class="ph" src="${esc(tiny(i.backdrop))}" alt=""><img src="${esc(i.backdrop)}" alt="" ${k ? 'loading="lazy"' : 'fetchpriority="high"'}></div></div>
      <div class="hero-copy">
        ${i.logo ? `<img class="hero-logo logo-img" src="${esc(i.logo)}" alt="${esc(i.name)}">` : `<h1 class="hero-title">${esc(i.name)}</h1>`}
        <div class="hero-meta">${meta}</div>
        <p class="hero-overview">${esc(i.overview)}</p>
        <div class="hero-actions">
          <button class="btn light" data-act="play-item" data-id="${esc(i.id)}">${icon('play')} ${resume ? `Resume <span class="sub">${fmtRuntime(i.runtime - i.position)} left</span>` : 'Play'}</button>
          <button class="btn on-dark" data-act="open" data-id="${esc(i.id)}">${icon('info')} More info</button>
        </div></div></div>`;
  }).join('') + (items.length > 1 ? `<button class="hero-nav prev" data-action="hero-prev" aria-label="Previous">${icon('left', 20)}</button>
      <button class="hero-nav next" data-action="hero-next" aria-label="Next">${icon('right', 20)}</button>
      <div class="hero-dots">${items.map((_, k) => `<button data-action="hero-go" data-n="${k}" aria-label="Show ${k + 1}"${k === 0 ? ' class="on"' : ''}><i style="--dur:${hero.dur}ms"></i></button>`).join('')}</div>` : '');
  el.onmouseenter = () => el.classList.add('paused');
  el.onmouseleave = () => el.classList.remove('paused');
  setAmbient(items[0].backdrop);
  hero.timer = setInterval(() => {
    if (!document.contains(el)) return clearInterval(hero.timer);
    if (el.classList.contains('paused') || document.hidden || anyLayerOpen()) return;
    heroGo(hero.idx + 1);
  }, 250);
  hero.next = performance.now() + hero.dur;
  heroTick();
}
/* Advance when the dot's fill completes (pauses with hover, like the dot animation). */
function heroTick() {
  const el = hero.el;
  const dot = el && $('.hero-dots button.on i', el);
  if (!dot) return;
  dot.onanimationend = () => heroGo(hero.idx + 1);
  clearInterval(hero.timer);
}
function heroGo(n) {
  const el = hero.el;
  if (!el || !document.contains(el) || hero.n < 2) return;
  hero.idx = (n + hero.n) % hero.n;
  $$('.hero-slide', el).forEach((s, k) => s.classList.toggle('on', k === hero.idx));
  $$('.hero-dots button', el).forEach((d, k) => {
    d.classList.toggle('on', k === hero.idx);
    const i = d.firstElementChild;
    i.replaceWith(i.cloneNode());   // restart the fill animation
  });
  const it = S.items.get($$('.hero-slide', el)[hero.idx].dataset.id);
  if (it) setAmbient(it.backdrop);
  heroTick();
}
function setAmbient(url) {
  const a = $('#ambient-img');
  if (!url) { a.classList.remove('on'); return; }
  const img = new Image();
  img.onload = () => { a.style.backgroundImage = `url("${tiny(url)}")`; a.classList.add('on'); };
  img.src = tiny(url);
}
/* Soft parallax on the hero art. */
let parallaxRaf = 0;
addEventListener('scroll', () => {
  if (reduced.matches || !hero.el) return;
  cancelAnimationFrame(parallaxRaf);
  parallaxRaf = requestAnimationFrame(() => {
    const y = scrollY;
    if (y > innerHeight) return;
    for (const a of $$('.hero-art', hero.el)) a.style.transform = `translate3d(0, ${(y * 0.22).toFixed(1)}px, 0)`;
    const copy = $('.hero-slide.on .hero-copy', hero.el);
    if (copy) { copy.style.transform = `translate3d(0, ${(-y * 0.08).toFixed(1)}px, 0)`; copy.style.opacity = Math.max(0, 1 - y / 520).toFixed(2); }
  });
}, { passive: true });

/* ================================================================== libraries */
const LIB_TITLES = { movies: ['Movies', 'film', 'films'], shows: ['Shows', 'show', 'shows'], anime: ['Anime', 'title', 'titles'] };
async function renderLibrary(kind, page, seq) {
  const [title, one, many] = LIB_TITLES[kind];
  page.innerHTML = `<div class="page-head"><div class="grow"><span class="eyebrow">Your library</span><h1 style="margin-top:8px">${title}</h1><p id="lib-sub">Loading…</p></div>
      ${kind === 'movies' ? `<button class="btn ghost" data-action="surprise">${icon('dice')} Surprise me</button>` : ''}</div>
    <div class="rows" id="rows">${skeletonRows(2)}</div><div id="everything"></div>`;
  let data = S.lib[kind];
  if (!data || Date.now() - data.at > 5 * 60e3) {
    try { data = { ...(await api(`/api/library/${kind}`)), at: Date.now() }; S.lib[kind] = data; }
    catch (e) { if (!stale(seq)) $('#rows', page).innerHTML = errorBox(e); return; }
  }
  if (stale(seq)) return;
  const all = data.rows.find(r => r.key === 'all');
  const rest = data.rows.filter(r => r.key !== 'all');
  const total = all ? all.items.length : 0;
  $('#lib-sub', page).textContent = total ? `${total} ${total === 1 ? one : many}, ${all.items.filter(i => !i.played).length} you haven't finished` : '';
  const rows = $('#rows', page);
  rows.innerHTML = rest.map(rowHTML).join('');
  watchRows(rows);
  if (!all || !all.items.length) {
    $('#everything', page).innerHTML = `<div class="empty"><span class="big">🎞️</span>Nothing here yet.</div>`;
    return;
  }
  renderEverything($('#everything', page), all.items, kind);
}

function renderEverything(box, items, kind) {
  const st = { q: '', sort: 'az', unwatched: false, removed: true, gone: [] };
  box.innerHTML = `<div class="grid-tools"><h2>Everything</h2>
      <label class="field-pill">${icon('search', 17)}<input type="search" placeholder="Filter by title" aria-label="Filter by title"></label>
      <div class="pill-toggle" role="group" aria-label="Sort">
        <button data-sort="az" aria-pressed="true">A–Z</button><button data-sort="new" aria-pressed="false">Newest</button><button data-sort="rating" aria-pressed="false">Top rated</button></div>
      <div class="pill-toggle"><button data-unw aria-pressed="false">Unwatched</button></div>
      <div class="pill-toggle" hidden id="rm-toggle"><button data-rm aria-pressed="true" title="Titles deleted to save space (watched ones keep their check)">Show removed</button></div></div><div class="grid"></div>`;
  const grid = $('.grid', box);
  const draw = () => {
    let list = items.filter(i => (!st.q || (i.name || '').toLowerCase().includes(st.q)) && (!st.unwatched || !i.played));
    if (st.sort === 'new') list = [...list].sort((a, b) => String(b.added || '').localeCompare(String(a.added || '')));
    if (st.sort === 'rating') list = [...list].sort((a, b) => (b.rating || 0) - (a.rating || 0));
    const gone = st.removed && !st.unwatched ? st.gone.filter(c => !st.q || (c.title || '').toLowerCase().includes(st.q)) : [];
    grid.innerHTML = list.length || gone.length ? list.map((i, k) => posterCard(i, k)).join('') + gone.map((c, k) => tmdbCard(c, k + list.length)).join('')
      : `<div class="empty" style="grid-column:1/-1"><span class="big">🔍</span>No matches.</div>`;
  };
  if (kind) {   // removed titles (storage): kept visible, darker glass, watched ones with their check
    const kinds = kind === 'movies' ? ['movie'] : kind === 'shows' ? ['tv'] : ['movie', 'tv'];
    Promise.all(kinds.map(k => api(`/api/storage/removed?kind=${k}`).then(r => r.items || [], () => [])))
      .then(lists => {
        st.gone = lists.flat().filter(c => kind !== 'anime' ? !c.anime : c.anime);
        if (!st.gone.length || !document.contains(box)) return;
        $('#rm-toggle', box).hidden = false;
        draw();
      });
  }
  $('input', box).addEventListener('input', e => { st.q = e.target.value.trim().toLowerCase(); draw(); });
  box.addEventListener('click', e => {
    const s = e.target.closest('[data-sort]');
    if (s) { st.sort = s.dataset.sort; $$('[data-sort]', box).forEach(b => b.setAttribute('aria-pressed', b === s)); draw(); }
    const u = e.target.closest('[data-unw]');
    if (u) { st.unwatched = !st.unwatched; u.setAttribute('aria-pressed', st.unwatched); draw(); }
    const rm = e.target.closest('[data-rm]');
    if (rm) { st.removed = !st.removed; rm.setAttribute('aria-pressed', st.removed); draw(); }
  });
  draw();
}

function errorBox(e) {
  return `<div class="notice glass"><span class="big">🛰️</span><h2>Couldn't reach your library</h2><p>${esc(e.message)}</p><button class="btn" data-action="reload">${icon('restart')} Try again</button></div>`;
}

/* ================================================================== details (title page)
   One page for every title: library films and shows (from the local catalog, so it opens offline) and titles that
   aren't in the library yet (TMDB through Seerr, cached). The hero plays a silent preview of the title itself
   (or its trailer when it isn't here), then: ratings, cast, seasons and episodes, extras, collection, more like it. */
const D = { seq: 0, kind: null, id: null, key: null, vm: null, item: null, extras: null, seasons: [], season: null,
  muted: true, media: null, openedAt: 0, focusEp: null, scrolledNext: false };
const fmtDate = (s, opts = { month: 'short', day: 'numeric', year: 'numeric' }) => {
  if (!s) return '';
  const d = new Date(String(s).length <= 10 ? s + 'T12:00:00' : s);
  return isNaN(d) ? '' : d.toLocaleDateString(undefined, opts);
};
const isOffline = () => !!(S.state && S.state.offline) || !navigator.onLine;
const mlKeyOf = i => (i.type === 'Episode' || i.type === 'Season') && i.series_id ? 'item:' + i.series_id : 'item:' + i.id;

function mlBtn(key, cls = 'btn round on-dark') {
  const on = S.mylist.has(key);
  return `<button class="${cls} ml${on ? ' is-on' : ''}" data-act="ml" data-ml-key="${esc(key)}" title="${on ? 'Remove from My List' : 'Add to My List'}" aria-label="${on ? 'Remove from My List' : 'Add to My List'}" aria-pressed="${on}">${icon(on ? 'check' : 'plus')}</button>`;
}

/* --- view model shared by library titles and TMDB titles */
function vmItem(i) {
  return { kind: 'item', id: i.id, type: i.type, title: i.name, year: i.year, age: i.age, runtime: i.runtime,
    seasonsCount: (i.seasons || []).filter(s => s.episodes > 0 && s.number !== 0).length, rating: i.rating, media: i.media || {},
    genres: i.genres || [], overview: i.overview, tagline: i.tagline, logo: i.logo, backdrop: i.backdrop, poster: i.poster,
    played: i.played, position: i.position, progress: i.progress, nextUp: i.next_up, mlKey: mlKeyOf(i), studios: i.studios || [],
    people: i.people || [], similar: i.similar || [], available: i.available, local: i.local };
}
function vmTmdb(d) {
  return { kind: 'tmdb', id: d.tmdb, key: `${d.kind}:${d.tmdb}`, type: d.kind === 'tv' ? 'Series' : 'Movie', title: d.title,
    year: d.year, age: d.age, runtime: d.runtime, seasonsCount: (d.seasons || []).filter(s => s.number > 0).length, rating: d.rating,
    media: {}, genres: d.genres || [], overview: d.overview, tagline: d.tagline, logo: null, backdrop: d.backdrop, poster: d.poster,
    status: d.status, mlKey: `${d.kind}:${d.tmdb}`, studios: d.networks && d.networks.length ? d.networks : (d.studios || []),
    anime: d.anime, showStatus: d.show_status, nextAir: d.next_air };
}

/* --- quality badges from the media streams */
function qualityBadges(m) {
  if (!m) return [];
  const out = [];
  const h = m.height || 0;
  if (h >= 1600) out.push('4K'); else if (h >= 700) out.push('HD');
  if (m.dv) out.push('Dolby Vision'); else if (m.hdr10plus) out.push('HDR10+'); else if (m.hlg) out.push('HLG'); else if (m.hdr) out.push('HDR');
  if (m.atmos) out.push('Atmos'); else if (m.dtsx) out.push('DTS:X'); else if (m.channels >= 8) out.push('7.1'); else if (m.channels >= 6) out.push('5.1');
  return out;
}

/* --- the shell: hero art + skeleton body, shown at once */
function dxShell(seed) {
  const bd = seed && seed.backdrop;
  return `<div class="dx">
    <div class="dx-hero">
      <div class="dx-art">${bd ? `<img class="ph" src="${esc(tiny(bd))}" alt=""><img class="bd" src="${esc(bd)}" alt="">` : `<div class="ph" style="background:${grad(seed ? seed.title || seed.name : '')}"></div>`}</div>
      <div class="dx-media"></div><div class="dx-shade"></div>
      <button class="icon-btn dx-close" data-action="close-modal" aria-label="Close">${icon('close', 20)}</button>
      <div class="dx-vctl" hidden>
        <button class="dx-vbtn" data-act="dx-replay" title="Replay" aria-label="Replay preview">${icon('restart', 18)}</button>
        <button class="dx-vbtn" data-act="dx-mute" title="Unmute" aria-label="Unmute">${icon('muted', 18)}</button></div>
      <div class="dx-copy">${seed ? copyHTML(seed, true) : `<div class="skel dx-skel-title"></div><div class="skel dx-skel-line"></div>`}</div>
    </div>
    <div class="dx-body">${bodySkeleton()}</div></div>`;
}
function bodySkeleton() {
  return `<div class="dx-cols"><div><div class="skel dx-skel-line" style="width:90%"></div><div class="skel dx-skel-line" style="width:80%"></div><div class="skel dx-skel-line" style="width:60%"></div>
    <div class="dx-ratings">${'<span class="skel dx-skel-chip"></span>'.repeat(3)}</div></div><div></div></div>
    <div class="dx-cast-row" style="margin-top:28px">${'<span class="skel dx-skel-face"></span>'.repeat(7)}</div>`;
}

function copyHTML(v, partial) {
  const isItem = v.kind === 'item';
  const meta = [v.year && `<span>${esc(v.year)}</span>`, v.age && `<span class="dx-age">${esc(v.age)}</span>`,
    v.type === 'Series' ? (v.seasonsCount ? `<span>${v.seasonsCount} season${v.seasonsCount === 1 ? '' : 's'}</span>` : '') : (v.runtime ? `<span>${fmtRuntime(v.runtime)}</span>` : ''),
    v.rating ? `<span class="dx-star">★ ${(+v.rating).toFixed(1)}</span>` : ''].filter(Boolean).join('<i class="sep"></i>');
  const badges = qualityBadges(v.media).map(b => `<span class="q-badge">${esc(b)}</span>`).join('');
  let primary = '', progress = '', extra = '';
  if (isItem && v.type === 'Series') {
    const nu = v.nextUp;
    primary = nu ? `<button class="btn light dx-play" data-act="play-ep" data-id="${esc(nu.id)}">${icon('play')} ${nu.position > 30 ? 'Resume' : 'Play'} <span class="sub">${esc(epCode(nu))}</span></button>`
      : `<button class="btn light dx-play" data-act="play-item" data-id="${esc(v.id)}">${icon('play')} Play</button>`;
    if (nu && nu.position > 30 && nu.runtime) progress = progressBar(nu.position / nu.runtime, `${fmtRuntime(nu.runtime - nu.position)} left in ${epCode(nu)}`);
  } else if (isItem) {
    const resume = !v.played && v.position > 30;
    primary = resume ? `<button class="btn light dx-play" data-act="resume" data-id="${esc(v.id)}">${icon('play')} Resume <span class="sub">${fmtClock(v.position)}</span></button>`
      : `<button class="btn light dx-play" data-act="restart" data-id="${esc(v.id)}">${icon('play')} Play</button>`;
    if (resume) {
      extra = `<button class="btn round on-dark" data-act="restart" data-id="${esc(v.id)}" title="Start over" aria-label="Start over">${icon('restart')}</button>`;
      if (v.runtime) progress = progressBar(v.progress, `${fmtRuntime(v.runtime - v.position)} left`);
    }
  } else {
    const st = v.status;
    primary = st === 2 || st === 3 ? `<span class="btn light dx-play is-static">${icon('check')} ${st === 2 ? 'Requested' : 'Downloading'}</span>`
      : `<button class="btn light dx-play" data-act="tmdb-request" data-tmdb="${esc(v.key)}">${icon('plus')} ${st === 4 ? 'Request the rest' : 'Request'}</button>`;
  }
  const watched = isItem ? `<button class="btn round on-dark${v.played ? ' is-on' : ''}" data-act="watched" data-id="${esc(v.id)}" title="${v.played ? 'Mark unwatched' : 'Mark watched'}" aria-label="${v.played ? 'Mark unwatched' : 'Mark watched'}">${icon('check')}</button>` : '';
  const title = v.logo ? `<img class="dx-logo logo-img" data-cls="dx-title" src="${esc(v.logo)}" alt="${esc(v.title)}">` : `<h1 class="dx-title">${esc(v.title)}</h1>`;
  const sub = v.kind === 'tmdb' ? `<span class="dx-kicker">${v.status === 4 ? 'Partly in your library' : v.status === 2 || v.status === 3 ? 'On its way' : 'Not in your library yet'}${v.anime ? ' · Anime' : ''}</span>` : '';
  return `${sub}${title}
    <div class="dx-meta">${meta}${badges ? `<span class="dx-badges">${badges}</span>` : ''}</div>
    ${v.type === 'Series' ? '<div id="smart-line" class="dx-smart"></div>' : ''}<div id="removed-line"></div>
    ${v.genres.length ? `<div class="dx-genres">${v.genres.slice(0, 4).map(esc).join(' · ')}</div>` : ''}
    <div class="dx-actions">${primary}${extra}${mlBtn(v.mlKey)}
      <button class="btn on-dark dx-trailer" data-act="dx-trailer" hidden>${icon('film')} Trailer</button>${watched}
      ${isItem && window.MarqueeStorage && (v.type === 'Series' || v.available) ? `<span class="more-wrap"><button class="btn round on-dark" data-act="more-menu" aria-haspopup="menu" aria-expanded="false" title="More" aria-label="More">${icon('dots')}</button>
        <span class="more-menu glass" role="menu" hidden><button role="menuitem" data-act="delete-local" data-id="${esc(v.id)}" data-name="${esc(v.title)}" data-type="${esc(v.type)}">${icon('close', 15)} Delete from this PC…</button></span></span>` : ''}
      ${isItem && v.type === 'Movie' && S.shareOn ? `<button class="btn round on-dark" data-act="share" data-id="${esc(v.id)}" data-name="${esc(v.title)}" ${v.available ? '' : 'disabled'} title="${v.available ? 'Share as a Google Drive link' : 'Only files on this PC can be shared'}" aria-label="Share">${icon('share')}</button>` : ''}</div>
    ${progress}`;
}
function progressBar(p, label) {
  return `<div class="dx-progress"><span><i style="--p:${clamp01(p).toFixed(3)}"></i></span>${esc(label)}</div>`;
}

/* --- open */
function beginDetails(kind, id, opts) {
  const seq = ++D.seq;
  const switching = D.id !== id || D.kind !== kind;
  if (switching || !opts.refresh) stopHero(true);
  Object.assign(D, { kind, id, extras: switching ? null : D.extras, focusEp: opts.focus || null });
  if (switching) { D.item = null; D.season = null; D.scrolledNext = false; }
  layers.modal.dataset.item = kind === 'item' ? id : 'tmdb:' + id;
  const fresh = layers.modal.hidden || layers.modal.classList.contains('closing');
  if (fresh) { openLayer(layers.modal); layers.modal.scrollTop = 0; }
  else if (!opts.refresh) layers.modal.scrollTo({ top: 0, behavior: reduced.matches ? 'auto' : 'smooth' });
  if (!opts.refresh) D.openedAt = performance.now();
  return { seq, fresh };
}

async function openDetails(id, opts = {}) {
  const seed = S.items.get(id);
  if (seed && (seed.type === 'Episode' || seed.type === 'Season') && seed.series_id) return openDetails(seed.series_id, { season: seed.season, focus: seed.type === 'Episode' ? seed.id : null, ...opts });
  const { seq, fresh } = beginDetails('item', id, opts);
  const box = $('#modal-box');
  if (!opts.refresh) { box.innerHTML = dxShell(seed ? vmItem(seed) : null); if (fresh) flyIn(opts.from, seed); }
  let item;
  try { item = (await api(`/api/item/${encodeURIComponent(id)}`)).item; }
  catch (e) { if (D.seq === seq) $('.dx-body', box).innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
  if (D.seq !== seq) return;
  reg(item);
  if (item.next_up) reg(item.next_up);
  if (item.mylist) S.mylist.add(mlKeyOf(item)); else if (item.mylist === false) S.mylist.delete(mlKeyOf(item));
  D.item = item;
  D.vm = vmItem(item);
  if (!opts.refresh && seed && !seed.backdrop && item.backdrop) $('.dx-art', box).innerHTML = `<img class="ph" src="${esc(tiny(item.backdrop))}" alt=""><img class="bd" src="${esc(item.backdrop)}" alt="">`;
  renderDetails(opts);
  if (!opts.refresh) heroPreview(seq);
  loadExtras(seq);
  loadWhy(seq);
}
/* A short "why watch" line (Classic mode's prepared lines or the assistant's; instant, works offline). */
async function loadWhy(seq) {
  if (D.why && D.why.id === D.id) return drawWhy();
  let r;
  try { r = await api(`/api/classic/intro/${encodeURIComponent(D.id)}`); } catch (e) { return; }
  if (D.seq !== seq || !r.text) return;
  D.why = { id: D.id, text: r.text };
  drawWhy();
}
function drawWhy() {
  const copy = $('.dx-copy');
  if (!copy || !D.why || D.why.id !== D.id || $('.dx-why', copy)) return;
  const at = $('.dx-actions', copy);
  if (at) at.insertAdjacentHTML('beforebegin', `<p class="dx-why">${icon('sparkle', 15)}<span>${esc(D.why.text)}</span></p>`);
}

async function openTmdb(key, opts = {}) {
  const [type, id] = key.split(':');
  const seed = S.tmdb.get(key);
  const { seq, fresh } = beginDetails('tmdb', key, opts);
  D.key = key;
  const box = $('#modal-box');
  const sv = seed ? { kind: 'tmdb', key, id: +id, type: type === 'tv' ? 'Series' : 'Movie', title: seed.title, year: seed.year, rating: seed.rating,
    genres: seed.genres || [], overview: seed.overview, backdrop: seed.backdrop, poster: seed.poster, status: seed.status, mlKey: key, media: {} } : null;
  if (!opts.refresh) { box.innerHTML = dxShell(sv); if (fresh) flyIn(opts.from, sv); }
  let d;
  try { d = await api(`/api/tmdb/${type}/${id}`); }
  catch (e) { if (D.seq === seq) $('.dx-body', box).innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
  if (D.seq !== seq) return;
  if (d.owned_id) { D.kind = null; return openDetails(d.owned_id, { ...opts, refresh: false }); }
  const it = d.item;
  S.tmdb.set(key, { ...(seed || {}), type, id: +id, title: it.title, year: it.year, poster: it.poster, backdrop: it.backdrop, overview: it.overview, status: it.status, anime: it.anime });
  if (it.mylist) S.mylist.add(key);
  D.item = it;
  D.vm = vmTmdb(it);
  D.extras = { ratings: it.ratings || {}, cast: it.cast || [], crew: it.crew || [], videos: it.videos || [], collection: it.collection,
    recommended: it.recommended || [], tmdb_seasons: it.seasons || [], seasons_status: it.seasons_status || {}, imdb: it.imdb };
  if (!opts.refresh && (!seed || !seed.backdrop) && it.backdrop) $('.dx-art', box).innerHTML = `<img class="ph" src="${esc(tiny(it.backdrop))}" alt=""><img class="bd" src="${esc(it.backdrop)}" alt="">`;
  renderDetails(opts);
  applyExtras();
  if (!opts.refresh) heroPreview(seq);
}

/* --- render */
function renderDetails(opts = {}) {
  const box = $('#modal-box'), v = D.vm;
  if (!box || !v) return;
  $('.dx-copy', box).innerHTML = copyHTML(v, false);
  $('.dx-copy', box).classList.add('in');
  if (D.kind === 'item') drawWhy();
  const isSeries = v.type === 'Series';
  const people = (v.people || []).filter(p => p.type === 'Actor' || p.type === 'GuestStar');
  const offline = isOffline() ? `<span class="dx-saved" title="Jellyfin or the internet isn't reachable: this page comes from what Marquee saved on this PC.">${icon('cloud-off', 14)} Saved on this PC</span>` : '';
  $('.dx-body', box).innerHTML = `
    <div class="dx-cols dx-sec" style="--i:0">
      <div class="dx-left">
        ${v.tagline ? `<p class="dx-tagline">${esc(v.tagline)}</p>` : ''}
        <p class="dx-ov">${esc(v.overview || 'No description yet.')}</p>
        <div class="dx-ratings" id="dx-ratings">${ratingsHTML(baseRatings())}</div>
        ${v.kind === 'item' && v.type === 'Movie' ? '<div id="dx-scenes"></div>' : ''}
      </div>
      <aside class="dx-facts" id="dx-facts">${factsHTML()}${offline}</aside>
    </div>
    ${isSeries ? `<section class="dx-sec" style="--i:1" id="dx-episodes"><div class="dx-sec-head"><h3>Episodes</h3><div id="dx-season-pick"></div></div>
      <div id="dx-season-info"></div><div class="dx-eps" id="dx-eps"></div></section>` : ''}
    <section class="dx-sec" style="--i:2" id="dx-cast"${people.length ? '' : ' hidden'}>${castHTML(people.map(p => ({ name: p.name, role: p.role, photo: p.photo, id: p.id })))}</section>
    <section class="dx-sec" style="--i:3" id="dx-videos" hidden></section>
    <section class="dx-sec" style="--i:4" id="dx-collection" hidden></section>
    <section class="dx-sec" style="--i:5" id="dx-more"${v.similar && v.similar.length ? '' : ' hidden'}>${moreHTML()}</section>`;
  if (isSeries) {
    D.seasons = seasonList();
    drawSeasonPicker();
    const want = opts.season ?? D.season?.number ?? (v.nextUp && v.nextUp.season) ?? (D.seasons[0] && D.seasons[0].number);
    const s = D.seasons.find(x => x.number === want) || D.seasons[0];
    if (s) loadSeason(s.key, { keepScroll: !!opts.refresh });
    else $('#dx-eps').innerHTML = D.kind === 'tmdb' ? '' : '<p class="empty">No episodes yet.</p>';
  }
  if (v.kind === 'item' && v.type === 'Movie') sceneSearch();
  storageLines();
  if (D.extras) applyExtras();
  if (window.MarqueeFit && $('.dx-ov', box)) {   // "Is this for you?"
    const slot = document.createElement('div');
    $('.dx-ov', box).after(slot);
    window.MarqueeFit.render(slot, D.kind === 'tmdb' ? { type: D.key.split(':')[0], id: +D.key.split(':')[1] } : { id: D.id });
  }
}

function baseRatings() {
  const v = D.vm, r = { community: v.rating };
  if (D.kind === 'item' && D.item.critic != null) r.rt = { critics: D.item.critic };
  return r;
}

function factsHTML() {
  const v = D.vm, ex = D.extras || {};
  const cast = (ex.cast && ex.cast.length ? ex.cast : (v.people || []).filter(p => p.type === 'Actor')).slice(0, 4);
  const crew = ex.crew && ex.crew.length ? ex.crew : (v.people || []).filter(p => p.type === 'Director' || p.type === 'Creator').map(p => ({ name: p.name, job: p.type, id: p.id }));
  const dirs = crew.filter(c => c.job === 'Director'), makers = crew.filter(c => c.job === 'Creator');
  const link = p => p.id ? `<button class="link" data-act="person" data-person="${esc(p.id)}" data-name="${esc(p.name)}">${esc(p.name)}</button>` : esc(p.name);
  const m = v.media || {};
  const audio = [m.atmos && 'Dolby Atmos', m.dtsx && 'DTS:X', !m.atmos && !m.dtsx && m.channels ? (m.channels >= 8 ? '7.1' : m.channels >= 6 ? '5.1' : 'Stereo') : ''].filter(Boolean)[0];
  const video = [m.height >= 1600 ? '4K' : m.height >= 1000 ? '1080p' : m.height >= 700 ? '720p' : m.height ? 'SD' : '', m.dv ? 'Dolby Vision' : m.hdr10plus ? 'HDR10+' : m.hdr ? 'HDR' : '', m.codec ? m.codec.toUpperCase() : ''].filter(Boolean).join(' · ');
  const extraFacts = D.kind === 'tmdb' && v.type === 'Series' && v.showStatus ? `<div><b>Status</b> ${esc(v.showStatus)}${v.nextAir ? ` · next episode ${esc(fmtDate(v.nextAir))}` : ''}</div>` : '';
  return `${cast.length ? `<div><b>Starring</b> ${cast.map(link).join(', ')}</div>` : ''}
    ${dirs.length ? `<div><b>Directed by</b> ${dirs.map(link).join(', ')}</div>` : ''}
    ${makers.length ? `<div><b>Created by</b> ${makers.map(link).join(', ')}</div>` : ''}
    ${v.genres.length ? `<div><b>Genres</b> ${v.genres.map(esc).join(', ')}</div>` : ''}
    ${v.studios && v.studios.length ? `<div><b>${D.kind === 'tmdb' && v.type === 'Series' ? 'Network' : 'Studio'}</b> ${v.studios.slice(0, 3).map(esc).join(', ')}</div>` : ''}
    ${video ? `<div><b>Video</b> ${esc(video)}</div>` : ''}${audio ? `<div><b>Audio</b> ${esc(audio)}</div>` : ''}${extraFacts}`;
}

/* --- ratings: IMDb, Rotten Tomatoes critics + audience (own drawings, not their logos) */
const TOMATO = {
  fresh: '<svg viewBox="0 0 32 32" class="rt-ico" aria-hidden="true"><path d="M16 9.5c6.4 0 11 4.3 11 10s-4.9 9.5-11 9.5S5 25.2 5 19.5s4.6-10 11-10Z" fill="#f2453d"/><path d="M11.5 13.6c-.8.6-1.6 1.7-2 3" stroke="#fff" stroke-opacity=".55" stroke-width="2" fill="none" stroke-linecap="round"/><path d="M16 9.8c-1.6-2.4-4.2-3.4-6.6-2.8 2 .6 3.3 1.6 4 3.2M16 9.8c1.4-2.6 4-3.8 6.8-3.2-2.1.6-3.6 1.8-4.4 3.4M16 10c-.2-2 .4-4 1.6-5.6" stroke="#2f9a3e" stroke-width="2.2" fill="none" stroke-linecap="round"/></svg>',
  rotten: '<svg viewBox="0 0 32 32" class="rt-ico" aria-hidden="true"><path d="M16 5.5l2.6 5.2 5.6-2.6-1.9 5.8 5.9 1.6-5 3.3 3.5 5-6-.6-.6 6-4.1-4.4-4.1 4.4-.6-6-6 .6 3.5-5-5-3.3 5.9-1.6-1.9-5.8 5.6 2.6Z" fill="#6aa83a"/><circle cx="13" cy="15.5" r="1.6" fill="#4b7f25"/><circle cx="18.5" cy="19" r="2" fill="#4b7f25"/></svg>',
  certified: '<svg viewBox="0 0 32 32" class="rt-ico" aria-hidden="true"><circle cx="16" cy="17.5" r="12.5" fill="none" stroke="#ffb02e" stroke-width="2.4"/><path d="M16 11.5c4.6 0 8 3.1 8 7.1s-3.6 6.9-8 6.9-8-2.9-8-6.9 3.4-7.1 8-7.1Z" fill="#f2453d"/><path d="M16 11.8c-1.2-1.8-3-2.5-4.8-2.1M16 11.8c1-1.9 2.9-2.8 5-2.4" stroke="#2f9a3e" stroke-width="1.9" fill="none" stroke-linecap="round"/><path d="M10 27.5 8 31l3.4-1 1.4 2.4 1.2-4M22 27.5l2 3.5-3.4-1-1.4 2.4-1.2-4" fill="#ffb02e"/></svg>',
};
const POPCORN = {
  up: '<svg viewBox="0 0 32 32" class="rt-ico" aria-hidden="true"><circle cx="10.5" cy="9.5" r="3.6" fill="#ffe6a6"/><circle cx="16" cy="7.5" r="4" fill="#fff1c9"/><circle cx="21.5" cy="9.5" r="3.6" fill="#ffe6a6"/><path d="M7 12h18l-2.4 16.5H9.4Z" fill="#f2453d"/><path d="M12 12l.8 16.5M16 12v16.5M20 12l-.8 16.5" stroke="#fff" stroke-width="1.8"/></svg>',
  spilled: '<svg viewBox="0 0 32 32" class="rt-ico" aria-hidden="true"><g transform="rotate(-62 16 20)"><path d="M8 13h16l-2.2 15H10.2Z" fill="#6aa83a"/><path d="M12.6 13l.7 15M16 13v15M19.4 13l-.7 15" stroke="#fff" stroke-width="1.6"/></g><circle cx="23" cy="26" r="2.6" fill="#ffe6a6"/><circle cx="27.5" cy="24" r="2.2" fill="#fff1c9"/></svg>',
};
const STAR_ICO = '<svg viewBox="0 0 32 32" class="rt-ico" aria-hidden="true"><path d="M16 4.5l3.4 7.3 8 .9-5.9 5.4 1.6 7.9L16 22l-7.1 4 1.6-7.9-5.9-5.4 8-.9Z" fill="#f5c518"/></svg>';
function ratingsHTML(r) {
  r = r || {};
  const out = [];
  const imdbUrl = (r.imdb && r.imdb.url) || ((D.extras && D.extras.imdb) || (D.item && D.item.imdb) ? `https://www.imdb.com/title/${(D.extras && D.extras.imdb) || D.item.imdb}/` : '');
  if (r.imdb && r.imdb.score != null) out.push(rChip(STAR_ICO, (+r.imdb.score).toFixed(1).replace(/\.0$/, '') + '<small>/10</small>', 'IMDb', imdbUrl, r.imdb.votes ? `${Number(r.imdb.votes).toLocaleString()} votes on IMDb` : 'IMDb rating'));
  const rt = r.rt;
  if (rt && rt.critics != null) {
    const cert = /certified/i.test(rt.critics_rating || ''), rotten = /rotten/i.test(rt.critics_rating || '') || (!rt.critics_rating && rt.critics < 60);
    out.push(rChip(cert ? TOMATO.certified : rotten ? TOMATO.rotten : TOMATO.fresh, rt.critics + '%', 'Critics', rt.url, `Tomatometer: ${rt.critics_rating || (rotten ? 'Rotten' : 'Fresh')} (Rotten Tomatoes critics)`));
  }
  if (rt && rt.audience != null) {
    const spilled = /spilled/i.test(rt.audience_rating || '') || (!rt.audience_rating && rt.audience < 60);
    out.push(rChip(spilled ? POPCORN.spilled : POPCORN.up, rt.audience + '%', 'Audience', rt.url, `Audience score: ${spilled ? 'Spilled' : 'Upright'} (Rotten Tomatoes)`));
  }
  if (!r.imdb && r.community) out.push(rChip(STAR_ICO, (+r.community).toFixed(1) + '<small>/10</small>', 'Rating', imdbUrl, 'Community rating'));
  return out.join('');
}
function rChip(ico, val, label, url, title) {
  const inner = `${ico}<span class="rv"><b>${val}</b><span>${esc(label)}</span></span>`;
  return url ? `<a class="r-chip" href="${esc(url)}" target="_blank" rel="noopener noreferrer" title="${esc(title)} · opens in your browser">${inner}</a>`
    : `<span class="r-chip" title="${esc(title)}">${inner}</span>`;
}

/* --- cast */
function castHTML(cast) {
  if (!cast.length) return '';
  return `<div class="dx-sec-head"><h3>Cast</h3></div><div class="dx-cast-row">${cast.slice(0, 18).map((p, k) => `
    <button class="dx-person" style="--i:${k}" data-act="person" data-person="${esc(p.id || '')}" data-name="${esc(p.name)}" ${p.id ? '' : 'disabled'}>
      <span class="face">${p.photo ? `<img src="${esc(p.photo)}" alt="" loading="lazy">` : ''}<span class="ini">${esc((p.name || '?')[0])}</span></span>
      <b>${esc(p.name)}</b><span>${esc(p.role || '')}</span></button>`).join('')}</div>`;
}

/* --- more like this: library first, then titles to request */
function moreHTML() {
  const sim = (D.vm.similar || []).slice(0, 12);
  const rec = ((D.extras && D.extras.recommended) || []).slice(0, Math.max(6, 12 - sim.length));
  if (!sim.length && !rec.length) return '';
  return `<div class="dx-sec-head"><h3>More like this</h3></div><div class="similar-grid">${sim.map((s, k) => posterCard(s, k)).join('')}${rec.map((c, k) => tmdbCard(c, k + sim.length)).join('')}</div>`;
}

/* --- extras that arrive online (or from the cache) */
async function loadExtras(seq) {
  let ex;
  try { ex = await api(`/api/item/${encodeURIComponent(D.id)}/extras`); } catch (e) { return; }
  if (D.seq !== seq || D.kind !== 'item') return;
  D.extras = ex;
  if (ex.age && !D.vm.age) D.vm.age = ex.age;
  if (!D.vm.tagline && ex.tagline) D.vm.tagline = ex.tagline;
  applyExtras();
}
function applyExtras() {
  const ex = D.extras, box = $('#modal-box');
  if (!ex || !box) return;
  const rEl = $('#dx-ratings', box);
  if (rEl) { const html = ratingsHTML({ ...baseRatings(), ...(ex.ratings || {}) }); rEl.innerHTML = html; rEl.classList.add('in'); rEl.hidden = !html; }
  const facts = $('#dx-facts', box);
  if (facts) { const saved = $('.dx-saved', facts); facts.innerHTML = factsHTML(); if (saved) facts.append(saved); }
  const castEl = $('#dx-cast', box);
  const libCast = (D.vm.people || []).filter(p => p.type === 'Actor' || p.type === 'GuestStar');
  if (castEl && !libCast.length && ex.cast && ex.cast.length) { castEl.innerHTML = castHTML(ex.cast); castEl.hidden = false; }
  const vids = ex.videos || [];
  const tr = $('.dx-trailer', box);
  if (tr) tr.hidden = !vids.length;
  const vEl = $('#dx-videos', box);
  if (vEl && vids.length) {
    vEl.hidden = false;
    vEl.innerHTML = `<div class="dx-sec-head"><h3>Trailers &amp; extras</h3></div><div class="dx-videos">${vids.map((x, k) => `
      <button class="dx-video" style="--i:${k}" data-act="video" data-key="${esc(x.key)}"><span class="vt"><img src="https://i.ytimg.com/vi/${esc(x.key)}/mqdefault.jpg" alt="" loading="lazy"><span class="pg">${icon('play', 18)}</span></span>
        <b>${esc(x.name)}</b><span>${esc(x.type || 'Video')}</span></button>`).join('')}</div>`;
  }
  const cEl = $('#dx-collection', box);
  if (cEl && ex.collection && ex.collection.items && ex.collection.items.length) {
    cEl.hidden = false;
    cEl.innerHTML = `<div class="dx-sec-head"><h3>${esc(ex.collection.name || 'Collection')}</h3><span class="count">${ex.collection.items.length + 1} films</span></div>
      <div class="similar-grid">${ex.collection.items.map((c, k) => c.owned ? posterCard(c, k) : tmdbCard(c, k)).join('')}</div>`;
  }
  const mEl = $('#dx-more', box);
  if (mEl) { const html = moreHTML(); mEl.innerHTML = html; mEl.hidden = !html; }
  if (D.vm.type === 'Series') {
    const before = D.seasons.map(s => s.key).join();
    D.seasons = seasonList();
    if (D.seasons.map(s => s.key).join() !== before || !$('#dx-season-pick').children.length) {
      drawSeasonPicker();
      if (!D.season && D.seasons[0]) loadSeason(D.seasons[0].key);
    }
    if (D.season) { drawSeasonInfo(D.seasons.find(s => s.key === D.season.key) || D.season); if (D.kind === 'item') mergeTmdbEpisodes(); }
  }
}

/* --- seasons */
function seasonList() {
  const tm = (D.extras && D.extras.tmdb_seasons) || [];
  const out = [];
  if (D.kind === 'item') {
    for (const s of D.item.seasons || []) if (s.episodes > 0) out.push({ ...s, key: s.id, tm: tm.find(t => t.number === s.number) });
  }
  const have = new Set(out.map(s => s.number));
  for (const t of tm) {
    if (have.has(t.number) || !(t.episodes > 0)) continue;
    out.push({ key: 'tmdb-' + t.number, number: t.number, name: t.name, overview: t.overview, episodes: t.episodes, watched: 0, missing: D.kind === 'item', tmdbOnly: true, air: t.air, tm: t });
  }
  out.sort((a, b) => (a.number === 0) - (b.number === 0) || (a.number ?? 999) - (b.number ?? 999));
  return out;
}
const seasonLabel = s => s.number === 0 ? 'Specials' : s.number != null ? `Season ${s.number}` : (s.name || 'Season');
function drawSeasonPicker() {
  const el = $('#dx-season-pick');
  if (!el) return;
  const list = D.seasons, cur = D.season && D.season.key;
  if (list.length <= 1) { el.innerHTML = list.length ? `<span class="dx-one-season">${esc(seasonLabel(list[0]))}</span>` : ''; return; }
  if (list.length <= 7) {
    el.innerHTML = `<div class="dx-pills" role="tablist">${list.map(s => `<button class="season-chip${s.missing ? ' missing' : ''}" role="tab" data-act="season" data-season="${esc(s.key)}" aria-pressed="${s.key === cur}">${esc(seasonLabel(s))}</button>`).join('')}</div>`;
  } else {
    const c = list.find(s => s.key === cur) || list[0];
    el.innerHTML = `<div class="dx-dd"><button class="dx-dd-btn" data-act="dd" aria-haspopup="listbox" aria-expanded="false">${esc(seasonLabel(c))} ${icon('right', 16)}</button>
      <div class="dx-dd-menu glass" role="listbox" hidden>${list.map(s => `<button role="option" data-act="season" data-season="${esc(s.key)}" aria-selected="${s.key === cur}" class="${s.missing ? 'missing' : ''}">${esc(seasonLabel(s))}<span>${s.missing ? 'Not in library' : `${s.episodes} ep`}</span></button>`).join('')}</div></div>`;
  }
}
function drawSeasonInfo(s) {
  const el = $('#dx-season-info');
  if (!el || !s) return;
  const ov = s.overview || (s.tm && s.tm.overview) || '';
  const year = s.year || (s.air || (s.tm && s.tm.air) || '').slice(0, 4);
  const bits = [`${s.episodes} episode${s.episodes === 1 ? '' : 's'}`, D.kind === 'item' && !s.missing ? (s.watched ? `${s.watched} watched` : 'none watched yet') : '', year].filter(Boolean);
  const reqState = ((D.extras && D.extras.seasons_status) || {})[String(s.number)];
  const showTmdb = D.kind === 'item' ? D.item.tmdb : D.item.tmdb;
  const req = (s.missing || D.kind === 'tmdb') && showTmdb ? (reqState === 2 || reqState === 3 ? `<span class="chip glass-chip">Requested</span>` : reqState === 5 ? '' : `<button class="btn small ghost" data-act="tmdb-request" data-tmdb="tv:${esc(showTmdb)}">${icon('plus', 15)} Request</button>`) : '';
  el.innerHTML = `<div class="season-head"><div><span class="season-bits">${bits.map(esc).join(' · ')}</span>${ov ? `<p>${esc(ov)}</p>` : ''}</div>${req}</div>`;
}
async function loadSeason(key, opts = {}) {
  const s = D.seasons.find(x => x.key === key);
  if (!s) return;
  D.season = s;
  $$('#dx-season-pick [data-season]').forEach(b => { b.setAttribute(b.getAttribute('role') === 'option' ? 'aria-selected' : 'aria-pressed', b.dataset.season === key); });
  const dd = $('#dx-season-pick .dx-dd-btn');
  if (dd) { dd.innerHTML = `${esc(seasonLabel(s))} ${icon('right', 16)}`; closeDropdown(); }
  drawSeasonInfo(s);
  const box = $('#dx-eps');
  if (!box) return;
  box.dataset.season = key;
  if (!opts.keepScroll) box.innerHTML = `<div class="ep-skel">${'<div class="skel"></div>'.repeat(3)}</div>`;
  let eps = [];
  if (D.kind === 'item' && !s.tmdbOnly) {
    try { eps = (await api(`/api/episodes/${encodeURIComponent(D.id)}/${encodeURIComponent(s.id)}`)).items; }
    catch (e) { if (box.dataset.season === key) box.innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
    if (box.dataset.season !== key) return;
    eps.forEach(reg);
    D.eps = eps.map(e => ({ ...e, own: true }));
    drawEpisodes();
    mergeTmdbEpisodes();
  } else {
    D.eps = [];
    drawEpisodes(true);
    mergeTmdbEpisodes();
  }
}
async function mergeTmdbEpisodes() {
  const s = D.season, box = $('#dx-eps');
  if (!s || !box) return;
  const showTmdb = D.item && D.item.tmdb;
  if (!showTmdb) { if (!D.eps.length) box.innerHTML = '<p class="empty">No episodes in this season yet.</p>'; return; }
  let data;
  try {
    data = D.kind === 'item' ? await api(`/api/episodes/${encodeURIComponent(D.id)}/${encodeURIComponent(s.tmdbOnly ? 'tmdb-' + s.number : s.id)}/tmdb`)
      : await api(`/api/tmdb/tv/${showTmdb}/season/${s.number}`);
  } catch (e) { data = null; }
  if (box.dataset.season !== s.key || !D.season || D.season.key !== s.key) return;
  const tm = (data && data.episodes) || [];
  D.removedEps = (data && data.removed) || [];
  if (!tm.length) { if (!D.eps.length) box.innerHTML = `<p class="empty">${isOffline() ? 'This season’s episode list isn’t saved on this PC yet.' : 'No episodes in this season yet.'}</p>`; return; }
  const byNo = new Map(D.eps.filter(e => e.own).map(e => [e.episode, e]));
  const merged = D.eps.filter(e => e.own).map(e => {
    const t = tm.find(x => x.episode === e.episode);
    return t ? { ...e, overview: e.overview || t.overview, thumb: e.thumb || t.still, tmAir: t.air } : e;
  });
  for (const t of tm) if (!byNo.has(t.episode)) merged.push({ id: `tmdb-${s.number}-${t.episode}`, own: false, missing: true, removed: removedEp(s.number, t.episode), name: t.name, episode: t.episode, season: t.season, overview: t.overview, thumb: t.still, premiere: t.air, aired: !!t.air && t.air <= (data.today || new Date().toISOString().slice(0, 10)) });
  merged.sort((a, b) => (a.episode ?? 999) - (b.episode ?? 999));
  const changed = merged.length !== D.eps.length || merged.some((e, k) => e.overview !== (D.eps[k] || {}).overview || e.thumb !== (D.eps[k] || {}).thumb);
  D.eps = merged;
  if (changed) drawEpisodes(false, true);
}
function drawEpisodes(waiting, quiet) {
  const box = $('#dx-eps');
  if (!box) return;
  if (!D.eps.length) { box.innerHTML = waiting ? `<div class="ep-skel">${'<div class="skel"></div>'.repeat(3)}</div>` : '<p class="empty">No episodes in this season yet.</p>'; return; }
  const nextId = D.vm.nextUp && D.vm.nextUp.id;
  const reqState = ((D.extras && D.extras.seasons_status) || {})[String(D.season.number)];
  const showTmdb = D.item && D.item.tmdb;
  box.innerHTML = D.eps.map((e, k) => {
    const next = e.id === nextId;
    const date = fmtDate(e.premiere || e.tmAir);
    let right;
    if (e.missing && e.removed) {
      right = `<span class="chip glass-chip" title="Deleted to save space${e.removed.removed_at ? ' on ' + esc(fmtDate(e.removed.removed_at)) : ''}">${e.removed.played ? 'Watched · removed' : 'Removed'}</span>`;
    } else if (e.missing) {
      right = !e.aired ? `<span class="chip glass-chip ep-air">${e.premiere ? 'Airs ' + esc(fmtDate(e.premiere, { month: 'short', day: 'numeric' })) : 'Not aired yet'}</span>`
        : reqState === 2 || reqState === 3 ? `<span class="chip glass-chip">Requested</span>`
        : showTmdb && D.kind === 'item' ? `<button class="btn small ghost" data-act="tmdb-request" data-tmdb="tv:${esc(showTmdb)}">${icon('plus', 15)} Request</button>` : '';
    } else right = (e.available && window.MarqueeStorage ? `<span class="more-wrap"><button class="ep-share" data-act="more-menu" aria-haspopup="menu" aria-expanded="false" title="More" aria-label="More">${icon('dots', 16)}</button>
        <span class="more-menu glass" role="menu" hidden><button role="menuitem" data-act="delete-local" data-id="${esc(e.id)}" data-name="${esc(epCode(e) + ' · ' + (e.name || ''))}" data-type="Episode">${icon('close', 15)} Delete from this PC…</button></span></span>` : '') + (S.shareOn && e.available ? `<button class="ep-share" data-act="share" data-id="${esc(e.id)}" data-name="${esc((D.vm.title || '') + ' ' + (epCode(e) || ''))}" title="Share as a Google Drive link" aria-label="Share">${icon('share', 16)}</button>` : '') + `<button class="watch-toggle${e.played ? ' on' : ''}" data-act="watched" data-id="${esc(e.id)}" title="${e.played ? 'Mark unwatched' : 'Mark watched'}" aria-label="${e.played ? 'Mark unwatched' : 'Mark watched'}">${icon('check')}</button>`;
    const meta = [e.runtime ? fmtRuntime(e.runtime) : '', date, e.quality || ''].filter(Boolean);
    return `<div class="epi${e.missing ? ' missing' : ''}${next ? ' is-next' : ''}${e.played ? ' played' : ''}" style="--i:${Math.min(k, 14)}" ${e.missing ? '' : `data-act="play-ep" data-id="${esc(e.id)}" role="button" tabindex="0" aria-label="Play ${esc(epCode(e) || '')} ${esc(e.name || '')}"`}>
      <div class="thumb">${e.thumb ? `<img src="${esc(e.thumb)}" alt="" loading="lazy">` : ''}<span class="ep-no">${e.episode ?? ''}</span>
        ${e.missing ? '' : `<video muted playsinline preload="none" disablepictureinpicture></video><span class="play-glyph">${icon('play')}</span>`}
        ${!e.missing && !e.played && e.progress > 0.01 ? `<div class="progress"><i style="--p:${e.progress.toFixed(3)}"></i></div>` : ''}
        ${e.played || (e.removed && e.removed.played) ? `<span class="tick ep-tick" title="Watched">${icon('check')}</span>` : ''}${next ? '<span class="up-next-tag">Up next</span>' : ''}</div>
      <div class="ep-info"><div class="t"><b>${e.episode != null ? `E${e.episode} · ` : ''}${esc(e.name || 'Episode')}</b></div>
        <div class="ep-meta">${meta.map((x, n) => n === meta.length - 1 && x === e.quality ? `<span class="q-badge small">${esc(x)}</span>` : `<span>${esc(x)}</span>`).join('')}</div>
        ${e.overview ? `<p class="ep-ov">${esc(e.overview)}</p><button class="ep-more" data-act="more" hidden>More</button>` : ''}</div>
      <div class="ep-right">${right}</div></div>`;
  }).join('');
  if (quiet) box.querySelectorAll('.epi').forEach(x => x.style.animation = 'none');
  requestAnimationFrame(() => {
    for (const p of $$('.ep-ov', box)) if (p.scrollHeight > p.clientHeight + 2) p.nextElementSibling.hidden = false;
    const target = (D.focusEp && $(`.epi[data-id="${CSS.escape(D.focusEp)}"]`, box)) || (!D.scrolledNext && $('.epi.is-next', box));
    if (target && !quiet) {
      D.scrolledNext = true;
      D.focusEp = null;
      setTimeout(() => {
        if (!document.contains(target)) return;
        const lay = layers.modal, r = target.getBoundingClientRect();
        if (r.top > innerHeight - 80 || r.bottom < 80) lay.scrollBy({ top: r.top - innerHeight * 0.45, behavior: reduced.matches ? 'auto' : 'smooth' });
      }, 450);
    }
  });
}
function closeDropdown() {
  const m = $('.dx-dd-menu');
  if (m && !m.hidden) { m.hidden = true; $('.dx-dd-btn')?.setAttribute('aria-expanded', 'false'); }
}

/* --- storage: smart season line, removed titles, delete by hand */
async function storageLines() {
  const v = D.vm, seq = D.seq;
  const tmdb = D.kind === 'tmdb' ? D.item.tmdb : D.item && D.item.tmdb;
  if (!tmdb || !window.MarqueeStorage) return;
  const sl = $('#smart-line');
  if (sl && v.type === 'Series') MarqueeStorage.smartLine(sl, tmdb);
  const kind = v.type === 'Series' ? 'tv' : 'movie';
  if (D.kind !== 'tmdb') return;
  let r;
  try { r = (await api(`/api/storage/removed/${kind}/${tmdb}`)).removed; } catch (e) { return; }
  const el = $('#removed-line');
  if (!r || D.seq !== seq || !el) return;
  const watched = r.played || r.any_played;
  const at = t => fmtDate(typeof t === 'number' ? new Date(t * 1000).toISOString() : t);
  el.innerHTML = `<div class="dx-removed">${watched ? `<span class="tick">${icon('check')}</span>` : ''}<span>${watched && r.last_played ? `Watched on ${esc(at(r.last_played))} · ` : ''}removed ${esc(at(r.removed_at))} to save space</span>
    <button class="btn small on-dark" data-act="removed-forget" data-tmdb="${esc(tmdb)}" title="Drop this from Marquee's memory">Forget</button></div>`;
  const kick = $('.dx-kicker'), req = $('.dx-play[data-act="tmdb-request"]');
  if (kick) kick.textContent = watched ? 'Watched · removed' : 'Removed to save space';
  if (req) req.innerHTML = `${icon('plus')} Request again`;
}
function removedEp(season, episode) {
  return (D.removedEps || []).find(x => +x.season === +season && +x.episode === +episode) || null;
}
async function forgetRemoved(btn) {
  if (!btn.dataset.sure) { btn.dataset.sure = '1'; btn.textContent = 'Sure? Forget it'; setTimeout(() => { if (document.contains(btn)) { delete btn.dataset.sure; btn.textContent = 'Forget'; } }, 3500); return; }
  try { await api('/api/storage/removed/forget', { tmdb: btn.dataset.tmdb }); toast('Forgotten', { icon: 'check' }); $('#removed-line').innerHTML = ''; }
  catch (e) { toast(e.message); }
}
function toggleMoreMenu(btn) {
  const m = btn.nextElementSibling, open = m.hidden;
  closeMoreMenu();
  if (!open) return;
  m.hidden = false;
  btn.setAttribute('aria-expanded', 'true');
  m.querySelector('button')?.focus({ preventScroll: true });
}
function closeMoreMenu() {
  for (const m of $$('.more-menu:not([hidden])')) { m.hidden = true; m.previousElementSibling?.setAttribute('aria-expanded', 'false'); }
}
function deleteLocal(id, name, type) {
  if (!window.MarqueeStorage) return;
  stopHero();
  MarqueeStorage.deleteSheet({ id, name, type }).then(entry => {
    if (!entry) return;
    S.home = null; S.lib = {};
    if (!layers.modal.hidden && D.kind === 'item') openDetails(D.id, { refresh: true, season: D.season && D.season.number });
  });
}

/* --- hero preview: the title itself (muted), or its trailer when it isn't in the library */
function stopHero(instant) {
  const m = D.media;
  D.media = null;
  const hero = $('.dx-hero');
  if (hero) hero.classList.remove('playing');
  if (!m) return;
  clearTimeout(m.timer);
  if (m.unfit) m.unfit();
  const el = m.el;
  const kill = () => { if (el.tagName === 'VIDEO') unloadVideo(el); el.remove(); };
  if (instant || reduced.matches) kill(); else setTimeout(kill, 600);
}
async function heroPreview(seq) {
  if (S.settings.details_autoplay === false) return;
  let src = null;
  if (D.kind === 'item') {
    try { const r = await api(`/api/preview/${encodeURIComponent(D.id)}`); if (r.available && r.url) src = { kind: 'video', url: r.url, start: r.start, seek: r.seek, subs: r.subs }; } catch (e) { /* artwork only */ }
  } else {
    const v = ((D.extras && D.extras.videos) || []).find(x => x.type === 'Trailer' || x.type === 'Teaser');
    if (v && !isOffline()) src = { kind: 'yt', key: v.key };
  }
  if (!src) return;
  const wait = 1000 - (performance.now() - D.openedAt);
  if (wait > 0) await sleep(wait);
  if (D.seq !== seq || layers.modal.hidden || layers.modal.classList.contains('closing')) return;
  if (reduced.matches) { $('.dx-vctl').hidden = false; D.pendingSrc = src; setVctl(true); return; }
  startHeroMedia(src);
}
function setVctl(idle) {
  const c = $('.dx-vctl');
  if (!c) return;
  c.hidden = false;
  const mute = $('[data-act="dx-mute"]', c), rep = $('[data-act="dx-replay"]', c);
  mute.innerHTML = icon(D.muted ? 'muted' : 'sound', 18);
  mute.title = D.muted ? 'Unmute' : 'Mute';
  mute.setAttribute('aria-label', mute.title);
  mute.hidden = !!idle;
  rep.innerHTML = icon(idle ? 'play' : 'restart', 18);
  rep.title = idle ? 'Play preview' : 'Replay';
}
function startHeroMedia(src) {
  stopHero(true);
  const host = $('.dx-media'), hero = $('.dx-hero');
  if (!host) return;
  const m = D.media = { src, el: null, timer: 0 };
  if (src.kind === 'video') {
    const v = document.createElement('video');
    v.muted = D.muted; v.playsInline = true; v.preload = 'auto'; v.disablePictureInPicture = true;
    v.setAttribute('muted', '');
    v.addEventListener('playing', () => { if (D.media === m) { hero.classList.add('playing'); setVctl(false); } });
    v.addEventListener('ended', () => { if (D.media === m) endHero(); });
    v.addEventListener('error', () => { if (D.media === m) stopHero(); }, { once: true });
    if (src.seek) v.addEventListener('loadedmetadata', () => { try { v.currentTime = src.start || 0; } catch (e) { /* not seekable */ } }, { once: true });
    addPreviewSubs(v, src.subs);
    v.src = src.url;
    m.el = v;
    host.append(v);
    v.play().catch(() => {});
    if (src.seek) m.timer = setTimeout(() => { if (D.media === m) endHero(); }, 32000);   // a whole file: stop after ~30 s
  } else {
    const f = document.createElement('iframe');
    const origin = encodeURIComponent(location.origin);
    f.src = `https://www.youtube-nocookie.com/embed/${encodeURIComponent(src.key)}?autoplay=1&mute=${D.muted ? 1 : 0}&controls=0&rel=0&playsinline=1&iv_load_policy=3&modestbranding=1&disablekb=1&fs=0&enablejsapi=1&cc_load_policy=1&cc_lang_pref=en&hl=en&vq=hd1080&origin=${origin}`;
    f.allow = 'autoplay; encrypted-media; picture-in-picture';
    f.referrerPolicy = 'strict-origin-when-cross-origin';
    f.title = 'Trailer';
    f.tabIndex = -1;
    f.addEventListener('load', () => { try { f.contentWindow.postMessage(JSON.stringify({ event: 'listening', id: 1 }), '*'); } catch (e) { /* gone */ } });
    m.el = f;
    const fit = () => host.style.setProperty('--yts', (Math.max(host.clientWidth / 1920, host.clientHeight / 1080) * 1.36).toFixed(4));
    fit();
    addEventListener('resize', fit);
    m.unfit = () => removeEventListener('resize', fit);
    host.append(f);
    m.timer = setTimeout(() => { if (D.media === m && m.ytState == null) { hero.classList.add('playing'); setVctl(false); } }, 2600);
  }
}
function endHero() {
  const m = D.media;
  if (!m) return;
  $('.dx-hero')?.classList.remove('playing');
  D.pendingSrc = m.src;
  setTimeout(() => { if (D.media === m) stopHero(true); }, 700);
  setVctl(true);
}
function ytCommand(func, args = []) {
  const f = D.media && D.media.el;
  if (f && f.tagName === 'IFRAME') try { f.contentWindow.postMessage(JSON.stringify({ event: 'command', func, args }), '*'); } catch (e) { /* gone */ }
}
addEventListener('message', e => {
  if (!/youtube(-nocookie)?\.com$/.test(new URL(e.origin || 'http://x').hostname)) return;
  let d; try { d = typeof e.data === 'string' ? JSON.parse(e.data) : e.data; } catch (err) { return; }
  const st = d && d.info && d.info.playerState;
  const m = D.media;
  if (!m || m.src.kind !== 'yt' || st == null) return;
  m.ytState = st;
  // YouTube flashes its own pause/play button and control strip when playback starts or pauses: the trailer only
  // shows once it has been playing for a moment, and a paused trailer gives way to the backdrop (our play button).
  if (st === 1) {
    clearTimeout(m.timer);
    m.timer = setTimeout(() => { if (D.media === m && m.ytState === 1) { $('.dx-hero')?.classList.add('playing'); setVctl(false); } },
      m.ytShown ? 350 : 1300);
    m.ytShown = true;
  }
  if (st === 2) { clearTimeout(m.timer); $('.dx-hero')?.classList.remove('playing'); setVctl(true); }
  if (st === 0) endHero();
});
function toggleMute() {
  D.muted = !D.muted;
  const m = D.media;
  if (m && m.el.tagName === 'VIDEO') m.el.muted = D.muted;
  else ytCommand(D.muted ? 'mute' : 'unMute');
  setVctl(!m);
}
function replayHero() {
  const m = D.media;
  if (m && m.el.tagName === 'VIDEO') { try { m.el.currentTime = m.src.seek ? (m.src.start || 0) : 0; m.el.play(); } catch (e) { /* ignore */ } return; }
  if (m && m.ytState === 2) { ytCommand('playVideo'); return; }   // paused trailer: carry on
  if (m) { ytCommand('seekTo', [0, true]); ytCommand('playVideo'); return; }
  if (D.pendingSrc) startHeroMedia(D.pendingSrc);
}
function playTrailer(key) {
  if (isOffline()) return toast('Trailers need an internet connection');
  const vids = (D.extras && D.extras.videos) || [];
  key = key || (vids.find(x => x.type === 'Trailer') || vids[0] || {}).key;
  if (!key) return;
  layers.modal.scrollTo({ top: 0, behavior: reduced.matches ? 'auto' : 'smooth' });
  startHeroMedia({ kind: 'yt', key });
}
/* Pause the hero when it scrolls out of sight or the window hides; carry on when it's back. */
layers.modal.addEventListener('scroll', () => {
  const m = D.media;
  if (!m || m.el.tagName !== 'VIDEO') return;
  const away = layers.modal.scrollTop > ($('.dx-hero')?.offsetHeight || 500) * 0.85;
  if (away && !m.el.paused) { m.el.pause(); m.scrollPaused = true; } else if (!away && m.scrollPaused) { m.scrollPaused = false; m.el.play().catch(() => {}); }
}, { passive: true });
document.addEventListener('visibilitychange', () => {
  const m = D.media;
  if (!m) return;
  if (m.el.tagName === 'VIDEO') { if (document.hidden) m.el.pause(); else if (!m.scrollPaused && !layers.modal.hidden) m.el.play().catch(() => {}); }
  else ytCommand(document.hidden ? 'pauseVideo' : 'playVideo');
});

/* --- shared-element open: the poster flies up into the hero while the page fades in */
function flyIn(fromEl, seed) {
  const modal = $('#modal-box');
  if (!fromEl || reduced.matches || !document.contains(fromEl)) return;
  const media = fromEl.closest('.card') ? $('.media', fromEl.closest('.card')) : fromEl;
  const img = media && $('img.loaded', media);
  if (!media) return;
  const C = media.getBoundingClientRect();
  const hero = $('.dx-hero', modal);
  const T = { left: modal.offsetLeft, top: modal.offsetTop - layers.modal.scrollTop, width: modal.offsetWidth, height: hero ? hero.offsetHeight : modal.offsetWidth * 0.46 };
  if (!C.width || !T.width) return;
  layers.modal.classList.add('zoom');
  const fly = document.createElement('div');
  fly.className = 'fly';
  const s = C.height / T.height, w = C.width / s, inset = Math.max(0, (T.width - w) / 2);
  const bd = seed && seed.backdrop;
  fly.style.cssText = `left:${T.left}px;top:${T.top}px;width:${T.width}px;height:${T.height}px`;
  fly.innerHTML = `${bd ? `<img class="f-bd" src="${esc(bd)}" alt="">` : `<div class="f-bd" style="background:${grad(seed ? seed.title || seed.name : '')}"></div>`}
    ${img ? `<img class="f-poster" src="${esc(img.currentSrc || img.src)}" alt="" style="width:${w}px;left:${inset}px">` : ''}`;
  document.body.append(fly);
  const dx = (C.left + C.width / 2) - (T.left + T.width / 2), dy = (C.top + C.height / 2) - (T.top + T.height / 2);
  const r = 18 / s;
  const dur = 560, ease = 'cubic-bezier(.2,.8,.2,1)';
  fly.animate([{ transform: `translate(${dx}px, ${dy}px) scale(${s})`, clipPath: `inset(0 ${inset}px round ${r}px)` },
    { transform: 'none', clipPath: 'inset(0 0px round 28px)' }], { duration: dur, easing: ease, fill: 'forwards' });
  const p = $('.f-poster', fly);
  if (p) p.animate([{ opacity: 1 }, { opacity: 1, offset: 0.35 }, { opacity: 0 }], { duration: dur, easing: 'ease', fill: 'forwards' });
  modal.animate([{ opacity: 0 }, { opacity: 0, offset: 0.55 }, { opacity: 1 }], { duration: dur + 80, easing: 'ease' });
  setTimeout(() => { fly.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 180, fill: 'forwards' }).onfinish = () => fly.remove(); layers.modal.classList.remove('zoom'); }, dur);
}

/* --- people */
async function openPerson(id, name) {
  if (!id) return;
  const box = $('#person-box');
  box.innerHTML = `<div class="ps-top"><span class="ps-photo skel"></span><div style="flex:1"><div class="skel dx-skel-title" style="width:60%"></div><div class="skel dx-skel-line"></div></div>
    <button class="icon-btn" data-action="close-person" aria-label="Close">${icon('close', 20)}</button></div><div class="ps-body">${skeletonRows(1)}</div>`;
  $('.ps-top .skel.dx-skel-title', box).insertAdjacentHTML('beforebegin', `<h2>${esc(name || '')}</h2>`);
  $('.ps-top .skel.dx-skel-title', box).remove();
  openLayer(layers.person);
  layers.person.scrollTop = 0;
  layers.person.dataset.person = id;
  let p;
  try { p = (await api(`/api/person/${encodeURIComponent(id)}?name=${encodeURIComponent(name || '')}`)).person; }
  catch (e) { if (layers.person.dataset.person === id) $('.ps-body', box).innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
  if (layers.person.dataset.person !== id) return;
  const facts = [p.dept, p.born && `Born ${fmtDate(p.born)}`, p.place].filter(Boolean);
  const known = (p.known || []).slice(0, 18);
  box.innerHTML = `<div class="ps-top">
      <span class="ps-photo">${p.photo ? `<img src="${esc(p.photo)}" alt="">` : `<span class="ini">${esc((p.name || '?')[0])}</span>`}</span>
      <div class="ps-head"><span class="eyebrow">Cast &amp; crew</span><h2>${esc(p.name)}</h2>${facts.length ? `<div class="ps-facts">${facts.map(esc).join(' · ')}</div>` : ''}</div>
      <button class="icon-btn" data-action="close-person" aria-label="Close">${icon('close', 20)}</button></div>
    <div class="ps-body">
      ${p.bio ? `<p class="ps-bio">${esc(p.bio)}</p><button class="ep-more ps-more" data-act="more" hidden>More</button>` : (isOffline() ? '<p class="ps-bio muted">Their biography isn’t saved on this PC yet.</p>' : '')}
      ${p.library && p.library.length ? `<h3>In your library</h3><div class="similar-grid">${p.library.map((c, k) => posterCard(c, k)).join('')}</div>` : ''}
      ${known.length ? `<h3>Known for</h3><div class="similar-grid">${known.map((c, k) => c.owned ? posterCard(c, k) : tmdbCard(c, k)).join('')}</div>` : ''}
    </div>`;
  requestAnimationFrame(() => { const b = $('.ps-bio', box); if (b && b.scrollHeight > b.clientHeight + 2) $('.ps-more', box).hidden = false; });
}

/* --- scene search (the optional media index) */
async function sceneSearch() {
  const el = $('#dx-scenes');
  if (!el) return;
  if (S.index === undefined) { try { S.index = await api('/api/index/status'); } catch (e) { S.index = { available: false }; } }
  if (!S.index.available || !document.contains(el)) return;
  el.innerHTML = `<form class="scene-search" id="scene-form"><label class="field-pill">${icon('search', 17)}<input name="q" type="search" placeholder="Find a scene: “the sandworm”, “a sword fight”…" aria-label="Search scenes in this film" autocomplete="off"></label></form><div class="scene-results" id="scene-results"></div>`;
}
async function runSceneSearch(q) {
  const out = $('#scene-results');
  if (!out || !q) return;
  const id = D.id;
  out.innerHTML = `<div class="scene-grid">${'<span class="skel scene-skel"></span>'.repeat(4)}</div>`;
  let r;
  try { r = await api(`/api/index/${encodeURIComponent(id)}/search?q=${encodeURIComponent(q)}&k=8`); }
  catch (e) { out.innerHTML = `<p class="scene-note">${esc(e.message)}</p>`; return; }
  if (D.id !== id) return;
  if (r.available === false) { out.innerHTML = '<p class="scene-note">Scene search needs the media index (Settings › Media index).</p>'; return; }
  if (!r.indexed) {
    const st = r.index || {};
    out.innerHTML = `<p class="scene-note">${icon('sparkle', 14)} Marquee is indexing this film${st.progress ? ` (${Math.round(st.progress * 100)}%)` : ''}. Try again in a few minutes.</p>`;
    return;
  }
  const hits = r.results || [];
  const real = hits.filter(h => (h.z ?? 0) >= 3);
  const show = real.length ? real : hits.slice(0, 3);
  out.innerHTML = `${real.length ? '' : '<p class="scene-note">No clear match. The closest moments:</p>'}<div class="scene-grid${real.length ? '' : ' weak'}">${show.map((h, k) => `
    <button class="scene" style="--i:${k}" data-act="scene" data-id="${esc(id)}" data-t="${h.t}"><img src="${esc(h.thumb)}&w=320" alt="" loading="lazy"><span class="ts">${fmtClock(h.t)}</span><span class="pg">${icon('play', 16)}</span></button>`).join('')}</div>`;
}

/* --- My List */
async function loadMyList(force) {
  if (!force && S.mlData && Date.now() - S.mlData.at < 60e3) return S.mlData;
  try {
    const d = await api('/api/mylist');
    S.mylist = new Set(d.keys || []);
    S.mlData = { items: d.items || [], sync: d.sync, at: Date.now() };
  } catch (e) { S.mlData = S.mlData || { items: [], at: 0 }; }
  return S.mlData;
}
function setMlButtons(key, on) {
  for (const b of $$(`[data-ml-key="${CSS.escape(key)}"]`)) {
    b.classList.toggle('is-on', on);
    b.setAttribute('aria-pressed', on);
    b.title = on ? 'Remove from My List' : 'Add to My List';
    b.setAttribute('aria-label', b.title);
    if (b.classList.contains('ml-text')) b.innerHTML = `${icon(on ? 'check' : 'plus', 16)} ${on ? 'In My List' : 'My List'}`;
    else b.innerHTML = icon(on ? 'check' : 'plus', b.classList.contains('ml-btn') ? 16 : 18);
  }
}
async function toggleMyList(key, btn, force) {
  const on = force ?? !S.mylist.has(key);
  on ? S.mylist.add(key) : S.mylist.delete(key);
  setMlButtons(key, on);
  if (on && btn && !reduced.matches) btn.animate([{ transform: 'scale(.8)' }, { transform: 'scale(1.15)' }, { transform: 'none' }], { duration: 450, easing: 'cubic-bezier(.34,1.56,.64,1)' });
  try {
    let res;
    if (on) {
      if (key.startsWith('item:')) res = await api('/api/mylist', { key });
      else { const c = S.tmdb.get(key) || {}; const [type, tmdb] = key.split(':'); res = await api('/api/mylist', { type, tmdb: +tmdb, title: c.title, year: c.year, poster: c.poster, backdrop: c.backdrop, overview: c.overview, status: c.status, anime: c.anime }); }
    } else res = await api('/api/mylist', { key }, 'DELETE');
    if (res && res.keys) S.mylist = new Set([...res.keys, ...(on ? [key] : [])].filter(k => on || k !== key));
    S.mlData = null;
    if (key.startsWith('item:')) reg({ id: key.slice(5), favorite: on });
    toast(on ? 'Added to My List' : 'Removed from My List', { icon: on ? 'check' : 'close', action: 'Undo', onAction: () => toggleMyList(key, null, !on) });
    refreshMyListViews();
  } catch (e) {
    on ? S.mylist.delete(key) : S.mylist.add(key);
    setMlButtons(key, !on);
    toast(`Couldn't update My List: ${e.message}`);
  }
}
function mlCard(entry, n, removable) {
  const c = entry.card;
  const html = entry.owned ? posterCard(c, n) : tmdbCard(c, n);
  if (!removable) return html;
  const t = document.createElement('div');
  t.innerHTML = html;
  const el = t.firstElementChild;
  el.insertAdjacentHTML('beforeend', `<div class="ml-foot">${!entry.owned && !(c.status >= 2 && c.status <= 4) ? `<button class="btn small" data-act="tmdb-request" data-tmdb="${esc(c.type + ':' + c.id)}">${icon('plus', 14)} Request</button>` : ''}
    <button class="btn small ghost${!entry.owned && !(c.status >= 2 && c.status <= 4) ? ' round' : ''}" data-act="ml-remove" data-ml-key="${esc(entry.key)}" title="Remove from My List" aria-label="Remove ${esc(c.name || c.title || '')} from My List">${icon('close', 14)}${!entry.owned && !(c.status >= 2 && c.status <= 4) ? '' : ' Remove'}</button></div>`);
  return el.outerHTML;
}
async function addMyListRow(rows, seq) {
  const d = await loadMyList();
  if (stale(seq) || !document.contains(rows)) return;
  drawMyListRow(rows, d);
}
function drawMyListRow(rows, d) {
  const old = $('.row[data-key="mylist"]', rows);
  if (!d.items.length) { if (old) foldAway(old); return; }
  const t = document.createElement('div');
  t.innerHTML = `<section class="row in" data-key="mylist"><div class="row-head"><h2>My List</h2><span class="count">${d.items.length}</span><a class="row-link" href="#/mylist">See all ${icon('right', 14)}</a></div>
    <button class="row-arrow l" data-action="row-l" aria-label="Scroll left">${icon('left', 20)}</button>
    <div class="scroller">${d.items.slice(0, 30).map((e, k) => mlCard(e, k)).join('')}</div>
    <button class="row-arrow r" data-action="row-r" aria-label="Scroll right">${icon('right', 20)}</button></section>`;
  const el = t.firstElementChild;
  if (old) { old.replaceWith(el); el.style.cssText = 'animation:none;opacity:1;transform:none'; }
  else {
    const after = $('.row[data-key="continue"]', rows) || null;
    if (after) after.after(el); else rows.prepend(el);
    if (!reduced.matches) el.animate([{ opacity: 0, transform: 'translateY(18px)' }, { opacity: 1, transform: 'none' }], { duration: 600, easing: 'cubic-bezier(.34,1.3,.64,1)' });
  }
  updateArrows(el);
}
async function refreshMyListViews() {
  if (S.route === 'home' && $('#rows')) { const d = await loadMyList(true); if ($('#rows')) drawMyListRow($('#rows'), d); }
  if (S.route === 'mylist' && $('.page-mylist')) drawMyListPage($('.page-mylist'), await loadMyList(true));
}
async function renderMyList(page, seq) {
  page.innerHTML = `<div class="page-head"><div class="grow"><span class="eyebrow">Watch later</span><h1 style="margin-top:8px">My List</h1><p id="ml-sub">Loading…</p></div></div>
    <div id="ml-grid"><div class="skeleton-row">${'<i></i>'.repeat(8)}</div></div>`;
  const d = await loadMyList(true);
  if (stale(seq)) return;
  drawMyListPage(page, d);
}
function drawMyListPage(page, d) {
  const sub = $('#ml-sub', page), grid = $('#ml-grid', page);
  if (!sub || !grid) return;
  const n = d.items.length;
  sub.textContent = n ? `${n} title${n === 1 ? '' : 's'} saved for later · ${d.sync ? 'synced with your Jellyfin favourites' : 'kept on this PC'}` : '';
  grid.innerHTML = n ? `<div class="grid ml-grid">${d.items.map((e, k) => mlCard(e, k, true)).join('')}</div>`
    : `<div class="empty"><span class="big">🍿</span>Nothing saved yet. Press <b>+</b> on any title to keep it here for later, even ones that aren't in your library yet.</div>`;
}

async function toggleWatched(id, btn) {
  const it = S.items.get(id) || {};
  const on = !it.played;
  btn && btn.classList.toggle(btn.classList.contains('watch-toggle') ? 'on' : 'is-on', on);
  try {
    await api(`/api/played/${id}`, { played: on });
    reg({ id, played: on, progress: on ? 0 : it.progress });
    S.home = null; S.lib = {};
    toast(on ? `Marked “${titleOf(it)}” as watched` : `Marked “${titleOf(it)}” as unwatched`, { icon: 'check' });
    for (const c of $$(`.card[data-id="${CSS.escape(id)}"] .badges`)) c.innerHTML = on ? `<span class="tick">${icon('check')}</span>` : '';
    if (!layers.modal.hidden && D.kind === 'item') openDetails(D.id, { refresh: true, season: D.season && D.season.number });
  } catch (e) {
    btn && btn.classList.toggle(btn.classList.contains('watch-toggle') ? 'on' : 'is-on', !on);
    toast(e.message);
  }
}

/* ================================================================== search */
const srch = { seq: 0, timer: 0, ai: null };
function closeSearch() {
  srch.seq++;
  clearTimeout(srch.timer);
  if (srch.ai) { srch.ai.abort(); srch.ai = null; }
  closeLayer(layers.search);
}
function openSearch() {
  openLayer(layers.search);
  const inp = $('#search-input');
  setTimeout(() => inp.focus(), 30);
  if (!inp.value) searchHint();
}
function searchHint() {
  const pool = [...S.items.values()].filter(i => i.type === 'Movie' || i.type === 'Series');
  const picks = pool.sort(() => Math.random() - 0.5).slice(0, 5);
  $('#search-results').innerHTML = `<div class="search-hint">Search your library, or find something new to request.
    ${picks.length ? `<div class="chips">${picks.map(p => `<button data-q="${esc(p.name)}">${esc(p.name)}</button>`).join('')}</div>` : ''}</div>`;
}
$('#search-input').addEventListener('input', e => {
  clearTimeout(srch.timer);
  const q = e.target.value.trim();
  if (!q) { srch.seq++; if (srch.ai) { srch.ai.abort(); srch.ai = null; } return searchHint(); }
  srch.timer = setTimeout(() => runSearch(q), 260);
});
async function runSearch(q) {
  const seq = ++srch.seq;
  if (srch.ai) { srch.ai.abort(); srch.ai = null; }
  const box = $('#search-results');
  if (!box.querySelector('.grid')) box.innerHTML = `<div class="skeleton-row" style="padding:10px 0">${'<i></i>'.repeat(8)}</div>`;
  let res;
  try { res = await api(`/api/search?q=${encodeURIComponent(q)}`); }
  catch (e) { if (seq === srch.seq) box.innerHTML = `<div class="search-hint">${esc(e.message)}</div>`; return; }
  if (seq !== srch.seq) return;
  const lib = res.items || [], req = res.requestable || [];
  box.innerHTML = `<div id="ai-slot"></div>
    <section id="s-lib"${lib.length ? '' : ' hidden'}><h3>${icon('film', 16)} In your library</h3><div class="grid" id="s-lib-grid">${lib.map((i, k) => posterCard(i, k)).join('')}</div></section>
    <section id="s-req"${req.length ? '' : ' hidden'}><h3>${icon('plus', 16)} Not in your library yet</h3><div class="grid" id="s-req-grid">${req.map((c, k) => tmdbCard(c, k + lib.length, true)).join('')}</div></section>
    <div class="search-hint" id="s-none"${!lib.length && !req.length && !res.prompt ? '' : ' hidden'}><span style="font-size:40px;display:block">🔭</span>Nothing found for “${esc(q)}”.</div>`;
  if (res.prompt) aiSearch(q, seq);
}

/* Questions and moods ("something funny tonight") also go to the assistant: slow, so it never holds up the
   instant results. Its answer lands in a bubble and its picks glide to the front. */
const THINKING = ['Thinking about your taste…', 'Flipping through your shelves…', 'Asking the projectionist…', 'Weighing the classics…', 'Checking what you haven’t seen…', 'Almost there…'];
async function aiSearch(q, seq) {
  const slot = $('#ai-slot');
  const name = S.ai.name || 'Assistant';
  slot.innerHTML = `<div class="ai-think glass"><span class="orbit">${icon('sparkle', 18)}</span><span class="ai-name">${esc(name)}</span><span class="think-label">${THINKING[0]}</span><span class="dots"><i></i><i></i><i></i></span></div>`;
  let k = 0;
  const lbl = $('.think-label', slot);
  const rot = setInterval(() => {
    if (!document.contains(lbl)) return clearInterval(rot);
    k = (k + 1) % THINKING.length;
    lbl.classList.remove('swap-in'); void lbl.offsetWidth;
    lbl.textContent = THINKING[k]; lbl.classList.add('swap-in');
  }, 2600);
  const ctl = srch.ai = new AbortController();
  let res;
  try {
    const r = await fetch(`/api/search/ai?q=${encodeURIComponent(q)}`, { signal: ctl.signal });
    res = await r.json();
    if (!r.ok || res.ok === false) throw new Error(res.message || 'failed');
  } catch (e) { res = null; }
  clearInterval(rot);
  if (seq !== srch.seq || ctl.signal.aborted) return;
  srch.ai = null;
  const think = $('.ai-think', slot);
  if (!res || !res.answer) {
    if (think) { think.classList.add('leaving'); setTimeout(() => think.remove(), 300); }
    if (!$('#s-lib-grid .card') && !$('#s-req-grid .card')) $('#s-none').hidden = false;
    return;
  }
  const words = res.answer.split(/(\s+)/).filter(Boolean);
  slot.innerHTML = `<div class="ai-bubble glass"><span class="ai-badge">${icon('sparkle', 16)} ${esc(name)}</span>
    <p>${words.map((w, n) => `<span class="w" style="animation-delay:${Math.min(n, 80) * 28}ms">${esc(w)}</span>`).join('')}</p></div>`;
  mergeResults($('#s-lib-grid'), $('#s-lib'), res.items || [], i => posterCard(i), i => i.id, '[data-id]', el => el.dataset.id);
  mergeResults($('#s-req-grid'), $('#s-req'), res.requestable || [], c => tmdbCard(c, 0, true), c => `${c.type}:${c.id}`, '[data-tmdb]', el => el.dataset.tmdb, 3);
  if ($('#s-lib-grid .card') || $('#s-req-grid .card')) $('#s-none').hidden = true;
}

/* Put the assistant's picks first (highlighted), keep the rest after; FLIP so cards glide to their new places. */
function mergeResults(grid, section, picks, render, keyOf, sel, keyEl, ringTop = Infinity) {
  if (!grid || !picks.length) return;
  const before = new Map();
  for (const el of grid.children) before.set(keyEl(el), el.getBoundingClientRect());
  const existing = new Map([...grid.children].map(el => [keyEl(el), el]));
  const frag = document.createDocumentFragment();
  const used = new Set();
  for (const p of picks) {
    const key = keyOf(p);
    if (used.has(key)) continue;
    used.add(key);
    let el = existing.get(key);
    if (!el) { const t = document.createElement('div'); t.innerHTML = render(p); el = t.firstElementChild; el.classList.add('fresh'); }
    if (used.size <= ringTop) el.classList.add('ai-hit');
    if (p.why && !el.querySelector('.why')) el.insertAdjacentHTML('beforeend', `<div class="why">${esc(p.why)}</div>`);
    frag.append(el);
  }
  for (const [key, el] of existing) if (!used.has(key)) frag.append(el);
  grid.append(frag);
  section.hidden = false;
  if (reduced.matches) return;
  for (const el of grid.children) {
    const a = before.get(keyEl(el));
    el.style.animation = 'none';
    if (!a) { el.animate([{ opacity: 0, transform: 'translateY(16px) scale(.92)' }, { opacity: 1, transform: 'none' }], { duration: 550, easing: 'cubic-bezier(.34,1.56,.64,1)', delay: 120 }); continue; }
    const b = el.getBoundingClientRect();
    const dx = a.left - b.left, dy = a.top - b.top;
    if (dx || dy) el.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'none' }], { duration: 650, easing: 'cubic-bezier(.34,1.3,.64,1)' });
  }
}

/* ================================================================== requests */
async function renderRequests(page, seq) {
  page.innerHTML = `<div class="page-head"><div class="grow"><span class="eyebrow orange">Requests</span><h1 style="margin-top:8px">Find something new</h1>
      <p>Ask for films and shows that aren't in your library yet. They appear here once they've downloaded.</p></div>
      <label class="field-pill req-search">${icon('search', 17)}<input type="search" id="req-q" placeholder="Search any film or show" aria-label="Search films and shows to request"></label></div>
    <div id="mine-wrap"></div><div class="rows" id="rows">${skeletonRows(3)}</div>`;
  let data;
  try { data = await api('/api/requests/discover'); }
  catch (e) {
    if (stale(seq)) return;
    $('#rows', page).innerHTML = e.status === 409
      ? `<div class="notice glass"><span class="big">🎟️</span><h2>Connect Seerr to request titles</h2><p>Marquee uses Seerr (Jellyseerr/Overseerr) to find new films and shows and send them to your downloaders.</p><a class="btn" href="#/settings">${icon('gear')} Open Settings</a></div>`
      : errorBox(e);
    $('#req-q', page).disabled = true;
    return;
  }
  if (stale(seq)) return;
  const rowsData = [
    { key: 'trending', title: 'Trending this week', items: data.trending || [] },
    { key: 'upcoming', title: 'Coming soon', items: data.movies || [] },
    { key: 'pop-movies', title: 'Popular films', items: data.popular_movies || [] },
    { key: 'pop-tv', title: 'Popular shows', items: data.popular_tv || [] },
  ].filter(r => r.items.length).map(r => ({ ...r, tmdb: true, style: 'poster' }));
  const rows = $('#rows', page);
  const drawRows = () => { rows.innerHTML = rowsData.map(rowHTML).join(''); watchRows(rows); };
  drawRows();
  loadMine(page, seq);
  let t = 0, rseq = 0;
  $('#req-q', page).addEventListener('input', e => {
    clearTimeout(t);
    const q = e.target.value.trim();
    if (!q) { rseq++; return drawRows(); }
    t = setTimeout(async () => {
      const my = ++rseq;
      rows.innerHTML = `<div class="skeleton-row">${'<i></i>'.repeat(10)}</div>`;
      try {
        const { items } = await api(`/api/requests/search?q=${encodeURIComponent(q)}`);
        if (my !== rseq) return;
        rows.innerHTML = items.length ? `<div class="grid-tools" style="margin-top:10px"><h2>Results for “${esc(q)}”</h2></div><div class="grid">${items.map((c, k) => tmdbCard(c, k)).join('')}</div>`
          : `<div class="empty"><span class="big">🔭</span>Nothing found for “${esc(q)}”.</div>`;
      } catch (err) { if (my === rseq) rows.innerHTML = `<div class="empty">${esc(err.message)}</div>`; }
    }, 320);
  });
}

async function loadMine(page, seq) {
  let items;
  try { items = (await api('/api/requests/mine')).items || []; } catch (e) { return; }
  if (stale(seq) || !items.length) return;
  const wrap = $('#mine-wrap', page);
  items = items.slice(0, 20);
  for (const r of items) {
    const key = `${r.type}:${r.tmdb}`;
    S.tmdb.set(key, { ...(S.tmdb.get(key) || {}), type: r.type, id: r.tmdb, title: r.title, poster: r.poster, year: r.year, status: r.status });
  }
  wrap.innerHTML = `<div class="row-head" style="margin-top:14px"><h2>My requests</h2><span class="count">${items.length}</span></div>
    <div class="mine">${items.map((r, k) => {
      const st = STATUS[r.status] || ['Pending', 'solid', 'Waiting for approval'];
      return `<div class="mine-item glass" style="--i:${k}" data-act="tmdb" data-tmdb="${r.type}:${r.tmdb}" role="button" tabindex="0">
        ${r.poster ? `<img src="${esc(r.poster.replace('/w500/', '/w185/'))}" alt="" loading="lazy">` : `<span class="ph" style="background:${grad(r.title)}"></span>`}
        <div><b>${esc(r.title || 'Untitled')}</b><span class="s">${esc([r.year, r.type === 'tv' ? 'Series' : 'Film'].filter(Boolean).join(' · '))}</span>
        <span class="chip ${st[1]}" style="margin-top:4px" title="${st[2]}">${st[0]}</span></div></div>`;
    }).join('')}</div>`;
}

/* ------------------------------------------------------------------ request sheet */
const R = {};
async function openRequest(key) {
  const [type, id] = key.split(':');
  const seed = S.tmdb.get(key) || { title: '', type, id };
  Object.assign(R, { key, type, id: +id, details: null, prefs: {}, done: false, q: null, qv: null, qEl: null });
  const box = $('#sheet-box');
  box.innerHTML = requestTop(seed) + `<div class="r-body">${skeletonRows(1)}</div>`;
  openLayer(layers.sheet);
  layers.sheet.scrollTop = 0;
  let details, opts;
  try {
    [details, opts] = await Promise.all([api(`/api/requests/details/${type}/${id}`).then(r => r.item), api(`/api/requests/options/${type}`).catch(() => ({}))]);
  } catch (e) {
    if (R.key === key) $('.r-body', box).innerHTML = `<p class="empty">${esc(e.message)}</p>`;
    return;
  }
  if (R.key !== key) return;
  S.tmdb.set(key, { ...seed, ...details });
  R.details = details;
  R.prefs = opts.prefs || {};
  R.anime = !!details.anime;
  const pref = R.prefs[R.anime ? 'anime' : type] || null;
  R.remembered = pref;
  const seasons = details.seasons || [];
  R.picked = new Set();
  R.latest = seasons.length ? seasons[seasons.length - 1].number : null;
  R.first = seasons.length ? seasons[0].number : 1;
  R.seasonMode = window.MarqueeStorage ? MarqueeStorage.seasonModes(details, pref).default
    : (pref && ['all', 'latest', 'pick'].includes(pref.season_mode) ? pref.season_mode : 'all');
  // The quality picker (storage module) is mounted once per sheet and moved into each redraw, so it keeps its state.
  if (window.MarqueeStorage) {
    R.qEl = document.createElement('div');
    R.qEl.id = 'r-quality';
    R.q = MarqueeStorage.mountQuality(R.qEl, { kind: type, tmdb: +id, anime: R.anime, seasons: rSeasons(),
      onChange: v => { R.qv = v; const b = $('#sheet [data-act="send-request"]'); if (b) b.disabled = !!v.blocked; } });
  }
  box.innerHTML = requestTop(details) + `<div class="r-body"></div>`;
  drawRequestBody();
}
const rSeasons = () => R.type !== 'tv' ? undefined : R.seasonMode === 'pick' ? [...R.picked].sort((a, b) => a - b) : R.seasonMode;
function requestTop(d) {
  const meta = [d.year, d.type === 'tv' ? 'Series' : 'Film', d.runtime && fmtRuntime(d.runtime * 60), d.rating && `★ ${(+d.rating).toFixed(1)}`,
    ...(d.genres || []).slice(0, 3)].filter(Boolean);
  return `<div class="r-top">${d.backdrop ? `<img class="bd" src="${esc(d.backdrop)}" alt="">` : ''}
    ${d.poster ? `<img class="r-poster" src="${esc(d.poster)}" alt="">` : `<div class="r-poster" style="background:${grad(d.title)}"></div>`}
    <div style="flex:1;min-width:0"><span class="eyebrow orange" style="color:#ffd3a8">Request${d.anime ? ' · Anime' : ''}</span>
      <h2 style="margin-top:6px">${esc(d.title || '')}</h2><div class="m-meta">${meta.map(m => `<span>${esc(m)}</span>`).join('<span>·</span>')}</div></div>
    <button class="icon-btn m-close" data-action="close-sheet" aria-label="Close">${icon('close', 20)}</button></div>`;
}
function requestLabel() {
  if (R.type !== 'tv') return 'Request';
  if (R.seasonMode === 'smart') return `Request season ${R.first} (more as you watch)`;
  if (R.seasonMode === 'all') return 'Request all seasons';
  if (R.seasonMode === 'latest') return `Request season ${R.latest}`;
  return `Request ${R.picked.size} season${R.picked.size === 1 ? '' : 's'}`;
}
function drawRequestBody() {
  const d = R.details, body = $('#sheet .r-body');
  if (!body || !d) return;
  const st = d.status;
  const seasons = d.seasons || [];
  let actions;
  if (st === 5) {
    actions = `<span class="note">${icon('check', 16)} Already in your library.</span>${d.jellyfin ? `<button class="btn" data-act="open-jf" data-id="${esc(d.jellyfin)}">${icon('play')} Open</button>` : ''}`;
  } else if ((st === 2 || st === 3) && R.type === 'movie') {
    actions = `<span class="note">It's on its way: ${st === 2 ? 'requested and waiting' : 'downloading or processing'}.</span>`;
  } else if (R.done) {
    actions = `<span class="note r-done">${icon('check', 16)} ${esc(R.doneMsg || 'Requested!')}</span><button class="btn ghost" data-action="close-sheet">Done</button>`;
  } else {
    actions = `<span class="note">${R.remembered ? 'Using the choices you made last time.' : 'Your choice is remembered for next time.'}</span>
      <button class="btn" data-act="send-request" ${R.qv && R.qv.blocked ? 'disabled' : ''}>${icon('plus')} ${esc(requestLabel())}</button>`;
  }
  const showOpts = st !== 5 && !(R.type === 'movie' && (st === 2 || st === 3)) && !R.done;
  const modes = window.MarqueeStorage ? MarqueeStorage.seasonModes(d, R.remembered).modes
    : [{ v: 'all', label: `All ${seasons.length}` }, { v: 'latest', label: `Latest (S${R.latest})` }, { v: 'pick', label: 'Pick…' }];
  body.innerHTML = `${d.overview ? `<p class="m-overview">${esc(d.overview)}</p>` : ''}
    ${st === 4 ? '<p class="chip orange" style="justify-self:start">Some seasons are already here</p>' : ''}
    ${showOpts ? '<div id="r-quality-slot"></div>' : ''}
    ${showOpts && R.type === 'tv' && seasons.length ? `<div class="opt-group"><h4>Seasons</h4><div class="opt-row">
        ${modes.map(m => `<button class="opt" data-act="r-smode" data-v="${m.v}" title="${esc(m.title || '')}" aria-pressed="${R.seasonMode === m.v}">${esc(m.label)}</button>`).join('')}</div>
        ${R.seasonMode === 'smart' ? `<p class="r-hint">${icon('sparkle', 14)} Season ${R.first} now; the next season is requested when you're partway through, so only what you'll watch takes space.</p>` : ''}
        ${R.seasonMode === 'pick' ? `<div class="season-grid">${seasons.map(s => `<button class="opt" data-act="r-season" data-v="${s.number}" aria-pressed="${R.picked.has(s.number)}">S${s.number}${s.episodes ? ` <span style="opacity:.6;font-weight:500">· ${s.episodes} ep</span>` : ''}</button>`).join('')}</div>` : ''}</div>` : ''}
    <div class="r-foot">${actions}</div>`;
  const slot = $('#r-quality-slot', body);
  if (slot) {
    if (R.qEl) slot.replaceWith(R.qEl);
    else slot.innerHTML = '<p class="note" style="color:var(--muted)">Quality options aren’t available in this version.</p>';
  }
}
async function sendRequest(btn) {
  if (R.type === 'tv' && R.seasonMode === 'pick' && !R.picked.size) { toast('Pick at least one season'); return; }
  if (R.qv && R.qv.blocked) { toast('Not enough space for this request'); return; }
  const seasons = R.type !== 'tv' ? undefined : R.seasonMode === 'all' ? 'all' : R.seasonMode === 'latest' ? [R.latest]
    : R.seasonMode === 'smart' ? [R.first] : [...R.picked].sort((a, b) => a - b);
  const body = { type: R.type, id: R.id, title: R.details.title, anime: R.anime, ...(R.qv || {}) };
  delete body.blocked;
  if (seasons !== undefined) { body.seasons = seasons; body.season_mode = R.seasonMode; }
  btn.disabled = true;
  const r = btn.getBoundingClientRect();
  try {
    const res = window.MarqueeStorage ? await MarqueeStorage.sendRequest(body) : await api('/api/requests', body);
    confetti(r.left + r.width / 2, r.top + r.height / 2, 34);
    R.done = true;
    R.doneMsg = res.message;
    toast(res.message || 'Requested!', { icon: 'sparkle', ms: 4200 });
    const c = S.tmdb.get(R.key);
    if (c) c.status = 2;
    for (const card of $$(`.card[data-tmdb="${R.key}"]`)) {
      card.classList.remove('is-requestable', 'is-removed'); card.classList.add('is-requested');
      card.querySelector('.req-hint')?.remove();
      card.querySelector('.badges')?.insertAdjacentHTML('afterbegin', `<span class="chip glass-chip status" title="${STATUS[2][2]}">Requested</span>`);
    }
    drawRequestBody();
    if (S.route === 'requests') loadMine($('.page-requests'), S.renderSeq);
  } catch (e) {
    btn.disabled = false;
    btn.classList.add('shake');
    setTimeout(() => btn.classList.remove('shake'), 600);
    toast(`Request failed: ${e.message}`);
  }
}

/* ================================================================== settings */
const LANGS = [['eng', 'English'], ['jpn', 'Japanese'], ['spa', 'Spanish'], ['fre', 'French'], ['ger', 'German'], ['ita', 'Italian'], ['por', 'Portuguese'],
  ['kor', 'Korean'], ['chi', 'Chinese'], ['rus', 'Russian'], ['hin', 'Hindi'], ['ara', 'Arabic'], ['dut', 'Dutch'], ['swe', 'Swedish'], ['pol', 'Polish'], ['tur', 'Turkish']];
function langSelect(key) {
  const v = S.settings[key] || 'eng';
  const list = LANGS.some(l => l[0] === v) ? LANGS : [[v, v], ...LANGS];
  return `<select class="select" data-set="${key}">${list.map(([c, n]) => `<option value="${c}"${c === v ? ' selected' : ''}>${n}</option>`).join('')}</select>`;
}
const sw = (key, label, hint) => `<div class="set"><div class="lbl"><b>${label}</b>${hint ? `<span>${hint}</span>` : ''}</div>
  <button class="switch" role="switch" data-switch="${key}" aria-checked="${!!S.settings[key]}" aria-label="${esc(label)}"></button></div>`;
const seg = (key, opts, label, hint) => `<div class="set"><div class="lbl"><b>${label}</b>${hint ? `<span>${hint}</span>` : ''}</div>
  <div class="pill-toggle" role="group" aria-label="${esc(label)}">${opts.map(([v, n]) => `<button data-seg="${key}" data-v="${v}"${typeof v === 'number' ? ' data-num' : ''}${typeof v === 'boolean' ? ' data-bool' : ''} aria-pressed="${String(S.settings[key]) === String(v)}">${n}</button>`).join('')}</div></div>`;
const has = k => S.settings && k in S.settings;
const listBox = (key, label, hint, ph) => `<div class="set stack"><div class="lbl"><b>${label}</b>${hint ? `<span>${hint}</span>` : ''}</div>
  <textarea class="input wide list-box" data-list="${key}" rows="3" spellcheck="false" placeholder="${esc(ph)}">${esc((S.settings[key] || []).join('\n'))}</textarea></div>`;

/* The player panel: rows appear only for settings this version of the backend knows about. */
function playbackRows(s) {
  const up = [['auto', 'Auto'], ['nvidia', 'NVIDIA RTX'], ['anime4k', 'Anime4K'], ['hq', 'High quality'], ['off', 'Off']];
  return `
      ${seg('quality', [['original', 'Original'], ['2160', '4K'], ['1080', '1080p'], ['720', '720p'], ['480', '480p']], 'Default quality', 'Original plays the file itself when it can.')}
      ${seg('upscaler', up, 'Upscaler', 'Sharpens lower-resolution video.')}
      ${has('sdr_to_hdr') ? seg('sdr_to_hdr', [['auto', 'Auto'], ['off', 'Off']], 'SDR to HDR', 'Brightens standard video on an HDR screen.') : sw('rtx_hdr', 'RTX HDR', 'Turns SDR video into HDR on supported NVIDIA cards.')}
      ${has('hwdec') ? sw('hwdec', 'Hardware decoding', 'Lets the graphics card decode video. Turn off if you see glitches.') : ''}
      ${has('seek_step') ? seg('seek_step', [[5, '5 s'], [10, '10 s']], 'Arrow-key jump', 'How far ← and → skip.') : ''}
      ${sw('autoplay_next', 'Autoplay next episode', '')}
      ${has('credits_mini') ? sw('credits_mini', 'Up next during the credits', 'When the credits roll, the video shrinks to a corner and what to watch next appears.') : ''}
      ${has('interpolation') ? sw('interpolation', 'Smooth motion', 'Frame interpolation for less judder on panning shots.') : ''}
      ${sw('skip_intros', 'Skip intros automatically', 'The Skip Intro button shows either way.')}`;
}
function soundRows(s) {
  return `
      ${sw('stable_volume', 'Stable volume', 'Evens out loud explosions and quiet dialogue.')}
      ${has('night_mode') ? sw('night_mode', 'Night mode', 'Quiet parts up, loud parts down, so you don\u2019t wake the house.') : ''}
      ${has('voice_focus') || has('dialogue_boost') ? (has('voice_focus')
        ? seg('voice_focus', [['off', 'Off'], ['dialogue', 'Dialogue boost'], ['isolate', 'AI voice isolation']], 'Voices', 'Make speech easier to follow.')
        : sw('dialogue_boost', 'Dialogue boost', 'Lifts speech over music and effects.')) : ''}
      <div class="set"><div class="lbl"><b>Starting volume</b><span>Every video starts at this level.</span></div>
        <div class="range-wrap"><input type="range" min="0" max="100" step="1" value="${s.base_volume ?? 80}" data-range="base_volume" style="--v:${s.base_volume ?? 80}%" aria-label="Starting volume"><output>${s.base_volume ?? 80}</output></div></div>
      ${has('passthrough') ? sw('passthrough', 'Passthrough', 'Send Dolby and DTS untouched to a receiver or soundbar.') : ''}
      ${has('audio_device') ? `<div class="set"><div class="lbl"><b>Output device</b><span>Where the sound goes.</span></div>
        <select class="select" data-set="audio_device" id="audio-device"><option value="${esc(s.audio_device || 'auto')}" selected>${s.audio_device && s.audio_device !== 'auto' ? esc(s.audio_device) : 'Windows default'}</option></select></div>` : ''}`;
}
async function loadAudioDevices() {
  const sel = $('#audio-device');
  if (!sel) return;
  let data;
  try { data = await api('/api/audio-devices'); } catch (e) { return; }
  const list = Array.isArray(data) ? data : (data.devices || data.items || []);
  if (!list.length || !document.contains(sel)) return;
  const cur = S.settings.audio_device || 'auto';
  const opts = list.map(d => typeof d === 'string' ? { name: d, description: d } : { name: d.name ?? d.id, description: d.description || d.label || d.name });
  if (!opts.some(o => o.name === 'auto')) opts.unshift({ name: 'auto', description: 'Windows default' });
  sel.innerHTML = opts.map(o => `<option value="${esc(o.name)}"${o.name === cur ? ' selected' : ''}>${esc(o.description)}</option>`).join('');
}

/* Settings > Mobile: Marquee Mobile (the phone app on this PC's server: watch on the phone, or use it as a remote).
   Talks to /api/mobile* (marquee/remote.py). Replaces the old "Phone remote" panel. */
const REM = { data: null, idx: 0, seq: 0, pairVersion: null };
function remotePanel(s) {
  return `<section class="panel glass mobile-panel" style="--i:6"><h2>${icon('phone')} Marquee Mobile</h2>
      <p>Your library on your phone: watch anything from this PC, or use the phone as a remote for the player here.</p>
      <div class="set"><div class="lbl"><b>Marquee Mobile</b><span>Phones connect after scanning a pairing code.</span></div>
        <button class="switch" role="switch" data-switch="mobile" aria-checked="${!!(s.mobile || s.remote)}" aria-label="Marquee Mobile"></button></div>
      <div id="remote-box"></div></section>`;
}
async function loadRemote(quiet) {
  const box = $('#remote-box');
  if (!box) return;
  if (!(S.settings.mobile || S.settings.remote)) { box.innerHTML = ''; return; }
  const seq = ++REM.seq;
  if (!quiet || !REM.data) box.innerHTML = `<div class="remote-card glass"><div class="qr skel"></div><div><div class="skel" style="height:14px;width:180px;border-radius:9px"></div></div></div>`;
  let d;
  try { d = await api('/api/mobile'); } catch (e) {
    if (seq === REM.seq) box.innerHTML = `<p class="note" style="color:var(--muted);font-size:13px;margin-top:10px">Marquee Mobile isn't available yet: ${esc(e.message)}</p>`;
    return;
  }
  if (seq !== REM.seq || !document.contains(box)) return;
  REM.data = d;
  REM.pairVersion = d.pair_version;
  REM.idx = Math.min(REM.idx, Math.max(0, (d.addresses || []).length - 1));
  drawRemote();
  if (d.firewall && d.firewall.pending) setTimeout(() => { if (document.contains(box)) loadRemote(true); }, 3000);
}
const STREAM_MODE = { direct: 'Direct', remux: 'Remux', transcode: 'Transcode' };
function drawRemote() {
  const box = $('#remote-box'), d = REM.data;
  if (!box || !d) return;
  const addrs = (d.addresses && d.addresses.length) ? d.addresses : [{ label: 'This PC', url: d.url, qr: d.qr, address: d.url }];
  const a = addrs[REM.idx] || addrs[0];
  const ts = d.tailscale || {}, https = d.https || {}, fw = d.firewall || {};
  const devs = d.devices || [], streams = d.streams || [];
  const when = t => t ? new Date((t > 1e12 ? t : t * 1000)).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '';
  const bind = S.settings.mobile_bind || d.bind || 'tailscale';
  const maxS = S.settings.mobile_max_streams || d.max_streams || 2;
  const qual = S.settings.mobile_quality || d.quality || 'auto';
  box.innerHTML = `<div class="remote-card glass">
      ${a.qr ? `<img class="qr" src="${esc(a.qr)}" alt="QR code to pair your phone">` : '<div class="qr"></div>'}
      <div class="remote-info">
        ${addrs.length > 1 ? `<div class="pill-toggle" role="group" aria-label="Address">${addrs.map((x, k) => `<button data-remote-addr="${k}" aria-pressed="${k === REM.idx}">${esc(x.label || x.ip || 'Address ' + (k + 1))}</button>`).join('')}</div>` : `<span class="eyebrow">${esc(a.label || 'Scan with your phone')}</span>`}
        <p class="remote-url" data-selectable>${esc(a.address || a.url || d.url || '')}</p>
        <p class="remote-steps">Point your phone's camera at the code and tap the link. Each code pairs one phone, then a new one appears.${d.running === false ? ' <b>Marquee Mobile isn’t running right now.</b>' : ''}</p>
        ${d.hint ? `<p class="remote-steps mobile-hint">${esc(d.hint)}</p>` : ''}${d.error ? `<p class="remote-steps mobile-err">${esc(d.error)}</p>` : ''}
        <div class="mobile-btns"><button class="btn small ghost" data-action="mobile-pair">${icon('restart', 16)} New pairing code</button>
          <button class="btn small ghost" data-action="remote-reset">${icon('logout', 16)} Sign out all phones</button></div>
      </div></div>
    ${fw.checked && !fw.allowed ? `<div class="set mobile-fw"><div class="lbl"><b>Windows Firewall blocks phones on Tailscale</b><span>Allow Marquee through: one rule, private networks only, for Marquee and this port. Windows asks for permission.</span></div>
      <button class="btn small" data-action="mobile-firewall">Allow Marquee</button></div>` : ''}
    ${ts.running ? `<div class="set"><div class="lbl"><b>HTTPS</b><span>${https.on ? `On: ${esc(https.url || '')}` : 'Needed to install the app properly on an iPhone home screen. Uses Tailscale’s own certificate.'}</span></div>
      <button class="btn small ghost" data-action="mobile-https" data-on="${https.on ? '0' : '1'}">${https.on ? 'Turn off HTTPS' : 'Turn on HTTPS'}</button></div>` : ''}
    <div class="set stack"><div class="lbl"><b>Network</b><span>Tailscale works anywhere; adding the home network lets phones on your Wi-Fi in without it.</span></div>
      <div class="pill-toggle" role="group" aria-label="Network"><button data-seg="mobile_bind" data-v="tailscale" aria-pressed="${bind === 'tailscale'}">Tailscale</button><button data-seg="mobile_bind" data-v="lan+tailscale" aria-pressed="${bind === 'lan+tailscale'}">Home network + Tailscale</button></div></div>
    <div class="set"><div class="lbl"><b>Phones streaming at once</b><span>Phones converting video at the same time (direct play doesn't count).</span></div>
      <div class="pill-toggle" role="group" aria-label="Phones streaming at once">${[1, 2, 3, 4].map(n => `<button data-seg="mobile_max_streams" data-num data-v="${n}" aria-pressed="${+maxS === n}">${n}</button>`).join('')}</div></div>
    <div class="set stack"><div class="lbl"><b>Default phone quality</b><span>Each phone can change its own.</span></div>
      <div class="pill-toggle" role="group" aria-label="Default phone quality">${[['auto', 'Auto'], ['original', 'Original'], ['1080', '1080p'], ['720', '720p'], ['480', '480p']].map(([v, n]) => `<button data-seg="mobile_quality" data-v="${v}" aria-pressed="${qual === v}">${n}</button>`).join('')}</div></div>
    ${sw('mobile_jellyfin_signin', 'Let friends sign in with Jellyfin', 'Friends use their own Jellyfin account on the phone app, with their own watch history.')}
    <div class="set stack"><div class="lbl"><b>Paired phones</b>${devs.length ? '' : '<span>None yet.</span>'}</div>
      ${devs.length ? `<ul class="mobile-list">${devs.map(v => `<li><div class="grow"><b>${esc(v.name || 'Phone')}</b><span>${esc([v.browser, v.kind === 'jellyfin' ? (v.user || 'Jellyfin user') : 'You', v.last_seen ? 'last seen ' + when(v.last_seen) : null].filter(Boolean).join(' · '))}</span></div>
        <button class="stk-mini" data-action="mobile-rename" data-id="${esc(v.id)}" data-name="${esc(v.name || '')}">Rename</button><button class="stk-mini" data-action="mobile-revoke" data-id="${esc(v.id)}" data-name="${esc(v.name || 'this phone')}">Remove</button></li>`).join('')}</ul>` : ''}</div>
    ${streams.length ? `<div class="set stack"><div class="lbl"><b>Streaming now</b></div><ul class="mobile-list">${streams.map(x => `<li><div class="grow"><b>${esc((x.item && x.item.title) || 'Video')}</b>
      <span>${esc([x.device, STREAM_MODE[x.mode] || x.mode, x.quality, x.progress && x.progress.speed ? x.progress.speed + '×' : null, x.suspended ? 'paused' : null].filter(Boolean).join(' · '))}</span></div>
      <button class="stk-mini" data-action="mobile-stop" data-id="${esc(x.id)}">Stop</button></li>`).join('')}</ul></div>` : ''}`;
}
async function resetRemote(btn) {
  if (!btn.dataset.sure) {
    btn.dataset.sure = '1';
    btn.innerHTML = `${icon('logout', 16)} Sure? Every phone must pair again`;
    setTimeout(() => { if (document.contains(btn)) { delete btn.dataset.sure; btn.innerHTML = `${icon('logout', 16)} Sign out all phones`; } }, 4000);
    return;
  }
  try { await api('/api/remote/reset', {}); toast('All phones signed out. Scan the new code to pair again.', { icon: 'check' }); loadRemote(); }
  catch (e) { toast(e.message); }
}
async function mobileAction(btn) {
  const a = btn.dataset.action;
  try {
    if (a === 'mobile-pair') { await api('/api/mobile/pair', {}); toast('New pairing code ready', { icon: 'check' }); }
    else if (a === 'mobile-firewall') { btn.disabled = true; const r = await api('/api/mobile/firewall', {}); toast(r.message || 'Done', { icon: 'check' }); }
    else if (a === 'mobile-https') { btn.disabled = true; const r = await api('/api/mobile/https', { on: btn.dataset.on === '1' }); toast(r.message || 'Done', { ms: 7000 }); }
    else if (a === 'mobile-stop') { await api(`/api/mobile/streams/${encodeURIComponent(btn.dataset.id)}/stop`, {}); }
    else if (a === 'mobile-revoke') {
      if (!btn.dataset.sure) { btn.dataset.sure = '1'; btn.textContent = 'Sure?'; setTimeout(() => { if (document.contains(btn)) { delete btn.dataset.sure; btn.textContent = 'Remove'; } }, 4000); return; }
      await api(`/api/mobile/devices/${encodeURIComponent(btn.dataset.id)}/revoke`, {}); toast(`Removed ${btn.dataset.name}`, { icon: 'check' });
    } else if (a === 'mobile-rename') {
      const li = btn.closest('li'), b = $('.grow b', li);
      const inp = Object.assign(document.createElement('input'), { className: 'input', value: btn.dataset.name, maxLength: 40 });
      inp.setAttribute('aria-label', 'Phone name');
      b.replaceWith(inp); inp.focus(); inp.select();
      const save = async () => {
        inp.onblur = null;
        const name = inp.value.trim();
        if (name && name !== btn.dataset.name) { try { await api(`/api/mobile/devices/${encodeURIComponent(btn.dataset.id)}/rename`, { name }); } catch (e) { toast(e.message); } }
        loadRemote(true);
      };
      inp.onblur = save;
      inp.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); save(); } else if (e.key === 'Escape') { e.stopPropagation(); inp.onblur = null; loadRemote(true); } };
      return;
    }
  } catch (e) { toast(e.message); }
  finally { btn.disabled = false; }
  loadRemote(true);
}

function renderSettings(page) {
  const s = S.settings;
  const ai = ({ openai: 'custom', off: 'classic' })[s.ai_provider] || s.ai_provider || 'auto';
  s.ai_provider = ai;   // old values shown under their new names
  page.innerHTML = `<div class="page-head"><div class="grow"><span class="eyebrow">Settings</span><h1 style="margin-top:8px">Make it yours</h1><p>Changes save as you go.</p></div></div>
  <div class="settings">
    <section class="panel glass" style="--i:0"><h2>${icon('sun')} Appearance</h2>
      <div class="set"><div class="lbl"><b>Theme</b><span>Clear daylight, or lights down for movie night.</span></div>
        <div class="theme-cards">
          <button class="theme-card" data-seg="theme" data-v="light" aria-pressed="${s.theme === 'light'}"><div class="sw l"><i></i><i></i><i></i><i></i></div>Light</button>
          <button class="theme-card" data-seg="theme" data-v="dark" aria-pressed="${s.theme !== 'light'}"><div class="sw d"><i></i><i></i><i></i><i></i></div>Dark</button></div></div>
      ${sw('previews', 'Hover previews', 'Rest on a poster to play a silent clip.')}
      ${has('details_autoplay') ? sw('details_autoplay', 'Previews on title pages', 'A title’s page plays a few silent seconds of it behind the artwork.') : ''}
      <div class="set stack"><div class="lbl"><b>Keyboard</b></div><div class="shortcuts"><kbd>/</kbd><span>Search</span><kbd>Esc</kbd><span>Close whatever is open</span><kbd>← →</kbd><span>Move along a row</span><kbd>Enter</kbd><span>Open the focused title</span></div></div>
    </section>
    <section class="panel glass" style="--i:1"><h2>${icon('play')} Playback</h2>${playbackRows(s)}
    </section>
    <section class="panel glass" style="--i:2"><h2>${icon('heart')} Sound</h2>${soundRows(s)}
    </section>
    <section class="panel glass" style="--i:2"><h2>${icon('info')} Languages</h2>
      <div class="set"><div class="lbl"><b>Subtitles</b><span>Preferred subtitle language.</span></div>${langSelect('sub_lang')}</div>
      <div class="set"><div class="lbl"><b>Anime audio</b><span>Audio language for anime.</span></div>${langSelect('audio_lang_anime')}</div>
    </section>
    <section class="panel glass" style="--i:3"><h2>${icon('sparkle')} Assistant</h2>
      <p>Greetings, picks and search answers. ${S.state.atlas ? 'Atlas was found on this PC.' : ''} ${S.ai.mode === 'classic' ? 'Currently in <b>Classic</b> mode: prepared lines and rules, no AI.' : S.ai.on ? `Currently using <b>${esc(S.ai.name)}</b>.` : ''}</p>
      <div class="set stack"><div class="lbl"><b>Provider</b><span>Auto uses your own key if set, else Atlas if it's on this PC, else the built-in AI if installed, else Classic.</span></div>
        <div class="pill-toggle" role="group" aria-label="AI provider">${[['auto', 'Auto'], ['builtin', 'Built-in'], ['atlas', 'Atlas'], ['custom', 'Custom key'], ['classic', 'Off (Classic, no AI)']].map(([v, n]) => `<button data-seg="ai_provider" data-v="${v}" aria-pressed="${ai === v}">${n}</button>`).join('')}</div></div>
      <div id="builtin-box"></div>
      <div class="fields" id="ai-fields" ${ai === 'custom' ? '' : 'hidden'}>
        <label>Endpoint URL<input class="input wide" data-text="ai_url" value="${esc(s.ai_url || '')}" placeholder="https://api.openai.com/v1" spellcheck="false"></label>
        <label>API key<input class="input wide" type="password" data-text="ai_key" placeholder="${s.ai_key_set ? 'Key saved (type to replace)' : 'Paste your API key'}" autocomplete="off"></label>
        <label>Model<input class="input wide" data-text="ai_model" value="${esc(s.ai_model || '')}" placeholder="gpt-4o-mini" spellcheck="false"></label>
      </div>
    </section>
    <section class="panel glass" style="--i:4"><h2>${icon('plus')} Requests (Seerr)</h2>
      <p>Seerr finds new titles and sends them to your downloaders.</p>
      <div class="set stack"><div class="lbl"><b>Seerr address</b></div><input class="input wide" data-text="seerr" value="${esc(s.seerr || '')}" placeholder="http://localhost:5055" spellcheck="false"></div>
      <div class="set stack"><div class="lbl"><b>${s.seerr_signed_in ? 'Signed in to Seerr' : 'Connect'}</b><span>Seerr signs in with your Jellyfin password.</span></div>
        <form class="fields" id="seerr-form" style="grid-template-columns:1fr auto"><input class="input wide" type="password" name="password" placeholder="Jellyfin password" autocomplete="current-password" aria-label="Jellyfin password"><button class="btn small">${s.seerr_signed_in ? 'Reconnect' : 'Connect'}</button></form></div>
    </section>
    <section class="panel glass" style="--i:5"><h2>${icon('logout')} Account</h2>
      <div class="set"><div class="account"><span class="avatar">${esc((S.user || '?')[0].toUpperCase())}</span><div><b>${esc(S.user || 'Signed in')}</b><div style="color:var(--muted);font-size:13px">${esc(s.server || 'Jellyfin')}</div></div></div>
        <button class="btn small danger" data-action="logout">Sign out</button></div>
      <p style="margin-top:10px;font-size:12.5px">Marquee is free software under the GPL-3.0 licence.</p>
    </section>
    <section class="panel glass taste-panel" id="taste-panel" style="--i:6"></section>
    <div id="storage-settings" class="mqs-settings"></div>
    ${remotePanel(s)}
    <section class="panel glass" style="--i:6"><h2>${icon('plus')} My List</h2>
      <p>Titles you save for later, in your library or not.</p>
      ${has('mylist_sync_favorites') ? sw('mylist_sync_favorites', 'Sync with Jellyfin favourites', 'Your list follows your favourites on every Jellyfin app, both ways. Titles you haven’t got yet join once they arrive.') : ''}
    </section>
    <section class="panel glass" style="--i:7"><h2>${icon('search')} Media index</h2>
      <p>Search inside films for a scene, and blur or skip scenes you'd rather not see. Runs on this PC.</p>
      <div id="index-box"><div class="skel dx-skel-line"></div></div></section>
    <section class="panel glass" style="--i:7"><h2>${icon('share')} Sharing</h2>
      <p>Share a film from this PC as a Google Drive link: it uploads to your own Drive.</p>
      <div id="share-box"><div class="skel dx-skel-line"></div></div></section>
    ${has('watch_folders') || has('library_folders') || has('autosort_after') || has('qbt_url') ? `<section class="panel glass" style="--i:7"><h2>${icon('film')} Library &amp; downloads</h2>
      <p>Where films come from and where they end up.</p>
      ${has('autosort_after') ? seg('autosort_after', [[0, 'Right away'], [30, '30 min'], [120, '2 h'], [360, '6 h']], 'Sort new downloads', 'How long to wait before tidying finished downloads into the library.') : ''}
      ${has('watch_folders') ? listBox('watch_folders', 'Watch folders', 'Folders Marquee keeps an eye on for new downloads. One per line.', 'D:\\Downloads') : ''}
      ${has('library_folders') ? listBox('library_folders', 'Library folders', 'Folders of films and shows Marquee plays even when Jellyfin is away. One per line.', 'D:\\Media\\Movies') : ''}
      ${has('qbt_url') ? `<div class="set stack"><div class="lbl"><b>qBittorrent address</b><span>Used to show download progress in “Arriving soon”.</span></div><input class="input wide" data-text="qbt_url" value="${esc(s.qbt_url || '')}" placeholder="http://localhost:8080" spellcheck="false"></div>` : ''}
    </section>` : ''}
    <div id="about-settings"></div>
  </div>`;
  window.MarqueeUpdate && MarqueeUpdate.mountAbout($('#about-settings'));
  loadAudioDevices();
  loadRemote();
  window.MarqueeTaste && window.MarqueeTaste.renderSettings($('#taste-panel'));
  window.MarqueeStorage && MarqueeStorage.mountSettings($('#storage-settings'));
  loadIndexPanel();
  loadSharePanel();
  loadBuiltin();
}

/* Settings > Assistant: the built-in AI (downloaded on demand). */
let builtinPoll = 0;
async function loadBuiltin() {
  const box = $('#builtin-box');
  clearTimeout(builtinPoll);
  if (!box) return;
  let st;
  try { st = await api('/api/ai/builtin/status'); } catch (e) { box.innerHTML = ''; return; }
  if (!document.contains(box)) return;
  const dl = st.download || {}, busy = ['downloading', 'verifying', 'extracting'].includes(dl.state);
  const sizes = st.sizes || {};
  const sz = k => sizes[k] || {};
  const gb = b => b ? (b / 1e9).toFixed(1) + ' GB' : '';
  const anyInstalled = Object.values(sizes).some(x => x.installed) || st.installed;
  const p = dl.total ? dl.done / dl.total : 0;
  box.innerHTML = `<div class="builtin-card"><div class="lbl"><b>Built-in AI</b><span>Runs on this PC, nothing leaves it. ${st.gpu ? `Uses your ${esc(st.gpu)}.` : st.cpu_only ? 'Runs on the processor (slower).' : ''}${st.running ? ' Running now.' : ''}</span></div>
    ${busy ? `<div><span class="muted-note">${esc(dl.state === 'downloading' ? `Downloading ${dl.file || ''}` : dl.state === 'verifying' ? 'Checking the download…' : 'Unpacking…')}${dl.total ? ` · ${gb(dl.done)} of ${gb(dl.total)}` : ''}</span><div class="index-bar"><i style="--p:${p.toFixed(3)}"></i></div></div>` : ''}
    ${dl.state === 'error' ? `<p class="muted-note">Download failed: ${esc(dl.error || 'unknown error')}</p>` : ''}
    <div class="row-btns">${['standard', 'small'].map(k => sz(k).installed ? `<span class="chip green">${icon('check', 12)} ${esc(sz(k).label || k)} installed</span>`
      : `<button class="btn small${k === 'small' ? ' ghost' : ''}" data-builtin-install="${k}" ${busy ? 'disabled' : ''}>Install ${k === 'standard' ? 'Standard' : 'Small'} (${gb(sz(k).download) || (k === 'standard' ? '~3.0 GB' : '~1.6 GB')})</button>`).join('')}
      ${anyInstalled ? `<button class="btn small ghost danger-text" data-builtin-remove>Remove</button>` : ''}</div>
    ${anyInstalled ? `${seg('ai_builtin_size', [['standard', 'Standard'], ['small', 'Small']], 'Model size', 'Small is quicker on modest PCs.')}
      ${sw('ai_builtin_gpu', 'Use graphics card', 'Much faster. Turn off if games or videos stutter while it thinks.')}` : ''}</div>`;
  if (busy) builtinPoll = setTimeout(loadBuiltin, 1500);
}
document.addEventListener('click', async e => {
  const b = e.target.closest && e.target.closest('[data-builtin-install], [data-builtin-remove]');
  if (!b) return;
  e.stopPropagation();
  try {
    if (b.dataset.builtinInstall) { b.disabled = true; await api('/api/ai/builtin/install', { size: b.dataset.builtinInstall }); toast('Downloading the built-in AI…', { icon: 'sparkle' }); }
    else {
      if (!b.dataset.sure) { b.dataset.sure = '1'; b.textContent = 'Sure? Remove it'; setTimeout(() => { if (document.contains(b)) { delete b.dataset.sure; b.textContent = 'Remove'; } }, 3500); return; }
      await api('/api/ai/builtin', {}, 'DELETE'); toast('Built-in AI removed');
    }
  } catch (err) { toast(err.message); }
  loadBuiltin();
}, true);

const saveQ = { pending: {}, timer: 0, toastTimer: 0 };
function saveSetting(changes, quiet) {
  Object.assign(S.settings, changes);
  Object.assign(saveQ.pending, changes);
  clearTimeout(saveQ.timer);
  return new Promise(done => { saveQ.timer = setTimeout(async () => {
    const body = saveQ.pending;
    saveQ.pending = {};
    try {
      const res = await api('/api/settings', body);
      S.settings = res.settings;
      if (Object.keys(body).some(k => k.startsWith('ai_'))) {
        try { const st = await api('/api/state'); S.ai = st.ai; S.state = st; S.welcome = undefined; S.picks = undefined; } catch (e) { /* ignore */ }
      }
      if ('seerr' in body) S.lib = { ...S.lib };
      if (!quiet) { clearTimeout(saveQ.toastTimer); saveQ.toastTimer = setTimeout(() => toast('Saved', { icon: 'check', ms: 1600 }), 120); }
    } catch (e) { toast(`Couldn't save: ${e.message}`); }
    done();
  }, 250); });
}

document.addEventListener('change', e => {
  const t = e.target;
  if (t.dataset.set) saveSetting({ [t.dataset.set]: t.value });
  if (t.dataset.text) {
    const v = t.value.trim();
    if (t.dataset.text === 'ai_key' && !v) return;
    saveSetting({ [t.dataset.text]: v });
    if (t.dataset.text === 'ai_key') t.value = '';
  }
  if (t.dataset.range) saveSetting({ [t.dataset.range]: +t.value });
  if (t.dataset.list) saveSetting({ [t.dataset.list]: t.value.split(/\r?\n/).map(x => x.trim()).filter(Boolean) });
});
document.addEventListener('input', e => {
  const t = e.target;
  if (t.dataset.range) { t.style.setProperty('--v', t.value + '%'); t.nextElementSibling.textContent = t.value; }
});
document.addEventListener('submit', async e => {
  if (e.target.id === 'seerr-form') {
    e.preventDefault();
    const pw = e.target.password.value;
    const addr = ($('[data-text="seerr"]').value || '').trim() || 'http://localhost:5055';
    const btn = e.target.querySelector('button');
    btn.disabled = true;
    try {
      await api('/api/seerr/login', { seerr: addr, password: pw });
      S.settings.seerr = addr;
      S.settings.seerr_signed_in = true;
      toast('Seerr connected', { icon: 'check', ms: 4000 });
      confetti(innerWidth / 2, innerHeight - 80, 20);
      e.target.password.value = '';
    } catch (err) { e.target.classList.add('shake'); setTimeout(() => e.target.classList.remove('shake'), 600); toast(err.message); }
    btn.disabled = false;
  }
  if (e.target.id === 'signin-form') { e.preventDefault(); doSignin(e.target); }
  if (e.target.id === 'scene-form') { e.preventDefault(); runSceneSearch(e.target.q.value.trim()); }
});

/* ================================================================== sign in */
function renderSignin() {
  $('#topbar').hidden = true;
  setAmbient(null);
  const s = (S.state && S.state.settings) || {};
  $('#view').innerHTML = `<div class="page signin"><div class="signin-card glass">
    <div class="brand"><span class="brand-mark"><img src="icon.svg" alt="" width="40" height="40"></span>marquee<span class="brand-dot">.</span></div>
    <h1>Lights, camera…</h1><p>Sign in with your Jellyfin account to start watching.</p>
    <form id="signin-form">
      <label>Jellyfin address<input class="input" name="server" value="${esc(s.server || 'http://localhost:8096')}" spellcheck="false" required></label>
      <label>Username<input class="input" name="user" value="${esc(s.user_name || '')}" autocomplete="username" required></label>
      <label>Password<input class="input" name="password" type="password" autocomplete="current-password"></label>
      <details${s.seerr ? ' open' : ''}><summary>+ Requests with Seerr (optional)</summary><label style="margin-top:8px">Seerr address<input class="input" name="seerr" value="${esc(s.seerr || 'http://localhost:5055')}" spellcheck="false"></label></details>
      <p class="err" id="signin-err" hidden></p>
      <button class="btn">Sign in ${icon('right')}</button>
    </form></div></div>`;
  setTimeout(() => { const f = $('#signin-form'); (f.user.value ? f.password : f.user).focus(); }, 60);
}
async function doSignin(f) {
  const btn = f.querySelector('.btn'), err = $('#signin-err');
  btn.disabled = true;
  err.hidden = true;
  const seerr = f.querySelector('details').open ? f.seerr.value.trim() : '';
  try {
    const res = await api('/api/login', { server: f.server.value.trim(), user: f.user.value.trim(), password: f.password.value, seerr });
    if (res.seerr) toast(res.seerr, { ms: 5000 });
    toast(`Welcome, ${res.user || f.user.value}!`, { icon: 'sparkle' });
    confetti(innerWidth / 2, innerHeight / 2, 30);
    await boot();
  } catch (e) {
    err.textContent = e.message || 'Sign in failed';
    err.hidden = false;
    const card = $('.signin-card');
    card.classList.remove('shake'); void card.offsetWidth; card.classList.add('shake');
    btn.disabled = false;
  }
}

/* ================================================================== surprise me */
async function surprise() {
  let pool = [];
  const seen = new Set();
  const add = list => (list || []).forEach(i => { if (i.type === 'Movie' && !i.played && !seen.has(i.id)) { seen.add(i.id); pool.push(i); } });
  Object.values(S.lib).forEach(l => (l.rows || []).forEach(r => add(r.items)));
  (S.home && S.home.rows || []).forEach(r => add(r.items));
  if (pool.length < 4) {
    try { const d = await api('/api/library/movies'); S.lib.movies = { ...d, at: Date.now() }; d.rows.forEach(r => add(r.items)); } catch (e) { /* use what we have */ }
  }
  if (!pool.length) return toast('Nothing unwatched to pick from. You’ve seen it all!');
  pool.forEach(reg);
  closePop();
  $('.surprise')?.remove();
  const el = document.createElement('div');
  el.className = 'surprise';
  el.innerHTML = `<div class="scrim" data-action="close-surprise"></div><div class="surprise-card glass" role="dialog" aria-label="Surprise pick">
    <span class="eyebrow orange" style="justify-content:center">Rolling the dice</span><div class="reel"></div><h3>&nbsp;</h3><p>&nbsp;</p>
    <div class="row-btns"><button class="btn ghost small" data-action="close-surprise">Cancel</button><button class="btn ghost small" data-action="surprise">${icon('dice', 16)} Again</button><button class="btn small" data-action="surprise-go" disabled>${icon('play', 16)} Play now</button></div></div>`;
  document.body.append(el);
  root.classList.add('locked');
  const reel = $('.reel', el);
  const pick = pool[Math.floor(Math.random() * pool.length)];
  const face = i => i.poster ? `<img class="flip" src="${esc(i.poster)}" alt="">` : `<div class="fallback flip" style="--fb:${grad(i.name)}">${esc(i.name)}</div>`;
  const spins = reduced.matches ? 0 : Math.min(9, pool.length * 2);
  for (let k = 0; k < spins; k++) {
    if (!document.contains(el)) return;
    reel.innerHTML = face(pool[Math.floor(Math.random() * pool.length)]);
    await sleep(70 + k * k * 4);
  }
  if (!document.contains(el)) return;
  reel.innerHTML = face(pick);
  reel.firstElementChild.classList.add('final');
  $('.eyebrow', el).textContent = 'Tonight’s pick';
  $('h3', el).textContent = pick.name;
  const go = $('[data-action="surprise-go"]', el);
  go.disabled = false;
  go.dataset.id = pick.id;
  let n = 6;
  const tick = () => {
    if (!document.contains(el)) return;
    if (n <= 0) { el.remove(); root.classList.toggle('locked', anyLayerOpen()); return play(pick.id, 0, pick.name); }
    $('p', el).textContent = `${[pick.year, fmtRuntime(pick.runtime)].filter(Boolean).join(' · ')}  ·  playing in ${n}…`;
    n--;
    el._t = setTimeout(tick, 1000);
  };
  tick();
}
function closeSurprise() {
  const el = $('.surprise');
  if (!el) return;
  clearTimeout(el._t);
  el.remove();
  root.classList.toggle('locked', anyLayerOpen());
}

/* ================================================================== episode hover previews (title page) */
const epPv = { timer: 0, el: null };
function stopEpPreview() {
  clearTimeout(epPv.timer);
  const th = epPv.el;
  epPv.el = null;
  if (!th) return;
  th.classList.remove('vid');
  const v = $('video', th);
  if (v) unloadVideo(v);
  const m = D.media;
  if (m && m.el.tagName === 'VIDEO' && m.epPaused) { m.epPaused = false; m.el.play().catch(() => {}); }
}
document.addEventListener('pointerover', e => {
  const th = e.target.closest && e.target.closest('#dx-eps .epi:not(.missing) .thumb');
  if (th === epPv.el) return;
  stopEpPreview();
  if (!th || S.settings.previews === false || !finePointer.matches) return;
  epPv.el = th;
  epPv.timer = setTimeout(async () => {
    const id = th.closest('.epi').dataset.id;
    let r;
    try { r = await api(`/api/preview/${encodeURIComponent(id)}`); } catch (err) { return; }
    if (epPv.el !== th || !r.available) return;
    const v = $('video', th);
    v.muted = true;
    v.onplaying = () => { if (epPv.el === th) th.classList.add('vid'); };
    if (r.seek) v.addEventListener('loadedmetadata', () => { try { v.currentTime = r.start || 0; } catch (x) { /* ignore */ } }, { once: true });
    addPreviewSubs(v, r.subs);
    v.src = r.url;
    v.play().catch(() => {});
    const m = D.media;
    if (m && m.el.tagName === 'VIDEO' && !m.el.paused) { m.el.pause(); m.epPaused = true; }
  }, 650);
});

/* ================================================================== connection (local-first) */
function onConnection(d) {
  const was = !!(S.state && S.state.offline);
  S.state = { ...(S.state || {}), offline: !!d.offline, since: d.since };
  setOffline(S.state);
  if (d.offline && !was) toast('Jellyfin is away. Everything on this PC still plays.', { icon: 'cloud-off', ms: 4500 });
  if (!d.offline && was) { toast('Jellyfin is back', { icon: 'check' }); scheduleLive(300); }
}

/* ================================================================== "Up next" (credits roll) */
/* The video shrinks into the bottom-right tile; this page sits behind it: the next pick's hero, the assistant's
   comment (fades in when it's ready), more picks with reasons. Enter plays next, Esc/B goes back to the credits. */
const UN = { open: false, id: null, p: null, next: null, timer: 0, ends: 0, local: null, atlas: null, moments: null };
async function fetchEnding(id) {
  // The assistant's comment and picks; without it (not installed, or failing) Classic mode's prepared ones.
  for (const url of [`/api/assistant/ending/${encodeURIComponent(id)}`, `/api/classic/ending/${encodeURIComponent(id)}`]) {
    try {
      const r = await fetch(url);
      if (!r.ok) continue;
      const j = await r.json();
      if (j && j.ok !== false && (j.comment || j.state === 'pending' || (j.picks && j.picks.length))) return j;
    } catch (e) { /* try the next */ }
  }
  return null;
}
async function openUpNext(p) {
  if (!p.item_id) return;
  UN.open = true; UN.id = p.item_id; UN.p = p; UN.local = null; UN.atlas = null; UN.next = null; UN.moments = null;
  stopHero();
  closePop(true);
  let el = $('#upnext');
  if (!el) { el = document.createElement('div'); el.id = 'upnext'; el.className = 'upnext'; el.setAttribute('role', 'dialog'); el.setAttribute('aria-label', 'Up next'); document.body.append(el); }
  el.innerHTML = `<div class="un-bg"></div><div class="un-shade"></div><div class="un-inner"><span class="eyebrow orange">Credits rolling</span>
    <div class="skel dx-skel-title" style="margin-top:18px"></div><div class="skel dx-skel-line"></div><div class="skel dx-skel-line" style="width:40%"></div></div>`;
  el.hidden = false;
  requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add('open')));
  root.classList.add('locked');
  UN.ends = performance.now() + 10000;
  const [local, atlas] = await Promise.all([api(`/api/upnext/${encodeURIComponent(p.item_id)}`).catch(() => null), fetchEnding(p.item_id)]);
  if (!UN.open || UN.id !== p.item_id) return;
  UN.local = local; UN.atlas = atlas;
  drawUpNext();
  pollEnding(p.item_id, 0);
  loadMoments(p.item_id);
}
/* Key moments of what was just watched (frames from the file, made while the credits approached): click = replay. */
async function loadMoments(id) {
  let m = null;
  try { m = await api(`/api/assistant/moments/${encodeURIComponent(id)}`); } catch (e) { return; }
  if (!UN.open || UN.id !== id || !m || !m.moments || !m.moments.length) return;
  UN.moments = m.moments;
  drawMoments();
}
function drawMoments() {
  const box = $('#un-moments');
  if (!box) return;
  const ms = UN.moments || [];
  box.hidden = !ms.length;
  if (!ms.length) return;
  box.innerHTML = `<div class="un-picks-head">Moments</div><div class="un-moments-row">${ms.map((m, k) => `
    <button class="un-moment" style="--i:${k}" data-action="upnext-moment" data-t="${+m.t || 0}" aria-label="Play from ${esc(m.label)}: ${esc(m.caption || '')}">
      <span class="um-img"><img src="${esc(m.img)}" alt="" loading="lazy"><span class="play-glyph">${icon('play')}</span></span>
      <span class="um-txt"><b>${esc(m.label)}</b> ${esc(m.caption || '')}</span></button>`).join('')}</div>`;
}
function upNextMoment(t) {
  const id = UN.id;
  if (!id) return;
  closeUpNext();
  play(id, Math.max(0, +t || 0));
}
/* The comment normally arrives as the "atlas-ending" event; ask again now and then in case it was missed. */
async function pollEnding(id, n) {
  if (!UN.open || UN.id !== id || !UN.atlas || UN.atlas.state !== 'pending' || n > 12) return;
  await sleep(3000);
  if (!UN.open || UN.id !== id || !UN.atlas || UN.atlas.state !== 'pending') return;
  const a = await fetchEnding(id);
  if (a && UN.open && UN.id === id && UN.atlas && UN.atlas.state === 'pending') onAtlasEnding({ ...a, item_id: id });
  pollEnding(id, n + 1);
}
function unPicks() {
  const out = [], seen = new Set();
  const add = (card, why, kind) => {
    if (!card) return;
    const k = card.tmdbKey || card.id;
    if (seen.has(k)) return;
    seen.add(k);
    out.push({ card, why, kind });
  };
  for (const a of (UN.atlas && UN.atlas.picks) || []) {
    if (a.card && a.card.id && !a.request) add(reg(a.card), a.why, a.kind);
    else if (a.id && S.items.get(a.id)) add(S.items.get(a.id), a.why, a.kind);
    else if (a.tmdb) { const key = `${a.type === 'tv' || a.type === 'Series' ? 'tv' : 'movie'}:${a.tmdb}`; const c = { ...(a.card || {}), type: key.split(':')[0], id: a.tmdb, title: a.title, year: a.year, tmdbKey: key }; S.tmdb.set(key, { ...(S.tmdb.get(key) || {}), ...c }); add(c, a.why, a.kind); }
  }
  for (const x of (UN.local && UN.local.picks) || []) add(reg(x.card), x.why, x.kind);
  return out;
}
function drawUpNext() {
  const el = $('#upnext');
  if (!el || !UN.open) return;
  const p = UN.p, picks = unPicks();
  const nextEp = p.next_episode_id && UN.local && UN.local.next && UN.local.next.id === p.next_episode_id ? UN.local.next : null;
  const hero = nextEp || (picks.find(x => x.card.id && !x.card.tmdbKey) || {}).card || (UN.local && UN.local.next);
  UN.next = hero || null;
  const ep = hero && hero.type === 'Episode';
  const bd = hero && (hero.backdrop || hero.thumb);
  const auto = ep && p.autoplay !== false && S.settings.autoplay_next !== false;
  const sub = hero ? (ep ? [epCode(hero), hero.name, fmtRuntime(hero.runtime)] : [hero.year, hero.type === 'Series' ? 'Series' : fmtRuntime(hero.runtime), hero.age]).filter(Boolean).join(' · ') : '';
  el.innerHTML = `<div class="un-bg">${bd ? `<img src="${esc(bd)}" alt="">` : ''}</div><div class="un-shade"></div>
    <div class="un-inner">
      <span class="eyebrow orange">${ep ? 'Next episode' : hero ? 'Up next' : 'That’s a wrap'}</span>
      ${hero ? `<div class="un-hero">${hero.logo ? `<img class="un-logo logo-img" data-cls="un-title" src="${esc(hero.logo)}" alt="${esc(titleOf(hero))}">` : `<h1 class="un-title">${esc(titleOf(hero))}</h1>`}
        <div class="un-sub">${esc(sub)}</div>${hero.overview ? `<p class="un-ov">${esc(hero.overview)}</p>` : ''}</div>` : `<h1 class="un-title">${esc(p.title || 'The end')}</h1>`}
      <div class="un-actions">
        ${hero ? `<button class="btn light un-play" data-action="upnext-play">${icon('play')} ${ep ? 'Play next' : 'Play'}${auto ? ' <span class="un-count" aria-live="polite"></span>' : ''}</button>` : ''}
        <button class="btn on-dark" data-action="upnext-full">${icon('film')} Keep watching credits</button>
        <button class="btn on-dark" data-action="upnext-home">Home</button></div>
      <div class="un-atlas glass" id="un-atlas" hidden></div>
      <div class="un-moments" id="un-moments" hidden></div>
      ${picks.some(x => !hero || (x.card.play_id || x.card.id) !== (hero.play_id || hero.id)) ? `<div class="un-picks-head">More for you</div><div class="un-picks">${picks.filter(x => !hero || (x.card.play_id || x.card.id) !== (hero.play_id || hero.id)).slice(0, 8).map((x, k) => unPick(x, k)).join('')}</div>` : ''}
    </div>`;
  drawAtlasComment();
  drawMoments();
  clearInterval(UN.timer);
  if (auto) {
    const tick = () => {
      const c = $('.un-count', el);
      if (!c || !UN.open) return clearInterval(UN.timer);
      const left = Math.max(0, Math.ceil((UN.ends - performance.now()) / 1000));
      c.textContent = left ? `· ${left}` : '· starting…';
      if (!left) clearInterval(UN.timer);
    };
    tick();
    UN.timer = setInterval(tick, 250);
  }
}
function unPick(x, k) {
  const c = x.card;
  if (c.tmdbKey) return `<div class="un-pick" style="--i:${k}">${tmdbCard(c, k)}<div class="why">${esc(x.why || '')}</div></div>`;
  const art = c.thumb || c.backdrop;
  return `<div class="un-pick" style="--i:${k}"><div class="card wide is-owned" role="button" tabindex="0" data-id="${esc(c.play_id || c.id)}" data-act="play">
      <div class="media${art ? '' : ' noimg ready'}" style="--fb:${grad(titleOf(c))}">${imgTag(art)}<div class="fallback">${esc(titleOf(c))}</div><div class="shade"></div>
      <div class="wide-info"><b>${esc(titleOf(c))}</b><span>${esc(c.type === 'Episode' ? epCode(c) : [c.year, c.type === 'Series' ? 'Series' : fmtRuntime(c.runtime)].filter(Boolean).join(' · '))}</span></div>
      <span class="play-glyph">${icon('play')}</span></div></div>${x.why ? `<div class="why">${esc(x.why)}</div>` : ''}</div>`;
}
function drawAtlasComment() {
  const box = $('#un-atlas');
  if (!box) return;
  const a = UN.atlas;
  if (!a) { box.hidden = true; return; }
  const name = (S.ai && S.ai.name) || 'Atlas';
  box.hidden = false;
  if (a.state === 'pending' || !a.comment) {
    box.innerHTML = `<span class="ai-badge">${icon('sparkle', 15)} ${esc(name)}</span><p class="un-thinking">${esc(typeof a.thinking === 'string' && a.thinking ? a.thinking : 'Thinking about that ending')}<span class="dots"><i></i><i></i><i></i></span></p>`;
    return;
  }
  const words = cleanComment(a.comment).split(/(\s+)/).filter(Boolean);
  box.innerHTML = `<span class="ai-badge">${icon('sparkle', 15)} ${esc(name)}</span><p>${words.map((w, n) => `<span class="w" style="animation-delay:${Math.min(n, 90) * 26}ms">${esc(w)}</span>`).join('')}</p>`;
}
/* A model sometimes hands back its JSON instead of the line inside it: show just the line. */
function cleanComment(t) {
  t = String(t || '').trim();
  if (!t.startsWith('{')) return t;
  try { const j = JSON.parse(t); if (j && typeof j.comment === 'string') return j.comment; } catch (e) { /* not clean JSON */ }
  const m = t.match(/"comment"\s*:\s*"((?:[^"\\]|\\.)*)"/);
  if (m) { try { return JSON.parse('"' + m[1] + '"'); } catch (e) { return m[1]; } }
  return t;
}
function onAtlasEnding(d) {
  if (!UN.open) return;
  if (d.item_id && d.item_id !== UN.id) return;
  const hadPicks = !!(UN.atlas && UN.atlas.picks && UN.atlas.picks.length);
  UN.atlas = d;
  if (!hadPicks && d.picks && d.picks.length) drawUpNext(); else drawAtlasComment();
  if (d.state === 'ready' && !(UN.moments && UN.moments.length)) loadMoments(UN.id);
}
async function upNextPlay() {
  const n = UN.next;
  if (!n) return;
  const id = n.play_id || n.id;
  closeUpNext();
  if (n.type === 'Series') return playItem(id);
  play(id, n.type === 'Episode' ? undefined : undefined);
}
async function upNextFull() {
  try { await api('/api/player/full', {}); } catch (e) { /* the player may already be full size */ }
  closeUpNext();
}
function upNextHome() {
  closeUpNext();
  if (S.route !== 'home') location.hash = '#/home'; else refreshHomeRows();
}
function closeUpNext() {
  if (!UN.open) return;
  UN.open = false;
  clearInterval(UN.timer);
  const el = $('#upnext');
  if (!el) return;
  el.classList.remove('open');
  setTimeout(() => { if (!UN.open) { el.hidden = true; el.innerHTML = ''; } }, reduced.matches ? 10 : 400);
  root.classList.toggle('locked', anyLayerOpen());
}

/* ================================================================== Ask Atlas answers off the video */
const ATL = { id: 0, timer: 0, keep: false };
function atlasToast(d) {
  if (d.id != null && d.id < ATL.id) return;
  if (d.id != null) ATL.id = d.id;
  let el = $('#atlas-toast');
  clearTimeout(ATL.timer);
  if (d.state === 'clear') { if (el) { el.classList.remove('open'); setTimeout(() => { if (!el.classList.contains('open')) el.hidden = true; }, 350); } return; }
  if (!el) { el = document.createElement('div'); el.id = 'atlas-toast'; el.className = 'atlas-toast glass'; el.setAttribute('role', 'status'); el.setAttribute('aria-live', 'polite'); document.body.append(el); }
  const name = d.name || (S.ai && S.ai.name) || 'Atlas';
  ATL.keep = false;
  if (d.state === 'listening' || d.state === 'thinking') {
    el.className = 'atlas-toast glass pill';
    el.innerHTML = `<span class="orbit">${icon('sparkle', 15)}</span><span>${esc(d.text || (d.state === 'listening' ? `Ask ${name}…` : `${name} is thinking…`))}</span><span class="dots"><i></i><i></i><i></i></span>`;
  } else {
    el.className = 'atlas-toast glass answer';
    const words = String(d.text || '').split(/(\s+)/).filter(Boolean);
    el.innerHTML = `<div class="at-head"><span class="ai-badge">${icon('sparkle', 15)} ${esc(name)}</span>${d.question ? `<span class="at-q">“${esc(d.question)}”</span>` : ''}
      <button class="icon-btn small" data-action="atlas-close" aria-label="Dismiss">${icon('close', 16)}</button></div>
      <p>${words.map((w, n) => `<span class="w" style="animation-delay:${Math.min(n, 90) * 20}ms">${esc(w)}</span>`).join('')}</p><span class="at-keep">Click to keep this open</span>`;
    el.onclick = ev => {
      if (ev.target.closest('[data-action]')) return;
      ATL.keep = !ATL.keep;
      el.classList.toggle('kept', ATL.keep);
      clearTimeout(ATL.timer);
      if (!ATL.keep) atlasToast({ state: 'clear', id: ATL.id });
    };
    const secs = Math.max(6, Math.min(30, +d.seconds || 10));
    ATL.timer = setTimeout(() => { if (!ATL.keep) atlasToast({ state: 'clear', id: ATL.id }); }, secs * 1000);
  }
  el.hidden = false;
  requestAnimationFrame(() => el.classList.add('open'));
}

/* ================================================================== Share to Google Drive */
const SH = { id: null, data: null };
function shareSheetEl() {
  let el = $('#share-sheet');
  if (!el) {
    el = document.createElement('div');
    el.id = 'share-sheet';
    el.className = 'share-sheet';
    el.innerHTML = `<div class="scrim" data-action="share-close"></div><div class="share-card glass" role="dialog" aria-modal="true" aria-label="Share"></div>`;
    document.body.append(el);
  }
  return el;
}
function openShareSheet(html) {
  const el = shareSheetEl();
  $('.share-card', el).innerHTML = html;
  el.hidden = false;
  requestAnimationFrame(() => el.classList.add('open'));
}
function closeShareSheet() {
  const el = $('#share-sheet');
  if (!el) return;
  el.classList.remove('open');
  SH.id = null;
  setTimeout(() => { if (!el.classList.contains('open')) el.hidden = true; }, 300);
}
const fmtBytes = b => !b ? '0 B' : b >= 1e9 ? (b / 1e9).toFixed(1) + ' GB' : b >= 1e6 ? (b / 1e6).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1e3)) + ' kB';
async function shareItem(id, name) {
  openShareSheet(shareHead(name || 'Share') + `<div class="share-body"><div class="skel dx-skel-line"></div><div class="skel dx-skel-line" style="width:60%"></div></div>`);
  let r;
  try { r = await api('/api/share', { item_id: id }); }
  catch (e) { return drawShareError(name, e.message, e.reason); }
  SH.id = r.share.id;
  SH.data = r.share;
  drawShare(r.share);
}
async function api2(path, body, method) {   // like api(), but keeps the server's "reason"
  const r = await fetch(path, { method: method || (body !== undefined ? 'POST' : 'GET'), headers: { 'Content-Type': 'application/json' }, body: body !== undefined ? JSON.stringify(body) : undefined });
  let d = {};
  try { d = await r.json(); } catch (e) { /* empty */ }
  if (!r.ok || d.ok === false) { const err = new Error(d.message || r.statusText); err.reason = d.reason; throw err; }
  return d;
}
function shareHead(title, file) {
  return `<div class="share-head"><span class="share-ico">${icon('share', 20)}</span><div><h3>${esc(title)}</h3>${file ? `<span>${esc(file)}</span>` : ''}</div>
    <button class="icon-btn small" data-action="share-close" aria-label="Close">${icon('close', 16)}</button></div>`;
}
function drawShareError(name, msg, reason) {
  const btn = reason === 'needs_client' ? `<a class="btn small" href="#/settings" data-action="share-close">${icon('gear', 16)} Open Settings › Sharing</a>`
    : reason === 'needs_connect' || reason === 'reauth' ? `<button class="btn small" data-act="share-connect">Connect Google Drive</button>` : '';
  openShareSheet(shareHead(name || 'Share') + `<div class="share-body"><p class="share-msg">${esc(msg)}</p>${btn}</div>`);
}
function drawShare(sh) {
  if (!$('#share-sheet') || SH.id !== sh.id) return;
  const ring = 2 * Math.PI * 26;
  let body;
  if (sh.status === 'ready') {
    const exp = [[0, 'Never'], [3600, '1 hour'], [86400, '1 day'], [604800, '7 days'], [2592000, '30 days']];
    const left = sh.expires ? Math.max(0, sh.expires - Date.now() / 1000) : 0;
    body = `<div class="share-ready">${sh.qr ? `<img class="qr" src="${esc(sh.qr)}" alt="QR code for the link"><span class="share-qr-note">Scan to open on your phone</span>` : ''}
      <div class="share-link"><input class="input wide" readonly value="${esc(sh.link || '')}" aria-label="Link"><button class="btn small" data-act="share-copy" data-sid="${esc(sh.id)}">Copy</button></div>
      <div class="share-exp"><span>Link works for</span><div class="pill-toggle">${exp.map(([v, n]) => `<button data-act="share-exp" data-sid="${esc(sh.id)}" data-v="${v}" aria-pressed="${v === 0 ? !sh.expires : Math.abs(left - v) < v * 0.5}">${n}</button>`).join('')}</div></div>
      ${sh.message ? `<p class="share-msg">${esc(sh.message)}</p>` : ''}
      <div class="share-foot"><button class="btn small ghost" data-act="share-stop" data-sid="${esc(sh.id)}">Stop sharing</button><button class="link danger-link" data-act="share-delete" data-sid="${esc(sh.id)}">Delete from Drive</button></div></div>`;
  } else if (sh.status === 'error' || sh.status === 'stopped' || sh.status === 'expired') {
    body = `<p class="share-msg">${esc(sh.message || (sh.status === 'stopped' ? 'Sharing stopped.' : sh.status === 'expired' ? 'This link has expired.' : 'Something went wrong.'))}</p>
      ${sh.reason === 'needs_connect' || sh.reason === 'reauth' ? '<button class="btn small" data-act="share-connect">Connect Google Drive</button>' : ''}`;
  } else {
    const pr = sh.progress || {}, f = pr.total ? pr.sent / pr.total : 0;
    const line = [pr.speed ? `${fmtBytes(pr.speed)}/s` : '', pr.eta ? `about ${fmtEta(pr.eta)} left` : ''].filter(Boolean).join(', ');
    body = `<div class="share-up"><div class="arr-ring share-ring"><svg viewBox="0 0 60 60" aria-hidden="true"><circle cx="30" cy="30" r="26" class="trk"/><circle cx="30" cy="30" r="26" class="val" style="stroke-dasharray:${ring.toFixed(1)};stroke-dashoffset:${(ring * (1 - f)).toFixed(1)}"/></svg><b>${Math.round(f * 100)}%</b></div>
      <div><b>${sh.status === 'queued' ? 'Waiting to upload…' : sh.status === 'paused' ? 'Paused' : 'Uploading to your Drive…'}</b><span>${esc(line || sh.message || (pr.total ? `${fmtBytes(pr.sent)} of ${fmtBytes(pr.total)}` : ''))}</span></div></div>
      <div class="share-foot"><button class="btn small ghost" data-act="share-stop" data-sid="${esc(sh.id)}">Cancel</button></div>`;
  }
  $('#share-sheet .share-card').innerHTML = shareHead(sh.title || sh.name || 'Share', sh.size ? `${sh.name} · ${fmtBytes(sh.size)}` : sh.name) + `<div class="share-body">${body}</div>`;
}
function onShareProgress(d) {
  if (SH.id !== d.id || !SH.data) return;
  SH.data = { ...SH.data, status: d.status || SH.data.status, message: d.message, progress: { sent: d.sent, total: d.total, speed: d.speed, eta: d.eta } };
  drawShare(SH.data);
}
async function onShareReady(d) {
  toast(d.copied ? `Link copied: ${d.name || 'shared'}` : (d.message || 'Your link is ready'), { icon: 'check', ms: 4500 });
  if (SH.id === d.id) { try { SH.data = (await api(`/api/share/${encodeURIComponent(d.id)}`)).share; drawShare(SH.data); } catch (e) { /* keep */ } }
  if (S.route === 'settings') loadSharePanel();
}
async function shareCopy(sid) {
  try { await api(`/api/share/${encodeURIComponent(sid)}/copy`, {}); toast('Link copied', { icon: 'check' }); }
  catch (e) { const v = $('.share-link input'); if (v && navigator.clipboard) { navigator.clipboard.writeText(v.value); toast('Link copied', { icon: 'check' }); } else toast(e.message); }
}
async function shareStop(sid, btn) {
  try { const r = await api(`/api/share/${encodeURIComponent(sid)}/stop`, {}); if (SH.id === sid && r.share) { SH.data = r.share; drawShare(r.share); } toast('Sharing stopped'); if (S.route === 'settings') loadSharePanel(); }
  catch (e) { toast(e.message); }
}
async function shareDelete(sid, btn) {
  if (!btn.dataset.sure) { btn.dataset.sure = '1'; btn.textContent = 'Sure? Delete from Drive'; setTimeout(() => { if (document.contains(btn)) { delete btn.dataset.sure; btn.textContent = 'Delete from Drive'; } }, 4000); return; }
  try { await api(`/api/share/${encodeURIComponent(sid)}`, {}, 'DELETE'); toast('Deleted from your Drive'); if (SH.id === sid) closeShareSheet(); if (S.route === 'settings') loadSharePanel(); }
  catch (e) { toast(e.message); }
}
async function shareExpiry(sid, v) {
  try { const r = await api(`/api/share/${encodeURIComponent(sid)}/expiry`, { expires_in: v }); if (r.share && SH.id === sid) { SH.data = r.share; drawShare(r.share); } toast(v ? 'Expiry updated' : 'The link no longer expires', { icon: 'check' }); }
  catch (e) { toast(e.message); }
}
async function shareConnect(btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Finish signing in in your browser…'; }
  try { const r = await api('/api/share/connect', {}); toast(`Google Drive connected${r.email ? ' as ' + r.email : ''}`, { icon: 'check' }); }
  catch (e) { toast(e.message); }
  if (btn && document.contains(btn)) { btn.disabled = false; btn.textContent = 'Connect Google Drive'; }
  if (S.route === 'settings') loadSharePanel();
}
async function shareDisconnect(btn) {
  if (!btn.dataset.sure) { btn.dataset.sure = '1'; btn.textContent = 'Sure? Disconnect'; setTimeout(() => { if (document.contains(btn)) { delete btn.dataset.sure; btn.textContent = 'Disconnect'; } }, 3500); return; }
  try { const r = await api('/api/share/disconnect', {}); toast(r.message || 'Disconnected', { ms: 5000 }); } catch (e) { toast(e.message); }
  loadSharePanel();
}
async function loadSharePanel() {
  const box = $('#share-box');
  if (!box) return;
  let st, list = [];
  try { st = await api('/api/share/status'); } catch (e) { box.innerHTML = `<p class="note muted-note">Sharing isn't available in this version.</p>`; return; }
  try { list = (await api('/api/shares')).shares || []; } catch (e) { /* none */ }
  if (!document.contains(box)) return;
  const q = st.quota || {};
  const line = st.signed_in ? `Connected as <b>${esc(st.email || st.name || 'your Google account')}</b>${q.used != null ? `, ${fmtBytes(q.used)}${q.limit ? ` of ${fmtBytes(q.limit)}` : ''} used` : ''}` : st.configured ? 'Not connected yet.' : 'Add your Google client below, then connect.';
  const caps = [[0, 'Off'], [5, '5'], [10, '10'], [25, '25'], [50, '50']];
  box.innerHTML = `<div class="set"><div class="lbl"><b>Google Drive</b><span>${line}</span></div>
      ${st.signed_in ? '<button class="btn small ghost" data-act="share-disconnect">Disconnect</button>' : `<button class="btn small" data-act="share-connect" ${st.configured ? '' : 'disabled'}>Connect Google Drive</button>`}</div>
    <div class="set stack"><div class="lbl"><b>Your Google client</b><span>Marquee uploads with your own Google “Desktop app” client. <a class="link" href="https://github.com/phonehseng/marquee/blob/main/docs/google-drive.md" target="_blank" rel="noopener">How to get these</a></span></div>
      <div class="fields"><label>Client ID<input class="input wide" data-share-cfg="client_id" value="${esc(st.client_id || '')}" spellcheck="false" placeholder="1234-abc.apps.googleusercontent.com"></label>
      <label>Client secret<input class="input wide" type="password" data-share-cfg="client_secret" autocomplete="off" placeholder="${st.client_secret_set ? 'Saved (type to replace)' : 'Paste the client secret'}"></label></div></div>
    <div class="set"><div class="lbl"><b>Upload speed cap</b><span>Mbit/s, so sharing doesn't hog your connection.</span></div>
      <div class="pill-toggle">${caps.map(([v, n]) => `<button data-share-cap="${v}" aria-pressed="${(st.speed_cap || 0) === v}">${n}</button>`).join('')}</div></div>
    ${list.length ? `<div class="set stack"><div class="lbl"><b>Shared files</b></div><div class="share-list">${list.map(sh => `<div class="share-row">
      <div><b>${esc(sh.title || sh.name)}</b><span>${esc([sh.status, sh.size ? fmtBytes(sh.size) : '', sh.expires ? 'until ' + new Date(sh.expires * 1000).toLocaleDateString() : (sh.status === 'ready' ? 'no expiry' : '')].filter(Boolean).join(' · '))}</span></div>
      ${sh.status === 'ready' ? `<button class="btn small ghost" data-act="share-copy" data-sid="${esc(sh.id)}">Copy</button><button class="btn small ghost" data-act="share-stop" data-sid="${esc(sh.id)}">Stop</button>` : ''}
      <button class="link danger-link" data-act="share-delete" data-sid="${esc(sh.id)}">Delete</button></div>`).join('')}</div></div>` : ''}`;
}
document.addEventListener('change', async e => {
  const t = e.target;
  if (!t.dataset || !t.dataset.shareCfg) return;
  const v = t.value.trim();
  if (t.dataset.shareCfg === 'client_secret' && !v) return;
  try { await api('/api/share/config', { [t.dataset.shareCfg]: v }); toast('Saved', { icon: 'check', ms: 1600 }); if (t.type === 'password') t.value = ''; loadSharePanel(); }
  catch (err) { toast(err.message); }
});
document.addEventListener('click', async e => {
  const b = e.target.closest && e.target.closest('[data-share-cap]');
  if (!b) return;
  $$('[data-share-cap]').forEach(x => x.setAttribute('aria-pressed', x === b));
  try { await api('/api/share/config', { speed_cap: +b.dataset.shareCap }); toast('Saved', { icon: 'check', ms: 1600 }); } catch (err) { toast(err.message); }
});

/* ================================================================== Settings: media index */
let indexPoll = 0;
async function loadIndexPanel() {
  const box = $('#index-box');
  clearTimeout(indexPoll);
  if (!box) return;
  let st;
  try { st = await api('/api/index/status'); } catch (e) { box.innerHTML = `<p class="note muted-note">The media index isn't part of this version.</p>`; return; }
  if (!document.contains(box)) return;
  S.index = st;
  const inst = st.installing || {};
  if (!st.available) {
    const p = inst.total ? inst.done / inst.total : 0;
    box.innerHTML = `<div class="set"><div class="lbl"><b>Scene search &amp; content filter</b><span>${inst.running ? esc(inst.step || 'Installing…') + (inst.total ? ` · ${Math.round(p * 100)}%` : '') : inst.error ? 'Install failed: ' + esc(inst.error) : `A small on-device model (about ${st.download_mb || '?'} MB) that lets you search inside films and skip or blur scenes.`}</span></div>
      <button class="btn small" data-act="index-install" ${inst.running ? 'disabled' : ''}>${inst.running ? 'Installing…' : `Install (${st.download_mb || '?'} MB)`}</button></div>
      ${inst.running ? `<div class="index-bar"><i style="--p:${p.toFixed(3)}"></i></div>` : ''}`;
    if (inst.running) indexPoll = setTimeout(loadIndexPanel, 2000);
    return;
  }
  const cfg = st.config || {}, sens = cfg.sensitive || {};
  const w = st.worker || {};
  const cats = [['violence', 'Violence'], ['gore', 'Gore'], ['nudity', 'Nudity']];
  box.innerHTML = `<div class="set"><div class="lbl"><b>Index my library</b><span>Slowly, in the background, while nothing plays.${w.done != null ? ` ${w.done} title${w.done === 1 ? '' : 's'} indexed.` : ''}</span></div>
      <button class="switch" role="switch" data-index-lib aria-checked="${!!cfg.library}" aria-label="Index my library"></button></div>
    <div class="set stack"><div class="lbl"><b>Sensitive scenes</b><span>Conservative: catches gore well, misses some violence. Applies to titles once they're indexed.</span></div>
      <div class="sens-grid">${cats.map(([k, n]) => `<span>${n}</span><div class="pill-toggle">${[['off', 'Show'], ['blur', 'Blur'], ['skip', 'Skip']].map(([v, l]) => `<button data-sens="${k}" data-v="${v}" aria-pressed="${(sens[k] || 'off') === v}">${l}</button>`).join('')}</div>`).join('')}</div></div>`;
}
document.addEventListener('click', async e => {
  const t = e.target.closest && e.target.closest('[data-act="index-install"], [data-index-lib], [data-sens]');
  if (!t) return;
  e.stopPropagation();
  try {
    if (t.dataset.act === 'index-install') { t.disabled = true; await api('/api/index/install', {}); toast('Installing the media index…', { icon: 'sparkle' }); return loadIndexPanel(); }
    if ('indexLib' in t.dataset) { const on = t.getAttribute('aria-checked') !== 'true'; t.setAttribute('aria-checked', on); await api('/api/index/config', { library: on }); return toast('Saved', { icon: 'check', ms: 1600 }); }
    if (t.dataset.sens) { $$(`[data-sens="${t.dataset.sens}"]`).forEach(b => b.setAttribute('aria-pressed', b === t)); await api('/api/index/config', { sensitive: { [t.dataset.sens]: t.dataset.v } }); return toast('Saved', { icon: 'check', ms: 1600 }); }
  } catch (err) { toast(err.message); }
}, true);

/* ================================================================== global events */
document.addEventListener('click', e => {
  const t = e.target;
  if (!t.closest('.arr-pop') && !t.closest('[data-arrival]')) closeArrPop();
  if (!t.closest('.dx-dd')) closeDropdown();
  if (!t.closest('.more-wrap')) closeMoreMenu();
  if (!t.closest('#nav-menu') || t.closest('a, button')) closeNavMenu();
  const actEl = t.closest('[data-action]');
  if (actEl) {
    const a = actEl.dataset.action;
    const row = actEl.closest('.row');
    switch (a) {
      case 'nav-menu': { e.stopPropagation(); const m = $('#nav-menu'), open = m.hidden; m.hidden = !open; actEl.setAttribute('aria-expanded', open); if (open) $('a[aria-current="page"], a', m)?.focus({ preventScroll: true }); return; }
      case 'search': return openSearch();
      case 'close-search': return closeSearch();
      case 'close-modal': return closeLayer(layers.modal);
      case 'close-sheet': return closeLayer(layers.sheet);
      case 'close-person': return closeLayer(layers.person);
      case 'upnext-play': return upNextPlay();
      case 'upnext-full': return upNextFull();
      case 'upnext-home': return upNextHome();
      case 'upnext-moment': return upNextMoment(actEl.dataset.t);
      case 'atlas-close': return atlasToast({ state: 'clear', id: ATL.id });
      case 'share-close': return closeShareSheet();
      case 'row-l': case 'row-r': {
        const sc = $('.scroller', row);
        return sc.scrollBy({ left: (a === 'row-l' ? -1 : 1) * sc.clientWidth * 0.85, behavior: reduced.matches ? 'auto' : 'smooth' });
      }
      case 'hero-prev': return heroGo(hero.idx - 1);
      case 'hero-next': return heroGo(hero.idx + 1);
      case 'hero-go': return heroGo(+actEl.dataset.n);
      case 'surprise': closeSurprise(); return surprise();
      case 'close-surprise': return closeSurprise();
      case 'surprise-go': { const id = actEl.dataset.id; closeSurprise(); return play(id, 0); }
      case 'reload': S.home = null; S.lib = {}; return route();
      case 'logout': return logout(actEl);
    }
  }
  const ra = t.closest('[data-remote-addr]');
  if (ra) { REM.idx = +ra.dataset.remoteAddr; return drawRemote(); }
  if (t.closest('[data-action="remote-reset"]')) return resetRemote(t.closest('[data-action="remote-reset"]'));
  const mob = t.closest('[data-action^="mobile-"]');
  if (mob) return mobileAction(mob);
  const q = t.closest('[data-q]');
  if (q) { const inp = $('#search-input'); inp.value = q.dataset.q; runSearch(q.dataset.q); return; }

  const seg = t.closest('[data-seg]');
  if (seg) {
    const key = seg.dataset.seg, v = seg.dataset.v;
    $$(`[data-seg="${key}"]`).forEach(b => b.setAttribute('aria-pressed', b === seg));
    if (key === 'theme') applyTheme(v);
    if (key === 'ai_provider') { $('#ai-fields').hidden = v !== 'custom'; }
    const saved = saveSetting({ [key]: 'num' in seg.dataset ? Number(v) : 'bool' in seg.dataset ? v === 'true' : v });
    if (key === 'mobile_bind') saved.then(() => setTimeout(() => loadRemote(true), 3500));  // the server moves to the new addresses
    return saved;
  }
  const swt = t.closest('[data-switch]');
  if (swt) {
    const on = swt.getAttribute('aria-checked') !== 'true';
    swt.setAttribute('aria-checked', on);
    const done = saveSetting(swt.dataset.switch === 'mobile' ? { mobile: on, remote: on } : { [swt.dataset.switch]: on });  // Mobile also replaces the old remote
    if (swt.dataset.switch === 'mobile') { if (!on) $('#remote-box').innerHTML = ''; else done.then(() => setTimeout(loadRemote, 600)); }
    if (swt.dataset.switch === 'mylist_sync_favorites') done.then(() => loadMyList(true));
    return;
  }

  const el = t.closest('[data-act]');
  if (!el) return;
  const act = el.dataset.act;
  const holder = el.closest('[data-id]');
  const id = el.dataset.id || (holder && holder.dataset.id);
  switch (act) {
    case 'arrival': e.stopPropagation(); return arrivalPop(el);
    case 'arrival-play': return playArrival(el.dataset.key);
    case 'open': closePop(); closeLayer(layers.person); return openDetails(id, { from: el.closest('.card') });
    case 'info': e.stopPropagation(); return openDetails(id, { from: el.closest('.card') });
    case 'ml': e.stopPropagation(); e.preventDefault(); return toggleMyList(el.dataset.mlKey, el);
    case 'ml-remove': e.stopPropagation(); return toggleMyList(el.dataset.mlKey, el, false);
    case 'tmdb-request': e.stopPropagation(); return openRequest(el.dataset.tmdb);
    case 'person': e.stopPropagation(); return openPerson(el.dataset.person, el.dataset.name);
    case 'dx-mute': return toggleMute();
    case 'dx-replay': return replayHero();
    case 'dx-trailer': return playTrailer();
    case 'video': return playTrailer(el.dataset.key);
    case 'scene': return play(el.dataset.id, +el.dataset.t);
    case 'more': { e.stopPropagation(); const p = el.previousElementSibling; const open = p.classList.toggle('open'); el.textContent = open ? 'Less' : 'More'; return; }
    case 'dd': { e.stopPropagation(); const m = el.nextElementSibling; m.hidden = !m.hidden; el.setAttribute('aria-expanded', !m.hidden); if (!m.hidden) $('[aria-selected="true"]', m)?.focus(); return; }
    case 'share': e.stopPropagation(); return shareItem(el.dataset.id, el.dataset.name);
    case 'share-copy': return shareCopy(el.dataset.sid);
    case 'share-stop': return shareStop(el.dataset.sid, el);
    case 'share-delete': return shareDelete(el.dataset.sid, el);
    case 'share-connect': return shareConnect(el);
    case 'share-disconnect': return shareDisconnect(el);
    case 'share-exp': return shareExpiry(el.dataset.sid, +el.dataset.v);
    case 'play': return play(id);
    case 'play-item': return playItem(id);
    case 'play-ep': return play(id);
    case 'resume': return play(id);
    case 'restart': return play(id, 0);
    case 'watched': e.stopPropagation(); return toggleWatched(id, el);
    case 'season': return loadSeason(el.dataset.season);
    case 'tmdb': closeLayer(layers.person); return openTmdb((el.closest('[data-tmdb]') || el).dataset.tmdb, { from: el.closest('.card') });
    case 'open-jf': closeLayer(layers.sheet); return openDetails(id);
    case 'send-request': return sendRequest(el);
    case 'r-smode': R.seasonMode = el.dataset.v; R.q && R.q.setSeasons(rSeasons()); return drawRequestBody();
    case 'r-season': { const n = +el.dataset.v; R.picked.has(n) ? R.picked.delete(n) : R.picked.add(n); R.q && R.q.setSeasons(rSeasons()); return drawRequestBody(); }
    case 'more-menu': { e.stopPropagation(); return toggleMoreMenu(el); }
    case 'delete-local': { e.stopPropagation(); closeMoreMenu(); return deleteLocal(el.dataset.id, el.dataset.name, el.dataset.type); }
    case 'removed-forget': return forgetRemoved(el);
  }
});

/* Pop card body (not a button) opens details. */
$('#pop-layer').addEventListener('click', e => {
  if (e.target.closest('button')) return;
  const p = e.target.closest('.pop');
  if (p) { const id = p.dataset.id; closePop(); openDetails(id); }
});

document.addEventListener('keydown', e => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement && document.activeElement.tagName);
  if (UN.open && !typing) {
    if (e.key === 'Enter') { e.preventDefault(); return upNextPlay(); }
    if (e.key === 'Escape' || e.key === 'b' || e.key === 'B') { e.preventDefault(); return upNextFull(); }
  }
  if (e.key === 'Escape') {
    if (closeArrPop()) return;
    if (closeNavMenu()) return;
    if ($('.dx-dd-menu:not([hidden])')) return closeDropdown();
    if ($('.share-sheet.open')) return closeShareSheet();
    if ($('.surprise')) return closeSurprise();
    if (!layers.person.hidden) return closeLayer(layers.person);
    if (!layers.sheet.hidden) return closeLayer(layers.sheet);
    if (!layers.modal.hidden) return closeLayer(layers.modal);
    if (!layers.search.hidden) return closeSearch();
    return closePop();
  }
  if (e.key === '/' && !typing && S.state && S.state.server_ok) { e.preventDefault(); return openSearch(); }
  /* Title page: arrows walk the episode list, Enter plays the focused one. */
  if (!typing && !layers.modal.hidden && layers.person.hidden && layers.sheet.hidden && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
    const eps = $$('#dx-eps .epi[tabindex]');
    const cur = document.activeElement && document.activeElement.closest && document.activeElement.closest('.epi');
    if (eps.length && (cur || e.key === 'ArrowDown' && !document.activeElement.closest('.dx-dd'))) {
      let k = cur ? eps.indexOf(cur) + (e.key === 'ArrowDown' ? 1 : -1) : Math.max(0, eps.findIndex(x => x.classList.contains('is-next')));
      if (k >= 0 && k < eps.length) { e.preventDefault(); eps[k].focus({ preventScroll: true }); eps[k].scrollIntoView({ block: 'center', behavior: reduced.matches ? 'auto' : 'smooth' }); return; }
    }
  }
  if (!typing && !layers.modal.hidden && document.activeElement && document.activeElement.closest && document.activeElement.closest('.dx-dd-menu') && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
    const opts = $$('.dx-dd-menu button'), k = opts.indexOf(document.activeElement) + (e.key === 'ArrowDown' ? 1 : -1);
    if (opts[k]) { e.preventDefault(); opts[k].focus(); }
    return;
  }
  const card = document.activeElement && document.activeElement.closest && document.activeElement.closest('.card, .epi, .mine-item');
  if (!typing && card && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); card.click(); return; }
  if (!typing && card && (e.key === 'ArrowRight' || e.key === 'ArrowLeft')) {
    const sib = e.key === 'ArrowRight' ? card.nextElementSibling : card.previousElementSibling;
    if (sib && sib.matches('.card')) { e.preventDefault(); sib.focus({ preventScroll: true }); sib.scrollIntoView({ inline: 'nearest', block: 'nearest', behavior: reduced.matches ? 'auto' : 'smooth' }); }
  }
});

/* Row arrows follow scroll position. */
let arrowRaf = 0;
document.addEventListener('scroll', e => {
  const sc = e.target;
  if (!sc.classList || !sc.classList.contains('scroller')) return;
  cancelAnimationFrame(arrowRaf);
  arrowRaf = requestAnimationFrame(() => updateArrows(sc.closest('.row')));
}, { capture: true, passive: true });
addEventListener('resize', () => $$('.row.in').forEach(updateArrows));

async function logout(btn) {
  if (!btn.dataset.sure) { btn.dataset.sure = '1'; btn.textContent = 'Sure? Sign out'; setTimeout(() => { if (document.contains(btn)) { delete btn.dataset.sure; btn.textContent = 'Sign out'; } }, 3500); return; }
  try { await api('/api/logout', {}); } catch (e) { return toast(e.message); }
  S.home = null; S.lib = {}; S.items.clear();
  toast('Signed out. See you soon!', { icon: 'logout' });
  boot();
}

function setUser(name) {
  S.user = name || '';
  $('#user-name').textContent = S.user || 'You';
  $('#user-avatar').textContent = (S.user || 'Y')[0].toUpperCase();
}

/* ================================================================== offline (local-first) */
/* When Jellyfin can't be reached Marquee keeps playing from this PC; a calm pill says so. */
let offlinePoll = 0;
function setOffline(st) {
  const pill = $('#offline-pill');
  const off = !!(st && st.offline);
  if (pill) {
    pill.hidden = !off;
    if (off && st.since) {
      const d = new Date(typeof st.since === 'number' && st.since < 1e12 ? st.since * 1000 : st.since);
      if (!isNaN(d)) pill.title = `Jellyfin hasn't answered since ${d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}. Downloaded titles still play.`;
    }
  }
  clearInterval(offlinePoll);
  offlinePoll = setInterval(async () => {
    if (document.hidden) return;
    try {
      const s2 = await api('/api/state');
      const was = !!(S.state && S.state.offline);
      S.state = { ...S.state, ...s2 };
      if (pill) pill.hidden = !s2.offline;
      if (was && !s2.offline) { toast('Jellyfin is back', { icon: 'check' }); scheduleLive(300); }
    } catch (e) { /* the page's own server is local; ignore blips */ }
  }, 30000);
}

/* ================================================================== bridge for the chat's actions (chat.js)
   "pull up Dune" opens the same details page; "play ..." is the same as pressing Play; deleting only ever opens the
   hold-to-delete sheet; requests open the request sheet or go through MarqueeStorage.sendRequest after a tap. */
window.MarqueeUI = {
  open: id => openDetails(id),
  openTmdb: key => openTmdb(key),
  request: key => openRequest(key),
  play: (id, label) => play(id, undefined, label),
  playItem: async id => { if (!S.items.get(id)) { try { reg((await api(`/api/item/${encodeURIComponent(id)}`)).item); } catch (e) { /* plays anyway */ } } return playItem(id); },
  deleteLocal: (id, name, type) => deleteLocal(id, name, type),
  mylistChanged: () => loadMyList(true).then(() => { S.home = null; }),
  watchedChanged: id => { reg({ id, played: true }); S.home = null; S.lib = {}; },
  requested: key => { const c = S.tmdb.get(key); if (c) c.status = 2; S.home = null; },
  confetti: (x, y) => confetti(x, y, 34),
};

/* ================================================================== boot */
async function boot() {
  let st;
  try { st = await api('/api/state'); }
  catch (e) {
    $('#view').innerHTML = `<div class="page">${errorBox(e)}</div>`;
    return;
  }
  S.state = st;
  S.settings = st.settings || {};
  S.ai = st.ai || { on: false };
  applyTheme(S.settings.theme);
  setOffline(st);
  if (!st.server_ok && !st.offline) return renderSignin();
  setUser(S.settings.user_name);
  $('#topbar').hidden = false;
  await loadMyList(true);
  fetch('/api/share/status').then(r => { S.shareOn = r.ok; }, () => { S.shareOn = false; });
  S.renderSeq++;
  $('#view').replaceChildren();
  route();
  window.MarqueeLaunchpad && window.MarqueeLaunchpad.maybeShow();
}
boot();
})();
