/* The first-run Launchpad (window.MarqueeLaunchpad): a blank, calm full-screen page that asks what you love to watch,
   then the vibes and anything you can't stand, optionally imports Letterboxd, builds the taste profile and melts into
   Home. maybeShow() shows it only when the profile hasn't been set up; show({rerun: true}) runs it again.
   Needs taste.js (window.MarqueeKit). Talks to /api/taste/* (marquee/taste_api.py). */
(() => {
'use strict';
const K = () => window.MarqueeKit;
const LIKES = ['Anime', 'Action', 'Comedy', 'Romance', 'Horror', 'Sci-fi', 'Fantasy', 'Drama', 'Thriller', 'Mystery', 'Documentary',
  'Animation', 'Family', 'Crime', 'Reality', 'K-drama', 'Musicals', 'War', 'Westerns', 'Superheroes', 'True crime', 'Adventure', 'History'];
const VIBES = ['Feel-good', 'Mind-bending', 'Dark', 'Cosy', 'Epic', 'Short episodes', 'Binge-able'];
const L = { el: null, step: 0, likes: new Set(), vibes: new Set(), dislikes: new Set(), texts: [], turns: { likes: 0, dislikes: 0 }, ai: false, resolve: null, busy: false };

async function maybeShow() {
  let st;
  try { st = await K().api('/api/taste/state'); } catch (e) { return false; }
  if (!st.launchpad) return false;
  return show({ state: st });
}

async function show(opts = {}) {
  const { api, icon } = K();
  if (L.el) return true;
  let st = opts.state;
  if (!st) { try { st = await api('/api/taste/state'); } catch (e) { st = { ai: { llm: false } }; } }
  L.ai = !!(st.ai && st.ai.llm);
  L.aiName = (st.ai && st.ai.name) || 'Marquee';
  L.rerun = !!opts.rerun;
  Object.assign(L, { step: 0, likes: new Set(), vibes: new Set(), dislikes: new Set(), texts: [], turns: { likes: 0, dislikes: 0 } });
  const el = document.createElement('div');
  el.className = 'lp';
  el.tabIndex = -1;
  el.setAttribute('role', 'dialog');
  el.setAttribute('aria-modal', 'true');
  el.setAttribute('aria-label', 'Tell Marquee what you like');
  el.innerHTML = `<div class="lp-top"><span class="lp-mark"><img src="icon.svg" alt="" width="26" height="26"></span>
      <div class="lp-steps" aria-hidden="true"><i class="on"></i><i></i><i></i></div>
      <button class="lp-skip" data-lp="skip">${L.rerun ? 'Close' : 'Skip for now'}</button></div>
    <div class="lp-stage"></div>`;
  document.body.append(el);
  document.documentElement.classList.add('locked', 'lp-open');
  L.el = el;
  el.addEventListener('keydown', e => {
    e.stopPropagation();  // app.js' shortcuts ("/" search, Esc) stay out of the way
    if (e.key === 'Escape' && !L.busy) finish(true);
  });
  el.addEventListener('click', onClick);
  el.addEventListener('submit', onSubmit);
  el.addEventListener('change', onChange);
  requestAnimationFrame(() => { el.classList.add('in'); el.focus({ preventScroll: true }); });
  stepLikes();
  return new Promise(r => { L.resolve = r; });
}

const stage = () => L.el.querySelector('.lp-stage');
function setStep(n) {
  L.step = n;
  L.el.querySelectorAll('.lp-steps i').forEach((d, k) => d.classList.toggle('on', k <= n));
}
async function swap(html) {
  const s = stage();
  if (s.firstElementChild && !K().reduced.matches) { s.firstElementChild.classList.add('leave'); await K().sleep(260); }
  s.innerHTML = html;
  L.el.scrollTo({ top: 0 });
  s.querySelector('textarea, input')?.focus({ preventScroll: true });
}
function chipsHTML(list, group, set, tone = '') {
  const { esc } = K();
  return `<div class="lp-chips ${tone}" role="group">${list.map((c, k) => `<button type="button" class="lp-chip" role="checkbox" aria-checked="${set.has(c)}" data-lp="chip" data-group="${group}" data-v="${esc(c)}" style="--i:${k}"><span class="lp-box">${K().icon('check', 13)}</span>${esc(c)}</button>`).join('')}</div>`;
}
function askHTML(group, placeholder) {
  return `<div class="lp-convo" data-group="${group}"></div>
    <form class="lp-ask glass" data-group="${group}"><textarea rows="1" placeholder="${placeholder}" aria-label="Your answer" maxlength="600"></textarea>
      <button class="lp-send" aria-label="Send"><svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h13M13 6l6 6-6 6"/></svg></button></form>`;
}
function stepLikes() {
  setStep(0);
  swap(`<div class="lp-pane"><h1 class="lp-q">${K().wordsHTML('What do you love to watch?', 70)}</h1>
    <p class="lp-hint">Tell me in your own words, tap a few, or both.</p>
    ${askHTML('likes', 'e.g. twisty thrillers, anything by Villeneuve, cosy anime…')}
    ${chipsHTML(LIKES, 'likes', L.likes)}
    <div class="lp-foot"><button class="btn" data-lp="next">Next ${K().icon('right', 16)}</button></div></div>`);
}
function stepVibes() {
  setStep(1);
  swap(`<div class="lp-pane"><h1 class="lp-q">${K().wordsHTML('What’s the vibe?', 70)}</h1>
    <p class="lp-hint">Pick any that sound like you.</p>
    ${chipsHTML(VIBES, 'vibes', L.vibes, 'vibes')}
    <h2 class="lp-q2">${K().wordsHTML('Anything you can’t stand?', 50)}</h2>
    ${askHTML('dislikes', 'e.g. jump scares, musicals, slow dramas…')}
    ${chipsHTML(LIKES.filter(c => !L.likes.has(c)), 'dislikes', L.dislikes, 'neg')}
    <div class="lp-foot"><button class="btn ghost" data-lp="back">${K().icon('left', 16)} Back</button><button class="btn" data-lp="next">Next ${K().icon('right', 16)}</button></div></div>`);
}
function stepLetterboxd() {
  setStep(2);
  swap(`<div class="lp-pane"><h1 class="lp-q">${K().wordsHTML('Got a Letterboxd?', 70)}</h1>
    <p class="lp-hint">Optional. Your ratings and reviews make a sharper start. No sign-in: your public diary feed, or the export ZIP from Letterboxd › Settings › Data.</p>
    <form class="lp-lbx glass" data-lp-form="rss"><input name="u" placeholder="Letterboxd username" autocomplete="off" spellcheck="false" aria-label="Letterboxd username"><button class="btn small">Import</button></form>
    <label class="btn small ghost lp-zip">${K().icon('plus', 15)} Or choose the export ZIP<input type="file" accept=".zip,application/zip" hidden data-lp-file></label>
    <div class="lp-lbx-status" aria-live="polite"></div>
    <div class="lp-foot"><button class="btn ghost" data-lp="back">${K().icon('left', 16)} Back</button><button class="btn" data-lp="finish">${K().icon('sparkle', 16)} Build my taste profile</button></div></div>`);
}

async function say(group, text) {
  const { api, esc } = K();
  const convo = L.el.querySelector(`.lp-convo[data-group="${group}"]`);
  if (!convo || L.busy) return;
  L.busy = true;
  L.texts.push(/^i(\s|')/i.test(text) ? text : (group === 'dislikes' ? `I can't stand ${text}` : `I love ${text}`));
  L.turns[group] = (L.turns[group] || 0) + 1;
  convo.insertAdjacentHTML('beforeend', `<div class="lp-msg me">${esc(text)}</div>`);
  const bot = document.createElement('div');
  bot.className = 'lp-msg bot';
  bot.innerHTML = '<span class="mq-dots"><i></i><i></i><i></i></span>';
  convo.append(bot);
  let d;
  try { d = await api('/api/taste/onboard/chat', { text, step: group, turn: L.turns[group], picks: [...L.likes, ...L.vibes] }); }
  catch (e) { d = { reply: 'Noted.', picked: { likes: [], dislikes: [], vibes: [] } }; }
  L.busy = false;
  bot.innerHTML = `<span>${K().wordsHTML(d.reply, 26)}</span>${d.question && L.turns[group] < 3 ? `<span class="lp-follow">${K().wordsHTML(d.question, 26)}</span>` : ''}`;
  const tick = (g, v) => {
    const set = L[g];
    if (!set || set.has(v)) return;
    set.add(v);
    const b = L.el.querySelector(`.lp-chip[data-group="${g}"][data-v="${CSS.escape(v)}"]`);
    if (b) { b.setAttribute('aria-checked', 'true'); b.classList.remove('boing'); void b.offsetWidth; b.classList.add('boing'); }
  };
  (d.picked.likes || []).forEach(v => tick(group === 'dislikes' ? 'dislikes' : 'likes', v));
  (d.picked.vibes || []).forEach(v => tick('vibes', v));
  (d.picked.dislikes || []).forEach(v => tick('dislikes', v));
  const ta = L.el.querySelector(`.lp-ask[data-group="${group}"] textarea`);
  if (ta) { ta.placeholder = d.question && L.turns[group] < 3 ? 'Answer, or tap Next' : 'Anything else? Or tap Next'; ta.focus({ preventScroll: true }); }
  if (L.turns[group] >= 3) L.el.querySelector(`.lp-ask[data-group="${group}"]`)?.classList.add('done');
}

function onClick(e) {
  const b = e.target.closest('[data-lp]');
  if (!b) return;
  const act = b.dataset.lp;
  if (act === 'chip') {
    const set = L[b.dataset.group], v = b.dataset.v;
    set.has(v) ? set.delete(v) : set.add(v);
    b.setAttribute('aria-checked', set.has(v));
    if (b.dataset.group === 'likes') L.dislikes.delete(v);
  } else if (act === 'next') { L.step === 0 ? stepVibes() : stepLetterboxd(); }
  else if (act === 'back') { L.step === 2 ? stepVibes() : stepLikes(); }
  else if (act === 'skip') finish(true);
  else if (act === 'finish') finish(false);
}
function onSubmit(e) {
  e.preventDefault();
  const f = e.target;
  if (f.classList.contains('lp-ask')) {
    const ta = f.querySelector('textarea'), t = ta.value.trim();
    if (!t) return;
    ta.value = '';
    say(f.dataset.group, t);
  } else if (f.dataset.lpForm === 'rss') {
    const u = f.u.value.trim();
    if (!u) return;
    startLbx(() => window.MarqueeLetterboxd.rss(u));
  }
}
document.addEventListener('keydown', e => {
  if (!L.el || e.key !== 'Enter' || e.shiftKey || !e.target.closest || !e.target.closest('.lp-ask')) return;
  e.preventDefault();
  e.target.closest('form').requestSubmit();
}, true);
function onChange(e) {
  if (!e.target.matches('[data-lp-file]') || !e.target.files[0]) return;
  const file = e.target.files[0];
  startLbx(() => window.MarqueeLetterboxd.zip(file));
}
async function startLbx(fn) {
  const { esc, icon } = K();
  const out = L.el.querySelector('.lp-lbx-status');
  try { await fn(); } catch (err) { out.innerHTML = `<span class="err">${esc(err.message)}</span>`; return; }
  L.lbx = true;
  const poll = async () => {
    if (!L.el) return;
    let st; try { st = await window.MarqueeLetterboxd.status(); } catch (e) { return; }
    const pct = st.total ? Math.round(st.done / st.total * 100) : 5;
    out.innerHTML = st.state === 'running' ? `<div class="lbx-bar"><i style="--p:${pct}%"></i></div><span>${esc(st.message)}</span>`
      : st.state === 'error' ? `<span class="err">${esc(st.message)}</span>` : `<span class="okk">${icon('check', 14)} ${esc(st.message)}</span>`;
    if (st.state === 'running') setTimeout(poll, 700);
  };
  poll();
}

async function finish(skipped) {
  const { api, esc, sleep, reduced } = K();
  if (!L.el || L.finishing) return;
  L.finishing = true;
  let summary = '';
  const any = L.likes.size || L.vibes.size || L.dislikes.size || L.texts.length || L.lbx;
  if (skipped && L.rerun) return close(false);
  if (skipped || !any) {
    try { await api('/api/taste/skip', {}); } catch (e) { /* stays for next time */ }
    return close(false);
  }
  await swap(`<div class="lp-pane lp-build"><span class="lp-orb"><i></i><i></i><i></i></span><h1 class="lp-q">${K().wordsHTML('Building your taste profile…', 50)}</h1><p class="lp-hint lp-summary"></p></div>`);
  setStep(3);
  const t0 = Date.now();
  try {
    const d = await api('/api/taste/onboard', { likes: [...L.likes], vibes: [...L.vibes], dislikes: [...L.dislikes], texts: L.texts });
    summary = d.summary;
  } catch (e) { summary = e.message; }
  await sleep(Math.max(0, (reduced.matches ? 300 : 1500) - (Date.now() - t0)));
  const p = L.el.querySelector('.lp-summary');
  if (p) p.innerHTML = K().wordsHTML(summary, 30);
  await sleep(reduced.matches ? 400 : 1700);
  close(true);
}
async function close(changed) {
  const el = L.el;
  if (!el) return;
  el.classList.add('out');
  if (changed) dispatchEvent(new CustomEvent('marquee:taste-changed'));
  await K().sleep(K().reduced.matches ? 10 : 650);
  el.remove();
  document.documentElement.classList.remove('lp-open');
  if (!document.querySelector('.layer:not([hidden])')) document.documentElement.classList.remove('locked');
  L.el = null; L.finishing = false;
  if (L.resolve) { L.resolve(true); L.resolve = null; }
}

window.MarqueeLaunchpad = { maybeShow, show };
})();
