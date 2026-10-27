/* Atlas Mobile: pairing, browsing (home, search, details, libraries), downloads and settings. The player lives in
   player.js and the PC remote in remote.js; they share the helpers on window.M. Hash routes (#/home, #/item/<id>...)
   so the phone's back button works; anything else in the hash is a pairing code. */
(() => {
  'use strict';
  const M = (window.M = window.M || {});
  const $ = (M.$ = (s, el = document) => el.querySelector(s));
  const $$ = (M.$$ = (s, el = document) => [...el.querySelectorAll(s)]);

  // ---------------------------------------------------------------- preferences (this phone only)
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
  };
  M.store = store;
  M.prefs = Object.assign({ theme: 'dark', quality: null, autoplay: true, stats: false }, store.get('marquee-mobile', {}));
  M.savePrefs = () => store.set('marquee-mobile', M.prefs);
  const applyTheme = () => {
    document.documentElement.dataset.theme = M.prefs.theme === 'light' ? 'light' : 'dark';
    $('meta[name=theme-color]').content = M.prefs.theme === 'light' ? '#eef3fc' : '#070c1d';
  };
  applyTheme();

  // ---------------------------------------------------------------- helpers
  M.buzz = (p = 10) => { try { navigator.vibrate && navigator.vibrate(p); } catch (e) { /* not supported */ } };
  M.fmt = (s) => {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = String(s % 60).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${x}` : `${m}:${x}`;
  };
  M.mins = (s) => { s = Math.round((s || 0) / 60); return s >= 60 ? `${Math.floor(s / 60)} h ${s % 60 ? (s % 60) + ' min' : ''}`.trim() : `${s} min`; };
  M.esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  M.epLabel = (c) => (c.type === 'Episode' ? `S${c.season ?? 0}:E${c.episode ?? 0} · ${c.name || ''}` : '');
  const esc = M.esc;

  let toastTimer;
  M.toast = (msg, ms = 2400) => {
    const el = $('#toast');
    el.textContent = msg;
    el.classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('on'), ms);
  };

  M.loadImg = (img, src, fallbackText) => {
    if (!img) return;
    const box = img.parentElement;
    if (!src) {
      img.removeAttribute('src');
      img.classList.remove('ok');
      if (fallbackText && box && !box.querySelector('.fallback')) box.insertAdjacentHTML('beforeend', `<span class="fallback">${esc(fallbackText)}</span>`);
      return;
    }
    if (img.dataset.src === src) return;
    img.dataset.src = src;
    img.classList.remove('ok');
    img.decoding = 'async';
    img.onload = () => img.classList.add('ok');
    img.onerror = () => {
      img.classList.remove('ok');
      if (fallbackText && box && !box.querySelector('.fallback')) box.insertAdjacentHTML('beforeend', `<span class="fallback">${esc(fallbackText)}</span>`);
    };
    img.src = src;
  };

  // ---------------------------------------------------------------- API
  M.api = async (path, body, opts = {}) => {
    const init = { headers: { 'X-Marquee': '1', ...(opts.headers || {}) }, credentials: 'same-origin', keepalive: !!opts.keepalive };
    if (body !== undefined || opts.method) {
      init.method = opts.method || 'POST';
      if (body !== undefined) {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(body);
      }
    }
    let r;
    try {
      r = await fetch('/r/api/' + path, init);
    } catch (e) {
      setLink('lost', 'Can’t reach your PC');
      throw new Error('Can’t reach your PC');
    }
    let data = {};
    try { data = await r.json(); } catch (e) { /* empty */ }
    if (r.status === 401 && !opts.noPairScreen) { showPair(data.message); throw Object.assign(new Error(data.message || 'Not paired'), { status: 401 }); }
    if (r.status === 429) { showPair(data.message); throw new Error(data.message || 'Too many tries'); }
    if (!r.ok || data.ok === false) throw Object.assign(new Error(data.message || `HTTP ${r.status}`), { status: r.status, data });
    if (!opts.quiet) setLink(M.me && M.me.offline ? 'warn' : 'live', M.me && M.me.offline ? 'Jellyfin away' : 'Connected');
    return data;
  };

  function setLink(cls, text) {
    const el = $('#link');
    el.className = 'link glass ' + cls;
    el.querySelector('span').textContent = text;
  }
  M.setLink = setLink;

  // ---------------------------------------------------------------- pairing
  function showPair(msg) {
    $('#pair').hidden = false;
    $('#pair-err').textContent = msg && !/^Not paired/.test(msg) ? msg : '';
    setLink('lost', 'Not paired');
    fetch('/r/api/login-info', { headers: { 'X-Marquee': '1' } }).then((r) => r.json()).then((d) => {
      $('#jf-login').hidden = !(d && d.jellyfin);
    }).catch(() => {});
  }
  async function pairWith(token) {
    const d = await M.api('pair', {}, { headers: { 'X-Marquee-Token': token }, noPairScreen: true });
    $('#pair').hidden = true;
    M.buzz([10, 50, 10]);
    M.toast(`Paired as ${d.device.name}`);
    return d;
  }
  $('#pair-paste').addEventListener('submit', async (e) => {
    e.preventDefault();
    const v = $('#pair-link').value.trim();
    const tok = v.includes('#') ? v.split('#').pop().replace(/^h=/, '') : v;
    if (!tok) return;
    try { await pairWith(decodeURIComponent(tok)); boot(); } catch (err) { $('#pair-err').textContent = err.message; }
  });
  $('#jf-login').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector('button');
    btn.disabled = true;
    try {
      await M.api('login', { user: $('#jf-user').value, password: $('#jf-pass').value }, { noPairScreen: true });
      $('#jf-pass').value = '';
      $('#pair').hidden = true;
      boot();
    } catch (err) {
      $('#pair-err').textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  });

  // ---------------------------------------------------------------- cards
  const POSTER = (c) => c.poster || c.thumb;
  function posterCard(c, i = 0) {
    const a = document.createElement('a');
    a.className = 'pcard';
    a.style.setProperty('--i', i);
    a.href = c.type === 'Episode' ? `#/item/${encodeURIComponent(c.series_id || c.id)}${c.season != null ? '?s=' + c.season : ''}` : `#/item/${encodeURIComponent(c.id)}`;
    const sub = c.type === 'Episode' ? `S${c.season ?? 0}:E${c.episode ?? 0}` : [c.year, c.type === 'Series' ? 'Series' : ''].filter(Boolean).join(' · ');
    a.innerHTML = `<div class="pimg"><img alt="" loading="lazy">${c.progress > 0.01 && !c.played ? '<div class="pbar"><i></i></div>' : ''}${c.played ? '<span class="tick"><svg width="13" height="13"><use href="#i-check"/></svg></span>' : ''}</div><b>${esc(c.type === 'Episode' ? (c.series || c.name) : c.name)}</b><span>${esc(sub)}</span>`;
    M.loadImg(a.querySelector('img'), POSTER(c), c.name);
    const bar = a.querySelector('.pbar i');
    if (bar) bar.style.width = Math.round(c.progress * 100) + '%';
    return a;
  }
  function wideCard(c, i = 0, onTap = null) {
    const el = document.createElement('div');
    el.className = 'wcard';
    el.style.setProperty('--i', i);
    el.setAttribute('role', 'button');
    el.tabIndex = 0;
    const img = c.type === 'Episode' ? (c.thumb || c.backdrop) : (c.backdrop || c.thumb || c.poster);
    const title = c.type === 'Episode' ? (c.series || c.name) : c.name;
    const left = c.runtime && c.position ? `${M.mins(c.runtime - c.position)} left` : '';
    const sub = c.type === 'Episode' ? M.epLabel(c) : [c.year, left].filter(Boolean).join(' · ');
    el.innerHTML = `<div class="wthumb"><img alt="" loading="lazy"><span class="glyph"><svg width="18" height="18"><use href="#i-play"/></svg></span>
      <a class="more" aria-label="Details" href="#/item/${encodeURIComponent(c.type === 'Episode' ? (c.series_id || c.id) : c.id)}${c.type === 'Episode' && c.season != null ? '?s=' + c.season : ''}"><svg width="18" height="18"><use href="#i-info"/></svg></a>
      ${c.progress > 0.01 ? '<div class="pbar"><i></i></div>' : ''}</div><b>${esc(title)}</b><span>${esc(sub)}</span>`;
    M.loadImg(el.querySelector('img'), img, title);
    const bar = el.querySelector('.pbar i');
    if (bar) bar.style.width = Math.round(c.progress * 100) + '%';
    el.addEventListener('click', (e) => {
      if (e.target.closest('.more')) return;
      if (onTap) onTap(c); else M.play(c.id);
    });
    return el;
  }
  M.posterCard = posterCard;
  M.wideCard = wideCard;

  function rowEl(r) {
    const sec = document.createElement('section');
    sec.className = 'row';
    sec.innerHTML = `<div class="row-h"><h2>${esc(r.title)}</h2>${r.more ? `<a href="#/lib/${r.more}">See all</a>` : ''}</div><div class="strip"></div>`;
    const strip = sec.querySelector('.strip');
    strip.append(...r.items.map((c, i) => (r.style === 'wide' ? wideCard(c, i) : posterCard(c, i))));
    return sec;
  }

  // ---------------------------------------------------------------- home
  let homeAt = 0;
  async function home(force) {
    if (!force && Date.now() - homeAt < 60000 && $('#home-rows').childElementCount) return;
    let d;
    try { d = await M.api('home'); } catch (e) { if (e.status !== 401) M.toast(e.message); return; }
    homeAt = Date.now();
    $('#home-skel').hidden = true;
    M.me && (M.me.offline = d.offline);
    hero(d.hero || []);
    $('#home-rows').replaceChildren(...d.rows.map(rowEl));
    $('#home-empty').hidden = !!d.rows.length;
  }
  M.refreshHome = () => { homeAt = 0; };

  function hero(items) {
    const wrap = $('#hero');
    wrap.hidden = !items.length;
    const track = $('#hero-track');
    track.replaceChildren(...items.map((c) => {
      const s = document.createElement('div');
      s.className = 'slide';
      const meta = [c.year, c.type === 'Series' ? 'Series' : (c.runtime ? M.mins(c.runtime) : ''), (c.genres || []).slice(0, 2).join(', ')].filter(Boolean).join(' · ');
      s.innerHTML = `<img class="bd" alt=""><div class="shade"></div><div class="cap">${c.logo ? '<img class="logo" alt="">' : `<h2>${esc(c.name)}</h2>`}<span class="meta">${esc(meta)}</span>
        <div class="acts"><button class="btn white" data-play><svg width="18" height="18"><use href="#i-play"/></svg>Play</button><a class="btn glass-btn" href="#/item/${encodeURIComponent(c.id)}"><svg width="18" height="18"><use href="#i-info"/></svg>Details</a></div></div>`;
      M.loadImg(s.querySelector('img.bd'), c.poster && window.innerWidth < 600 ? c.poster : c.backdrop);
      const logo = s.querySelector('img.logo');
      if (logo) { logo.src = c.logo; logo.onerror = () => { logo.replaceWith(Object.assign(document.createElement('h2'), { textContent: c.name })); }; }
      s.querySelector('[data-play]').addEventListener('click', () => M.play(c.id));
      return s;
    }));
    const dots = $('#hero-dots');
    dots.replaceChildren(...items.map((_, i) => Object.assign(document.createElement('i'), { className: i ? '' : 'on' })));
    track.onscroll = () => {
      const i = Math.round(track.scrollLeft / Math.max(1, track.clientWidth));
      $$('i', dots).forEach((d, k) => d.classList.toggle('on', k === i));
    };
  }

  // ---------------------------------------------------------------- search
  const q = $('#q');
  let qTimer, qSeq = 0;
  q.addEventListener('input', () => {
    $('#q-clear').hidden = !q.value;
    clearTimeout(qTimer);
    qTimer = setTimeout(search, 250);
  });
  q.addEventListener('keydown', (e) => { if (e.key === 'Enter') { clearTimeout(qTimer); search(); q.blur(); } });
  $('#q-clear').addEventListener('click', (e) => { e.preventDefault(); q.value = ''; $('#q-clear').hidden = true; search(); q.focus(); });
  async function search() {
    const text = q.value.trim();
    const seq = ++qSeq;
    const res = $('#results');
    if (!text) { res.replaceChildren(); $('#search-empty').hidden = true; return; }
    let d;
    try { d = await M.api('search?q=' + encodeURIComponent(text)); } catch (e) { return; }
    if (seq !== qSeq) return;
    const titles = d.items.filter((c) => c.type !== 'Episode');
    const eps = d.items.filter((c) => c.type === 'Episode');
    const nodes = titles.map(posterCard);
    for (const c of eps) {
      const b = document.createElement('div');
      b.className = 'ritem wide';
      b.setAttribute('role', 'button');
      b.innerHTML = `<div class="wthumb"><img alt="" loading="lazy">${c.progress > 0.01 ? '<div class="pbar"><i></i></div>' : ''}</div><div><b>${esc(c.name)}</b><span>${esc(`${c.series || ''} · S${c.season ?? 0}:E${c.episode ?? 0}`)}</span></div>`;
      M.loadImg(b.querySelector('img'), c.thumb || c.backdrop, c.name);
      const bar = b.querySelector('.pbar i');
      if (bar) bar.style.width = Math.round(c.progress * 100) + '%';
      b.addEventListener('click', () => M.play(c.id));
      nodes.push(b);
    }
    res.replaceChildren(...nodes);
    $('#search-empty').hidden = !!d.items.length;
    $('#search-empty').textContent = `Nothing in your library matches “${text}”.`;
  }

  // ---------------------------------------------------------------- details
  let itemCache = {};
  async function item(id, params) {
    const v = $('#v-item');
    const cached = itemCache[id];
    if (!cached) v.innerHTML = '<div class="skeleton" style="margin-top:80px"><i></i><i></i></div>';
    let d;
    try { d = (await M.api('item/' + encodeURIComponent(id))).item; } catch (e) { if (!cached) v.innerHTML = `<p class="empty" style="margin-top:120px">${esc(e.message)}</p>`; return; }
    itemCache[id] = d;
    if (route().name !== 'item' || route().id !== id) return;
    renderItem(d, params);
  }
  M.forgetItem = (id) => { if (id) delete itemCache[id]; else itemCache = {}; };

  function renderItem(d, params) {
    const v = $('#v-item');
    const isSeries = d.type === 'Series';
    const target = isSeries ? d.next_up : d;
    const resume = target && target.position > 30 && target.progress < 0.95;
    const quality = d.quality ? `<span class="badge">${esc(d.quality)}</span>` : '';
    const meta = [d.year, d.age ? `<span class="badge">${esc(d.age)}</span>` : '', isSeries ? `${(d.seasons || []).filter((s) => s.number).length} seasons` : (d.runtime ? M.mins(d.runtime) : ''),
      d.rating ? `★ ${Number(d.rating).toFixed(1)}` : '', quality].filter(Boolean).join(' <span aria-hidden="true">·</span> ');
    let playLabel = 'Play';
    if (isSeries && target) playLabel = `${resume ? 'Resume' : 'Play'} S${target.season ?? 0}:E${target.episode ?? 0}`;
    else if (resume) playLabel = 'Resume';
    v.innerHTML = `
      <div class="d-hero"><img class="bd" alt=""><div class="shade"></div></div>
      <div class="d-head">
        ${d.logo ? '<img class="logo" alt="">' : `<h1>${esc(d.name)}</h1>`}
        <div class="d-meta">${meta}</div>
        <div class="d-actions">
          ${target || !isSeries ? `<button class="btn white wide" id="d-play"${d.available === false ? ' disabled' : ''}><svg width="20" height="20"><use href="#i-play"/></svg>${esc(playLabel)}</button>` : ''}
          ${resume ? `<div class="d-prog"><div class="bar"><i style="width:${Math.round(target.progress * 100)}%"></i></div><span>${M.mins(target.runtime - target.position)} left</span></div>` : ''}
          <div class="row2">
            ${resume ? '<button class="btn glass-btn" id="d-restart"><svg width="20" height="20"><use href="#i-restart"/></svg>Start over</button>' : ''}
            ${M.me && M.me.owner ? '<button class="btn glass-btn" id="d-pc"><svg width="20" height="20"><use href="#i-tv"/></svg>Play on PC</button>' : ''}
            ${d.mylist != null ? `<button class="btn glass-btn" id="d-list"><svg width="20" height="20"><use href="#${d.mylist ? 'i-check' : 'i-plus'}"/></svg>My List</button>` : ''}
            ${!isSeries ? '<button class="btn glass-btn" id="d-dl"><svg width="20" height="20"><use href="#i-download"/></svg>Download</button>' : ''}
          </div>
        </div>
      </div>
      ${isSeries && target && target.name ? `<p class="d-tag">${esc(M.epLabel(target))}</p>` : (d.tagline ? `<p class="d-tag">${esc(d.tagline)}</p>` : '')}
      <p class="d-over" id="d-over">${esc(d.overview || '')}</p>
      ${isSeries ? '<h2 class="sec-h">Episodes</h2><div class="seasons" id="d-seasons"></div><div class="eps" id="d-eps"></div>' : ''}
      ${(d.people || []).length ? '<h2 class="sec-h">Cast & crew</h2><div class="cast" id="d-cast"></div>' : ''}
      ${(d.similar || []).length ? '<h2 class="sec-h">More like this</h2><div class="strip" id="d-similar"></div>' : ''}`;
    M.loadImg($('.d-hero img', v), window.innerWidth < 600 && d.poster ? d.poster : (d.backdrop || d.poster));
    const logo = $('.d-head img.logo', v);
    if (logo) { logo.src = d.logo; logo.onerror = () => logo.replaceWith(Object.assign(document.createElement('h1'), { textContent: d.name })); }
    $('#d-over').addEventListener('click', (e) => e.currentTarget.classList.toggle('open'));
    const play = $('#d-play', v);
    if (play) play.addEventListener('click', () => M.play(target ? target.id : d.id));
    const rs = $('#d-restart', v);
    if (rs) rs.addEventListener('click', () => M.play(target.id, { start: 0 }));
    const pc = $('#d-pc', v);
    if (pc) pc.addEventListener('click', () => M.playOnPC(target ? target.id : d.id));
    const ml = $('#d-list', v);
    if (ml) ml.addEventListener('click', async () => {
      try {
        await M.api('mylist', { id: d.id, on: !d.mylist });
        d.mylist = !d.mylist;
        ml.querySelector('use').setAttribute('href', d.mylist ? '#i-check' : '#i-plus');
        M.toast(d.mylist ? 'Added to My List' : 'Removed from My List');
        M.refreshHome();
        M.buzz(10);
      } catch (e) { M.toast(e.message); }
    });
    const dl = $('#d-dl', v);
    if (dl) dl.addEventListener('click', () => M.download(d.id));
    if (d.people && d.people.length) {
      $('#d-cast').replaceChildren(...d.people.map((p) => {
        const el = document.createElement('div');
        el.className = 'person';
        el.innerHTML = `<div class="ph">${p.photo ? '<img alt="" loading="lazy">' : '<svg width="28" height="28"><use href="#i-user"/></svg>'}</div><b>${esc(p.name)}</b><span>${esc(p.role || p.type || '')}</span>`;
        if (p.photo) M.loadImg(el.querySelector('img'), p.photo);
        return el;
      }));
    }
    if (d.similar && d.similar.length) $('#d-similar').replaceChildren(...d.similar.map(posterCard));
    if (isSeries) seasons(d, params);
    const amb = $('#ambient');
    if (d.backdrop) { amb.style.backgroundImage = `url("${d.backdrop}")`; amb.classList.add('on'); }
  }

  function seasons(d, params) {
    const box = $('#d-seasons');
    const list = d.seasons || [];
    if (!list.length) { $('#d-eps').innerHTML = '<p class="empty">No episodes on this PC yet.</p>'; return; }
    let want = params && params.get('s') != null ? Number(params.get('s')) : (d.next_up ? d.next_up.season : null);
    let pick = list.find((s) => s.number === want) || list.find((s) => s.number) || list[0];
    box.replaceChildren(...list.map((s) => {
      const b = document.createElement('button');
      b.className = 'chip' + (s === pick ? ' on' : '');
      b.textContent = s.number === 0 ? 'Specials' : (s.name || `Season ${s.number}`);
      b.addEventListener('click', () => {
        $$('.chip', box).forEach((x) => x.classList.toggle('on', x === b));
        episodes(d, s);
        M.buzz(6);
      });
      return b;
    }));
    episodes(d, pick);
    requestAnimationFrame(() => { const on = $('.chip.on', box); if (on) box.scrollLeft = on.offsetLeft - 16; });
  }

  async function episodes(d, s) {
    const box = $('#d-eps');
    box.innerHTML = '<div class="skeleton"><i style="height:90px"></i><i style="height:90px"></i></div>';
    let r;
    try { r = await M.api(`episodes/${encodeURIComponent(d.id)}/${encodeURIComponent(s.id)}`); } catch (e) { box.innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
    box.replaceChildren(...r.items.map((e) => {
      const el = document.createElement('div');
      el.className = 'ep' + (e.played ? ' watched' : '') + (e.available === false ? ' na' : '');
      el.setAttribute('role', 'button');
      el.tabIndex = 0;
      const rt = [e.runtime ? M.mins(e.runtime) : '', e.played ? 'Watched' : (e.progress > 0.01 ? `${M.mins(e.runtime - e.position)} left` : ''), e.quality || ''].filter(Boolean).join(' · ');
      el.innerHTML = `<div class="wthumb"><img alt="" loading="lazy"><span class="glyph"><svg width="16" height="16"><use href="#i-play"/></svg></span>${e.progress > 0.01 && !e.played ? '<div class="pbar"><i></i></div>' : ''}</div>
        <div><b>${e.episode != null ? e.episode + '. ' : ''}${esc(e.name)}</b><span class="rt">${esc(rt)}</span></div>${e.overview ? `<p>${esc(e.overview)}</p>` : ''}`;
      M.loadImg(el.querySelector('img'), e.thumb || d.backdrop, `Episode ${e.episode ?? ''}`);
      const bar = el.querySelector('.pbar i');
      if (bar) bar.style.width = Math.round(e.progress * 100) + '%';
      el.addEventListener('click', () => {
        if (e.available === false) { M.toast('That episode isn’t on your PC'); return; }
        M.play(e.id);
      });
      return el;
    }));
    if (d.next_up && d.next_up.season === s.number) {
      const idx = r.items.findIndex((e) => e.id === d.next_up.id);
      if (idx > 2) requestAnimationFrame(() => box.children[idx] && box.children[idx].scrollIntoView({ block: 'center' }));
    }
  }

  // ---------------------------------------------------------------- library grid
  async function lib(kind) {
    const v = $('#v-lib');
    v.innerHTML = '<div class="skeleton" style="margin-top:20px"><i></i></div>';
    let d;
    try { d = await M.api('library/' + kind); } catch (e) { v.innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
    v.innerHTML = `<h1 class="page-title">${esc(d.title)}</h1><div class="grid"></div>`;
    $('.grid', v).replaceChildren(...d.items.map(posterCard));
  }

  // ---------------------------------------------------------------- downloads
  let dlTimer = null;
  M.download = async (id) => {
    try {
      await M.api('downloads', { id });
      M.toast('Your PC is making a copy. It’s in Downloads when ready.', 3200);
      M.buzz(10);
    } catch (e) { M.toast(e.message); }
  };
  async function downloads() {
    clearTimeout(dlTimer);
    let d;
    try { d = await M.api('downloads', undefined, { quiet: true }); } catch (e) { return; }
    const list = $('#dl-list');
    $('#dl-empty').hidden = !!d.items.length;
    list.replaceChildren(...d.items.map((x) => {
      const el = document.createElement('div');
      el.className = 'dl';
      const state = { queued: 'Waiting', working: `Making it · ${Math.round((x.progress || 0) * 100)}%`, ready: `Ready · ${(x.size / 1e9).toFixed(2)} GB`, error: x.message || 'Failed' }[x.state] || x.state;
      el.innerHTML = `<img alt="">
        <div><b>${esc(x.title)}</b><span>${esc(x.label)} · ${esc(state)}</span>${x.state === 'working' || x.state === 'queued' ? `<div class="bar"><i style="width:${Math.round((x.progress || 0) * 100)}%"></i></div>` : ''}</div>
        <div class="acts">${x.url ? `<a class="iconbtn" href="${x.url}" download aria-label="Save to phone"><svg width="18" height="18"><use href="#i-download"/></svg></a>` : ''}
        <button class="iconbtn" data-del aria-label="Remove"><svg width="18" height="18"><use href="#i-trash"/></svg></button></div>`;
      M.loadImg(el.querySelector('img'), x.poster);
      el.querySelector('[data-del]').addEventListener('click', async () => {
        try { await M.api('downloads/' + x.id, undefined, { method: 'DELETE' }); downloads(); } catch (e) { M.toast(e.message); }
      });
      return el;
    }));
    if (d.items.some((x) => x.state === 'working' || x.state === 'queued') && route().name === 'downloads') dlTimer = setTimeout(downloads, 2000);
  }

  // ---------------------------------------------------------------- settings
  function settingsView() {
    const seg = (box, val, set) => {
      $$('button', box).forEach((b) => b.setAttribute('aria-checked', String(b.dataset.v === val)));
      box.onclick = (e) => { const b = e.target.closest('button'); if (!b) return; set(b.dataset.v); $$('button', box).forEach((x) => x.setAttribute('aria-checked', String(x === b))); M.buzz(6); };
    };
    seg($('#set-theme'), M.prefs.theme, (v) => { M.prefs.theme = v; M.savePrefs(); applyTheme(); });
    const qbox = $('#set-quality');
    qbox.innerHTML = (M.me.qualities || []).map((x) => `<button data-v="${x.key}">${esc(x.label.replace(/ · .*/, ''))}</button>`).join('');
    seg(qbox, M.prefs.quality || M.me.quality || 'auto', (v) => { M.prefs.quality = v; M.savePrefs(); });
    const sw = (el, key) => {
      el.setAttribute('aria-checked', String(!!M.prefs[key]));
      el.onclick = () => { M.prefs[key] = !M.prefs[key]; M.savePrefs(); el.setAttribute('aria-checked', String(!!M.prefs[key])); M.buzz(6); };
    };
    sw($('#set-autoplay'), 'autoplay');
    sw($('#set-stats'), 'stats');
    $('#set-dev-name').textContent = M.me.device.name;
    $('#set-dev-sub').textContent = M.me.owner ? `Paired with ${M.me.pc}` : `Signed in as ${M.me.user} on ${M.me.pc}`;
    const ios = /iPhone|iPad|iPod/.test(navigator.userAgent);
    const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
    $('#set-install-row').hidden = standalone || !(installPrompt || ios);
    if (ios && !installPrompt) {
      $('#set-install-sub').textContent = 'In Safari: tap Share, then “Add to Home Screen”';
      $('#set-install').hidden = true;
    }
  }
  let installPrompt = null;
  window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); installPrompt = e; });
  $('#set-install').addEventListener('click', async () => { if (installPrompt) { installPrompt.prompt(); installPrompt = null; } });
  $('#set-signout').addEventListener('click', async () => {
    if (!confirm('Sign this phone out? You’ll need to scan the QR code again.')) return;
    try { await M.api('signout', {}); } catch (e) { /* signed out anyway */ }
    location.replace('/r');
  });

  // ---------------------------------------------------------------- router
  const TABS = { home: 'home', search: 'search', downloads: 'downloads', remote: 'remote', settings: null, item: null, lib: 'home' };
  function route() {
    const h = location.hash.replace(/^#/, '');
    if (!h.startsWith('/')) return { name: 'home' };
    const [path, qs] = h.slice(1).split('?');
    const [name, ...rest] = path.split('/');
    return { name: name || 'home', id: rest.length ? decodeURIComponent(rest.join('/')) : null, params: new URLSearchParams(qs || '') };
  }
  M.route = route;
  let current = null;
  function render() {
    const r = route();
    if (r.name === 'play') { M.player && M.player.openFromRoute(r.id); if (!current) show('home'); return; }
    if (M.player && M.player.isOpen()) M.player.close(true);
    show(r.name in TABS ? r.name : 'home', r);
  }
  function show(name, r = {}) {
    const prev = current;
    current = name;
    const viewId = { home: 'v-home', search: 'v-search', item: 'v-item', lib: 'v-lib', downloads: 'v-downloads', remote: 'v-remote', settings: 'v-settings' }[name];
    $$('.view').forEach((v) => v.classList.toggle('on', v.id === viewId));
    document.body.classList.toggle('on-item', name === 'item');
    document.body.classList.toggle('on-lib', name === 'lib');
    const tab = TABS[name] === undefined ? null : TABS[name];
    $$('#tabs a').forEach((a) => a.classList.toggle('on', a.dataset.tab === tab));
    moveInk();
    if (prev !== name || name === 'item' || name === 'lib') window.scrollTo({ top: 0 });
    if (name !== 'item') $('#ambient').classList.remove('on');
    if (prev === 'remote' && name !== 'remote' && M.remote) M.remote.leave();
    if (name === 'home') home();
    else if (name === 'item') item(r.id, r.params);
    else if (name === 'lib') lib(r.id);
    else if (name === 'downloads') downloads();
    else if (name === 'settings') settingsView();
    else if (name === 'remote' && M.remote) M.remote.enter();
    else if (name === 'search') setTimeout(() => { if (!q.value) q.focus(); }, 300);
  }
  function moveInk() {
    const a = $('#tabs a.on');
    const ink = $('#tab-ink');
    if (!a) { ink.style.opacity = '0'; return; }
    ink.style.opacity = '1';
    ink.style.width = a.offsetWidth + 'px';
    ink.style.transform = `translateX(${a.offsetLeft - 5}px)`;
  }
  const startLen = history.length;   // Back never leaves the app
  $('#top-back').addEventListener('click', () => { if (history.length > startLen) history.back(); else location.replace('#/home'); });
  window.addEventListener('hashchange', render);
  window.addEventListener('resize', moveInk);
  window.addEventListener('scroll', () => $('#top').classList.toggle('scrolled', window.scrollY > 8), { passive: true });
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(moveInk);

  // ---------------------------------------------------------------- playing
  M.playOnPC = async (id, start) => {
    try {
      await M.api('pcplay', { id, start: start ?? null });
      M.toast('Starting on your PC');
      M.buzz([10, 40, 10]);
      if (route().name !== 'remote') location.hash = '#/remote';
    } catch (e) { M.toast(e.message); }
  };

  // generic sheet (option lists)
  M.sheet = (html, onClick) => {
    const w = $('#sheet');
    $('#sheet-body').innerHTML = html;
    w.hidden = false;
    w.classList.remove('closing');
    document.body.classList.add('locked');
    w.onclick = (e) => {
      if (e.target.closest('[data-close]')) return M.closeSheet();
      if (onClick) onClick(e);
    };
  };
  M.closeSheet = () => {
    const w = $('#sheet');
    if (w.hidden) return;
    w.classList.add('closing');
    document.body.classList.remove('locked');
    setTimeout(() => { w.hidden = true; w.classList.remove('closing'); }, 260);
  };

  // ---------------------------------------------------------------- start
  async function boot() {
    try {
      const d = await M.api('me');
      M.me = d;
      $('#tab-remote').hidden = !d.owner;
      setLink(d.offline ? 'warn' : 'live', d.offline ? 'Jellyfin away' : 'Connected');
    } catch (e) {
      if (e.status === 401) return;
      setLink('lost', 'Can’t reach your PC');
      setTimeout(boot, 4000);
      return;
    }
    render();
    if ('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('/r/sw.js', { scope: '/r/' }).catch(() => {});
  }

  async function start() {
    const h = location.hash.replace(/^#/, '');
    if (h && !h.startsWith('/')) {
      const handoff = h.startsWith('h=');
      const token = decodeURIComponent(handoff ? h.slice(2) : h);
      history.replaceState(null, '', location.pathname + '#/home');
      let paired = false;
      if (handoff) {  // an installed app's first start: maybe it already shares the browser's cookie
        try { await M.api('me', undefined, { noPairScreen: true }); paired = true; } catch (e) { /* not yet */ }
      }
      if (!paired) {
        try { await pairWith(token); } catch (e) {
          if (e.status === 401) { showPair(e.message); return; }
        }
      }
    }
    boot();
  }
  // a QR code scanned again while the page is open only changes the fragment
  window.addEventListener('hashchange', () => { const h = location.hash.slice(1); if (h && !h.startsWith('/')) location.reload(); });
  start();
})();
