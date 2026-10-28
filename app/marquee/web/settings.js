/* Settings shell (window.MarqueeSettings.mount(page)): the category list, search across every category,
   "Advanced" disclosures that stay open across redraws, and short hints with an (i) for the long version.
   It works on the rendered rows (.set and friends), so panels drawn by other modules are covered too:
   app.js renders every category's pane up front, this file only shows, hides, filters and highlights. */
(() => {
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const icon = (id, size = 18) => `<svg width="${size}" height="${size}" aria-hidden="true"><use href="#i-${id}"/></svg>`;
function toast(msg, opts = {}) {
  let box = $('#toasts');
  if (!box) { box = document.createElement('div'); box.id = 'toasts'; box.className = 'toasts'; document.body.append(box); }
  const t = document.createElement('div');
  t.className = 'toast';
  t.innerHTML = `${opts.icon ? icon(opts.icon, 18) : ''}<span>${esc(msg)}</span>`;
  box.append(t);
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 300); }, opts.ms || 4200);
  while (box.children.length > 3) box.firstElementChild.remove();
}

const CATS = [
  { id: 'general', name: 'General', icon: 'sun', hint: 'Theme, previews, keyboard' },
  { id: 'playback', name: 'Playback', icon: 'play', hint: 'Quality, sound, subtitles' },
  { id: 'library', name: 'Library & requests', icon: 'film', hint: 'Servers, folders, smart seasons' },
  { id: 'storage', name: 'Storage', icon: 'sx-disk', hint: 'Space, cleanup' },
  { id: 'ai', name: 'Taste & AI', icon: 'sparkle', hint: 'AI provider, taste & memory' },
  { id: 'assistant', name: 'Assistant & voice', icon: 'sx-mic', hint: 'Voice control' },
  { id: 'index', name: 'Media index', icon: 'search', hint: 'Scene search, content filter' },
  { id: 'phone', name: 'Phone & casting', icon: 'phone', hint: 'Atlas Mobile, TVs' },
  { id: 'sharing', name: 'Sharing', icon: 'share', hint: 'Google Drive links' },
  { id: 'stack', name: 'Your stack', icon: 'sx-stack', hint: 'Media Hub' },
  { id: 'about', name: 'About & updates', icon: 'info', hint: 'Version, updates' },
];
const ALIAS = { appearance: 'general', theme: 'general', video: 'playback', sound: 'playback', audio: 'playback', subtitles: 'playback',
  requests: 'library', servers: 'library', seerr: 'library', space: 'storage', taste: 'ai', memory: 'ai', chat: 'ai', voice: 'assistant',
  mobile: 'phone', remote: 'phone', cast: 'phone', casting: 'phone', share: 'sharing', drive: 'sharing', updates: 'about', update: 'about' };
const catById = id => CATS.find(c => c.id === id);
const resolve = id => { id = String(id || '').toLowerCase(); return catById(id) ? id : ALIAS[id] || null; };

/* Synonyms, by the setting's key (or the row's label when it has no key): what people type that isn't on the row. */
const KW = {
  theme: 'dark mode light mode night appearance colour color look',
  previews: 'hover trailer clip preview autoplay', details_autoplay: 'title page trailer background video autoplay',
  quality: '4k uhd 2160p 1080p hd resolution bitrate transcode', upscaler: '4k upscale upscaling sharpen nvidia rtx vsr super resolution anime4k scaling',
  sdr_to_hdr: 'hdr rtx tone mapping brightness 4k', rtx_hdr: 'hdr nvidia', hwdec: 'hardware decoding gpu nvdec dxva acceleration glitches hwdec',
  seek_step: 'seek skip arrow keys jump rewind forward', autoplay_next: 'binge next episode continuous', fullscreen_on_play: 'fullscreen full screen maximise maximize window play', credits_mini: 'credits up next end',
  interpolation: 'smooth motion frame interpolation judder 60fps fps', skip_intros: 'skip intro opening recap',
  stable_volume: 'loudness normalize volume compressor', night_mode: 'night quiet loudness dynamic range neighbours',
  voice_focus: 'dialogue voice speech clarity isolation hear', dialogue_boost: 'dialogue speech voice', base_volume: 'volume loud level start',
  passthrough: 'dolby atmos dts truehd receiver soundbar bitstream hdmi audio', audio_device: 'speakers headphones output device sound card hdmi audio',
  sub_lang: 'subtitles subs captions cc language srt', audio_lang_anime: 'anime audio language japanese dub dual audio',
  ai_provider: 'ai key openai api chatgpt llm localai local assistant built-in classic provider model', ai_url: 'ai key endpoint openai api url',
  ai_key: 'ai key api token secret openai', ai_model: 'ai model gpt', ai_builtin_size: 'ai model size built-in', ai_builtin_gpu: 'ai gpu graphics card built-in', ai_vram_gb: 'vram gpu memory limit cap graphics memory ai', ai_while_watching: 'ai background gpu while watching playing movie performance lag stutter',
  builtin: 'ai built-in local model download offline', seerr: 'requests jellyseerr overseerr server address', seerr_form: 'requests seerr password connect login sign in',
  qbt_url: 'qbittorrent torrent downloads progress', autosort_after: 'sort downloads organise organize move', watch_folders: 'downloads folders watch path',
  library_folders: 'folders media path offline drive', mylist_sync_favorites: 'favourites favorites my list sync', logout: 'sign out log out account jellyfin user server',
  mobile: 'phone mobile iphone android remote tailscale vpn qr pair app tv television google tv code', mobile_bind: 'tailscale vpn lan wifi home network bind', mobile_max_streams: 'phones streams transcode limit',
  mobile_quality: 'phone quality 1080p 720p bitrate', mobile_jellyfin_signin: 'friends jellyfin users accounts phone', 'mobile-https': 'https certificate iphone tailscale',
  'mobile-firewall': 'firewall windows blocked', 'paired phones': 'phone devices remove rename pair', cast: 'cast chromecast airplay tv television',
  cast_quality: 'cast tv quality 1080p 4k', cast_4k_hdr: '4k hdr tv cast', 'tvs on your network': 'tv chromecast airplay devices cast', network: 'firewall cast tv port',
  storage_limit_gb: 'space disk storage limit gb full quota', storage_enforce: 'space full limit over', delete_after_watch: 'delete remove cleanup space watched auto', shrink_4k_watched: 'shrink 4k 2160p 1080p downgrade compress re-encode space save watched',
  delete_after_watch_days: 'delete wait days', delete_whole_seasons: 'delete seasons', delete_unwatched_days: 'delete unwatched cleanup space old',
  delete_torrents: 'delete torrent seeding qbittorrent hardlink space', smart_request: 'smart seasons next season request auto', smart_request_at: 'smart seasons percent request',
  radarr_url: 'radarr movies arr api key', radarr_key: 'radarr api key', sonarr_url: 'sonarr shows tv arr api key', sonarr_key: 'sonarr api key',
  preview: 'cleanup preview delete space', refresh: 'storage measure space disk', mqsdash: 'space disk usage free storage',
  client_id: 'google drive client id oauth', client_secret: 'google drive client secret oauth', shareCap: 'upload speed cap bandwidth mbit throttle limit',
  'share-connect': 'google drive connect sign in', 'share-disconnect': 'google drive disconnect', update_check: 'updates auto update version upgrade', update_channel: 'beta channel stable pre-release',
  updCheck: 'updates check version', indexlib: 'index scan library scene search', sens: 'blur skip violence gore nudity content filter parental',
  'index-install': 'index install scene search model', learning: 'taste learn history recommendations', tasteactions: 'taste letterboxd import memory note reset launchpad',
  tastecard: 'taste memory genres favourite people', voice: 'voice microphone mic wake word hey atlas push to talk speech', hotkey: 'voice key push to talk hotkey f8',
  stt_model: 'voice speech recognition whisper model', stt_device: 'voice gpu cpu cuda', wake_engine: 'wake word hey atlas', language: 'voice language speech',
  'voice-install': 'voice install download speech', 'ask atlas': 'chat assistant', 'media hub': 'stack hub health backups downloads vpn phone alerts ntfy watchdog',
  shortcuts: 'keyboard keys hotkeys', 'release notes': 'changelog what changed version',
};
const UNIT = '.set, .tp-learn, .tp-actions, .tp-card, .mqs-dash';
const KEYATTR = ['switch', 'seg', 'set', 'text', 'list', 'range', 'sw', 'mqsSeg', 'mqsSet', 'arr', 'shareCfg', 'updSet', 'vset'];

const X = { page: null, cat: 'general', q: '', mo: null, busy: false, adv: new Set(), raf: 0, tip: null };
try { JSON.parse(localStorage.getItem('marquee.settings.adv') || '[]').forEach(a => X.adv.add(a)); } catch (e) { /* private mode */ }

/* ================================================================== mount */
function mount(page) {
  X.page = page;
  X.q = '';
  page.classList.add('setx-page');
  sprite();
  const head = $('.setx-head', page);
  head.insertAdjacentHTML('beforeend', `<label class="setx-search glass" role="search">${icon('search', 18)}
      <input type="search" id="setx-q" placeholder="Search settings" autocomplete="off" spellcheck="false" aria-label="Search settings" aria-controls="setx-main">
      <kbd aria-hidden="true">/</kbd><button type="button" class="setx-clear" data-sx-clear aria-label="Clear search" hidden>${icon('close', 14)}</button></label>`);
  const main = $('.setx-main', page);
  main.id = 'setx-main';
  main.insertAdjacentHTML('afterbegin', `<p class="setx-status" aria-live="polite"></p><div class="setx-empty" hidden></div>`);
  $('.setx', page).insertAdjacentHTML('afterbegin', `<nav class="setx-nav glass" aria-label="Settings categories"><div class="setx-nav-in" role="tablist">
    ${CATS.map(c => `<button role="tab" data-sx-cat="${c.id}" aria-selected="false" aria-controls="setx-main">${icon(c.icon, 17)}<span>${esc(c.name)}</span></button>`).join('')}</div></nav>`);
  for (const p of $$('.setx-pane', page)) {
    const c = catById(p.dataset.cat);
    p.insertAdjacentHTML('afterbegin', `<div class="setx-pane-head sx-keep"><h3>${icon(c.icon, 16)} ${esc(c.name)}</h3><span class="setx-count"></span>
      <button class="btn small ghost" data-sx-open="${c.id}" aria-label="Open ${esc(c.name)}">Open ${icon('right', 14)}</button></div>`);
  }
  const fromHash = resolve((location.hash.match(/^#\/?settings\/([\w-]+)/) || [])[1]);
  let last = null;
  try { last = resolve(localStorage.getItem('marquee.settings.cat')); } catch (e) { /* private mode */ }
  show(fromHash || last || 'general', { instant: true });
  loadVoice();   // drawn up front so search finds them too
  loadStack();
  page.addEventListener('click', onClick);
  page.addEventListener('toggle', onToggle, true);
  page.addEventListener('input', e => { if (e.target.id === 'setx-q') { X.q = e.target.value; search(); } });
  page.addEventListener('keydown', onKey);
  page.addEventListener('pointerover', tipOver);
  page.addEventListener('focusin', tipOver);
  page.addEventListener('pointerout', tipOut);
  page.addEventListener('focusout', tipOut);
  if (X.mo) X.mo.disconnect();
  X.mo = new MutationObserver(() => { if (!X.busy) schedule(); });
  X.mo.observe(main, { childList: true, subtree: true });
  tidy();
}

/* Modules redraw their panels whenever they like: tidy up after them on the next frame. */
function schedule() { clearTimeout(X.raf); X.raf = setTimeout(tidy, 16); }
function quietly(fn) {
  X.busy = true;
  try { fn(); } finally { if (X.mo) X.mo.takeRecords(); X.busy = false; }
}
function tidy() {
  if (!X.page || !document.contains(X.page)) { if (X.mo) X.mo.disconnect(); return; }
  quietly(() => {
    condense(X.page);
    for (const d of $$('details[data-adv]', X.page)) if (X.adv.has(d.dataset.adv)) d.open = true;
    if (X.q.trim()) apply();
  });
  if (X.cat === 'stack') loadStack();
}

/* ================================================================== categories */
function show(id, o = {}) {
  id = resolve(id) || 'general';
  const changed = id !== X.cat || o.instant;
  X.cat = id;
  try { localStorage.setItem('marquee.settings.cat', id); } catch (e) { /* private mode */ }
  const want = `#/settings/${id}`;
  if (location.hash !== want) history.replaceState(history.state, '', want);
  for (const b of $$('.setx-nav [data-sx-cat]', X.page)) {
    const on = b.dataset.sxCat === id;
    b.setAttribute('aria-selected', on);
    b.tabIndex = on ? 0 : -1;
    if (on) { const nav = b.parentElement; if (nav.scrollWidth > nav.clientWidth) nav.scrollTo({ left: b.offsetLeft - (nav.clientWidth - b.offsetWidth) / 2, behavior: reduced.matches || o.instant ? 'auto' : 'smooth' }); }
  }
  if (X.q.trim() && !o.keepSearch) return;
  quietly(() => {
    for (const p of $$('.setx-pane', X.page)) {
      const on = p.dataset.cat === id;
      p.hidden = !on;
      if (on && changed && !o.instant && !reduced.matches) { p.classList.remove('sx-enter'); void p.offsetWidth; p.classList.add('sx-enter'); }
    }
  });
  if (id === 'assistant') loadVoice();
  if (id === 'stack') loadStack();
}

/* ================================================================== search */
const reEsc = t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const wordStart = t => new RegExp('(?:^|[^a-z0-9])' + reEsc(t));   // "tv" finds "TVs", and "phone" doesn't find "headphones"
const norm = s => String(s || '').toLowerCase().normalize('NFKD').replace(/[̀-ͯ]/g, '').replace(/[’']/g, '');
function keyOf(u) {
  for (const el of $$('[data-switch], [data-seg], [data-set], [data-text], [data-list], [data-range], [data-sw], [data-mqs-seg], [data-mqs-set], [data-arr], [data-share-cfg], [data-upd-set], [data-vset], [data-share-cap], [data-index-lib], [data-sens], [data-tact="learning"], [data-builtin-install], [data-action], [data-act], [data-mqs-act], [data-upd-check]', u)) {
    for (const a of KEYATTR) if (el.dataset[a]) return el.dataset[a];
    if ('shareCap' in el.dataset) return 'shareCap';
    if ('indexLib' in el.dataset) return 'indexlib';
    if (el.dataset.sens) return 'sens';
    if (el.dataset.tact === 'learning') return 'learning';
    if (el.dataset.builtinInstall) return 'builtin';
    if ('updCheck' in el.dataset) return 'updCheck';
    if (el.dataset.mqsAct) return el.dataset.mqsAct;
    if (el.dataset.action || el.dataset.act) return el.dataset.action || el.dataset.act;
  }
  if (u.id === 'seerr-form' || $('#seerr-form', u)) return 'seerr_form';
  if (u.classList.contains('tp-actions')) return 'tasteactions';
  if (u.classList.contains('tp-card')) return 'tastecard';
  if (u.classList.contains('mqs-dash')) return 'mqsdash';
  return '';
}
function hay(u) {
  if (!u._sx || u._sx.html !== u.childElementCount + ':' + u.textContent.length) {
    const lb = $('.lbl b, h3, b', u);
    const label = norm((lb && lb.firstChild ? lb.firstChild.textContent : '').trim());
    const tips = $$('[data-tip]', u).map(t => t.dataset.tip).join(' ');
    const key = keyOf(u);
    const kw = [KW[key], KW[label]].filter(Boolean).join(' ');
    const parts = [], w = document.createTreeWalker(u, NodeFilter.SHOW_TEXT);
    while (w.nextNode()) parts.push(w.currentNode.nodeValue);
    const text = parts.join(' ');   // "Answers come from" + "LocalAI" stay two words
    u._sx = { html: u.childElementCount + ':' + u.textContent.length, text: norm(text + ' ' + tips), kw: norm(kw + ' ' + key.replace(/_/g, ' ')) };
  }
  return u._sx;
}
function units(root) {
  const all = $$(UNIT, root);
  return all.filter(u => !all.some(o => o !== u && o.contains(u)));
}
function search() {
  const q = X.q.trim();
  const clr = $('[data-sx-clear]', X.page);
  if (clr) clr.hidden = !q;
  quietly(() => apply(true));
}
function clearMarks() {
  for (const m of $$('span.sx-txt', X.page)) { const p = m.parentNode; m.replaceWith(document.createTextNode(m.textContent)); p.normalize(); }
  $$('.sx-why, .sx-go', X.page).forEach(x => x.remove());
  $$('.sx-hide', X.page).forEach(x => x.classList.remove('sx-hide'));
  $$('.sx-hit', X.page).forEach(x => x.classList.remove('sx-hit', 'sx-in'));
  for (const d of $$('details[data-sx-opened]', X.page)) { d.removeAttribute('data-sx-opened'); if (!X.adv.has(d.dataset.adv)) d.open = false; }
}
function apply(fresh) {
  const page = X.page, q = norm(X.q.trim());
  clearMarks();
  const status = $('.setx-status', page), empty = $('.setx-empty', page);
  if (!q) {
    page.classList.remove('sx-searching');
    empty.hidden = true;
    status.textContent = '';
    for (const p of $$('.setx-pane', page)) p.hidden = p.dataset.cat !== X.cat;
    return;
  }
  page.classList.add('sx-searching');
  const toks = q.split(/\s+/).filter(Boolean);
  const res = toks.map(wordStart), whole = q.length > 3 ? wordStart(q) : null;
  let total = 0, k = 0;
  for (const pane of $$('.setx-pane', page)) {
    const hits = new Set();
    for (const u of units(pane)) {
      const h = hay(u);
      const lit = toks.filter((t, i) => res[i].test(h.text));
      if (res.every(r => r.test(h.text) || r.test(h.kw)) || (whole && whole.test(h.kw))) { hits.add(u); u._lit = lit; }
    }
    pane.hidden = !hits.size;
    $('.setx-count', pane).textContent = hits.size ? `${hits.size} match${hits.size === 1 ? '' : 'es'}` : '';
    if (!hits.size) continue;
    total += hits.size;
    prune(pane, hits);
    for (const u of hits) {
      u.classList.add('sx-hit');
      if (u.tagName === 'DETAILS' && !u.open) { u.open = true; u.dataset.sxOpened = '1'; }
      if (fresh && !reduced.matches) { u.style.setProperty('--k', Math.min(k++, 12)); u.classList.add('sx-in'); }
      mark(u, u._lit.length ? u._lit : []);
      const kwOnly = toks.filter(t => !u._lit.includes(t));
      const lbl = $('.lbl', u) || $('h3', u) || u;
      if (kwOnly.length) lbl.insertAdjacentHTML('beforeend', `<span class="sx-why">matches “${esc(kwOnly.join(' '))}”</span>`);
      lbl.insertAdjacentHTML('beforeend', `<button type="button" class="sx-go" data-sx-go="${pane.dataset.cat}" aria-label="Show in ${esc(catById(pane.dataset.cat).name)}">${esc(catById(pane.dataset.cat).name)} ${icon('right', 12)}</button>`);
    }
  }
  empty.hidden = !!total;
  if (!total) empty.innerHTML = `<span class="setx-empty-orb">${icon('search', 22)}</span><b>No settings match “${esc(X.q.trim())}”</b>
    <p>Try a simpler word, like “subtitles”, “4k”, “phone” or “space”.</p><button class="btn small ghost" data-sx-clear>Clear search</button>`;
  status.textContent = total ? `${total} setting${total === 1 ? '' : 's'} found` : '';
}
/* Hide everything in a pane that isn't a match, keeping card titles and the path to each match. */
function prune(el, hits) {
  let any = false;
  for (const c of [...el.children]) {
    if (c.classList.contains('sx-keep')) continue;
    if (hits.has(c)) { any = true; continue; }
    if (c.matches(UNIT)) { c.classList.add('sx-hide'); continue; }
    if (c.matches('h2, h3, summary')) continue;
    if (c.querySelector(UNIT)) {
      if (prune(c, hits)) { any = true; if (c.tagName === 'DETAILS' && !c.open) { c.open = true; c.dataset.sxOpened = '1'; } }
      else c.classList.add('sx-hide');
    } else if (c.querySelector('h2, h3')) {
      for (const x of c.children) if (!x.matches('h2, h3') && !x.querySelector('h2, h3')) x.classList.add('sx-hide');
    } else c.classList.add('sx-hide');
  }
  return any;
}
function mark(u, toks) {
  if (!toks.length) return;
  const re = new RegExp('(^|[^a-z0-9])(' + toks.map(reEsc).sort((a, b) => b.length - a.length).join('|') + ')', 'gi');
  const w = document.createTreeWalker(u, NodeFilter.SHOW_TEXT, { acceptNode: n => n.parentElement.closest('select, option, textarea, script, style, .sx-why, .sx-go') || !n.nodeValue.trim() ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
  const nodes = [];
  while (w.nextNode()) nodes.push(w.currentNode);
  for (const n of nodes) {
    const v = n.nodeValue, lower = norm(v);
    if (lower.length !== v.length) continue;   // accented text: skip rather than mis-highlight
    re.lastIndex = 0;
    if (!re.test(lower)) continue;
    const frag = Object.assign(document.createElement('span'), { className: 'sx-txt' });   // one inline box, so flex labels keep their spaces
    let at = 0;
    lower.replace(re, (m, pre, word, i) => { i += pre.length; frag.append(v.slice(at, i), Object.assign(document.createElement('mark'), { className: 'sx-mark', textContent: v.slice(i, i + word.length) })); at = i + word.length; return m; });
    frag.append(v.slice(at));
    n.replaceWith(frag);
  }
}
function jump(cat, row) {
  const input = $('#setx-q', X.page);
  X.q = '';
  if (input) input.value = '';
  search();
  show(cat);
  if (!row || !document.contains(row)) { scrollTo({ top: 0, behavior: reduced.matches ? 'auto' : 'smooth' }); return; }
  const d = row.closest('details');
  if (d && !d.open) d.open = true;
  setTimeout(() => {
    row.scrollIntoView({ block: 'center', behavior: reduced.matches ? 'auto' : 'smooth' });
    row.classList.remove('sx-pulse'); void row.offsetWidth; row.classList.add('sx-pulse');
    setTimeout(() => row.classList.remove('sx-pulse'), 2200);
    const f = $('button, input, select, textarea, a', row);
    if (f) f.focus({ preventScroll: true });
  }, reduced.matches ? 0 : 120);
}

/* ================================================================== condense: short hints, (i) for the rest */
function condense(root) {
  for (const sp of $$('.setx-pane .set .lbl > span:not([data-sx]), .setx-pane .tp-learn .lbl > span:not([data-sx])', root)) {
    sp.dataset.sx = '1';
    if (sp.children.length || sp.closest('.setx-account')) continue;
    const t = sp.textContent.trim();
    if (t.length <= 62) continue;
    const m = t.match(/^(.{14,64}?[.:;!?])\s/);
    const short = m ? m[1].replace(/[:;]$/, '.') : t.slice(0, 56).replace(/[\s,]+\S*$/, '') + '…';
    sp.textContent = short;
    const b = sp.parentElement.querySelector('b');
    (b || sp).insertAdjacentHTML('beforeend', tipBtn(t, b ? b.textContent : 'this setting'));
  }
  for (const p of $$('.setx-pane .panel > h2 + p:not([class]):not([data-sx]), .setx-pane .tp-head > div > h2 + p:not([data-sx])', root)) {
    p.dataset.sx = '1';
    const h = p.parentElement.querySelector('h2');
    if (!h || $('.sx-tip', h) || p.textContent.trim().length < 60 || p.querySelector('a, button')) continue;   // short status lines stay visible
    h.insertAdjacentHTML('beforeend', tipBtn(p.textContent.trim(), h.textContent.trim()));
    p.classList.add('sx-intro');
  }
}
const tipBtn = (text, about) => `<button type="button" class="sx-tip" data-tip="${esc(text)}" aria-label="About ${esc(about.trim())}: ${esc(text)}">i</button>`;
function tipOver(e) {
  const b = e.target.closest && e.target.closest('.sx-tip');
  if (!b) return;
  let box = X.tip;
  if (!box || !document.contains(box)) { box = X.tip = Object.assign(document.createElement('div'), { className: 'sx-tipbox', role: 'tooltip', id: 'sx-tipbox' }); document.body.append(box); }
  box.textContent = b.dataset.tip;
  box.classList.add('on');
  const r = b.getBoundingClientRect(), w = Math.min(300, innerWidth - 24);
  box.style.width = w + 'px';
  const left = Math.max(12, Math.min(innerWidth - w - 12, r.left + r.width / 2 - w / 2));
  const below = r.bottom + 8 + box.offsetHeight < innerHeight;
  box.style.left = left + 'px';
  box.style.top = (below ? r.bottom + 8 : r.top - 8 - box.offsetHeight) + 'px';
}
function tipOut(e) {
  if (!X.tip || !e.target.closest || !e.target.closest('.sx-tip')) return;
  if (e.relatedTarget && e.relatedTarget.closest && e.relatedTarget.closest('.sx-tip') === e.target.closest('.sx-tip')) return;
  X.tip.classList.remove('on');
}

/* ================================================================== events */
function onClick(e) {
  const t = e.target;
  const cat = t.closest('[data-sx-cat]');
  if (cat) {
    if (X.q.trim()) { jump(cat.dataset.sxCat); return; }
    show(cat.dataset.sxCat);
    if (innerWidth > 760 && scrollY > 120) scrollTo({ top: 0, behavior: reduced.matches ? 'auto' : 'smooth' });
    return;
  }
  const go = t.closest('[data-sx-go]');
  if (go) { e.preventDefault(); e.stopPropagation(); jump(go.dataset.sxGo, go.closest(UNIT)); return; }
  const open = t.closest('[data-sx-open]');
  if (open) { jump(open.dataset.sxOpen); return; }
  if (t.closest('[data-sx-clear]')) { const i = $('#setx-q', X.page); X.q = ''; i.value = ''; search(); i.focus(); return; }
  const tip = t.closest('.sx-tip');
  if (tip) { e.preventDefault(); e.stopPropagation(); if (X.tip && X.tip.classList.contains('on')) X.tip.classList.remove('on'); else tipOver({ target: tip }); }
}
function onToggle(e) {
  const d = e.target;
  if (!(d instanceof HTMLDetailsElement) || !d.dataset.adv || X.busy || d.dataset.sxOpened) return;
  d.open ? X.adv.add(d.dataset.adv) : X.adv.delete(d.dataset.adv);
  try { localStorage.setItem('marquee.settings.adv', JSON.stringify([...X.adv])); } catch (err) { /* private mode */ }
}
function onKey(e) {
  if (e.target.id === 'setx-q') {
    if (e.key === 'Escape') { e.stopPropagation(); e.preventDefault(); if (X.q) { X.q = ''; e.target.value = ''; search(); } else e.target.blur(); }
    if (e.key === 'Enter') { const g = $('.sx-go', X.page); if (g) { e.preventDefault(); g.click(); } }
    if (e.key === 'ArrowDown') { const f = $('.sx-hit button, .sx-hit input, .sx-hit select, .sx-hit a', X.page); if (f) { e.preventDefault(); f.focus(); } }
    return;
  }
  const tab = e.target.closest && e.target.closest('[data-sx-cat]');
  if (tab && /^Arrow(Up|Down|Left|Right)$|^Home$|^End$/.test(e.key)) {
    const tabs = $$('.setx-nav [data-sx-cat]', X.page), i = tabs.indexOf(tab);
    const n = e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : (i + (/Up|Left/.test(e.key) ? -1 : 1) + tabs.length) % tabs.length;
    e.preventDefault();
    tabs[n].focus();
    tabs[n].click();
  }
}
/* "/" and Ctrl+F search the settings while they're open (instead of the library). */
addEventListener('keydown', e => {
  if (!X.page || !document.contains(X.page) || !X.page.offsetParent) return;
  const a = document.activeElement, typing = a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName);
  const find = (e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'f';
  if (!find && (e.key !== '/' || typing || e.ctrlKey || e.metaKey || e.altKey)) return;
  if ($('.layer:not([hidden])')) return;   // search, a title page or a sheet is open on top
  const i = $('#setx-q', X.page);
  if (!i) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  i.focus();
  i.select();
}, true);
/* #/settings/<category> while Settings is open: switch in place instead of redrawing the page. */
addEventListener('hashchange', e => {
  const m = location.hash.match(/^#\/?settings(?:\/([\w-]+))?\/?$/);
  if (!m || !X.page || !document.contains(X.page)) return;
  e.stopImmediatePropagation();
  const id = resolve(m[1]) || X.cat;
  if (X.q.trim()) jump(id); else show(id);
}, true);

/* ================================================================== Assistant & voice */
const VOICE = { st: null, poll: 0 };
async function loadVoice() {
  const slot = $('[data-slot="voice"]', X.page);
  if (!slot) return;
  clearTimeout(VOICE.poll);
  let st;
  try { st = await (await fetch('/api/assistant/status')).json(); } catch (e) { st = null; }
  if (!document.contains(slot)) return;
  if (!st || st.ok === false) {
    slot.innerHTML = `<section class="panel glass setx-card" data-card="voice"><h2>${icon('sx-mic')} Voice control</h2>
      <div class="set"><div class="lbl"><b>Voice control</b><span>Not available in this version.</span></div></div></section>`;
    return;
  }
  VOICE.st = st;
  const s = st.settings || {}, ins = st.install || {}, have = (ins.installed || {}).voice;
  const mode = s.voice || 'off';
  const pills = (key, opts, cur, label) => `<div class="pill-toggle" role="group" aria-label="${esc(label)}">${opts.map(([v, n]) => `<button data-vset="${key}" data-v="${v}" aria-pressed="${String(cur) === String(v)}">${n}</button>`).join('')}</div>`;
  const ai = st.llm || {};
  slot.innerHTML = `<section class="panel glass setx-card" data-card="voice"><h2>${icon('sx-mic')} Voice control</h2>
      <p>Talk to Atlas instead of typing. Speech is recognised on this PC, and the microphone only opens while voice is on.</p>
      ${have ? '' : `<div class="set"><div class="lbl"><b>Voice pack</b><span>${ins.running ? esc(ins.step || 'Installing…') : ins.error ? 'Install failed: ' + esc(ins.error) : `Speech recognition that runs on this PC${(ins.download_mb || {}).voice ? ` (about ${ins.download_mb.voice} MB)` : ''}.`}</span></div>
        <button class="btn small" data-vact="install" ${ins.running ? 'disabled' : ''}>${ins.running ? 'Installing…' : 'Install'}</button></div>`}
      <div class="set stack"><div class="lbl"><b>Listening</b><span>${mode === 'wake_word' ? 'Say “Hey Atlas”, then your request.' : mode === 'push_to_talk' ? `Hold ${esc((s.hotkey || 'F8').toUpperCase())} while you speak.` : 'Off: the microphone stays closed.'}</span></div>
        ${pills('voice', [['off', 'Off'], ['push_to_talk', 'Push to talk'], ['wake_word', 'Wake word']], mode, 'Listening')}</div>
      <div class="set"><div class="lbl"><b>Push-to-talk key</b><span>Hold it while speaking, or tap it for one sentence.</span></div>
        <input class="input setx-key" data-vset="hotkey" value="${esc(s.hotkey || 'f8')}" maxlength="20" spellcheck="false" aria-label="Push-to-talk key"></div>
      <div class="set"><div class="lbl"><b>Answers come from</b><span>${ai.on ? esc(ai.name || 'your AI') : 'Classic replies (no AI)'}. Change it in Taste &amp; AI.</span></div>
        <button class="btn small ghost" data-sx-open="ai">AI provider ${icon('right', 14)}</button></div>
      <details class="setx-adv" data-adv="voice"><summary>Advanced</summary><div class="setx-adv-body">
        <div class="set"><div class="lbl"><b>Speech model</b><span>Bigger understands more, but is slower.</span></div>${pills('stt_model', [['tiny', 'Tiny'], ['base', 'Base'], ['small', 'Small']], s.stt_model || 'base', 'Speech model')}</div>
        <div class="set"><div class="lbl"><b>Recognise speech on</b><span>Auto uses the graphics card when it works.</span></div>${pills('stt_device', [['auto', 'Auto'], ['cuda', 'Graphics card'], ['cpu', 'Processor']], s.stt_device || 'auto', 'Recognise speech on')}</div>
        <div class="set"><div class="lbl"><b>Wake word engine</b><span>Whisper listens for “Hey Atlas”; openWakeWord is lighter.</span></div>${pills('wake_engine', [['whisper', 'Whisper'], ['openwakeword', 'openWakeWord']], s.wake_engine || 'whisper', 'Wake word engine')}</div>
        <div class="set"><div class="lbl"><b>Spoken language</b><span>Two-letter code, like en or es.</span></div>
          <input class="input setx-key" data-vset="language" value="${esc(s.language || 'en')}" maxlength="5" spellcheck="false" aria-label="Spoken language"></div>
      </div></details></section>`;
  if (ins.running) VOICE.poll = setTimeout(loadVoice, 2000);
}
async function voiceSave(key, v, btn) {
  try {
    if (key === 'voice') {
      const r = await fetch('/api/assistant/listen', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(v === 'off' ? { on: false } : { on: true, mode: v }) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.ok === false) throw new Error(d.message || 'Voice couldn’t start');
    } else {
      const r = await fetch('/api/assistant/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ [key]: v }) });
      if (!r.ok) throw new Error('Couldn’t save');
    }
    toast('Saved', { icon: 'check', ms: 1600 });
  } catch (err) { toast(err.message); }
  loadVoice();
}
document.addEventListener('click', async e => {
  const b = e.target.closest && e.target.closest('button[data-vset], [data-vact]');
  if (!b || !X.page || !X.page.contains(b)) return;
  e.stopPropagation();
  if (b.dataset.vact === 'install') {
    b.disabled = true;
    try { await fetch('/api/assistant/install', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ parts: ['voice', 'model'] }) }); toast('Installing voice…'); } catch (err) { toast(err.message); }
    return loadVoice();
  }
  $$(`button[data-vset="${b.dataset.vset}"]`, X.page).forEach(x => x.setAttribute('aria-pressed', x === b));
  voiceSave(b.dataset.vset, b.dataset.v, b);
}, true);
document.addEventListener('change', e => {
  const t = e.target;
  if (t.tagName !== 'INPUT' || !t.dataset.vset || !X.page || !X.page.contains(t)) return;
  const v = t.value.trim().toLowerCase();
  if (v) voiceSave(t.dataset.vset, v);
});

/* ================================================================== Your stack */
const STK = { at: 0, data: null, busy: false };
async function loadStack() {
  const slot = $('[data-slot="stack"]', X.page);
  if (!slot) return;
  if (!slot.firstElementChild) {
    slot.innerHTML = `<section class="panel glass setx-card" data-card="stack"><h2>${icon('sx-stack')} Your stack</h2>
      <p>The Media Hub checks on Jellyfin, Seerr and the downloaders, and backs up their settings nightly. Health checks, the request doctor, backups and phone alerts are on its page.</p>
      <div class="set"><div class="lbl"><b>Media Hub</b><span class="stx-line">Checking…</span></div><a class="btn small" href="#/stack">Open Your stack ${icon('right', 14)}</a></div>
      <div class="setx-stack-upd" hidden></div></section>`;
    window.MarqueeUpdate && MarqueeUpdate.mountStack($('.setx-stack-upd', slot));
  }
  if (STK.busy || Date.now() - STK.at < 30000) return draw();
  STK.busy = true;
  try { STK.data = await (await fetch('/api/stack')).json(); } catch (e) { STK.data = { error: e.message }; }
  STK.busy = false; STK.at = Date.now();
  draw();
  function draw() {
    const line = $('.stx-line', slot), d = STK.data;
    if (!line || !d) return;
    const s = d.summary || {};
    const sum = String(s.text || 'Checked').replace(/[.!?]?$/, '.');
    const text = d.error ? `Couldn’t check: ${d.error}` : d.source === 'hub' ? sum : `${sum} No Media Hub found, so Atlas runs its own read-only checks.`;
    if (line.textContent !== text) quietly(() => { line.textContent = text; delete line.dataset.sx; line.closest('.lbl').querySelectorAll('.sx-tip').forEach(x => x.remove()); condense(slot); });
  }
}

/* ================================================================== icons */
function sprite() {
  const defs = $('svg defs');
  if (!defs || $('#i-sx-mic')) return;
  defs.insertAdjacentHTML('beforeend', `
    <symbol id="i-sx-mic" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></g></symbol>
    <symbol id="i-sx-disk" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/></g></symbol>
    <symbol id="i-sx-stack" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 13 9 5 9-5"/></g></symbol>`);
}

window.MarqueeSettings = { mount, show, cats: CATS };
})();
