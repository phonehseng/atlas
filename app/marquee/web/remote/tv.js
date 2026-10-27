/* Atlas on a TV (/tv): the library and player for a TV remote, served by Atlas Mobile.
   Only five keys matter: the arrows move a focus ring between things (rows scroll sideways, pages slide up), OK picks,
   Back goes back. The focus engine below never leaves the ring on nothing: rows remember where you were, grids move
   by index, and anything that disappears hands the ring to the nearest sensible place.
   In the Android TV app, window.AtlasNative is there (keep the screen on, change PC) and the app asks atlasBack()
   what Back should do. */
(() => {
  'use strict';
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const NATIVE = window.AtlasNative || null;
  const remPx = () => parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
  const fmt = (s) => {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = String(s % 60).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${x}` : `${m}:${x}`;
  };
  const mins = (s) => { s = Math.round((s || 0) / 60); return s >= 60 ? `${Math.floor(s / 60)} h${s % 60 ? ' ' + (s % 60) + ' min' : ''}` : `${s} min`; };
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* storage off */ } },
  };
  const prefs = Object.assign({ quality: 'auto', autoplay: true }, store.get('atlas-tv', {}));
  const savePrefs = () => store.set('atlas-tv', prefs);
  const native = (fn, ...a) => { try { if (NATIVE && typeof NATIVE[fn] === 'function') return NATIVE[fn](...a); } catch (e) { /* old app */ } return undefined; };

  let toastTimer;
  function toast(msg, ms = 2600) {
    const el = $('#toast');
    el.textContent = msg;
    el.classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('on'), ms);
  }

  function loadImg(img, src, fallback) {
    if (!img) return;
    if (!src) { if (fallback) img.insertAdjacentHTML('afterend', `<span class="fallback">${esc(fallback)}</span>`); return; }
    img.decoding = 'async';
    img.onload = () => img.classList.add('ok');
    img.onerror = () => { if (fallback && !img.parentElement.querySelector('.fallback')) img.insertAdjacentHTML('afterend', `<span class="fallback">${esc(fallback)}</span>`); };
    img.src = src;
  }

  // ---------------------------------------------------------------- API
  let me = null;
  async function api(path, body, opts = {}) {
    const init = { headers: { 'X-Marquee': '1', ...(opts.headers || {}) }, credentials: 'same-origin', keepalive: !!opts.keepalive };
    if (body !== undefined) { init.method = 'POST'; init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    let r;
    try { r = await fetch('/r/api/' + path, init); } catch (e) { throw new Error('Can’t reach your PC'); }
    let data = {};
    try { data = await r.json(); } catch (e) { /* empty */ }
    if (r.status === 401 && !opts.noPair) { showPair(); throw Object.assign(new Error(data.message || 'Not paired'), { status: 401 }); }
    if (!r.ok || data.ok === false) throw Object.assign(new Error(data.message || `HTTP ${r.status}`), { status: r.status, data });
    return data;
  }

  // ================================================================ focus engine
  const F = { cur: null, mem: new WeakMap() };
  const visible = (el) => !!el && el.isConnected && !el.disabled && el.getClientRects().length > 0 && !el.closest('[hidden]');
  const focusables = (root) => (root ? $$('.f', root).filter(visible) : []);
  const groupOf = (el) => el && el.closest('[data-h],[data-grid],[data-vl],[data-group]');

  function layer() {
    if (!$('#player').hidden) {
      if (!$('#p-err').hidden) return $('#p-err');
      if (!$('#p-panel').hidden) return $('#p-panel');
      return null;   // the player handles its own keys
    }
    if (!$('#pair').hidden) return $('#pair');
    return $('#app');
  }

  function focus(el) {
    if (!el) return;
    if (F.cur && F.cur !== el) F.cur.classList.remove('focus');
    F.cur = el;
    el.classList.add('focus');
    try { el.focus({ preventScroll: true }); } catch (e) { /* not focusable */ }
    const g = groupOf(el);
    if (g) F.mem.set(g, el);
    reveal(el);
    const inRail = !!el.closest('#rail');
    $('#app').classList.toggle('rail-open', inRail);
    const v = topView();
    if (v && !inRail && v.el.contains(el)) { v.last = el; if (v.onFocus) v.onFocus(el); }
  }

  function ensureFocus() {
    const L = layer();
    if (!L) return;
    if (F.cur && visible(F.cur) && L.contains(F.cur)) return;
    focus(defaultFocus(L));
  }
  setInterval(ensureFocus, 700);

  function defaultFocus(L) {
    if (L.id === 'app') {
      const v = topView();
      if (v) {
        if (v.last && visible(v.last)) return v.last;
        const k = v.lastK && $(`[data-k="${CSS.escape(v.lastK)}"]`, v.el);
        if (k && visible(k)) return k;
        const f = v.first ? v.first() : null;
        if (f && visible(f)) return f;
        const any = focusables(v.el)[0];
        if (any) return any;
      }
      return $('#rail .ri.cur') || focusables($('#rail'))[0];
    }
    if (L.id === 'p-panel') return $('.opt.on', L) || focusables(L)[0];
    return focusables(L)[0];
  }

  function remembered(g) {
    const m = g && F.mem.get(g);
    return m && visible(m) && g.contains(m) ? m : null;
  }

  // where something will be once the rows and pages have finished sliding (keys can come faster than animations)
  function rectOf(el) {
    const r = el.getBoundingClientRect();
    let dx = 0, dy = 0;
    for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
      if (n.dataset && n.dataset.x != null && n.classList.contains('track')) dx += +n.dataset.x - currentShift(n).x;
      else if (n.classList && n.classList.contains('vstack') && n.style.transform) dy += targetY(n) - currentShift(n).y;
    }
    return { left: r.left + dx, right: r.right + dx, top: r.top + dy, bottom: r.bottom + dy, width: r.width, height: r.height };
  }
  function currentShift(n) {
    const t = getComputedStyle(n).transform;
    if (!t || t === 'none') return { x: 0, y: 0 };
    const m = t.match(/matrix\(([^)]+)\)/);
    if (!m) return { x: 0, y: 0 };
    const p = m[1].split(',').map(parseFloat);
    return { x: p[4] || 0, y: p[5] || 0 };
  }
  function targetY(n) { const m = /translateY\((-?[\d.]+)px\)/.exec(n.style.transform); return m ? parseFloat(m[1]) : 0; }

  function center(r) { return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; }
  function nearestX(list, x) {
    let best = null, bd = Infinity;
    for (const c of list) { const d = Math.abs(center(rectOf(c)).x - x); if (d < bd) { bd = d; best = c; } }
    return best;
  }

  function move(dir) {
    const el = F.cur;
    if (!el || !visible(el)) { ensureFocus(); return; }
    const horiz = dir === 'left' || dir === 'right';
    const step = dir === 'right' || dir === 'down' ? 1 : -1;
    let exclude = null;

    // the rail: right goes back to where you were in the page
    if (el.closest('#rail') && dir === 'right') { const v = topView(); focus((v && v.last && visible(v.last) && v.last) || defaultFocus($('#app'))); return; }
    if (el.closest('#rail') && dir === 'left') { bump(el); return; }

    // a row: left/right walk the row
    const h = el.parentElement && el.parentElement.closest('[data-h]');
    if (h && horiz) {
      const items = focusables(h).filter((x) => x.parentElement.closest('[data-h]') === h);
      const n = items[items.indexOf(el) + step];
      if (n) { focus(n); return; }
      if (dir === 'right') { bump(el); return; }
      exclude = h;
    }
    // a grid: by index
    const g = el.closest('[data-grid]');
    if (g && !exclude) {
      const items = focusables(g);
      const i = items.indexOf(el);
      const cols = gridCols(items);
      let j = null;
      if (dir === 'left') j = i % cols ? i - 1 : null;
      else if (dir === 'right') { if ((i % cols) < cols - 1 && i + 1 < items.length) j = i + 1; else { bump(el); return; } }
      else if (dir === 'up') j = i - cols >= 0 ? i - cols : null;
      else {
        if (i + cols < items.length) j = i + cols;
        else if (Math.floor(i / cols) < Math.floor((items.length - 1) / cols)) j = items.length - 1;
        else { if (g._more && g._more()) { setTimeout(() => move('down'), 30); } else bump(el); return; }
      }
      if (j != null) {
        focus(items[j]);
        if (g._more && j >= items.length - cols * 3) g._more();
        return;
      }
      exclude = g;
    }
    // a vertical list (menus, the rail)
    const vl = el.parentElement && el.parentElement.closest('[data-vl]');
    if (vl && !horiz && !exclude) {
      const items = focusables(vl).filter((x) => x.parentElement.closest('[data-vl]') === vl);
      const n = items[items.indexOf(el) + step];
      if (n) { focus(n); return; }
      if (vl.closest('#rail')) { bump(el); return; }
      exclude = vl;
    }
    // a stack of rows: up/down go to the next row, to the item you were on there
    const vs = el.closest('[data-v]');
    if (vs && !horiz && !exclude) {
      const rows = [...vs.children];
      let r = rows.findIndex((x) => x.contains(el));
      const x = center(rectOf(el)).x;
      for (r += step; r >= 0 && r < rows.length; r += step) {
        const items = focusables(rows[r]);
        if (!items.length) continue;
        const gr = items.map(groupOf).find(Boolean);
        focus(remembered(gr) || nearestX(items, x));
        return;
      }
      exclude = vs;
    }
    const n = geometric(el, dir, exclude);
    if (n) focus(n); else bump(el);
  }

  function gridCols(items) {
    if (items.length < 2) return 1;
    const top = items[0].offsetTop;
    let c = 1;
    while (c < items.length && items[c].offsetTop === top) c++;
    return c;
  }

  function geometric(el, dir, exclude) {
    const L = layer();
    const er = rectOf(el);
    const ec = center(er);
    const fromRail = !!el.closest('#rail');
    let best = null, bs = Infinity;
    for (const c of focusables(L)) {
      if (c === el || (exclude && exclude.contains(c))) continue;
      if (dir !== 'left' && !fromRail && c.closest('#rail')) continue;   // only Left opens the menu
      const r = rectOf(c);
      const cc = center(r);
      let primary, ortho;
      if (dir === 'right') { if (cc.x <= ec.x + 1 || r.left < er.left + 1) continue; primary = Math.max(0, r.left - er.right); ortho = gap(r.top, r.bottom, er.top, er.bottom); }
      else if (dir === 'left') { if (cc.x >= ec.x - 1 || r.right > er.right - 1) continue; primary = Math.max(0, er.left - r.right); ortho = gap(r.top, r.bottom, er.top, er.bottom); }
      else if (dir === 'down') { if (cc.y <= ec.y + 1 || r.top < er.top + 1) continue; primary = Math.max(0, r.top - er.bottom); ortho = gap(r.left, r.right, er.left, er.right); }
      else { if (cc.y >= ec.y - 1 || r.bottom > er.bottom - 1) continue; primary = Math.max(0, er.top - r.bottom); ortho = gap(r.left, r.right, er.left, er.right); }
      const s = primary + ortho * 2 + (ortho === 0 ? Math.abs((dir === 'left' || dir === 'right') ? cc.y - ec.y : cc.x - ec.x) * 0.05 : 0);
      if (s < bs) { bs = s; best = c; }
    }
    if (best) {
      const gr = groupOf(best);
      if (gr && gr !== groupOf(el) && !gr.hasAttribute('data-nomem')) return remembered(gr) || best;
    }
    return best;
  }
  const gap = (a1, a2, b1, b2) => (a2 < b1 ? b1 - a2 : b2 < a1 ? a1 - b2 : 0);

  function bump(el) {   // nothing that way: a small nudge so it doesn't feel dead
    try { el.animate([{ translate: '0 0' }, { translate: '.4rem 0' }, { translate: '0 0' }], { duration: 160 }); } catch (e) { /* old WebView */ }
  }

  // keep the focused thing on screen: rows slide sideways, pages slide up
  function reveal(el) {
    const tr = el.parentElement && el.parentElement.closest('.track');
    if (tr) {
      const wrap = tr.parentElement;
      const base = wrap.getBoundingClientRect().left + (parseFloat(getComputedStyle(wrap).paddingLeft) || 0);   // the track's left, unscrolled
      const pl = parseFloat(getComputedStyle(tr).paddingLeft) || 0;
      const avail = window.innerWidth - 6 * remPx() - base;
      let x = +tr.dataset.x || 0;
      const left = el.offsetLeft, right = left + el.offsetWidth;
      if (left + x < pl) x = pl - left;
      else if (right + x > avail) x = avail - right;
      x = Math.min(0, Math.round(x));
      tr.dataset.x = x;
      tr.style.transform = `translateX(${x}px)`;
    }
    const st = el.closest('.vstack');
    if (st) {
      const vs = el.closest('[data-v]');
      const row = vs && vs.closest('.vstack') === st ? [...vs.children].find((x) => x.contains(el)) : el;
      let top = 0;
      for (let n = row; n && n !== st; n = n.offsetParent) top += n.offsetTop;
      const y = Math.min(0, Math.round(parseFloat((row && row.dataset && row.dataset.anchor) || st.dataset.anchor || '0') * remPx() - top));
      st.style.transform = `translateY(${y}px)`;
    }
    for (const sc of [el.closest('.view'), $('#views')]) if (sc && (sc.scrollTop || sc.scrollLeft)) { sc.scrollTop = 0; sc.scrollLeft = 0; }
  }

  function activate(el) {
    if (!el) return;
    if (el._act) el._act(); else el.click();
  }

  // ================================================================ views
  const V = [];   // the view stack: [{name, el, last, onFocus, first, refresh}]
  const topView = () => V[V.length - 1];
  const BUILD = {};

  async function go(name, params = {}, opts = {}) {
    const cur = topView();
    if (cur) { cur.lastK = cur.last && cur.last.dataset.k; cur.el.hidden = true; }
    if (opts.root) {
      while (V.length && !(V.length === 1 && V[0].name === 'home' && name !== 'home')) { const v = V.pop(); v.el.remove(); }
    }
    if (V.length > 14) { const v = V.splice(1, 1)[0]; v.el.remove(); }
    const el = document.createElement('div');
    el.className = 'view ' + name;
    $('#views').appendChild(el);
    const v = { name, params, el, last: null };
    V.push(v);
    railCurrent();
    el.innerHTML = '<p class="loading">Loading…</p>';
    try { await BUILD[name](v, params); } catch (e) { if (e.status !== 401) el.innerHTML = `<p class="loading">${esc(e.message)}</p>`; }
    if (topView() === v && $('#player').hidden) focus(defaultFocus($('#app')));
  }

  function railCurrent() {
    const root = V.length > 1 ? V[1] : V[0];
    let pick = !root ? 'home' : root.name === 'lib' ? root.params.kind : root.name;
    if (!$(`#rail .ri[data-go="${pick}"]`)) pick = 'home';
    $$('#rail .ri').forEach((b) => b.classList.toggle('cur', b.dataset.go === pick));
    const cur = $('#rail .ri.cur');
    if (cur) F.mem.set($('#rail [data-vl]'), cur);
  }

  function back() {
    if (!$('#pair').hidden) return false;
    if (F.cur && F.cur.closest('#rail')) {
      if (V.length > 1) { go('home', {}, { root: true }); return true; }
      return false;
    }
    if (V.length > 1) {
      const v = V.pop();
      v.el.remove();
      const t = topView();
      t.el.hidden = false;
      railCurrent();
      if (t.dirty && t.refresh) { t.dirty = false; t.refresh(); }
      focus(defaultFocus($('#app')));
      return true;
    }
    const v = topView();
    const first = v && v.first && v.first();
    if (first && F.cur !== first) { focus(first); return true; }
    return false;
  }

  function after() {   // something played: lists and progress changed
    for (const v of V) v.dirty = true;
    const t = topView();
    if (t && t.refresh) { t.dirty = false; t.refresh(); }
  }

  // rail
  $$('#rail .ri').forEach((b) => {
    b._act = () => {
      const g = b.dataset.go;
      if (g === 'home') go('home', {}, { root: true });
      else if (['movies', 'shows', 'anime'].includes(g)) go('lib', { kind: g }, { root: true });
      else go(g, {}, { root: true });
    };
  });

  // ---------------------------------------------------------------- shared pieces
  const cards = new Map();   // id -> card (billboards read from here)

  function card(c, opts = {}) {
    cards.set(c.id, c);
    const b = document.createElement('button');
    const wide = !!opts.wide;
    b.className = 'f card' + (wide ? ' wide' : '') + (opts.ep ? ' ep' : '') + (c.available === false ? ' na' : '');
    b.dataset.id = c.id;
    b.dataset.k = (opts.key || 'c') + ':' + c.id;
    const img = wide ? (c.thumb || c.backdrop || c.poster) : (c.poster || c.thumb);
    const prog = c.progress > 0.01 && !c.played;
    const title = c.type === 'Episode' && !opts.ep ? (c.series || c.name) : c.name;
    const sub = opts.ep ? [c.runtime ? mins(c.runtime) : '', c.played ? 'Watched' : prog ? `${mins((c.runtime || 0) - (c.position || 0))} left` : ''].filter(Boolean).join(' · ')
      : c.type === 'Episode' ? `S${c.season ?? 0}:E${c.episode ?? 0} · ${c.name || ''}` : [c.year, c.type === 'Series' ? 'Series' : ''].filter(Boolean).join(' · ');
    b.innerHTML = `<div class="art"><img alt="">${opts.ep && c.episode != null ? `<span class="num">${esc(c.episode)}</span>` : ''}${prog ? `<div class="pbar"><i></i></div>` : ''}${c.played && !opts.ep ? '<span class="tick"><svg><use href="#i-check"/></svg></span>' : ''}</div>${opts.cap === false ? '' : `<span class="cap"><b>${esc(opts.ep ? c.name : title)}</b>${esc(sub)}</span>`}`;
    loadImg($('img', b), img, title);
    if (prog) $('.pbar i', b).style.width = Math.round(c.progress * 100) + '%';
    b._act = opts.act || (() => openItem(c));
    return b;
  }

  function openItem(c) {
    if (c.available === false) { toast('That isn’t on your PC'); return; }
    if (c.type === 'Episode') { P.open(c.id); return; }
    go('title', { id: c.id });
  }

  function row(key, title, items, opts = {}) {
    const r = document.createElement('section');
    r.className = 'row';
    r.dataset.key = key;
    r.innerHTML = `<h2>${esc(title)}</h2><div class="track-wrap"><div class="track" data-h></div></div>`;
    const tr = $('.track', r);
    tr.append(...items.map((c) => card(c, { ...opts, key })));
    return r;
  }

  // backdrop: two layers that cross-fade
  let bgWant = null, bgTimer = null;
  function setBg(url, dim) {
    document.body.classList.toggle('dim', !!dim);
    if (url === bgWant) return;
    bgWant = url;
    clearTimeout(bgTimer);
    bgTimer = setTimeout(() => {
      const a = $('#bg-a'), b = $('#bg-b');
      const on = a.classList.contains('on') ? a : b, off = on === a ? b : a;
      if (!url) { on.classList.remove('on'); return; }
      if (on.dataset.src === url && on.classList.contains('on')) return;
      off.onload = () => { if (bgWant !== url) return; off.classList.add('on'); on.classList.remove('on'); };
      off.dataset.src = url;
      off.src = url;
    }, 180);
  }

  function metaLine(c) {
    const bits = [];
    if (c.year) bits.push(`<span>${esc(c.year)}</span>`);
    if (c.age) bits.push(`<span class="pill">${esc(c.age)}</span>`);
    if (c.type === 'Series') { const n = (c.seasons || []).filter((s) => s.number).length; if (n) bits.push(`<span>${n} season${n > 1 ? 's' : ''}</span>`); else bits.push('<span>Series</span>'); }
    else if (c.runtime) bits.push(`<span>${mins(c.runtime)}</span>`);
    if (c.rating) bits.push(`<span>★ ${esc(Number(c.rating).toFixed(1))}</span>`);
    if (c.quality) bits.push(`<span class="q">${esc(c.quality)}</span>`);
    if (c.genres && c.genres.length) bits.push(`<span>${esc(c.genres.slice(0, 3).join(' · '))}</span>`);
    return bits.join('');
  }

  // ---------------------------------------------------------------- Home
  BUILD.home = async (v) => {
    const d = await api('home');
    const el = v.el;
    el.innerHTML = `<section class="billboard"><div class="bb-info"></div><div class="btns" data-h data-group="hero"></div></section><div class="vstack rows" data-anchor="0"><div class="rowlist" data-v></div></div>`;
    const bb = $('.billboard', el), info = $('.bb-info', bb), btns = $('.btns', bb), stack = $('.rows', el), list = $('.rowlist', el);
    const hero = (d.hero || []).filter(Boolean);
    let heroIdx = 0, heroTimer = null;
    for (const r of d.rows || []) list.appendChild(row(r.key, r.title, r.items, { wide: r.style === 'wide', cap: r.style === 'wide' }));
    if (!list.children.length) list.innerHTML = '<section class="row"><p class="empty indent">Nothing in the library on your PC yet.</p></section>';

    function showInfo(c, withButtons) {
      if (!c) return;
      info.innerHTML = `${c.logo ? `<img class="bb-logo" alt="">` : `<h1 class="bb-title">${esc(c.type === 'Episode' ? c.series || c.name : c.name)}</h1>`}
        <p class="meta">${c.type === 'Episode' ? `<span>S${c.season ?? 0}:E${c.episode ?? 0} · ${esc(c.name || '')}</span>` : metaLine(c)}</p>
        ${c.overview ? `<p class="overview">${esc(c.overview)}</p>` : ''}`;
      if (c.logo) { const im = $('.bb-logo', info); im.onerror = () => { im.outerHTML = `<h1 class="bb-title">${esc(c.series || c.name)}</h1>`; }; im.src = c.logo; }
      btns.hidden = !withButtons;
      setBg(c.backdrop || c.thumb);
    }
    function heroButtons() {
      const c = hero[heroIdx];
      btns.innerHTML = '';
      if (!c) return;
      const play = Object.assign(document.createElement('button'), { className: 'f btn primary', innerHTML: `<svg><use href="#i-play"/></svg>${c.position > 30 && c.progress < 0.95 ? 'Resume' : 'Play'}` });
      play.dataset.k = 'hero:play';
      play._act = () => playTitle(c);
      const more = Object.assign(document.createElement('button'), { className: 'f btn', innerHTML: '<svg><use href="#i-info"/></svg>More info' });
      more.dataset.k = 'hero:more';
      more._act = () => go('title', { id: c.id });
      btns.append(play, more);
    }
    function heroRotate(on) {
      clearInterval(heroTimer);
      if (!on || hero.length < 2) return;
      heroTimer = setInterval(() => {
        if (topView() !== v || !$('#player').hidden || !F.cur || !F.cur.closest('.billboard')) return;
        const k = F.cur.dataset.k;
        heroIdx = (heroIdx + 1) % hero.length;
        heroButtons();
        showInfo(hero[heroIdx], true);
        focus($(`[data-k="${k}"]`, btns) || $('.btn', btns));
      }, 14000);
    }
    if (hero.length) { heroButtons(); showInfo(hero[0], true); } else btns.hidden = true;

    let infoTimer = null;
    v.onFocus = (f) => {
      const inRows = list.contains(f);
      const rows = [...list.children];
      const at = rows.findIndex((r) => r.contains(f));
      rows.forEach((r, i) => r.classList.toggle('gone', inRows && i < at));
      if (!inRows) {
        stack.style.transform = `translateY(${Math.round(9 * remPx())}px)`;
        clearTimeout(infoTimer);
        if (hero.length) showInfo(hero[heroIdx], true);
        heroRotate(true);
        return;
      }
      heroRotate(false);
      clearTimeout(infoTimer);
      btns.hidden = true;
      infoTimer = setTimeout(() => showInfo(cards.get(f.dataset.id), false), 120);
    };
    v.first = () => $('.btn', btns) || focusables(list)[0];
    v.refresh = async () => {
      let nd;
      try { nd = await api('home'); } catch (e) { return; }
      if (!v.el.isConnected) return;
      const k = F.cur && v.el.contains(F.cur) ? F.cur.dataset.k : v.lastK;
      const rowKey = F.cur && F.cur.closest('.row') ? F.cur.closest('.row').dataset.key : null;
      list.innerHTML = '';
      for (const r of nd.rows || []) list.appendChild(row(r.key, r.title, r.items, { wide: r.style === 'wide', cap: r.style === 'wide' }));
      const again = (k && $(`[data-k="${CSS.escape(k)}"]`, el)) || (rowKey && focusables($(`.row[data-key="${rowKey}"]`, list) || list)[0]);
      if (topView() === v && $('#player').hidden) focus(again && visible(again) ? again : v.first());
    };
  };

  async function playTitle(c) {
    if (c.type !== 'Series') { P.open(c.id); return; }
    try {
      const d = await api('item/' + encodeURIComponent(c.id));
      if (d.item.next_up) P.open(d.item.next_up.id); else go('title', { id: c.id });
    } catch (e) { toast(e.message); }
  }

  // ---------------------------------------------------------------- a title
  BUILD.title = async (v, { id }) => {
    const d = (await api('item/' + encodeURIComponent(id))).item;
    cards.set(d.id, d);
    const el = v.el;
    const isSeries = d.type === 'Series';
    const target = isSeries ? d.next_up : d;
    const resume = target && target.position > 30 && target.progress < 0.95;
    const cast = (d.people || []).filter((p) => p.type === 'Actor').slice(0, 5).map((p) => p.name);
    const dir = (d.people || []).filter((p) => p.type === 'Director').map((p) => p.name).slice(0, 2);
    el.innerHTML = `<div class="vstack" data-anchor="4"><div class="secs" data-v>
      <section class="t-head">
        ${d.logo ? '<img class="bb-logo" alt="">' : `<h1 class="bb-title">${esc(d.name)}</h1>`}
        <p class="meta">${metaLine(d)}</p>
        ${d.tagline ? `<p class="tagline">${esc(d.tagline)}</p>` : ''}
        ${d.overview ? `<p class="overview">${esc(d.overview)}</p>` : ''}
        ${cast.length || dir.length ? `<p class="people">${cast.length ? 'Starring ' + esc(cast.join(', ')) : ''}${dir.length ? `${cast.length ? ' · ' : ''}Directed by ${esc(dir.join(', '))}` : ''}</p>` : ''}
        ${resume ? `<div class="prog"><div class="bar"><i></i></div><span>${isSeries ? `S${target.season ?? 0}:E${target.episode ?? 0} · ` : ''}${mins((target.runtime || 0) - (target.position || 0))} left</span></div>` : ''}
        <div class="btns" data-h data-group="tbtns"></div>
      </section>
      ${isSeries ? '<section class="sec"><div class="track-wrap"><div class="chips track" data-h data-group="seasons"></div></div></section><section class="sec eps-sec" data-anchor="9"><div class="track-wrap"><div class="track" data-h data-group="eps"></div></div><p class="ep-desc"></p></section>' : ''}
      ${d.similar && d.similar.length ? '<section class="sec sim"></section>' : ''}
    </div></div>`;
    if (d.logo) { const im = $('.bb-logo', el); im.onerror = () => { im.outerHTML = `<h1 class="bb-title">${esc(d.name)}</h1>`; }; im.src = d.logo; }
    if (resume) $('.prog .bar i', el).style.width = Math.round(target.progress * 100) + '%';
    setBg(d.backdrop || d.thumb);
    const btns = $('.btns', el);
    const mk = (cls, icon, label, k, act) => {
      const b = document.createElement('button');
      b.className = 'f btn ' + cls;
      b.innerHTML = `<svg><use href="#${icon}"/></svg><span>${esc(label)}</span>`;
      b.dataset.k = k;
      b._act = act;
      btns.appendChild(b);
      return b;
    };
    if (target) {
      const lbl = resume ? 'Resume' : isSeries ? (target.season != null ? `Play S${target.season}:E${target.episode}` : 'Play') : 'Play';
      mk('primary', 'i-play', lbl, 't:play', () => P.open(target.id));
      if (resume) mk('', 'i-restart', 'From the start', 't:restart', () => P.open(target.id, { start: 0 }));
    } else if (!isSeries && d.available === false) {
      mk('', 'i-info', 'Not on your PC yet', 't:na', () => toast('That isn’t on your PC yet'));
    }
    if (d.mylist != null) {
      const ml = mk('', d.mylist ? 'i-check' : 'i-plus', 'My List', 't:list', async () => {
        try {
          await api('mylist', { id: d.id, on: !d.mylist });
          d.mylist = !d.mylist;
          $('use', ml).setAttribute('href', d.mylist ? '#i-check' : '#i-plus');
          toast(d.mylist ? 'Added to My List' : 'Removed from My List');
          for (const x of V) if (x.name === 'home' || x.name === 'mylist') x.dirty = true;
        } catch (e) { toast(e.message); }
      });
    }
    if (d.similar && d.similar.length) {
      const sim = row('sim', 'More like this', d.similar, {});
      sim.classList.remove('row');
      $('.sim', el).replaceWith(Object.assign(sim, { className: 'sec sim' }));
    }

    let seasonTimer = null, epSeq = 0;
    const desc = $('.ep-desc', el);
    async function loadSeason(s, focusEp) {
      const seq = ++epSeq;
      const tr = $('.eps-sec .track', el);
      let r;
      try { r = await api(`episodes/${encodeURIComponent(d.id)}/${encodeURIComponent(s.id)}`); } catch (e) { tr.innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
      if (seq !== epSeq || !el.isConnected) return;
      const hadFocus = F.cur && tr.contains(F.cur);
      tr.dataset.x = 0;
      tr.style.transform = '';
      tr.replaceChildren(...r.items.map((e) => card({ ...e, type: 'Episode', series: d.name }, { wide: true, ep: true, key: 'ep', act: () => openItem({ ...e, type: 'Episode' }) })));
      const want = (focusEp && $(`[data-id="${CSS.escape(focusEp)}"]`, tr)) || null;
      if (want) F.mem.set(tr, want);
      if (hadFocus) focus(want || focusables(tr)[0]);
      else if (want) { reveal(want); F.mem.set(tr, want); }
      const f = remembered(tr) || focusables(tr)[0];
      if (f) epDesc(f);
    }
    function epDesc(f) {
      const c = cards.get(f.dataset.id);
      if (desc && c) desc.textContent = c.overview || '';
    }
    if (isSeries) {
      const chips = $('.chips', el);
      const list = d.seasons || [];
      const pick = list.find((s) => target && s.number === target.season) || list.find((s) => s.number) || list[0];
      for (const s of list) {
        const b = document.createElement('button');
        b.className = 'f chip' + (s === pick ? ' on' : '');
        b.textContent = s.number === 0 ? 'Specials' : (s.name || `Season ${s.number}`);
        b.dataset.k = 'season:' + s.id;
        b._act = () => { const t = focusables($('.eps-sec .track', el)); if (t.length) focus(remembered($('.eps-sec .track', el)) || t[0]); };
        b._season = s;
        chips.appendChild(b);
      }
      if (pick) { F.mem.set(chips, $('.chip.on', chips)); loadSeason(pick, target && target.id); } else $('.eps-sec .track', el).innerHTML = '<p class="empty">No episodes on your PC yet.</p>';
    }
    v.onFocus = (f) => {
      if (f.classList.contains('chip') && f._season) {
        clearTimeout(seasonTimer);
        if (!f.classList.contains('on')) {
          seasonTimer = setTimeout(() => {
            $$('.chip', el).forEach((x) => x.classList.toggle('on', x === f));
            loadSeason(f._season);
          }, 300);
        }
      }
      if (f.closest('.eps-sec')) epDesc(f);
    };
    v.first = () => $('.btn', btns) || focusables(el)[0];
    v.refresh = async () => {
      const k = v.last && v.last.dataset.k;
      const keep = v.last;
      try { await BUILD.title(Object.assign(v, { last: null }), { id }); } catch (e) { return; }
      v.lastK = k;
      if (topView() === v && $('#player').hidden) {
        const again = k && $(`[data-k="${CSS.escape(k)}"]`, el);
        focus(again && visible(again) ? again : v.first());
      } else if (keep && !keep.isConnected) v.last = null;
    };
  };

  // ---------------------------------------------------------------- grids: libraries, My List
  function grid(v, title, sub, items, opts = {}) {
    const el = v.el;
    el.innerHTML = `<div class="grid-fade"></div><header class="page-head"><h1>${esc(title)}</h1><p>${esc(sub)}</p></header><div class="vstack grid-stack" data-anchor="1"><div class="grid" data-grid></div></div>`;
    const g = $('.grid', el);
    let shown = 0;
    const CHUNK = 70;
    g._more = () => {
      if (shown >= items.length) return false;
      const next = items.slice(shown, shown + CHUNK);
      shown += next.length;
      g.append(...next.map((c) => card(c, { key: opts.key || 'g' })));
      return true;
    };
    g._more();
    if (!items.length) g.outerHTML = `<p class="empty">${esc(opts.empty || 'Nothing here yet.')}</p>`;
    v.first = () => focusables($('.grid', el) || el)[0];
    v.onFocus = (f) => { const c = cards.get(f.dataset.id); if (c) setBg(c.backdrop || c.thumb, true); };
    setBg(null, true);
  }

  BUILD.lib = async (v, { kind }) => {
    const d = await api('library/' + kind);
    grid(v, d.title, `${d.items.length} title${d.items.length === 1 ? '' : 's'}`, d.items, { key: kind });
  };

  BUILD.mylist = async (v) => {
    const d = await api('home');
    const r = (d.rows || []).find((x) => x.key === 'mylist');
    grid(v, 'My List', r ? `${r.items.length} title${r.items.length === 1 ? '' : 's'}` : 'Add titles with the My List button on their page.', r ? r.items : [], { key: 'ml', empty: 'Nothing on My List yet.' });
    v.refresh = async () => { const k = v.last && v.last.dataset.k; await BUILD.mylist(v); const a = k && $(`[data-k="${CSS.escape(k)}"]`, v.el); if (topView() === v && $('#player').hidden) focus(a && visible(a) ? a : v.first() || $('#rail .ri.cur')); };
  };

  // ---------------------------------------------------------------- search
  BUILD.search = async (v) => {
    const el = v.el;
    const keys = 'abcdefghijklmnopqrstuvwxyz1234567890'.split('');
    el.innerHTML = `<div class="kb-box">
        <input class="f s-input" id="s-input" readonly placeholder="Search" data-k="s:input" autocomplete="off" spellcheck="false" enterkeyhint="search">
        <p class="s-tip"><svg><use href="#i-mic"/></svg>Press OK on the box for the TV keyboard or voice typing</p>
        <div class="kb">${keys.map((k) => `<button class="f key" data-key="${k}" data-k="k:${k}">${k}</button>`).join('')}
          <button class="f key w2" data-key=" " data-k="k:space" aria-label="Space"><svg><use href="#i-space"/></svg></button>
          <button class="f key w2" data-key="bk" data-k="k:bk" aria-label="Delete"><svg><use href="#i-bksp"/></svg></button>
          <button class="f key w2" data-key="clr" data-k="k:clr">Clear</button></div>
      </div>
      <div class="vstack grid-stack" data-anchor="1"><p class="empty">Find movies and shows on your PC.</p></div>`;
    const inp = $('#s-input', el), out = $('.grid-stack', el);
    let q = '', timer = null, seq = 0;
    const set = (nq) => { q = nq.slice(0, 60); inp.value = q; clearTimeout(timer); timer = setTimeout(run, 380); };
    async function run() {
      const my = ++seq;
      if (!q.trim()) { out.innerHTML = '<p class="empty">Find movies and shows on your PC.</p>'; return; }
      let d;
      try { d = await api('search?q=' + encodeURIComponent(q.trim())); } catch (e) { out.innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
      if (my !== seq || !el.isConnected) return;
      const hadFocus = F.cur && out.contains(F.cur);
      out.innerHTML = d.items.length ? '<div class="grid" data-grid></div>' : `<p class="empty">Nothing on your PC matches “${esc(q.trim())}”.</p>`;
      out.style.transform = '';
      if (d.items.length) $('.grid', out).append(...d.items.map((c) => card(c, { key: 's' })));
      if (hadFocus) focus(focusables(out)[0] || $('[data-key="a"]', el));
    }
    $$('.key', el).forEach((b) => {
      b._act = () => {
        const k = b.dataset.key;
        if (k === 'bk') set(q.slice(0, -1));
        else if (k === 'clr') set('');
        else set(q + k);
      };
    });
    inp._act = () => {   // the TV's own keyboard (with its microphone, where the TV has one)
      inp.readOnly = false;
      inp.focus();
      try { inp.setSelectionRange(inp.value.length, inp.value.length); } catch (e) { /* fine */ }
    };
    inp.addEventListener('input', () => set(inp.value));
    inp.addEventListener('blur', () => { inp.readOnly = true; });
    v.first = () => $('[data-key="a"]', el);
    v.onFocus = (f) => { const c = cards.get(f.dataset.id); setBg(c ? (c.backdrop || c.thumb) : null, true); };
    v.typeKey = (ch) => { if (ch === 'Backspace') set(q.slice(0, -1)); else set(q + ch.toLowerCase()); };
    setBg(null, true);
  };

  // ---------------------------------------------------------------- settings
  BUILD.settings = async (v) => {
    const el = v.el;
    if (!me) me = await api('me');
    const qs = [['auto', 'Auto'], ['original', 'Original'], ['1080', '1080p'], ['720', '720p'], ['480', '480p']];
    el.innerHTML = `<header class="page-head"><h1>Settings</h1><p>${esc(me.device ? me.device.name : 'This TV')} · paired with ${esc(me.pc || 'your PC')}</p></header>
      <div class="settings-box"><div class="secs" data-v>
        <div class="set-row"><div class="lbl"><b>Picture quality</b><span>Auto picks the best your network and TV can take.</span></div><div class="chips" data-h data-group="q">${qs.map(([k, n]) => `<button class="f chip${prefs.quality === k ? ' on' : ''}" data-q="${k}" data-k="q:${k}">${n}</button>`).join('')}</div></div>
        <div class="set-row"><div class="lbl"><b>Play the next episode by itself</b><span>When an episode ends.</span></div><div class="chips" data-h data-group="ap"><button class="f chip${prefs.autoplay ? ' on' : ''}" data-ap="1" data-k="ap:1">On</button><button class="f chip${prefs.autoplay ? '' : ' on'}" data-ap="0" data-k="ap:0">Off</button></div></div>
        ${NATIVE ? `<div class="set-row"><div class="lbl"><b>PC</b><span>${esc(location.host)}</span></div><div class="chips" data-h><button class="f chip" data-k="s:pc" id="set-pc">Change PC</button></div></div>` : ''}
        <div class="set-row"><div class="lbl"><b>Unpair this TV</b><span>It will need a new code to connect again. You can also remove it in Atlas’s Settings on your PC.</span></div><div class="chips" data-h><button class="f chip" data-k="s:out" id="set-out">Unpair</button></div></div>
      </div></div>`;
    $$('[data-q]', el).forEach((b) => { b._act = () => { prefs.quality = b.dataset.q; savePrefs(); $$('[data-q]', el).forEach((x) => x.classList.toggle('on', x === b)); }; });
    $$('[data-ap]', el).forEach((b) => { b._act = () => { prefs.autoplay = b.dataset.ap === '1'; savePrefs(); $$('[data-ap]', el).forEach((x) => x.classList.toggle('on', x === b)); }; });
    const pc = $('#set-pc', el);
    if (pc) pc._act = () => native('setup');
    const out = $('#set-out', el);
    out._act = async () => {
      if (!out.dataset.sure) { out.dataset.sure = '1'; out.textContent = 'Press OK again to unpair'; setTimeout(() => { if (out.isConnected) { delete out.dataset.sure; out.textContent = 'Unpair'; } }, 4000); return; }
      try { await api('signout', {}); } catch (e) { /* signed out anyway */ }
      showPair();
    };
    v.first = () => $('.chip.on', el) || focusables(el)[0];
    setBg(null, true);
  };

  // ================================================================ pairing
  const PAIR_KEYS = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'.split('');
  let pairPoll = null, pairRid = null, pairTyped = '';
  function showPair() {
    if (!$('#player').hidden) P.close(true);
    $('#app').hidden = true;
    $('#pair').hidden = false;
    setBg(null);
    pairTyped = '';
    drawSlots();
    $('#pair-err').textContent = '';
    if (!$('#pair-keys').children.length) {
      $('#pair-keys').innerHTML = PAIR_KEYS.map((k) => `<button class="f key" data-pk="${k}" data-k="pk:${k}">${k}</button>`).join('') +
        '<button class="f key w4" data-pk="bk" data-k="pk:bk" aria-label="Delete"><svg><use href="#i-bksp"/></svg></button><button class="f key w4" data-pk="go" data-k="pk:go">Pair</button>';
      $$('[data-pk]').forEach((b) => { b._act = () => pairKey(b.dataset.pk); });
    }
    newPairCode();
    focus($('[data-pk="A"]'));
  }
  function drawSlots() {
    $$('#pair-slots i').forEach((s, i) => { s.textContent = pairTyped[i] || ''; s.classList.toggle('cur', i === pairTyped.length); });
  }
  function pairKey(k) {
    $('#pair-err').textContent = '';
    if (k === 'bk') pairTyped = pairTyped.slice(0, -1);
    else if (k === 'go') { if (pairTyped.length === 6) redeem(pairTyped); else $('#pair-err').textContent = 'The code has 6 letters and numbers'; return; }
    else if (pairTyped.length < 6) pairTyped += k;
    drawSlots();
    if (pairTyped.length === 6) redeem(pairTyped);
  }
  async function redeem(code) {
    try {
      await api('tv/redeem', { code }, { noPair: true });
      paired();
    } catch (e) {
      $('#pair-err').textContent = e.message;
      pairTyped = '';
      drawSlots();
    }
  }
  async function newPairCode() {
    clearTimeout(pairPoll);
    $('#pair-code-hint').textContent = 'Getting a code…';
    let d;
    try { d = await api('tv/code', {}, { noPair: true }); } catch (e) {
      $('#pair-code').textContent = '· · ·';
      $('#pair-code-hint').textContent = e.message;
      pairPoll = setTimeout(newPairCode, 20000);
      return;
    }
    pairRid = d.rid;
    $('#pair-code').textContent = d.code.slice(0, 3) + ' ' + d.code.slice(3);
    $('#pair-code-hint').textContent = 'The code works for 10 minutes. A new one appears by itself.';
    const until = Date.now() + (d.expires_in - 5) * 1000;
    const poll = async () => {
      if ($('#pair').hidden) return;
      if (Date.now() > until) { newPairCode(); return; }
      try {
        const r = await api('tv/poll', { rid: pairRid }, { noPair: true });
        if (r.state === 'paired') { paired(); return; }
        if (r.state === 'expired') { newPairCode(); return; }
      } catch (e) { /* the PC is busy or away: keep asking */ }
      pairPoll = setTimeout(poll, 2000);
    };
    pairPoll = setTimeout(poll, 2000);
  }
  function paired() {
    clearTimeout(pairPoll);
    $('#pair').hidden = true;
    toast('Paired. Welcome to Atlas!');
    boot();
  }

  // ================================================================ player
  const P = (() => {
    const pl = $('#player'), v = $('#pv');
    const ORDER = ['original', '1080', '720', '480'];
    const metrics = (window.__tv = { ttff: [], seeks: [], events: [], mode: null });
    let S = null, hls = null, uiTimer = null, saveTimer = null, watchdog = null, nextTimer = null, nextCount = 0;
    let seekTarget = null, seekTimer = null, seekReps = 0, seekT0 = 0;

    // what this TV can play (same questions the phone app asks)
    let capsP = null;
    const useHlsJs = () => !!(window.Hls && window.Hls.isSupported());
    function caps() {
      if (capsP) return capsP;
      capsP = (async () => {
        const probe = document.createElement('video');
        const can = (t) => { const r = probe.canPlayType(t); return r === 'probably' || r === 'maybe'; };
        const MS = window.ManagedMediaSource || window.MediaSource;
        const mse = (t) => !!(MS && MS.isTypeSupported && MS.isTypeSupported(t));
        const viaHls = useHlsJs();
        const vid = (c) => (viaHls ? mse(`video/mp4; codecs="${c}"`) : false) || can(`video/mp4; codecs="${c}"`);
        const aud = (c) => (viaHls ? mse(`audio/mp4; codecs="${c}"`) : false) || can(`audio/mp4; codecs="${c}"`);
        const c = {
          h264: vid('avc1.640028'), hevc: vid('hvc1.1.6.L120.90') || vid('hev1.1.6.L120.90'), hevc10: vid('hvc1.2.4.L153.B0') || vid('hvc1.2.4.L120.90'),
          av1: vid('av01.0.08M.08'), vp9: vid('vp09.00.40.08'), mkv: can('video/x-matroska; codecs="avc1.640028"') || can('video/webm; codecs="avc1.640028"'),
          audio: { aac: aud('mp4a.40.2'), ac3: aud('ac-3'), eac3: aud('ec-3'), opus: aud('opus'), flac: aud('flac'), mp3: aud('mp4a.69') || aud('mp3') },
          hdr: false, max_height: Math.max(1080, Math.min(2160, Math.round(Math.max(screen.width, screen.height) * (window.devicePixelRatio || 1)) >= 3000 ? 2160 : 1080)),
          hls: viaHls ? 'hls.js' : 'native', tv: true,
        };
        const screenHdr = matchMedia('(dynamic-range: high)').matches;
        if (navigator.mediaCapabilities && navigator.mediaCapabilities.decodingInfo) {
          const type = viaHls ? 'media-source' : 'file';
          const ask = (video) => navigator.mediaCapabilities.decodingInfo({ type, video }).catch(() => null);
          const [hdr, hevc4k] = await Promise.all([
            c.hevc10 && screenHdr ? ask({ contentType: 'video/mp4; codecs="hvc1.2.4.L153.B0"', width: 3840, height: 2160, bitrate: 40e6, framerate: 24, transferFunction: 'pq', colorGamut: 'rec2020', hdrMetadataType: 'smpteSt2086' }) : null,
            c.hevc ? ask({ contentType: 'video/mp4; codecs="hvc1.1.6.L153.90"', width: 3840, height: 2160, bitrate: 40e6, framerate: 24 }) : null,
          ]);
          c.hdr = !!(hdr && hdr.supported && hdr.smooth);
          if (hevc4k && hevc4k.supported && hevc4k.smooth && screenHdr) c.max_height = 2160;   // a 4K TV that says so
        }
        return c;
      })();
      return capsP;
    }
    let bw = { bps: 0, at: 0 };
    async function measure() {
      if (bw.bps && Date.now() - bw.at < 180000) return bw.bps;
      const get = async (kb) => {
        const t = performance.now();
        const r = await fetch(`/r/api/speed?kb=${kb}&_=${Math.random()}`, { headers: { 'X-Marquee': '1' }, cache: 'no-store' });
        const b = await r.arrayBuffer();
        return { bytes: b.byteLength, secs: Math.max(0.001, (performance.now() - t) / 1000) };
      };
      try {
        const a = await get(256);
        let est = (a.bytes * 8) / a.secs;
        if (a.secs < 0.6) { const b = await get(est > 40e6 ? 4096 : 1536); est = (b.bytes * 8) / b.secs; }
        bw = { bps: est, at: Date.now() };
      } catch (e) { bw = { bps: 0, at: 0 }; }
      return bw.bps;
    }

    const isOpen = () => !pl.hidden;
    function open(id, o = {}) {
      if (S) { save(); teardown(true); }
      pl.hidden = false;
      $('#app').hidden = true;
      pl.classList.add('ui');
      spin(true);
      $('#p-title').textContent = '';
      $('#p-sub').textContent = '';
      $('#p-badge').textContent = '';
      $('#p-panel').hidden = true;
      $('#p-err').hidden = true;
      v.muted = !!window.__tvMuted;
      F.cur && F.cur.classList.remove('focus');
      load(id, o);
    }
    async function load(id, o) {
      S = { id, t0: performance.now(), quality: prefs.quality || 'auto', tries: 0, firstFrame: false, nextShown: false, cancelledNext: false };
      const me = S;
      let info;
      try { info = await api(`play/${encodeURIComponent(id)}/info`); } catch (e) { if (S === me) fail(e.message); return; }
      if (S !== me) return;
      S.info = info;
      S.audio = info.default_audio;
      S.sub = info.default_sub;
      $('#p-title').textContent = info.title || info.name || '';
      $('#p-sub').textContent = info.type === 'Episode' ? info.subtitle : (info.name !== info.title ? info.name : info.subtitle);
      const pos = o.start != null ? o.start : (info.position || 0);
      await startStream(pos);
      clearInterval(saveTimer);
      saveTimer = setInterval(() => { if (S && !v.paused && !v.seeking) save(); }, 10000);
      showUI();
    }
    async function startStream(pos, extra = {}) {
      const me = S;
      spin(true);
      $('#p-err').hidden = true;
      const c = await caps();
      const quality = S.override || S.quality;
      const bandwidth = quality === 'auto' ? await measure() : null;
      let r;
      try {
        r = await api(`play/${encodeURIComponent(S.id)}/start`, { caps: c, quality, audio: S.audio, sub: S.sub, start: pos, bandwidth, force: extra.force || S.force || null });
      } catch (e) { if (S === me) fail(e.message); return; }
      if (S !== me) { api(`stream/${r.session}/stop`, {}).catch(() => {}); return; }
      if (S.stream && S.stream.session !== r.session) api(`stream/${S.stream.session}/stop`, {}).catch(() => {});
      S.stream = r;
      S.mode = r.mode;
      metrics.mode = r.mode;
      metrics.events.push({ ev: 'start', mode: r.mode, label: r.label, reason: r.reason, encoder: r.encoder });
      $('#p-badge').textContent = [r.mode === 'transcode' ? r.label : 'Original', r.mode === 'direct' ? 'direct' : r.mode].filter(Boolean).join(' · ');
      attach(r, pos);
      subs(r);
      marks();
    }
    function attach(r, pos) {
      if (hls) { try { hls.destroy(); } catch (e) { /* gone */ } hls = null; }
      const playIt = () => v.play().catch(() => { pl.classList.add('ui'); spin(false); });
      clearTimeout(watchdog);
      watchdog = setTimeout(() => { if (S && !S.firstFrame && v.readyState < 2) fallback('The video didn’t start'); }, r.kind === 'file' ? 12000 : 40000);
      if (r.kind === 'hls' && useHlsJs()) {
        hls = new window.Hls({ startPosition: pos || 0, maxBufferLength: 45, maxMaxBufferLength: 120, backBufferLength: 30, maxBufferHole: 0.6,
          fragLoadingTimeOut: 40000, fragLoadingMaxRetry: 4, levelLoadingTimeOut: 15000, manifestLoadingTimeOut: 15000, enableWorker: true, startFragPrefetch: true });
        hls.on(window.Hls.Events.ERROR, (_e, data) => onHlsError(data));
        hls.loadSource(r.url);
        hls.attachMedia(v);
        hls.on(window.Hls.Events.MANIFEST_PARSED, playIt);
      } else {
        v.src = r.url;
        if (pos > 0) v.addEventListener('loadedmetadata', () => { try { v.currentTime = pos; } catch (e) { /* not yet */ } }, { once: true });
        playIt();
      }
    }
    let recovered = 0;
    function onHlsError(data) {
      metrics.events.push({ ev: 'hls-error', details: data.details, fatal: data.fatal });
      if (!data.fatal) return;
      if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR && recovered < 2) { recovered++; hls.recoverMediaError(); return; }
      if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR && S && S.tries < 2) { S.tries++; hls.startLoad(); return; }
      fallback(data.details || 'Playback error');
    }
    function fallback(why) {
      if (!S || !S.stream) return;
      const pos = v.currentTime || (S.info && S.info.position) || 0;
      metrics.events.push({ ev: 'fallback', from: S.mode, why });
      if (S.mode === 'direct') { S.force = 'remux'; startStream(pos); return; }
      if (S.mode === 'remux') { S.force = 'transcode'; startStream(pos); return; }
      const lower = ORDER[ORDER.indexOf(S.stream.quality) + 1];
      if (lower && S.tries < 3) { S.tries++; S.override = lower; startStream(pos); return; }
      fail(`This video couldn’t play: ${why}`);
    }
    function fail(msg) {
      spin(false);
      $('#p-err-msg').textContent = msg;
      $('#p-err').hidden = false;
      focus($('#p-err-retry'));
    }
    $('#p-err-retry')._act = () => { if (!S) return; S.tries = 0; $('#p-err').hidden = true; startStream(v.currentTime || (S.info && S.info.position) || 0, { force: 'transcode' }); };
    $('#p-err-close')._act = () => close();

    function teardown(stop) {
      clearInterval(saveTimer);
      clearTimeout(watchdog);
      clearTimeout(seekTimer);
      seekTarget = null;
      hideNext();
      $('#p-skip').hidden = true;
      if (hls) { try { hls.destroy(); } catch (e) { /* gone */ } hls = null; }
      if (stop && S && S.stream) api(`stream/${S.stream.session}/stop`, {}, { keepalive: true }).catch(() => {});
      v.pause();
      v.removeAttribute('src');
      try { v.load(); } catch (e) { /* fine */ }
      cues = [];
      renderSubs(true);
      native('keepAwake', false);
    }
    function close(quiet) {
      if (!isOpen()) return;
      save();
      teardown(true);
      S = null;
      pl.hidden = true;
      pl.classList.remove('ui', 'seeking');
      if (quiet) return;
      $('#app').hidden = false;
      F.cur = null;
      after();
      ensureFocus();
    }

    // events
    v.addEventListener('playing', () => {
      spin(false);
      native('keepAwake', true);
      if (S && !S.firstFrame) { S.firstFrame = true; clearTimeout(watchdog); S.ttff = Math.round(performance.now() - S.t0); metrics.ttff.push({ id: S.id, mode: S.mode, ms: S.ttff }); }
      if (seekT0) { metrics.seeks.push({ mode: S && S.mode, ms: Math.round(performance.now() - seekT0) }); seekT0 = 0; }
      hideUISoon();
    });
    v.addEventListener('pause', () => { if (!isOpen()) return; showUI(true); save(); native('keepAwake', false); });
    v.addEventListener('seeking', () => { seekT0 = performance.now(); spin(true); });
    v.addEventListener('seeked', () => { if (v.paused) spin(false); });
    v.addEventListener('waiting', () => spin(true));
    v.addEventListener('canplay', () => { if (!v.seeking) spin(false); });
    v.addEventListener('error', () => { if (!S || hls || !v.getAttribute('src')) return; fallback((v.error && v.error.message) || 'The TV couldn’t decode this'); });
    v.addEventListener('ended', () => { save(true); if (S && S.info && S.info.next && prefs.autoplay && !S.cancelledNext) playNext(); else showUI(true); });

    function save(ended) {
      if (!S || !S.info || !S.firstFrame) return;
      const pos = v.currentTime || 0;
      if (pos < 2 && !ended) return;
      api('progress', { id: S.info.id, position: pos, duration: S.info.duration, ended: !!ended }, { keepalive: true }).catch(() => {});
    }

    // controls
    const spin = (on) => $('#p-spin').classList.toggle('on', !!on);
    function showUI(stay) { pl.classList.add('ui'); clearTimeout(uiTimer); if (!stay) hideUISoon(); }
    function hideUISoon(ms = 3500) {
      clearTimeout(uiTimer);
      uiTimer = setTimeout(() => { if (!v.paused && seekTarget == null && $('#p-panel').hidden) pl.classList.remove('ui'); }, ms);
    }
    function flash(icon, text) {
      const f = $('#p-flash');
      $('use', f).setAttribute('href', '#' + icon);
      $('span', f).textContent = text || '';
      f.classList.remove('go'); void f.offsetWidth; f.classList.add('go');
    }
    function togglePlay(mode) {
      if (!S || !S.stream) return;
      const play = mode === 'play' || (mode !== 'pause' && v.paused);
      if (play) { v.play().catch(() => {}); flash('i-play'); } else { v.pause(); flash('i-pause'); }
      showUI(!play);
    }
    const duration = () => (S && S.info && S.info.duration) || (isFinite(v.duration) ? v.duration : 0);
    function seekStep(dir, big) {
      if (!S || !S.firstFrame) return;
      const d = duration();
      const base = seekTarget != null ? seekTarget : v.currentTime;
      const step = big ? 30 : seekReps >= 12 ? 60 : seekReps >= 6 ? 30 : 10;
      seekTarget = Math.max(0, Math.min(d ? d - 2 : Infinity, base + dir * step));
      seekReps++;
      pl.classList.add('ui', 'seeking');
      clearTimeout(uiTimer);
      draw(seekTarget);
      const delta = Math.round(seekTarget - v.currentTime);
      $('#p-tip').textContent = `${fmt(seekTarget)}  (${delta >= 0 ? '+' : '−'}${fmt(Math.abs(delta))})`;
      clearTimeout(seekTimer);
      seekTimer = setTimeout(commitSeek, 650);
    }
    function commitSeek() {
      if (seekTarget == null) return;
      metrics.events.push({ ev: 'seek', to: Math.round(seekTarget) });
      v.currentTime = seekTarget;
      seekTarget = null;
      seekReps = 0;
      pl.classList.remove('seeking');
      hideUISoon();
    }
    function draw(t) {
      const d = duration();
      const f = d ? Math.min(1, t / d) : 0;
      $('#p-play').style.width = (f * 100) + '%';
      $('#p-knob').style.left = (f * 100) + '%';
      $('#p-pos').textContent = fmt(t);
      $('#p-dur').textContent = '−' + fmt(Math.max(0, d - t));
    }
    function bufferedAhead() {
      const t = v.currentTime;
      for (let i = 0; i < v.buffered.length; i++) if (v.buffered.start(i) <= t + 0.3 && v.buffered.end(i) >= t) return v.buffered.end(i) - t;
      return 0;
    }
    function tick() {
      if (!isOpen()) return;
      if (seekTarget == null) draw(v.currentTime || 0);
      const d = duration();
      if (d) $('#p-buf').style.width = Math.min(100, ((v.currentTime + bufferedAhead()) / d) * 100) + '%';
      renderSubs();
      segments();
    }
    v.addEventListener('timeupdate', tick);
    setInterval(tick, 250);
    function marks() {
      const d = duration(), box = $('#p-marks');
      box.replaceChildren();
      if (!d || !S || !S.info) return;
      for (const s of S.info.segments || []) {
        const i = document.createElement('i');
        i.style.left = (s.start / d) * 100 + '%';
        i.style.width = Math.max(0.4, ((s.end - s.start) / d) * 100) + '%';
        box.appendChild(i);
      }
    }

    // skip intro / credits, next episode
    let skipSeg = null;
    function segments() {
      if (!S || !S.info || !S.firstFrame) return;
      const t = v.currentTime, d = duration();
      const seg = (S.info.segments || []).find((s) => t >= s.start && t < s.end - 0.8);
      const next = S.info.next;
      const creditsAt = ((S.info.segments || []).find((s) => s.kind === 'credits') || {}).start;
      const nearEnd = next && (creditsAt ? t >= creditsAt : d && t >= d - 25);
      if (nearEnd && !S.cancelledNext && !S.nextShown) showNext();
      if (!nearEnd && S.nextShown) hideNext();
      skipSeg = seg && !(seg.kind === 'credits' && S.nextShown) ? seg : null;
      $('#p-skip').hidden = !skipSeg;
      if (skipSeg) $('#p-skip span').textContent = skipSeg.kind === 'intro' ? 'Skip intro' : 'Skip credits';
    }
    function skip() {
      if (!skipSeg) return;
      if (skipSeg.kind === 'credits' && S.info.next) { playNext(); return; }
      v.currentTime = Math.min(skipSeg.end, duration() - 0.5);
      skipSeg = null;
      $('#p-skip').hidden = true;
    }
    function showNext() {
      const n = S.info.next;
      S.nextShown = true;
      $('#p-next').hidden = false;
      $('#p-next-title').textContent = `S${n.season ?? 0}:E${n.episode ?? 0} · ${n.name || ''}`;
      $('#p-next-img').src = n.thumb || n.backdrop || '';
      clearInterval(nextTimer);
      nextCount = 10;
      $('#p-next-count').textContent = prefs.autoplay ? `in ${nextCount}` : '';
      if (prefs.autoplay) {
        nextTimer = setInterval(() => {
          if (v.paused) return;
          nextCount--;
          $('#p-next-count').textContent = `in ${nextCount}`;
          if (nextCount <= 0) { clearInterval(nextTimer); playNext(); }
        }, 1000);
      }
    }
    function hideNext() { clearInterval(nextTimer); if (S) S.nextShown = false; $('#p-next').hidden = true; }
    function playNext() {
      if (!S || !S.info || !S.info.next) return;
      const id = S.info.next.id;
      save(true);
      hideNext();
      open(id, {});
    }

    // subtitles (drawn here so every TV shows them the same way)
    let cues = [], cueIdx = 0, subsShown = '';
    async function subs(r) {
      cues = [];
      renderSubs(true);
      if (!r.sub_url) return;
      const me = S;
      for (let tries = 0; S === me && S.stream === r && tries < 160; tries++) {
        let res;
        try { res = await fetch(r.sub_url, { cache: 'no-store' }); } catch (e) { return; }
        if (res.status === 202) { if (!tries) toast('Getting the subtitles ready…'); await new Promise((ok) => setTimeout(ok, 1500)); continue; }
        if (!res.ok) { toast('Those subtitles couldn’t be read'); return; }
        const text = await res.text();
        if (S !== me) return;
        cues = parseVtt(text);
        cueIdx = 0;
        return;
      }
    }
    function parseVtt(text) {
      const out = [];
      const ts = (s) => { const p = s.trim().split(':').map(parseFloat); return p.length === 3 ? p[0] * 3600 + p[1] * 60 + p[2] : p[0] * 60 + p[1]; };
      for (const block of text.replace(/\r/g, '').split(/\n\n+/)) {
        const lines = block.split('\n');
        const i = lines.findIndex((l) => l.includes('-->'));
        if (i < 0) continue;
        const [a, b] = lines[i].split('-->');
        const body = lines.slice(i + 1).join('\n').replace(/<[^>]+>/g, '').replace(/\{\\[^}]*\}/g, '')
          .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&nbsp;/g, ' ').trim();
        if (body) out.push({ s: ts(a), e: ts(b.trim().split(' ')[0]), t: body });
      }
      return out.sort((x, y) => x.s - y.s);
    }
    function renderSubs(clear) {
      const box = $('#p-subs');
      if (clear || !cues.length) { if (subsShown) { box.replaceChildren(); subsShown = ''; } return; }
      const t = v.currentTime;
      if (cueIdx >= cues.length || cues[cueIdx].s > t) cueIdx = 0;
      while (cueIdx < cues.length && cues[cueIdx].e < t) cueIdx++;
      const on = [];
      for (let i = Math.max(0, cueIdx - 3); i < cues.length && cues[i].s <= t; i++) if (cues[i].e > t) on.push(cues[i].t);
      const key = on.join('\u0000');
      if (key === subsShown) return;
      subsShown = key;
      box.replaceChildren(...on.map((x) => Object.assign(document.createElement('span'), { textContent: x })));
    }

    // subtitles / audio / quality panel
    function opt(label, sub, on, k, act) {
      const b = document.createElement('button');
      b.className = 'f opt' + (on ? ' on' : '');
      b.dataset.k = k;
      b.innerHTML = `<span class="ck">${on ? '<svg><use href="#i-check"/></svg>' : ''}</span><span>${esc(label)}${sub ? `<small>${esc(sub)}</small>` : ''}</span>`;
      b._act = () => { closePanel(); act(); };
      return b;
    }
    function openPanel() {
      if (!S || !S.info) return;
      const info = S.info;
      $('#p-subs-list').replaceChildren(opt('Off', '', !S.sub, 'sub:off', () => pickSub(null)),
        ...info.subs.map((s) => opt(s.label, s.kind === 'image' ? 'Burned into the picture' : '', s.id === S.sub, 'sub:' + s.id, () => pickSub(s.id))));
      const curA = S.stream ? S.stream.audio : S.audio;
      $('#p-audio-list').replaceChildren(...(info.audio.length ? info.audio.map((a) => opt(a.label, '', a.index === curA, 'a:' + a.index, () => { if (a.index !== curA) { S.audio = a.index; startStream(v.currentTime); } }))
        : [opt('No audio', '', true, 'a:none', () => {})]));
      const curQ = S.override || S.quality;
      const qs = (me && me.qualities) || [{ key: 'auto', label: 'Auto' }, { key: 'original', label: 'Original' }];
      $('#p-q-list').replaceChildren(...qs.map((q) => opt(q.key === 'auto' && S.stream ? `Auto (${S.stream.mode === 'transcode' ? S.stream.label : 'Original'})` : q.label, '', q.key === curQ, 'q:' + q.key,
        () => { S.quality = q.key; S.override = null; S.force = null; startStream(v.currentTime); })));
      $('#p-panel').hidden = false;
      showUI(true);
      focus($('#p-subs-list .opt.on') || $('#p-subs-list .opt'));
    }
    function closePanel() {
      $('#p-panel').hidden = true;
      if (F.cur) F.cur.classList.remove('focus');
      F.cur = null;
      hideUISoon();
    }
    function pickSub(id) {
      const info = S.info;
      const was = info.subs.find((s) => s.id === S.sub), now = info.subs.find((s) => s.id === id);
      S.sub = id;
      if ((was && was.kind === 'image') || (now && now.kind === 'image')) { startStream(v.currentTime); return; }
      if (!now) { cues = []; renderSubs(true); return; }
      S.stream = { ...S.stream, sub_url: S.stream.base + `sub/${now.id}.vtt` };
      subs(S.stream);
    }

    // the remote
    function key(k) {
      if (!$('#p-err').hidden) {
        if (k === 'back') close();
        else if (k === 'ok') activate(F.cur);
        else if (['left', 'right', 'up', 'down'].includes(k)) move(k);
        return;
      }
      if (!$('#p-panel').hidden) {
        if (k === 'back' || k === 'menu') closePanel();
        else if (k === 'ok') activate(F.cur);
        else if (['left', 'right', 'up', 'down'].includes(k)) move(k);
        return;
      }
      if (!S) { if (k === 'back') close(); return; }
      switch (k) {
        case 'ok':
          if (skipSeg) skip();
          else if (S.nextShown) playNext();
          else togglePlay();
          break;
        case 'playpause': togglePlay(); break;
        case 'play': togglePlay('play'); break;
        case 'pause': togglePlay('pause'); break;
        case 'left': seekStep(-1); break;
        case 'right': seekStep(1); break;
        case 'rewind': seekStep(-1, true); break;
        case 'forward': seekStep(1, true); break;
        case 'up': showUI(); break;
        case 'down': case 'menu': openPanel(); break;
        case 'back':
          if (seekTarget != null) { clearTimeout(seekTimer); seekTarget = null; seekReps = 0; pl.classList.remove('seeking'); draw(v.currentTime); }
          else if (S.nextShown) { S.cancelledNext = true; hideNext(); }
          else close();
          break;
        case 'stop': close(); break;
        default:
      }
    }
    window.__tvDebug = () => ({ mode: S && S.mode, cues: cues.length, t: v.currentTime, paused: v.paused });
    return { open, close, isOpen, key, save, pause: () => { if (isOpen() && !v.paused) v.pause(); save(); }, debug: () => ({ mode: S && S.mode, stream: S && S.stream, cues: cues.length, t: v.currentTime, paused: v.paused, ready: v.readyState, w: v.videoWidth, h: v.videoHeight }) };
  })();

  // ================================================================ keys
  const KEYS = {
    ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right', Up: 'up', Down: 'down', Left: 'left', Right: 'right',
    Enter: 'ok', Select: 'ok', Accept: 'ok', Escape: 'back', GoBack: 'back', BrowserBack: 'back', Backspace: 'back', XF86Back: 'back',
    MediaPlayPause: 'playpause', MediaPlay: 'play', MediaPause: 'pause', MediaStop: 'stop', MediaFastForward: 'forward', MediaRewind: 'rewind',
    MediaTrackNext: 'forward', MediaTrackPrevious: 'rewind', ContextMenu: 'menu', ' ': 'playpause',
  };
  const CODES = { 13: 'ok', 23: 'ok', 27: 'back', 461: 'back', 10009: 'back', 179: 'playpause', 415: 'play', 19: 'pause', 413: 'stop', 417: 'forward', 412: 'rewind', 37: 'left', 38: 'up', 39: 'right', 40: 'down' };

  document.addEventListener('keydown', (e) => {
    const t = e.target;
    const editing = t && t.tagName === 'INPUT' && !t.readOnly;
    let k = KEYS[e.key] || CODES[e.keyCode] || null;
    if (editing) {   // typing on the TV's keyboard: only Down/Enter/Back leave the box
      if (k === 'down' || k === 'ok' || e.key === 'Escape') { e.preventDefault(); t.readOnly = true; t.blur(); try { t.focus({ preventScroll: true }); } catch (x) { /* fine */ } if (k === 'down') move('down'); }
      return;
    }
    if (k === 'playpause' && e.key === ' ' && $('#player').hidden) k = null;
    if (!k) {
      // a real keyboard (testing, or a TV with one): letters type into search or the pairing code
      if (e.key && e.key.length === 1 && /[a-z0-9 ]/i.test(e.key)) {
        if (!$('#pair').hidden) { const c = e.key.toUpperCase(); if (PAIR_KEYS.includes(c)) { pairKey(c); e.preventDefault(); } }
        else if (topView() && topView().typeKey && $('#player').hidden) { topView().typeKey(e.key); e.preventDefault(); }
      }
      return;
    }
    e.preventDefault();
    if (k === 'back' && e.key === 'Backspace' && topView() && topView().typeKey && $('#player').hidden && !(F.cur && F.cur.closest('#rail'))) { topView().typeKey('Backspace'); return; }
    if (k === 'ok' && e.repeat) return;
    if (!$('#player').hidden) { P.key(k); return; }
    ensureFocus();
    if (k === 'back') { back(); return; }
    if (k === 'ok') { activate(F.cur); return; }
    if (['up', 'down', 'left', 'right'].includes(k)) move(k);
  }, true);

  // the TV app asks this when the remote's Back is pressed: true = handled here, false = leave the app
  window.atlasBack = () => {
    if (!$('#player').hidden) { P.key('back'); return true; }
    const t = document.activeElement;
    if (t && t.tagName === 'INPUT' && !t.readOnly) { t.readOnly = true; t.blur(); return true; }
    return back();
  };
  window.atlasPause = () => P.pause();
  window.atlasKey = (k) => { document.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true })); };
  document.addEventListener('visibilitychange', () => { if (document.hidden) P.save(); });
  // a mouse or touch (an emulator, a TV with a pointer) still works: focus follows it, a click picks
  document.addEventListener('click', (e) => {
    const f = e.target.closest('.f');
    if (!f || !layer() || !layer().contains(f)) return;
    e.preventDefault();
    focus(f);
    activate(f);
  });

  // ================================================================ start
  async function boot() {
    try { me = await api('me'); } catch (e) {
      if (e.status === 401) return;
      $('#app').hidden = false;
      $('#views').innerHTML = `<p class="loading">${esc(e.message)}</p>`;
      setTimeout(boot, 5000);
      return;
    }
    $('#pair').hidden = true;
    $('#app').hidden = false;
    if (!me.owner) $('#rail [data-go="mylist"]').hidden = true;
    for (const v of V.splice(0)) v.el.remove();
    $('#views').replaceChildren();
    await go('home');
  }

  (async () => {
    const m = /code=([A-Za-z0-9-]{4,12})/.exec(location.hash);
    if (m) {
      history.replaceState(null, '', location.pathname);
      try { await api('tv/redeem', { code: m[1].replace(/-/g, '') }, { noPair: true }); toast('Paired. Welcome to Atlas!'); } catch (e) { /* show the pairing screen below */ }
    }
    boot();
  })();
})();
