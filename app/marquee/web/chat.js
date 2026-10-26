/* Ask Marquee: the chat page (window.MarqueeChat.mount(pageEl)).
   With AI: liquid-glass bubbles, a streamed reply, title cards under each answer (like ClearHome's listings under the
   chat bubble), "Saved to memory" chips with undo. Classic mode: an honest rule-based finder (chips + search box).
   Needs taste.js (window.MarqueeKit). Talks to /api/chat/* (marquee/taste_api.py). */
(() => {
'use strict';
const K = () => window.MarqueeKit;
const C = { page: null, busy: false, ctrl: null, state: null, statusTimer: 0, acts: new Map(), finder: { genres: new Set(), moods: new Set(), era: null, kind: 'any' } };
const THINKING = ['Looking through your shelves…', 'Thinking about your taste…', 'Checking what you haven’t seen…', 'Asking the projectionist…', 'Weighing the classics…', 'Almost there…'];

async function mount(page) {
  const { api, esc, icon } = K();
  C.page = page;
  page.classList.add('page-chat');
  page.innerHTML = `<div class="chat-wrap"><div class="chat-head"><div class="grow"><span class="eyebrow orange">Ask Marquee</span>
      <h1>What are we watching?</h1><p class="chat-sub">Loading…</p></div>
      <div class="chat-head-acts"></div></div><div class="chat-main"></div></div>`;
  let st;
  try { st = await api('/api/chat/state'); } catch (e) { $('.chat-main').innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
  if (C.page !== page) return;
  C.state = st;
  if (st.playing) {
    $('.chat-main').innerHTML = `<div class="chat-empty"><span class="chat-orb">${icon('play', 26)}</span><h2>Chat waits while a video plays</h2><p>Come back when you're done watching.</p></div>`;
    return;
  }
  st.mode === 'classic' ? mountFinder(st) : mountChat(st);
}
const $ = (s, r) => (r || C.page).querySelector(s);

/* ================================================================== AI chat */
function mountChat(st) {
  const { esc, icon } = K();
  $('.chat-sub').innerHTML = `Recommendations from your library and Seerr, picked for your taste. Tell it what you like and it remembers. <span class="chat-engine">${icon('sparkle', 12)} ${esc(st.engine)}</span>`;
  $('.chat-head-acts').innerHTML = `<button class="btn small ghost" data-cact="clear" title="Clear this conversation">${icon('restart', 15)} Clear</button>`;
  $('.chat-main').innerHTML = `<div class="chat-log" role="log" aria-live="polite" aria-label="Conversation"></div>
    <div class="chat-dock"><div class="chat-chips" role="group" aria-label="Suggestions"></div>
      <form class="chat-composer glass"><textarea rows="1" placeholder="Ask for a film, a show, a mood… or “remember I hate jump scares”" aria-label="Message" maxlength="1000"></textarea>
        <button class="chat-send" type="submit" aria-label="Send">${sendIcon}</button></form>
      <p class="chat-note">Marquee can be wrong about titles; cards are matched to your library and Seerr.</p></div>`;
  const log = $('.chat-log');
  if (st.history.length) {
    st.history.forEach(m => m.role === 'user' ? userBubble(m.text, false) : botBubble(m, false));
    requestAnimationFrame(() => scrollEnd(false));
  } else emptyState();
  chips(st.suggestions);
  const ta = $('.chat-composer textarea');
  ta.addEventListener('input', () => autosize(ta));
  ta.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(ta.value); } });
  $('.chat-composer').addEventListener('submit', e => { e.preventDefault(); send(ta.value); });
  setTimeout(() => ta.focus({ preventScroll: true }), 300);
  log.addEventListener('click', onLogClick);
  log.addEventListener('mouseover', e => hoverRef(e, true));
  log.addEventListener('mouseout', e => hoverRef(e, false));
}
const sendIcon = '<svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h13M13 6l6 6-6 6"/></svg>';
function autosize(ta) { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 160) + 'px'; }
function emptyState() {
  const { icon } = K();
  $('.chat-log').innerHTML = `<div class="chat-empty"><span class="chat-orb">${icon('sparkle', 28)}</span><h2>Ask for anything to watch</h2>
    <p>“Something like Dune”, “a cosy show for tonight”, “what should I request next?”. Tell it what you love or can't stand and it'll remember.</p></div>`;
}
function chips(list) {
  const { esc } = K();
  const box = $('.chat-chips');
  if (!box) return;
  box.innerHTML = (list || []).map((s, k) => `<button type="button" class="chat-chip" style="--i:${k}" data-cact="ask" data-q="${esc(s)}">${esc(s)}</button>`).join('');
}
function scrollEnd(smooth = true) {
  const log = $('.chat-log');
  if (!log) return;
  const last = log.lastElementChild;
  if (!last) return;
  const r = last.getBoundingClientRect();
  if (r.bottom > innerHeight - 170 || r.top < 0) scrollBy({ top: r.bottom - innerHeight + 190, behavior: smooth && !K().reduced.matches ? 'smooth' : 'auto' });
}
function userBubble(text, animate = true) {
  const { esc } = K();
  $('.chat-empty')?.remove();
  const el = document.createElement('div');
  el.className = 'cmsg user' + (animate ? ' mq-pop' : '');
  el.innerHTML = `<div class="bubble">${esc(text)}</div>`;
  $('.chat-log').append(el);
  return el;
}
/* The answer text: escaped, paragraphs kept, **Title (Year)** becomes a link to its card below. */
function richText(text) {
  const { esc, norm } = K();
  return String(text || '').split(/(\*\*[^*\n]{1,90}?\*\*)/).map(part => {
    if (!/^\*\*.*\*\*$/.test(part)) return esc(part);
    const t = part.slice(2, -2);
    const bare = t.replace(/\s*[\(\[]\s*(19|20)\d{2}[^)\]]*[\)\]]\s*$/, '');
    return `<a class="chat-ref" href="#" data-ref="${esc(norm(bare))}">${esc(t)}</a>`;
  }).join('').replace(/\n{2,}/g, '<br><br>').replace(/\n/g, '<br>');
}
function botBubble(m, animate = true) {
  const { icon } = K();
  const el = document.createElement('div');
  el.className = 'cmsg bot' + (animate ? ' mq-pop' : '');
  el.innerHTML = `<span class="cav" aria-hidden="true">${icon('sparkle', 14)}</span><div class="cstack"><div class="bubble">${m.text ? richText(m.text) : ''}</div></div>`;
  $('.chat-log').append(el);
  if (m.cards && m.cards.length) showCards(el, m.cards, animate);
  if (m.actions && m.actions.length) m.actions.forEach(a => actionCard(el, a, animate, animate ? null : m.at));
  if (m.memory) memoryChip(el, m.memory, false);
  if (m.engine) engineLine(el, m.engine);
  return el;
}
function engineLine(el, engine) {
  const { esc } = K();
  const s = $('.cstack', el);
  s.querySelector('.msg-engine')?.remove();
  s.insertAdjacentHTML('beforeend', `<div class="msg-engine">Answered by ${esc(engine)}</div>`);
}
function showCards(el, cards, animate = true) {
  const { titleCard } = K();
  const s = $('.cstack', el);
  s.querySelector('.chat-cards')?.remove();
  const box = document.createElement('div');
  box.className = 'chat-cards' + (animate ? ' mq-pop' : '');
  box.setAttribute('role', 'list');
  box.setAttribute('aria-label', 'Titles in this answer');
  box.innerHTML = cards.map((c, k) => titleCard(c, k)).join('');
  const eng = s.querySelector('.msg-engine');
  eng ? eng.before(box) : s.append(box);
  // names in the bubble that found a card become links; the rest stay plain bold
  const have = new Set(cards.map(c => K().norm(c.named || c.title)));
  cards.forEach(c => have.add(K().norm(c.title)));
  $$('.chat-ref', el).forEach(a => { if (!have.has(a.dataset.ref)) a.replaceWith(Object.assign(document.createElement('b'), { textContent: a.textContent })); });
}
const $$ = (s, r) => [...(r || C.page).querySelectorAll(s)];
function memoryChip(el, note, animate = true) {
  const { esc, icon } = K();
  const s = $('.cstack', el);
  const chip = document.createElement('div');
  chip.className = 'mem-chip' + (animate ? ' mq-pop' : '');
  chip.dataset.note = note.id;
  chip.innerHTML = `<span class="mem-ico">${icon('check', 14)}</span><span>Saved to memory: <b>${esc(note.text)}</b></span><button type="button" class="mem-undo" data-cact="undo-mem">Undo</button>`;
  chip._note = note;
  const eng = s.querySelector('.msg-engine');
  eng ? eng.before(chip) : s.append(chip);
}
function thinking(el) {
  const { esc } = K();
  const b = $('.bubble', el);
  b.classList.add('is-thinking');
  b.innerHTML = `<span class="mq-dots"><i></i><i></i><i></i></span><span class="think-text">${esc(THINKING[0])}</span>`;
  let k = 0;
  clearInterval(C.statusTimer);
  C.statusTimer = setInterval(() => { const t = $('.think-text', el); if (!t) return clearInterval(C.statusTimer); k = Math.min(k + 1, THINKING.length - 1); swapText(t, THINKING[k]); }, 2600);
}
function swapText(t, text) {
  t.classList.add('out');
  setTimeout(() => { t.textContent = text; t.classList.remove('out'); }, 180);
}

async function send(text, extra) {
  text = (text || '').trim();
  if (!text || C.busy) return;
  const ta = $('.chat-composer textarea');
  if (ta) { ta.value = ''; autosize(ta); }
  C.busy = true;
  $('.chat-composer')?.classList.add('busy');
  $('.chat-chips') && ($('.chat-chips').hidden = true);
  userBubble(text);
  const el = botBubble({ text: '' });
  thinking(el);
  scrollEnd();
  const bubble = $('.bubble', el);
  let shown = '', started = false, gotCards = false;
  const paint = () => { bubble.innerHTML = richText(shown) + '<i class="caret" aria-hidden="true"></i>'; };
  const startText = () => { if (started) return; started = true; clearInterval(C.statusTimer); bubble.classList.remove('is-thinking'); bubble.classList.add('streaming'); };
  try {
    C.ctrl = new AbortController();
    const r = await fetch('/api/chat/stream', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: text, ...(extra || {}) }), signal: C.ctrl.signal });
    if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.message || r.statusText); }
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
        if (!line) continue;
        let ev; try { ev = JSON.parse(line); } catch (e) { continue; }
        if (ev.kind === 'status' && !started) { const t = $('.think-text', el); if (t) { clearInterval(C.statusTimer); swapText(t, ev.text); } }
        else if (ev.kind === 'delta') { startText(); shown += ev.text; paint(); scrollEnd(false); }
        else if (ev.kind === 'replace') { startText(); shown = ev.text; paint(); }
        else if (ev.kind === 'cards') { gotCards = true; bubble.innerHTML = richText(shown); showCards(el, ev.items || []); scrollEnd(); }
        else if (ev.kind === 'memory') { memoryChip(el, ev.note); }
        else if (ev.kind === 'action') { startText(); bubble.innerHTML = richText(shown); actionCard(el, ev.card, true); scrollEnd(); }
        else if (ev.kind === 'ui') { setTimeout(() => uiDo(ev), 350); }
        else if (ev.kind === 'error') { throw new Error(ev.text); }
        else if (ev.kind === 'done') { engineLine(el, ev.engine); }
      }
    }
    bubble.classList.remove('streaming');
    bubble.innerHTML = richText(shown);
    if (gotCards) relink(el);
    if (!shown && !$('.mem-chip', el) && !$('.acard', el)) throw new Error('No answer came back. Try asking another way.');
  } catch (e) {
    clearInterval(C.statusTimer);
    if (e.name === 'AbortError') { el.remove(); }
    else { bubble.classList.remove('is-thinking', 'streaming'); bubble.classList.add('is-error'); bubble.textContent = e.message || 'Something went wrong.'; }
  } finally {
    C.busy = false; C.ctrl = null;
    $('.chat-composer')?.classList.remove('busy');
    scrollEnd();
    if (document.activeElement === document.body) $('.chat-composer textarea')?.focus({ preventScroll: true });
  }
}
/* After the final paint: bold names without a card go back to plain bold. */
function relink(el) {
  const have = new Set($$('.tcard', el).map(c => c.dataset.ref));
  $$('.chat-ref', el).forEach(a => { if (!have.has(a.dataset.ref)) a.replaceWith(Object.assign(document.createElement('b'), { textContent: a.textContent })); });
}
function hoverRef(e, on) {
  const a = e.target.closest && e.target.closest('.chat-ref');
  if (!a) return;
  const card = a.closest('.cmsg').querySelector(`.tcard[data-ref="${CSS.escape(a.dataset.ref)}"]`);
  card && card.classList.toggle('hl', on);
}
async function onLogClick(e) {
  const a = e.target.closest('.chat-ref');
  if (a) {
    e.preventDefault();
    const card = a.closest('.cmsg').querySelector(`.tcard[data-ref="${CSS.escape(a.dataset.ref)}"]`);
    if (card) { card.scrollIntoView({ block: 'nearest', behavior: K().reduced.matches ? 'auto' : 'smooth' }); card.classList.remove('flash'); void card.offsetWidth; card.classList.add('flash'); card.focus({ preventScroll: true }); }
  }
}
document.addEventListener('click', async e => {
  if (!C.page || !C.page.contains(e.target)) return;
  const b = e.target.closest('[data-cact]');
  if (!b) return;
  const { api, toast } = K();
  const act = b.dataset.cact;
  if (act === 'ask') send(b.dataset.q);
  else if (act === 'a') onActionButton(b);
  else if (act === 'clear') {
    if (C.busy && C.ctrl) C.ctrl.abort();
    try { await api('/api/chat/history', undefined, 'DELETE'); } catch (err) { return toast(err.message); }
    emptyState();
    $('.chat-chips') && ($('.chat-chips').hidden = false);
    toast('Conversation cleared', { icon: 'check' });
  } else if (act === 'undo-mem') {
    const chip = b.closest('.mem-chip');
    try { await api(`/api/taste/notes/${chip.dataset.note}`, undefined, 'DELETE'); } catch (err) { return toast(err.message); }
    chip.classList.add('undone');
    chip.innerHTML = `<span class="mem-ico">${K().icon('close', 13)}</span><span>Not saved. Forgot: <s>${K().esc(chip._note ? chip._note.text : '')}</s></span>`;
    window.MarqueeTaste && window.MarqueeTaste.changed();
  } else if (act === 'f-chip') {
    const set = C.finder[b.dataset.group];
    if (b.dataset.group === 'era') { C.finder.era = C.finder.era === b.dataset.v ? null : b.dataset.v; $$('[data-group="era"]').forEach(x => x.setAttribute('aria-pressed', x.dataset.v === C.finder.era)); }
    else if (b.dataset.group === 'kind') { C.finder.kind = b.dataset.v; $$('[data-group="kind"]').forEach(x => x.setAttribute('aria-pressed', x === b)); }
    else { set.has(b.dataset.v) ? set.delete(b.dataset.v) : set.add(b.dataset.v); b.setAttribute('aria-pressed', set.has(b.dataset.v)); }
    runFind();
  } else if (act === 'f-reset') {
    C.finder = { genres: new Set(), moods: new Set(), era: null, kind: 'any' };
    $$('.f-chip').forEach(x => x.setAttribute('aria-pressed', x.dataset.group === 'kind' && x.dataset.v === 'any'));
    $('.finder-q').value = '';
    runFind();
  }
});

/* ================================================================== Classic: the finder (no AI) */
function mountFinder(st) {
  const { esc, icon } = K();
  $('.eyebrow').textContent = 'Finder';
  $('h1').textContent = 'Find something to watch';
  $('.chat-sub').innerHTML = `Classic mode: no AI. Pick a mood, genre or era, or type a title; Marquee filters your library and Seerr by rules and sorts by your taste. Type “I love …” or “I hate …” and it's remembered. <span class="chat-engine">${icon('film', 12)} Rules only</span>`;
  const group = (name, label, list, multi = true) => `<div class="f-group"><span class="f-label">${label}</span><div class="f-chips">${list.map((v, k) => `<button type="button" class="f-chip" style="--i:${k}" data-cact="f-chip" data-group="${name}" data-v="${esc(v)}" aria-pressed="${!multi && v === 'any'}">${esc({ any: 'Anything', movie: 'Films', tv: 'Shows' }[v] || v)}</button>`).join('')}</div></div>`;
  $('.chat-main').innerHTML = `<div class="finder glass">
      <form class="finder-search"><svg width="20" height="20" aria-hidden="true"><use href="#i-search"/></svg><input class="finder-q" placeholder="A title, a word, or “I love slow-burn mysteries”" aria-label="Search or tell Marquee what you like" autocomplete="off"><button class="btn small">Find</button></form>
      ${group('kind', 'Type', ['any', 'movie', 'tv'], false)}
      ${group('moods', 'Mood', st.finder.moods)}
      ${group('genres', 'Genre', st.finder.genres)}
      ${group('era', 'Era', st.finder.eras, false)}
      <button type="button" class="f-reset" data-cact="f-reset">Clear filters</button></div>
    <div class="finder-out" aria-live="polite"></div>`;
  $('.finder-search').addEventListener('submit', e => { e.preventDefault(); runFind(); });
  $('.finder-out').innerHTML = `<div class="chat-empty"><span class="chat-orb">${icon('search', 26)}</span><h2>Pick a few chips to start</h2><p>Results are ordered by how well they fit your taste profile.</p></div>`;
}
let findSeq = 0;
async function runFind() {
  const { api, esc, icon, titleCard } = K();
  const out = $('.finder-out');
  const q = $('.finder-q').value.trim();
  const f = C.finder;
  if (!q && !f.genres.size && !f.moods.size && !f.era) { mountFinderEmpty(); return; }
  const seq = ++findSeq;
  if (q && !C.busy) {   // Classic: commands ("pull up Dune", "is everything ok") are answered like in the chat
    let cmd = false;
    try { cmd = (await api(`/api/chat/command?q=${encodeURIComponent(q)}`)).command; } catch (e) { /* treat as a search */ }
    if (seq !== findSeq) return;
    if (cmd) {
      if (!$('.chat-log', out)) out.innerHTML = '<div class="chat-log finder-log" role="log" aria-live="polite"></div>';
      $('.finder-q').value = '';
      $('.chat-log', out).addEventListener('click', onLogClick);
      return send(q);
    }
  }
  out.classList.add('loading');
  let d;
  try { d = await api('/api/chat/find', { q, genres: [...f.genres], moods: [...f.moods], era: f.era, kind: f.kind }); }
  catch (e) { out.innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
  if (seq !== findSeq) return;
  out.classList.remove('loading');
  if (d.memory) {
    out.innerHTML = `<div class="mem-chip mq-pop" data-note="${esc(d.memory.id)}"><span class="mem-ico">${icon('check', 14)}</span><span>Saved to memory: <b>${esc(d.memory.text)}</b></span><button type="button" class="mem-undo" data-cact="undo-mem">Undo</button></div>`;
    $('.mem-chip', out)._note = d.memory;
    $('.finder-q').value = '';
    window.MarqueeTaste && window.MarqueeTaste.changed();
    return;
  }
  out.innerHTML = `<p class="finder-answer">${esc(d.answer)}</p>
    ${d.items.length ? `<h3 class="f-h">In your library</h3><div class="chat-cards mq-pop">${d.items.map((c, k) => titleCard(c, k)).join('')}</div>` : ''}
    ${d.requestable.length ? `<h3 class="f-h">You could request</h3><div class="chat-cards mq-pop">${d.requestable.map((c, k) => titleCard(c, k)).join('')}</div>` : ''}`;
}
function mountFinderEmpty() {
  const { icon } = K();
  $('.finder-out').innerHTML = `<div class="chat-empty"><span class="chat-orb">${icon('search', 26)}</span><h2>Pick a few chips to start</h2><p>Results are ordered by how well they fit your taste profile.</p></div>`;
}

/* ================================================================== action cards ("request seasons 5-8 of ...")
   Cards come from marquee/chat_actions.py. Nothing here acts on its own except what the person taps: Request sends
   the exact body shown (MarqueeStorage.sendRequest, the request sheet's own path), Mark watched marks, Delete only
   opens the hold-to-delete sheet, Fix buttons ask first where the hub's action restarts something. */
const UI = () => window.MarqueeUI || {};
function uiDo(ev) {
  const ui = UI();
  if (ev.do === 'open' && ui.open) ui.open(ev.id);
  else if (ev.do === 'tmdb' && ui.openTmdb) ui.openTmdb(ev.key);
  else if (ev.do === 'play' && ui.play) ui.play(ev.id, ev.label);
  else if (ev.do === 'delete' && ui.deleteLocal) ui.deleteLocal(ev.id, ev.name, ev.type);
  else if (ev.do === 'mylist-changed' && ui.mylistChanged) ui.mylistChanged();
}
const STATE_TAG = { downloading: 'Downloading', waiting: 'Not out yet', airing: 'Airing', searching: 'Searching', available: 'Arrived',
  pending: 'Needs approval', partial: 'Partly here', error: 'Couldn’t check', stuck: 'Stuck' };
function aBtn(b, n) {
  const { esc, icon } = K();
  return `<button type="button" class="btn small ${b.primary ? '' : 'ghost'}${b.danger ? ' danger' : ''}" data-cact="a" data-n="${n}">${b.icon ? icon(b.icon, 15) : ''} ${esc(b.label)}</button>`;
}
function dlList(items, more) {
  const { esc } = K();
  return `<ul class="stk-list">${items.map(i => `<li class="stk-dlrow"><div class="grow"><b class="stk-clip">${esc(i.title)}</b>
    <div class="stk-bar"><i style="width:${Math.max(2, Math.min(100, i.progress || 0))}%"></i></div>
    <span>${Math.round(i.progress || 0)}%${i.count > 1 ? ` · ${i.count} episodes` : ''}${i.eta ? ' · ' + esc(i.eta) + ' left' : ''}${i.stalled ? ' · stalled' : ''}${i.warning ? ' · ' + esc(i.warning) : ''}</span></div></li>`).join('')}</ul>
    ${more ? `<p class="stk-none">and ${more} more in Your stack</p>` : ''}`;
}
function actionCard(el, a, animate, at) {
  const { esc, icon } = K();
  const s = $('.cstack', el);
  let box = s.querySelector('.chat-acts');
  if (!box) {
    box = document.createElement('div');
    box.className = 'chat-acts';
    const after = s.querySelector('.chat-cards, .mem-chip, .msg-engine');
    after ? after.before(box) : s.append(box);
  }
  const card = document.createElement('div');
  card.className = 'acard' + (animate ? ' mq-pop' : '');
  card.dataset.aid = a.id || '';
  C.acts.set(a.id, a);
  const old = at && Date.now() / 1000 - at > 3600;   // from the history: a confirmation an hour old isn't offered again
  let btns = [];
  if (a.type === 'request') {
    const fit = a.fit || {};
    const notes = [...(a.notes || []).map(n => [n, '']), ...(a.smart_note ? [[a.smart_note, '']] : []),
      ...(fit.message ? [[fit.message, fit.level === 'bad' ? 'bad' : 'warn']] : []), ...(a.explain ? [[a.explain, '']] : [])];
    btns = [{ label: 'Request', primary: true, icon: 'plus', do: 'send-request' }, { label: 'Change', icon: 'gear', do: 'request-sheet', key: a.key }];
    card.innerHTML = `<div class="acard-row"><div class="acard-poster">${a.poster ? `<img src="${esc(a.poster)}" alt="">` : ''}</div>
      <div class="acard-main"><span class="acard-kicker">Ready to request</span><b class="acard-line">${esc(a.line)}</b>
        <span class="acard-sub">${esc([a.size_text && 'Size ' + a.size_text, a.server && 'via ' + a.server].filter(Boolean).join(' · '))}</span>
        ${notes.map(([n, c]) => `<span class="acard-note ${c}">${icon(c ? 'info' : 'check', 13)}<span>${esc(n)}</span></span>`).join('')}</div></div>
      <div class="acard-btns"></div>`;
    if (fit.blocked) btns[0].disabled = true;
  } else if (a.type === 'choose') {
    card.innerHTML = `<div class="acard-opts">${(a.options || []).map((o, k) => `<button type="button" class="acard-opt" data-cact="a" data-pick="${k}">
      ${o.poster ? `<img src="${esc(o.poster)}" alt="">` : '<span class="ph"></span>'}<div><b>${esc(o.label)}</b><span>${esc(o.sub || '')}</span></div></button>`).join('')}</div>`;
  } else if (a.type === 'watched') {
    btns = [{ label: a.on ? 'Mark watched' : 'Mark unwatched', primary: true, icon: 'check', do: 'mark-watched' }, { label: 'Cancel', do: 'cancel' }];
    card.innerHTML = `<div class="acard-row"><div class="acard-poster">${a.poster ? `<img src="${esc(a.poster)}" alt="">` : ''}</div>
      <div class="acard-main"><span class="acard-kicker">Confirm</span><b class="acard-line">${esc(a.title)}</b><span class="acard-sub">${a.series ? 'Every episode' : 'The film'} will be marked ${a.on ? 'watched' : 'unwatched'}, here and in Jellyfin.</span></div></div><div class="acard-btns"></div>`;
  } else if (a.type === 'mylist') {
    btns = [{ label: 'Undo', do: 'mylist-undo' }];
    card.innerHTML = `<div class="acard-head">${icon('check', 16)} ${a.on ? 'On My List' : 'Off My List'}: ${esc(a.title)}</div><div class="acard-btns"></div>`;
  } else if (a.type === 'buttons') {
    btns = a.buttons || [];
    card.classList.add('bare');
    card.innerHTML = '<div class="acard-btns"></div>';
  } else if (a.type === 'downloads') {
    card.innerHTML = `<div class="acard-head">${icon('cloud-off', 16)} Downloads</div>${dlList(a.items || [], a.more)}<div class="acard-btns"></div>`;
    btns = [{ label: 'Open Your stack', do: 'route', to: '#/stack', icon: 'film' }];
  } else if (a.type === 'doctor') {
    btns = a.buttons || [];
    card.innerHTML = `<div class="acard-head"><span class="stk-tag st-${esc(a.doc_state)}">${esc(STATE_TAG[a.doc_state] || a.doc_state)}</span> ${esc(a.title)}</div>
      <span class="acard-sub">${esc(a.reason || '')}</span>${(a.fixed || []).map(f => `<span class="acard-note">${icon('check', 13)}<span>Already fixed: ${esc(f)}</span></span>`).join('')}
      <div class="acard-btns"></div>`;
  } else if (a.type === 'stack') {
    const lv = { ok: 'ok', warn: 'warn', error: 'bad', unknown: 'unknown' }[a.level] || 'unknown';
    btns = [...(a.buttons || []), { label: 'Open Your stack', do: 'route', to: '#/stack', icon: 'film' }];
    card.innerHTML = `<div class="acard-head"><span class="stk-dot lv-${lv}"></span>${esc(a.summary || '')}</div>
      ${(a.problems || []).length ? `<ul class="stk-plist">${a.problems.map(p => `<li><span class="stk-dot lv-${p.level === 'error' ? 'bad' : p.level === 'warn' ? 'warn' : 'setup'}"></span><div class="grow"><b>${esc(p.name || '')}</b><span>${esc(p.text)}</span></div></li>`).join('')}</ul>` : ''}
      ${(a.facts || []).length && !(a.problems || []).length ? `<ul class="stk-hl">${a.facts.map(f => `<li class="ok">${icon('info', 13)}<span>${esc(f)}</span></li>`).join('')}</ul>` : ''}
      <span class="acard-via">${esc(a.via || '')}</span><div class="acard-btns"></div>`;
  }
  card._btns = btns;
  const bb = card.querySelector('.acard-btns');
  if (bb) {
    if (a.state) bb.innerHTML = doneLine(a);
    else if (old && ['request', 'watched'].includes(a.type)) bb.innerHTML = `<span class="acard-done muted">This was a while ago: ask again to do it.</span>`;
    else bb.innerHTML = btns.map((b, k) => aBtn(b, k)).join('').replace(/data-n="0">/, btns[0] && btns[0].disabled ? 'data-n="0" disabled>' : 'data-n="0">');
  }
  if (old && a.type === 'choose') card.querySelectorAll('.acard-opt').forEach(o => { o.disabled = true; });
  box.append(card);
}
function doneLine(a) {
  const { esc, icon } = K();
  const t = { sent: 'Requested', watched: a.on === false ? 'Marked unwatched' : 'Marked watched', cancelled: 'Cancelled', undone: 'Undone' }[a.state] || 'Done';
  return `<span class="acard-done${a.state === 'cancelled' || a.state === 'undone' ? ' muted' : ''}">${icon('check', 15)} ${esc(t)}</span>`;
}
function markCard(card, a, state) {
  a.state = state;
  const bb = card.querySelector('.acard-btns');
  if (bb) bb.innerHTML = doneLine(a);
  K().api('/api/chat/action-state', { id: a.id, state }).catch(() => {});
}
async function onActionButton(b) {
  const { api, toast } = K();
  const card = b.closest('.acard');
  const a = C.acts.get(card && card.dataset.aid);
  if (!a) return;
  if (b.dataset.pick != null) {
    const o = a.options[+b.dataset.pick];
    card.querySelectorAll('.acard-opt').forEach(x => { x.disabled = true; x.classList.toggle('picked', x === b); });
    return send(o.label, { pick: o.pick, intent: a.intent });
  }
  const btn = card._btns[+b.dataset.n];
  if (!btn) return;
  const ui = UI();
  switch (btn.do) {
    case 'send-request': {
      if (!window.MarqueeStorage) return toast('Requests aren’t available in this version');
      b.disabled = true;
      try {
        const r = await window.MarqueeStorage.sendRequest(a.body);
        const rc = b.getBoundingClientRect();
        ui.confetti && ui.confetti(rc.left + rc.width / 2, rc.top + rc.height / 2);
        toast(r.message || 'Requested', { icon: 'sparkle', ms: 4200 });
        ui.requested && ui.requested(a.key);
        markCard(card, a, 'sent');
      } catch (e) { b.disabled = false; toast(`Request failed: ${e.message}`); }
      return;
    }
    case 'request-sheet': return ui.request && ui.request(btn.key);
    case 'mark-watched':
      b.disabled = true;
      try { await api(`/api/played/${encodeURIComponent(a.item_id)}`, { played: a.on !== false }); ui.watchedChanged && ui.watchedChanged(a.item_id); markCard(card, a, 'watched'); }
      catch (e) { b.disabled = false; toast(e.message); }
      return;
    case 'cancel': return markCard(card, a, 'cancelled');
    case 'mylist-undo':
      try {
        if (a.on) await api('/api/mylist', { key: a.key }, 'DELETE');
        else await api('/api/mylist', { key: a.key, ...(a.data || {}) });
        ui.mylistChanged && ui.mylistChanged();
        markCard(card, a, 'undone');
      } catch (e) { toast(e.message); }
      return;
    case 'open': return ui.open && ui.open(btn.id);
    case 'tmdb': return ui.openTmdb && ui.openTmdb(btn.key);
    case 'play': return ui.play && ui.play(btn.id, btn.label);
    case 'play-item': return ui.playItem && ui.playItem(btn.id);
    case 'delete': return ui.deleteLocal && ui.deleteLocal(btn.id, btn.name, btn.type_);
    case 'ask': return send(btn.q);
    case 'route': location.hash = btn.to; return;
    case 'stack-action':
      if (!window.MarqueeStack) return;
      b.disabled = true;
      await window.MarqueeStack.runAction(btn.action, btn.arg, btn.label, btn.confirm);
      b.disabled = false;
      return;
  }
}

window.MarqueeChat = { mount, send: t => send(t) };
})();
