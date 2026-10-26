/* Marquee Mobile: the Remote tab, which controls the player on the PC. Polls the PC once a second while the tab is
   open, moves the progress bar smoothly in between, and applies taps optimistically so it feels instant. */
(() => {
  'use strict';
  const M = window.M;
  const { $, $$, esc } = M;
  const R = (M.remote = {});
  let active = false;

  let S = null;              // last state from the PC
  let base = { pos: 0, at: 0 }; // where playback was at a known moment (for smooth progress)
  const hold = {};           // field -> until: ignore the PC's value for a moment after a local change
  const held = (k) => (hold[k] || 0) > performance.now();
  const holdFor = (k, ms = 1500) => { hold[k] = performance.now() + ms; };
  let lastItem = null;
  let dragging = false;
  let fails = 0;
  let volDragging = false;

  R.enter = () => {
    active = true;
    poll();
    if (!frameOn) { frameOn = true; requestAnimationFrame(frame); }
  };
  R.leave = () => {
    active = false;
    clearTimeout(pollTimer);
    $('#ambient').classList.remove('on');
  };

  function position() {
    if (!S) return 0;
    if (!S.playing || S.paused || S.buffering) return base.pos;
    return Math.min(S.duration || Infinity, base.pos + (performance.now() - base.at) / 1000);
  }

  function apply(st) {
    const prev = S;
    S = { ...(S || {}), ...st };
    if (prev) {
      if (held('paused')) S.paused = prev.paused;
      if (held('volume')) S.volume = prev.volume;
      if (held('muted')) S.muted = prev.muted;
      if (held('subs')) S.subs = prev.subs;
    }
    if (!held('pos') && !dragging) base = { pos: st.position || 0, at: performance.now() };
    render();
  }

  function render() {
    const playing = !!(S && S.playing && S.item);
    $('#np').hidden = !playing;
    $('#idle').hidden = playing;
    if (!playing) {
      lastItem = null;
      $('#ambient').classList.remove('on');
      loadIdle();
      return;
    }
    const it = S.item;
    if (lastItem !== it.id) {
      lastItem = it.id;
      const art = it.type === 'Episode' ? (it.thumb || it.backdrop) : (it.backdrop || it.thumb || it.poster);
      M.loadImg($('#art-img'), art);
      M.loadImg($('#art-logo'), it.type === 'Episode' ? null : it.logo);
      $('#np-title').textContent = it.type === 'Episode' ? (it.series || it.name) : it.name;
      $('#np-sub').textContent = it.type === 'Episode' ? M.epLabel(it)
        : [it.year, it.age, it.runtime ? M.mins(it.runtime) : ''].filter(Boolean).join(' · ');
      const amb = $('#ambient');
      const bg = it.backdrop || it.poster;
      amb.classList.remove('on');
      if (bg) {
        const pre = new Image();
        pre.onload = () => { if (active) { amb.style.backgroundImage = `url("${bg}")`; amb.classList.add('on'); } };
        pre.src = bg;
      }
    }
    $('#pp').classList.toggle('paused', !!S.paused);
    $('#art').classList.toggle('paused', !!S.paused);
    $('#buffer').classList.toggle('on', !!S.buffering);
    const muted = !!S.muted;
    $('#mute').classList.toggle('muted', muted);
    $('.volume').classList.toggle('muted', muted);
    if (!volDragging) setVol(S.volume);
    const subs = $('#t-subs');
    subs.classList.toggle('on', !!S.subs);
    subs.disabled = !S.has_subs;
    $('#t-next').disabled = !S.next;
    $('#t-full').classList.toggle('on', !!S.fullscreen);
    $('#t-pip').classList.toggle('on', !!S.pip);
    const nl = $('#next-line');
    nl.hidden = !S.next;
    if (S.next) nl.innerHTML = `Up next: <b>${esc(S.next.title)}</b>`;
  }

  let frameOn = false;
  function frame() {
    if (!active) { frameOn = false; return; }
    if (S && S.playing && !dragging) drawProgress(position());
    requestAnimationFrame(frame);
  }
  setInterval(() => { if (active && S && S.playing && !dragging) drawProgress(position()); }, 250);
  function drawProgress(p) {
    const d = (S && S.duration) || 0;
    const f = d ? Math.min(1, Math.max(0, p / d)) : 0;
    const w = $('#scrub').clientWidth;
    $('#fill').style.transform = `scaleX(${f})`;
    $('#knob').style.transform = `translateX(${f * w}px)`;
    $('#t-pos').textContent = M.fmt(p);
    $('#t-left').textContent = '-' + M.fmt(d - p);
    $('#scrub').setAttribute('aria-valuenow', Math.round(p));
    $('#scrub').setAttribute('aria-valuemax', Math.round(d));
    return f;
  }

  // ---------------------------------------------------------------- polling
  let pollTimer;
  async function poll() {
    clearTimeout(pollTimer);
    if (!active) return;
    try {
      const d = await M.api('state', undefined, { quiet: true });
      fails = 0;
      M.setLink('live', 'Connected');
      apply(d.state);
    } catch (e) {
      fails++;
      if (e.status === 401 || e.status === 403) return;
      if (fails > 1) M.setLink('lost', 'Reconnecting');
    }
    if (active && !document.hidden) pollTimer = setTimeout(poll, fails > 3 ? 3000 : 1000);
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden && active) poll(); });

  // ---------------------------------------------------------------- commands
  async function cmd(action, value, extra) {
    try {
      await M.api('cmd', { action, value, ...(extra || {}) });
      return true;
    } catch (e) {
      M.toast(e.message);
      M.buzz([30, 40, 30]);
      setTimeout(poll, 150);
      return false;
    }
  }
  function seekBy(sec) {
    if (!S) return;
    const p = Math.max(0, Math.min(S.duration || Infinity, position() + sec));
    base = { pos: p, at: performance.now() };
    holdFor('pos', 1500);
    cmd('seek', sec, { relative: true });
  }
  function ping(el) {
    el.classList.remove('ping');
    void el.offsetWidth;
    el.classList.add('ping');
  }
  $('#v-remote').addEventListener('click', (e) => {
    const b = e.target.closest('[data-cmd]');
    if (!b || b.disabled) return;
    const c = b.dataset.cmd;
    if (c === 'pp') {
      M.buzz([12, 30, 12]);
      ping(b);
      const now = position();
      S.paused = !S.paused;
      base = { pos: now, at: performance.now() };
      holdFor('paused');
      render();
      cmd('pause', S.paused ? 'pause' : 'play');
    } else if (c === 'back' || c === 'fwd') {
      M.buzz(10);
      ping(b);
      seekBy(c === 'back' ? -10 : 10);
    } else if (c === 'mute') {
      M.buzz(10);
      S.muted = !S.muted;
      holdFor('muted');
      render();
      cmd('mute', S.muted);
    } else if (c === 'subs') {
      M.buzz(10);
      S.subs = !S.subs;
      holdFor('subs', 2000);
      render();
      cmd('subs');
    } else if (c === 'next') {
      M.buzz([10, 40, 10]);
      cmd('next').then((ok) => ok && M.toast('Starting the next episode'));
    } else if (c === 'fullscreen' || c === 'pip') {
      M.buzz(10);
      cmd(c);
      setTimeout(poll, 500);
    } else if (c === 'stop') {
      M.buzz([20, 50, 20]);
      cmd('stop').then(() => setTimeout(poll, 400));
    }
  });

  // ---------------------------------------------------------------- scrubber
  const scrub = $('#scrub');
  let dragTo = 0;
  function scrubAt(x) {
    const r = scrub.getBoundingClientRect();
    const f = Math.min(1, Math.max(0, (x - r.left) / r.width));
    dragTo = f * ((S && S.duration) || 0);
    drawProgress(dragTo);
    const bub = $('#bubble');
    bub.textContent = M.fmt(dragTo);
    const half = bub.offsetWidth / 2;
    bub.style.left = Math.min(r.width - half, Math.max(half, f * r.width)) + 'px';
  }
  let lastTick = 0;
  scrub.addEventListener('pointerdown', (e) => {
    if (!S || !S.duration) return;
    dragging = true;
    scrub.classList.add('drag');
    scrub.setPointerCapture(e.pointerId);
    scrubAt(e.clientX);
    M.buzz(6);
  });
  scrub.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    scrubAt(e.clientX);
    const minute = Math.floor(dragTo / 60);
    if (minute !== lastTick) { lastTick = minute; M.buzz(3); }
  });
  const endDrag = () => {
    if (!dragging) return;
    dragging = false;
    scrub.classList.remove('drag');
    base = { pos: dragTo, at: performance.now() };
    holdFor('pos', 1800);
    M.buzz(12);
    cmd('seek', dragTo, { relative: false });
  };
  scrub.addEventListener('pointerup', endDrag);
  scrub.addEventListener('pointercancel', endDrag);
  scrub.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowLeft') seekBy(-10);
    if (e.key === 'ArrowRight') seekBy(10);
  });

  // ---------------------------------------------------------------- volume
  const vol = $('#vol');
  let volTimer = null;
  function setVol(x) {
    x = Math.round(x || 0);
    vol.value = x;
    vol.style.setProperty('--p', x + '%');
    $('#vol-num').textContent = x;
  }
  function sendVol() {
    volTimer = null;
    holdFor('volume', 1200);
    cmd('volume', +vol.value);
  }
  vol.addEventListener('input', () => {
    volDragging = true;
    setVol(+vol.value);
    if (S) { S.volume = +vol.value; if (S.muted && +vol.value > 0) { S.muted = false; holdFor('muted'); render(); } }
    holdFor('volume', 1200);
    if (+vol.value % 10 === 0) M.buzz(4);
    if (!volTimer) volTimer = setTimeout(sendVol, 120);
  });
  vol.addEventListener('change', () => {
    volDragging = false;
    clearTimeout(volTimer);
    sendVol();
  });

  // ---------------------------------------------------------------- idle: pick something for the PC
  let idleAt = 0;
  async function loadIdle() {
    if (Date.now() - idleAt < 30000) return;
    idleAt = Date.now();
    let d;
    try { d = await M.api('home', undefined, { quiet: true }); } catch (e) { return; }
    const items = d.rows.filter((r) => r.key === 'continue' || r.key === 'nextup').flatMap((r) => r.items).slice(0, 12);
    const row = $('#idle-row');
    row.hidden = !items.length;
    $('#idle-strip').replaceChildren(...items.map((c, i) => M.wideCard(c, i, (x) => M.playOnPC(x.id))));
  }
})();
