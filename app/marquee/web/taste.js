/* Marquee taste profile UI: shared helpers (window.MarqueeKit), the "Is this for you?" fit card (window.MarqueeFit)
   and Settings > Taste & memory (window.MarqueeTaste). Standalone: talks to /api/taste/* (marquee/taste_api.py) and
   uses app.css' tokens and classes so both themes match. */
(() => {
'use strict';

/* ================================================================== kit (shared by chat.js and launchpad.js) */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const icon = (id, size = 18) => `<svg width="${size}" height="${size}" aria-hidden="true"><use href="#i-${id}"/></svg>`;
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function api(path, body, method) {
  const opt = body !== undefined
    ? { method: method || 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : (method ? { method } : {});
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty */ }
  if (!r.ok || data.ok === false) { const e = new Error(data.message || r.statusText || 'Something went wrong'); e.status = r.status; throw e; }
  return data;
}
function toast(msg, opts = {}) {
  let box = $('#toasts');
  if (!box) { box = document.createElement('div'); box.id = 'toasts'; box.className = 'toasts'; document.body.append(box); }
  const t = document.createElement('div');
  t.className = 'toast';
  t.innerHTML = `${opts.icon ? icon(opts.icon) : ''}<span>${esc(msg)}</span>${opts.action ? `<button class="btn small on-dark">${esc(opts.action)}</button>` : ''}`;
  const dismiss = () => { t.classList.add('out'); setTimeout(() => t.remove(), 300); };
  if (opts.action) t.querySelector('button').onclick = () => { opts.onAction(); dismiss(); };
  box.append(t);
  setTimeout(dismiss, opts.ms || 3200);
  while (box.children.length > 3) box.firstElementChild.remove();
  return dismiss;
}
const PALETTE = [['#23408f', '#6f8fd6'], ['#14203f', '#2f55b5'], ['#ff8a1f', '#ff5f6d'], ['#1f7a55', '#5fb8a0'], ['#5a3fa0', '#a07fe0'], ['#0d1738', '#ff8a1f'], ['#2f55b5', '#5fc6e0']];
function grad(name) {
  let h = 0;
  for (const c of String(name || '')) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  const [a, b] = PALETTE[h % PALETTE.length];
  return `linear-gradient(150deg, ${a}, ${b})`;
}
function when(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000), days = (Date.now() - d) / 864e5;
  if (days < 1 && d.getDate() === new Date().getDate()) return 'today';
  if (days < 2) return 'yesterday';
  if (days < 7) return d.toLocaleDateString(undefined, { weekday: 'long' });
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: d.getFullYear() === new Date().getFullYear() ? undefined : 'numeric' });
}
/* Words that animate in one by one (like the Home greeting). */
function wordsHTML(text, step = 22) {
  return String(text || '').split(/(\s+)/).filter(Boolean).map((w, k) => `<span class="mq-w" style="animation-delay:${Math.min(k, 90) * step}ms">${esc(w)}</span>`).join('');
}
/* Which glass a title wears: clear = in your library, white = requested / partly here, smoky navy = requestable. */
const glassOf = c => c.source === 'library' || c.status === 5 ? 'is-owned' : (c.status >= 2 && c.status <= 4) ? 'is-requested' : 'is-requestable';
const STATUS_LABEL = { 2: 'Requested', 3: 'Downloading', 4: 'Partly here', 5: 'In your library' };
function fitTone(s) { return s >= 68 ? 'good' : s >= 45 ? 'mid' : 'low'; }
/* A compact title card for chat answers (ClearHome's chat-card: thumbnail beside details). Clicking it goes through
   app.js' own delegation: data-act="open" (library) or data-act="tmdb" (Seerr), so it opens the same preview. */
function titleCard(c, n = 0) {
  const lib = c.source === 'library';
  const kind = lib ? (c.type === 'Series' ? 'Series' : 'Film') : (c.type === 'tv' ? 'Series' : 'Film');
  const status = lib ? (c.played ? 'Watched' : 'In your library') : (STATUS_LABEL[c.status] || 'Request');
  const attrs = lib ? `data-act="open" data-id="${esc(c.id)}"` : `data-act="tmdb" data-tmdb="${esc(c.type)}:${esc(c.id)}"`;
  const fit = c.fit != null ? `<span class="tc-fit tone-${fitTone(c.fit)}" title="How well it fits your taste">${c.fit}% fit</span>` : '';
  return `<article class="tcard ${glassOf(c)}" role="button" tabindex="0" ${attrs} data-ref="${esc(norm(c.named || c.title))}" style="--i:${n}" aria-label="${esc(c.title)}${c.year ? ' (' + esc(c.year) + ')' : ''}: ${esc(status)}">
    <div class="tc-thumb${c.poster ? '' : ' noimg'}" style="--fb:${grad(c.title)}">${c.poster ? `<img src="${esc(c.poster)}" alt="" loading="lazy" decoding="async" onload="this.classList.add('loaded')" onerror="this.parentNode.classList.add('noimg');this.remove()">` : ''}<span class="tc-fb">${esc((c.title || '?').slice(0, 1))}</span></div>
    <div class="tc-body">
      <span class="tc-status">${!lib && !STATUS_LABEL[c.status] ? icon('plus', 12) : lib ? icon('play', 11) : ''}${esc(status)}</span>
      <b class="tc-title">${esc(c.title)}</b>
      <span class="tc-meta">${esc([c.year, kind, c.anime ? 'Anime' : null].filter(Boolean).join(' · '))}</span>
      ${fit}${c.why ? `<span class="tc-why">${esc(c.why)}</span>` : ''}
    </div></article>`;
}
const norm = s => String(s || '').toLowerCase().replace(/^(the|a|an)\s+/, '').replace(/[^a-z0-9]+/g, ' ').trim();
/* Keyboard: Enter/Space on a focused card clicks it (app.js does this for .card; tcards need it too). */
document.addEventListener('keydown', e => {
  const t = document.activeElement;
  if (!t || !t.classList || !t.classList.contains('tcard') || (e.key !== 'Enter' && e.key !== ' ')) return;
  e.preventDefault(); e.stopPropagation(); t.click();
}, true);

window.MarqueeKit = { $, $$, esc, icon, api, toast, grad, when, wordsHTML, titleCard, glassOf, fitTone, norm, sleep, reduced };

/* ================================================================== "Is this for you?" (details / preview) */
function fitKey(item) {
  if (!item) return null;
  if (item.type === 'movie' || item.type === 'tv') return `${item.type}/${item.id ?? item.tmdb}`;
  if (item.kind && item.tmdb && !item.id) return `${item.kind}/${item.tmdb}`;
  return item.id ? encodeURIComponent(item.id) : null;
}
const RING = 2 * Math.PI * 26;
function ring(score) {
  const v = Math.max(0, Math.min(100, score || 0)) / 100;
  return `<div class="fit-ring tone-${fitTone(score)}" role="img" aria-label="${score} out of 100 fit">
    <svg viewBox="0 0 64 64" width="64" height="64"><circle class="trk" cx="32" cy="32" r="26"/><circle class="arc" cx="32" cy="32" r="26" style="stroke-dasharray:${RING.toFixed(1)};--off:${(RING * (1 - v)).toFixed(1)}"/></svg>
    <b>${score}</b><small>fit</small></div>`;
}
const fitSeq = new WeakMap();
async function renderFit(el, item) {
  if (!el) return;
  const key = fitKey(item);
  if (!key) { el.innerHTML = ''; return; }
  const seq = (fitSeq.get(el) || 0) + 1;
  fitSeq.set(el, seq);
  el.classList.add('fit-slot');
  el.innerHTML = `<div class="fit glass is-loading" aria-busy="true"><div class="fit-ring skel-ring"></div><div class="fit-copy"><span class="eyebrow orange">Is this for you?</span><i class="mq-skel" style="width:46%"></i><i class="mq-skel" style="width:82%"></i></div></div>`;
  let d;
  try { d = await api(`/api/taste/fit/${key}`); }
  catch (e) { if (fitSeq.get(el) === seq) el.innerHTML = ''; return; }
  if (fitSeq.get(el) !== seq || !document.contains(el)) return;
  if (d.empty) {
    el.innerHTML = `<div class="fit glass is-empty"><span class="fit-orb">${icon('sparkle', 22)}</span><div class="fit-copy"><span class="eyebrow orange">Is this for you?</span>
      <p class="fit-comment">Tell Marquee what you like and it'll say whether titles suit you.</p></div>
      <button class="btn small ghost" data-fit-setup>Set up my taste</button></div>`;
    $('[data-fit-setup]', el).onclick = () => window.MarqueeLaunchpad && window.MarqueeLaunchpad.show({ rerun: true });
    return;
  }
  const tone = fitTone(d.score);
  el.innerHTML = `<div class="fit glass tone-${tone}${d.different ? ' is-different' : ''}">
    ${ring(d.score)}
    <div class="fit-copy">
      <div class="fit-head"><span class="eyebrow orange">Is this for you?</span>${d.different ? '<span class="fit-tag">Different from your usual</span>' : ''}</div>
      <h4 class="fit-verdict">${esc(d.verdict)}</h4>
      <p class="fit-comment"><span class="fit-text">${wordsHTML(d.comment)}</span>${d.ai_pending ? '<span class="mq-dots" aria-label="Thinking"><i></i><i></i><i></i></span>' : ''}</p>
      ${d.reasons && d.reasons.length ? `<div class="fit-reasons">${d.reasons.slice(0, 4).map(r => `<span>${esc(r)}</span>`).join('')}</div>` : ''}
      <span class="fit-by">${d.source === 'ai' ? `${icon('sparkle', 12)} ${esc(d.engine)} on your taste` : 'From your taste profile'}</span>
    </div>${starsHTML(d.mine)}</div>`;
  wireStars(el, key, item);
  requestAnimationFrame(() => el.querySelector('.fit')?.classList.add('in'));
  if (!d.ai_pending) return;
  let a;
  try { a = await api(`/api/taste/fit/${key}?ai=1`); } catch (e) { a = null; }
  if (fitSeq.get(el) !== seq || !document.contains(el)) return;
  const p = $('.fit-comment', el);
  $('.mq-dots', el)?.remove();
  if (!a || a.source !== 'ai' || !p) return;
  const txt = $('.fit-text', p);
  txt.classList.add('swap-out');
  await sleep(reduced.matches ? 0 : 220);
  txt.innerHTML = wordsHTML(a.comment);
  txt.classList.remove('swap-out');
  const by = $('.fit-by', el);
  if (by) by.innerHTML = `${icon('sparkle', 12)} ${esc(a.engine)} on your taste`;
}
/* Your own 1-5 stars (click the same star again to clear). A rating steers the taste profile, so the card is
   redrawn with the new fit afterwards. */
const STAR = '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M12 2.8l2.8 5.9 6.4.8-4.7 4.4 1.2 6.4L12 17.2l-5.7 3.1 1.2-6.4-4.7-4.4 6.4-.8z"/></svg>';
const STAR_WORDS = ['Not rated', 'Hated it', 'Not for me', 'It was fine', 'Really liked it', 'Loved it'];
function starsHTML(mine) {
  mine = mine || 0;
  return `<div class="fit-stars" role="radiogroup" aria-label="Your rating" data-mine="${mine}">
    <span class="fit-stars-label">${mine ? 'Your rating' : 'Rate it'}</span>
    <div class="stars">${[1, 2, 3, 4, 5].map(n => `<button class="star${n <= mine ? ' on' : ''}" data-star="${n}" role="radio" aria-checked="${n === mine}" aria-label="${n} star${n > 1 ? 's' : ''}: ${STAR_WORDS[n]}" title="${STAR_WORDS[n]}">${STAR}</button>`).join('')}</div>
    <span class="fit-stars-word">${mine ? STAR_WORDS[mine] : ''}</span></div>`;
}
function wireStars(el, key, item) {
  const box = $('.fit-stars', el);
  if (!box) return;
  const stars = [...box.querySelectorAll('.star')], word = $('.fit-stars-word', box);
  const paint = n => { stars.forEach((s, i) => s.classList.toggle('lit', i < n)); word.textContent = n ? STAR_WORDS[n] : (+box.dataset.mine ? STAR_WORDS[+box.dataset.mine] : ''); };
  stars.forEach(s => {
    s.onmouseenter = () => paint(+s.dataset.star);
    s.onfocus = () => paint(+s.dataset.star);
    s.onclick = async () => {
      const n = +s.dataset.star === +box.dataset.mine ? 0 : +s.dataset.star;
      box.dataset.mine = n;
      stars.forEach((x, i) => { x.classList.toggle('on', i < n); x.setAttribute('aria-checked', i + 1 === n); });
      box.classList.remove('pop'); void box.offsetWidth; box.classList.add('pop');
      try {
        await api(`/api/taste/rate/${key}`, { stars: n });
      } catch (e) { return; }
      toast(n ? `Rated ${n}★: Marquee will lean ${n >= 4 ? 'towards' : n <= 2 ? 'away from' : 'a little on'} titles like this` : 'Rating cleared', { icon: 'sparkle' });
      dispatchEvent(new CustomEvent('marquee:taste-changed'));
      setTimeout(() => renderFit(el, item), 900);
    };
  });
  box.querySelector('.stars').onmouseleave = () => paint(0);
}
window.MarqueeFit = { render: renderFit, key: fitKey };

/* ================================================================== Settings > Taste & memory */
const T = { el: null, data: null, lbxTimer: 0, resetUndo: null };
const SOURCE = { onboarding: 'Launchpad', chat: 'Chat', letterboxd: 'Letterboxd', 'watch history': 'Watch history', manual: 'You' };
const srcLabel = s => SOURCE[s] || (s || '').replace(/^\w/, c => c.toUpperCase());

async function renderSettings(el) {
  if (!el) return;
  T.el = el;
  el.classList.add('taste-panel');
  if (!el.innerHTML.trim()) el.innerHTML = `<h2>${icon('sparkle')} Taste &amp; memory</h2><p>Loading what Marquee knows…</p>`;
  try { T.data = await api('/api/taste'); }
  catch (e) { el.innerHTML = `<h2>${icon('sparkle')} Taste &amp; memory</h2><p>${esc(e.message)}</p>`; return; }
  draw();
}

function bar(g) {
  const w = Math.max(-1, Math.min(1, g.w));
  const pct = Math.abs(w) * 50;
  const tip = `${g.name}: ${w >= 0 ? '+' : ''}${w.toFixed(2)}` + (g.plays ? ` · ${g.plays} finished` : '') + (g.stated ? ` · you said ${g.stated > 0 ? 'you like it' : 'not for you'}` : '');
  return `<div class="tg-row" title="${esc(tip)}"><span class="tg-name">${esc(g.name)}</span>
    <span class="tg-track"><i class="tg-mid"></i><i class="tg-fill ${w < 0 ? 'neg' : 'pos'}" style="--w:${pct.toFixed(1)}%"></i>${g.stated ? `<i class="tg-said" style="--at:${(50 + Math.max(-1, Math.min(1, g.stated)) * 50).toFixed(1)}%" title="What you said"></i>` : ''}</span>
    <span class="tg-val">${w >= 0 ? '+' : '−'}${Math.abs(w).toFixed(2)}</span></div>`;
}
function chipList(items, cls = '') {
  return items.map(x => `<span class="tchip ${cls} ${x.w < 0 ? 'neg' : 'pos'}" title="${x.w >= 0 ? '+' : ''}${x.w.toFixed(2)}">${x.w < 0 ? '−' : '+'} ${esc(x.name)}</span>`).join('');
}
function habits(h) {
  const out = [];
  if (Math.abs(h.anime) >= .1) out.push(h.anime > 0 ? `Enjoys anime (${h.anime.toFixed(2)})` : `Not into anime (${h.anime.toFixed(2)})`);
  const k = h.kinds || {};
  if (k.movie || k.tv) out.push(Math.abs((k.movie || 0) - (k.tv || 0)) < .1 ? 'Films and series about equally' : (k.movie || 0) > (k.tv || 0) ? 'Leans towards films' : 'Leans towards series');
  const eras = Object.entries(h.eras || {}).filter(([, v]) => v > .1).sort((a, b) => b[1] - a[1]).map(([e]) => e);
  if (eras.length) out.push('Often watches ' + eras.slice(0, 3).join(', ') + ' titles');
  if ((h.runtime || {}).long > .2) out.push('Happy with long films');
  if ((h.runtime || {}).short > .2) out.push('Likes shorter films');
  const langs = Object.entries(h.languages || {}).filter(([, v]) => v > .2).map(([l]) => ({ ko: 'Korean', ja: 'Japanese', fr: 'French', es: 'Spanish', hi: 'Hindi' }[l] || l));
  if (langs.length) out.push('Likes titles in ' + langs.join(', '));
  return out;
}
function noteHTML(n) {
  return `<li class="tnote kind-${esc(n.kind || 'info')}" data-note="${esc(n.id)}">
    <span class="tn-dot" aria-hidden="true"></span>
    <div class="tn-body"><p class="tn-text">${esc(n.text)}</p>
      <span class="tn-meta">${esc(srcLabel(n.source))} · ${esc(when(n.date))} · ${Math.round((n.confidence || 0) * 100)}% sure</span></div>
    <div class="tn-acts"><button class="icon-btn small" data-tact="edit" title="Edit" aria-label="Edit note">${pencil}</button>
      <button class="icon-btn small" data-tact="del" title="Delete" aria-label="Delete note">${icon('close', 15)}</button></div></li>`;
}
const pencil = '<svg width="15" height="15" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4L19 9l-4-4L4 16v4Z"/><path d="m13.5 6.5 4 4"/></svg>';
function titleChip(t, liked) {
  return `<span class="ttitle ${liked ? 'pos' : 'neg'}" title="${esc(srcLabel(t.source))}${t.rating ? ' · ' + t.rating + '★' : ''} · ${esc(when(t.date))}">${esc(t.title)}${t.year ? ` <small>${t.year}</small>` : ''}${t.rating ? ` <small>${t.rating}★</small>` : ''}</span>`;
}

function draw() {
  const { profile: p, ai } = T.data;
  const el = T.el;
  const empty = !p.genres.length && !p.notes.length && !p.liked.length;
  const lb = p.letterboxd || {};
  el.innerHTML = `<div class="tp-head"><div><h2>${icon('sparkle')} Taste &amp; memory</h2>
      <p>Everything Marquee has learned about what you like to watch. It's used to pick titles and to say whether something suits you.</p></div>
      <div class="tp-learn"><div class="lbl"><b>Learn from what I watch</b><span>${p.learning ? 'On: finished and abandoned titles update your taste.' : 'Paused: only what you tell it counts.'}</span></div>
        <button class="switch" role="switch" aria-checked="${p.learning}" data-tact="learning" aria-label="Learn from what I watch"></button></div></div>
    <div class="tp-actions">
      <button class="btn small" data-tact="add">${icon('plus', 16)} Add a note</button>
      <button class="btn small ghost" data-tact="lbx">${lbIcon} Import Letterboxd</button>
      <button class="btn small ghost" data-tact="rerun">${icon('restart', 16)} Rerun launchpad</button>
      <button class="btn small ghost danger-ghost" data-tact="reset">Reset taste profile</button>
    </div>
    <div class="tp-drawer" id="tp-drawer" hidden></div>
    ${empty ? `<div class="tp-empty"><span class="fit-orb">${icon('sparkle', 22)}</span><div><b>Nothing learned yet</b><p>Answer a few questions, chat with Marquee, import Letterboxd, or just watch things: your taste builds up here.</p></div></div>` : ''}
    <div class="tp-grid">
      <section class="tp-card"><h3>Genres</h3>
        ${p.genres.length ? `<div class="tg-legend"><span>Not for you</span><span>Love it</span></div><div class="tg">${p.genres.slice(0, 14).map(bar).join('')}</div>` : '<p class="tp-none">No genre leanings yet.</p>'}
        ${p.moods.length || p.themes.length ? `<h4>Vibes &amp; themes</h4><div class="tchips">${chipList(p.moods)}${chipList(p.themes, 'theme')}</div>` : ''}
        ${habits(p.habits).length ? `<h4>Habits</h4><ul class="tp-habits">${habits(p.habits).map(h => `<li>${esc(h)}</li>`).join('')}</ul>` : ''}
      </section>
      <section class="tp-card tp-memory"><h3>Memory <span class="tp-count">${p.notes.length}</span></h3>
        <form class="tp-add" id="tp-add" hidden><input class="input wide" name="text" maxlength="240" placeholder="e.g. Loves slow-burn sci-fi, or: no musicals please" aria-label="New memory note" autocomplete="off"><button class="btn small">Save</button></form>
        ${p.notes.length ? `<ul class="tnotes">${p.notes.map(noteHTML).join('')}</ul>` : '<p class="tp-none">No notes yet. Tell the chat “remember I hate jump scares”, or add one here.</p>'}
      </section>
      <section class="tp-card"><h3>Favourite people</h3>
        ${p.people.length ? `<div class="tpeople">${p.people.map(x => `<span class="tperson" title="${esc((x.titles || []).join(', '))}"><span class="tp-av" style="background:${grad(x.name)}">${esc(x.name[0])}</span><span><b>${esc(x.name)}</b><small>${esc(x.role === 'Director' ? 'Director' : 'Cast')} · ${x.n} title${x.n === 1 ? '' : 's'}</small></span></span>`).join('')}</div>` : '<p class="tp-none">Directors and actors you keep coming back to will show up here.</p>'}
        <h4>Loved</h4>${p.liked.length ? `<div class="ttitles">${p.liked.slice(0, 24).map(t => titleChip(t, true)).join('')}</div>` : '<p class="tp-none">Titles you finish, rewatch or rate highly.</p>'}
        ${p.disliked.length ? `<h4>Not for you</h4><div class="ttitles">${p.disliked.slice(0, 16).map(t => titleChip(t, false)).join('')}</div>` : ''}
      </section>
      <details class="tp-card tp-how" data-adv="taste-how"><summary><h3>How this changes over time</h3>
        <p class="tp-stat">${p.plays ? `Learned from ${p.plays} finished title${p.plays === 1 ? '' : 's'}.` : 'Nothing learned from watching yet.'}${lb.imported ? ` Letterboxd: ${lb.films} films imported ${esc(when(lb.imported))} (${lb.liked} liked, ${lb.disliked} not for you).` : ''}</p>
        </summary>
        <p>What you tell Marquee starts at a moderate weight. Every title you <b>finish</b> nudges its genres up, giving up in the <b>first 20%</b> nudges them down a little, and <b>rewatches and favourites</b> count double. Genres you stop watching slowly fade. So if you say “romance” but only finish horror, horror climbs past romance over two to four weeks of watching.</p>
        ${p.why.length ? `<h4>Why things moved</h4><ul class="tp-why">${p.why.map(w => `<li><span>${esc(w.text)}</span><small>${esc(when(w.date))}</small></li>`).join('')}</ul>` : ''}
        <p class="tp-local">${icon('cloud-off', 15)} <span>Your taste profile stays on this PC (taste.json in Marquee's folder). ${ai.llm ? `When ${esc(ai.name)} writes a comment or answers in chat, it gets a short summary of it.` : 'Classic mode: no AI ever sees it.'}</span></p>
      </details>
    </div>`;
}
const lbIcon = '<svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true"><circle cx="6" cy="12" r="3.2"/><circle cx="12" cy="12" r="3.2"/><circle cx="18" cy="12" r="3.2"/></svg>';

function drawer(html) {
  const d = $('#tp-drawer', T.el);
  if (!html) { d.hidden = true; d.innerHTML = ''; return d; }
  d.innerHTML = html;
  d.hidden = false;
  return d;
}

function letterboxdHTML(prefill) {
  return `<div class="lbx glass-lite"><div class="lbx-head"><b>Import from Letterboxd</b><button class="icon-btn small" data-tact="close-drawer" aria-label="Close">${icon('close', 15)}</button></div>
    <p>Your public diary feed (recent ratings, likes and reviews), or the full export from Letterboxd › Settings › Data. No sign-in, nothing else is read.</p>
    <form class="lbx-row" data-lbx="rss"><input class="input wide" name="u" placeholder="Letterboxd username" value="${esc(prefill || '')}" autocomplete="off" spellcheck="false" aria-label="Letterboxd username"><button class="btn small">Import feed</button></form>
    <label class="lbx-zip btn small ghost">${icon('plus', 15)} Choose export ZIP<input type="file" accept=".zip,application/zip" data-lbx="zip" hidden></label>
    <div class="lbx-status" aria-live="polite"></div></div>`;
}
async function pollLbx(box) {
  clearTimeout(T.lbxTimer);
  let st;
  try { st = (await api('/api/taste/letterboxd/status')).status; } catch (e) { return; }
  const out = box && $('.lbx-status', box);
  if (out) {
    const pct = st.total ? Math.round(st.done / st.total * 100) : 0;
    out.innerHTML = st.state === 'running' ? `<div class="lbx-bar"><i style="--p:${pct}%"></i></div><span>${esc(st.message)}</span>`
      : st.state === 'error' ? `<span class="err">${esc(st.message)}</span>` : st.state === 'done' ? `<span class="okk">${icon('check', 14)} ${esc(st.message)}</span>` : '';
  }
  if (st.state === 'running') T.lbxTimer = setTimeout(() => pollLbx(box), 700);
  else if (st.state === 'done') { renderSettings(T.el); toast('Letterboxd imported into your taste profile', { icon: 'check' }); changed(); }
  return st;
}
window.MarqueeLetterboxd = {
  async rss(username) { return api('/api/taste/letterboxd/rss', { username }); },
  async zip(file) {
    const fd = new FormData(); fd.append('file', file);
    const r = await fetch('/api/taste/letterboxd/zip', { method: 'POST', body: fd });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) throw new Error(d.message || 'Import failed');
    return d;
  },
  async status() { return (await api('/api/taste/letterboxd/status')).status; },
};
function changed() { dispatchEvent(new CustomEvent('marquee:taste-changed')); }

document.addEventListener('click', async e => {
  if (!T.el || !T.el.contains(e.target)) return;
  const b = e.target.closest('[data-tact]');
  if (!b) return;
  const act = b.dataset.tact;
  if (act === 'learning') {
    const on = b.getAttribute('aria-checked') !== 'true';
    b.setAttribute('aria-checked', on);
    try { await api('/api/taste/learning', { on }); T.data.profile.learning = on; b.closest('.tp-learn').querySelector('.lbl span').textContent = on ? 'On: finished and abandoned titles update your taste.' : 'Paused: only what you tell it counts.'; }
    catch (err) { toast(err.message); }
  } else if (act === 'add') {
    const f = $('#tp-add', T.el); f.hidden = !f.hidden; if (!f.hidden) f.querySelector('input').focus();
  } else if (act === 'lbx') {
    const d = drawer(letterboxdHTML((T.data.profile.letterboxd || {}).username));
    pollLbx(d);
  } else if (act === 'close-drawer') {
    drawer(null);
  } else if (act === 'rerun') {
    if (window.MarqueeLaunchpad) window.MarqueeLaunchpad.show({ rerun: true });
  } else if (act === 'reset') {
    if (!b.dataset.sure) { b.dataset.sure = '1'; b.textContent = 'Sure? Reset everything'; b.classList.add('armed'); setTimeout(() => { if (document.contains(b)) { delete b.dataset.sure; b.textContent = 'Reset taste profile'; b.classList.remove('armed'); } }, 4000); return; }
    try { await api('/api/taste/reset', {}); } catch (err) { return toast(err.message); }
    renderSettings(T.el); changed();
    toast('Taste profile reset. A backup was kept.', { icon: 'restart', action: 'Undo', ms: 30000, onAction: async () => {
      try { await api('/api/taste/reset/undo', {}); renderSettings(T.el); changed(); toast('Taste profile restored', { icon: 'check' }); } catch (err) { toast(err.message); }
    } });
  } else if (act === 'del') {
    const li = b.closest('[data-note]');
    let note;
    try { note = (await api(`/api/taste/notes/${li.dataset.note}`, undefined, 'DELETE')).note; } catch (err) { return toast(err.message); }
    li.classList.add('gone'); setTimeout(() => renderSettings(T.el), reduced.matches ? 0 : 260);
    toast('Note deleted', { icon: 'close', action: 'Undo', ms: 8000, onAction: async () => { await api('/api/taste/notes/restore', { note }); renderSettings(T.el); } });
  } else if (act === 'edit') {
    const li = b.closest('[data-note]'), p = $('.tn-text', li);
    if (li.classList.contains('editing')) return;
    li.classList.add('editing');
    const old = p.textContent;
    p.outerHTML = `<form class="tn-edit"><input class="input wide" maxlength="240" value="${esc(old)}" aria-label="Edit note"><button class="btn small">Save</button></form>`;
    const f = $('.tn-edit', li), inp = $('input', f);
    inp.focus(); inp.select();
    inp.addEventListener('keydown', ev => { if (ev.key === 'Escape') { ev.stopPropagation(); renderSettings(T.el); } });
    f.onsubmit = async ev => { ev.preventDefault(); try { await api(`/api/taste/notes/${li.dataset.note}`, { text: inp.value }, 'PATCH'); renderSettings(T.el); } catch (err) { toast(err.message); } };
  }
});
document.addEventListener('submit', async e => {
  if (!T.el || !T.el.contains(e.target)) return;
  const f = e.target;
  if (f.id === 'tp-add') {
    e.preventDefault();
    const text = f.text.value.trim();
    if (!text) return;
    try { await api('/api/taste/notes', { text }); toast('Saved to memory', { icon: 'check' }); renderSettings(T.el); changed(); } catch (err) { toast(err.message); }
  } else if (f.dataset.lbx === 'rss') {
    e.preventDefault();
    const box = f.closest('.lbx');
    try { await window.MarqueeLetterboxd.rss(f.u.value.trim()); pollLbx(box); } catch (err) { $('.lbx-status', box).innerHTML = `<span class="err">${esc(err.message)}</span>`; }
  }
});
document.addEventListener('change', async e => {
  if (!T.el || !T.el.contains(e.target) || e.target.dataset.lbx !== 'zip') return;
  const file = e.target.files[0], box = e.target.closest('.lbx');
  if (!file) return;
  try { await window.MarqueeLetterboxd.zip(file); pollLbx(box); } catch (err) { $('.lbx-status', box).innerHTML = `<span class="err">${esc(err.message)}</span>`; }
});
addEventListener('marquee:taste-changed', () => { if (T.el && document.contains(T.el)) renderSettings(T.el); });

window.MarqueeTaste = { renderSettings, refresh: () => T.el && renderSettings(T.el), changed };
})();
