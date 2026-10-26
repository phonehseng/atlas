/* Marquee Mobile player: plays a title on the phone straight from the PC.
   The PC decides direct play / remux / transcode from what this phone says it can play (canPlayType, MSE and
   MediaCapabilities) and, on Auto, a quick bandwidth probe; HLS goes through hls.js where Media Source exists, or the
   phone's own HLS player (iOS). YouTube-style controls: double-tap the sides to jump 10 s, red progress bar,
   captions/audio/speed/quality menus, Skip intro / credits, next-episode autoplay, picture-in-picture, full screen.
   The position is saved every 10 s, so the PC and Jellyfin pick up where the phone left off. */
(() => {
  'use strict';
  const M = window.M;
  const { $, $$, esc } = M;
  const P = (M.player = {});
  const el = $('#player');
  const v = $('#pv');
  const ORDER = ['original', '1080', '720', '480'];
  const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];
  const metrics = (window.__mm = { ttff: [], seeks: [], stalls: 0, mode: null, events: [] });

  let S = null;            // the title playing: {id, info, stream, audio, sub, quality, ...}
  let hls = null;
  let pending = null;      // options for the next open from the route
  let uiTimer = null;
  let saveTimer = null;
  let statsTimer = null;
  let rafOn = false;

  const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

  // ---------------------------------------------------------------- what this phone can play
  let capsP = null;
  function useHlsJs() {
    const nativeHls = !!v.canPlayType('application/vnd.apple.mpegurl');
    return !!(window.Hls && window.Hls.isSupported()) && !(isIOS && nativeHls);
  }
  async function caps() {
    if (window.__mmCaps) return { ...(await detect()), ...window.__mmCaps };   // tests: pretend to be another phone
    return detect();
  }
  async function detect() {
    if (capsP) return capsP;
    capsP = (async () => {
      const probe = document.createElement('video');
      const can = (t) => { const r = probe.canPlayType(t); return r === 'probably' || r === 'maybe'; };
      const MS = window.ManagedMediaSource || window.MediaSource;
      const mse = (t) => !!(MS && MS.isTypeSupported && MS.isTypeSupported(t));
      const viaHls = useHlsJs();
      const vid = (codec) => (viaHls ? mse(`video/mp4; codecs="${codec}"`) : false) || can(`video/mp4; codecs="${codec}"`);
      const aud = (codec) => (viaHls ? mse(`audio/mp4; codecs="${codec}"`) : false) || can(`audio/mp4; codecs="${codec}"`);
      const c = {
        h264: vid('avc1.640028'),
        hevc: vid('hvc1.1.6.L120.90') || vid('hev1.1.6.L120.90'),
        hevc10: vid('hvc1.2.4.L153.B0') || vid('hvc1.2.4.L120.90'),
        av1: vid('av01.0.08M.08'),
        vp9: vid('vp09.00.40.08'),
        mkv: can('video/x-matroska; codecs="avc1.640028"') || can('video/webm; codecs="avc1.640028"'),
        audio: { aac: aud('mp4a.40.2'), ac3: aud('ac-3'), eac3: aud('ec-3'), opus: aud('opus'), flac: aud('flac'), mp3: aud('mp4a.69') || aud('mp3') },
        hdr: false,
        max_height: Math.min(2160, Math.round(Math.max(screen.width, screen.height) * (window.devicePixelRatio || 1))),
        hls: viaHls ? 'hls.js' : 'native',
      };
      c.hevc_out = isIOS && c.hevc;   // iPhones decode HEVC in hardware: better picture for the same bitrate
      const screenHdr = matchMedia('(dynamic-range: high)').matches;
      if (navigator.mediaCapabilities && navigator.mediaCapabilities.decodingInfo) {
        const type = viaHls ? 'media-source' : 'file';
        const ask = (video) => navigator.mediaCapabilities.decodingInfo({ type, video }).catch(() => null);
        const [hdr, h264_4k] = await Promise.all([
          c.hevc10 && screenHdr ? ask({ contentType: 'video/mp4; codecs="hvc1.2.4.L153.B0"', width: 3840, height: 2160, bitrate: 40e6, framerate: 24, transferFunction: 'pq', colorGamut: 'rec2020', hdrMetadataType: 'smpteSt2086' }) : null,
          ask({ contentType: 'video/mp4; codecs="avc1.640033"', width: 3840, height: 2160, bitrate: 40e6, framerate: 24 }),
        ]);
        c.hdr = !!(hdr && hdr.supported && hdr.smooth);
        if (h264_4k && !(h264_4k.supported && h264_4k.smooth)) c.max_height = Math.min(c.max_height, 1440);
      }
      return c;
    })();
    return capsP;
  }
  P.caps = caps;

  // ---------------------------------------------------------------- bandwidth probe (Auto)
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
      if (a.secs < 0.6) {  // fast enough for a bigger sample (latency dominates the small one)
        const b = await get(est > 40e6 ? 4096 : 1536);
        est = (b.bytes * 8) / b.secs;
      }
      bw = { bps: est, at: Date.now() };
    } catch (e) { bw = { bps: 0, at: 0 }; }
    metrics.bandwidth = bw.bps;
    return bw.bps;
  }
  P.measure = measure;

  // ---------------------------------------------------------------- open / close
  M.play = (id, opts = {}) => {
    M.buzz(12);
    pending = { ...opts, t0: performance.now(), pushed: !(P.isOpen() && S) || (S && S.pushed) };
    v.muted = !!(window.__mmMuted || v.muted);
    v.play().catch(() => {});   // unlock the element inside the tap (iOS needs a gesture for sound)
    if (P.isOpen() && S) location.replace('#/play/' + encodeURIComponent(id));
    else location.hash = '#/play/' + encodeURIComponent(id);
  };
  P.isOpen = () => !el.hidden;
  P.openFromRoute = (id) => {
    if (S && S.id === id && P.isOpen()) return;
    const o = pending || { t0: performance.now() };
    pending = null;
    load(id, o);
  };
  P.close = (fromRoute) => {
    if (!P.isOpen()) return;
    save();
    teardown(true);
    el.hidden = true;
    el.classList.remove('ui', 'paused');
    document.body.classList.remove('player-open');
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    try { screen.orientation && screen.orientation.unlock && screen.orientation.unlock(); } catch (e) { /* not supported */ }
    const pushed = S && S.pushed;
    S = null;
    M.refreshHome();
    M.forgetItem();
    if (!fromRoute) { if (pushed) history.back(); else location.replace('#/home'); }
  };

  function teardown(stopSession) {
    clearInterval(saveTimer);
    clearInterval(statsTimer);
    clearTimeout(watchdog);
    hideNext();
    $('#p-skip').hidden = true;
    $('#p-err').hidden = true;
    if (hls) { try { hls.destroy(); } catch (e) { /* gone */ } hls = null; }
    if (stopSession && S && S.stream) M.api(`stream/${S.stream.session}/stop`, {}, { quiet: true, keepalive: true }).catch(() => {});
    v.pause();
    v.removeAttribute('src');
    $$('track', v).forEach((t) => t.remove());
    try { v.load(); } catch (e) { /* fine */ }
    cues = [];
    renderSubs(true);
  }

  async function load(id, o) {
    if (S) { save(); teardown(true); }
    el.hidden = false;
    document.body.classList.add('player-open');
    el.classList.add('ui');
    el.classList.remove('paused');
    spin(true);
    $('#p-title').textContent = '';
    $('#p-sub').textContent = '';
    $('#p-badge').textContent = '';
    $('#p-pc').hidden = !(M.me && M.me.owner);
    S = { id, pushed: !!o.pushed, t0: o.t0 || performance.now(), quality: o.quality || M.prefs.quality || (M.me && M.me.quality) || 'auto', tries: 0, firstFrame: false, nextShown: false, cancelledNext: false };
    const me = S;
    let info;
    try { info = await M.api(`play/${encodeURIComponent(id)}/info`); } catch (e) { if (S === me) fail(e.message); return; }
    if (S !== me) return;
    S.info = info;
    S.audio = info.default_audio;
    S.sub = info.default_sub;
    $('#p-title').textContent = info.title || info.name || '';
    $('#p-sub').textContent = info.type === 'Episode' ? info.subtitle : (info.name !== info.title ? info.name : info.subtitle);
    marks();
    mediaSession(info);
    const pos = o.start != null ? o.start : (info.position || 0);
    await startStream(pos);
    saveTimer = setInterval(() => { if (S && !v.paused && !v.seeking) save(); }, 10000);
    statsTimer = setInterval(stats, 1000);
    if (!rafOn) { rafOn = true; requestAnimationFrame(frame); }
    showUI();
  }

  let watchdog = null;
  async function startStream(pos, extra = {}) {
    const me = S;
    spin(true);
    $('#p-err').hidden = true;
    const c = await caps();
    const quality = S.override || S.quality;
    const bandwidth = quality === 'auto' ? await measure() : null;
    let r;
    try {
      r = await M.api(`play/${encodeURIComponent(S.id)}/start`, { caps: c, quality, audio: S.audio, sub: S.sub, start: pos, bandwidth, force: extra.force || S.force || null });
    } catch (e) { if (S === me) fail(e.message); return; }
    if (S !== me) { M.api(`stream/${r.session}/stop`, {}, { quiet: true }).catch(() => {}); return; }
    if (S.stream && S.stream.session !== r.session) M.api(`stream/${S.stream.session}/stop`, {}, { quiet: true }).catch(() => {});
    S.stream = r;
    S.mode = r.mode;
    metrics.mode = r.mode;
    metrics.events.push({ t: Math.round(performance.now()), ev: 'start', mode: r.mode, label: r.label, reason: r.reason });
    $('#p-badge').textContent = { direct: '', remux: '', transcode: r.height >= 1000 ? 'HD' : '' }[r.mode] || '';
    attach(r, pos);
    subs(r);
  }

  function attach(r, pos) {
    if (hls) { try { hls.destroy(); } catch (e) { /* gone */ } hls = null; }
    $$('track', v).forEach((t) => t.remove());
    const playIt = () => v.play().then(() => {}, () => { el.classList.add('paused'); showUI(true); spin(false); });
    clearTimeout(watchdog);
    watchdog = setTimeout(() => { if (S && !S.firstFrame && v.readyState < 2) fallback('The video didn’t start'); }, r.kind === 'file' ? 10000 : 30000);
    if (r.kind === 'hls' && useHlsJs()) {
      hls = new window.Hls({
        startPosition: pos || 0, maxBufferLength: 45, maxMaxBufferLength: 120, backBufferLength: 30, maxBufferHole: 0.6,
        fragLoadingTimeOut: 40000, fragLoadingMaxRetry: 4, levelLoadingTimeOut: 15000, manifestLoadingTimeOut: 15000,
        enableWorker: true, lowLatencyMode: false, startFragPrefetch: true, progressive: false, nudgeMaxRetry: 6,
      });
      hls.on(window.Hls.Events.ERROR, (_e, data) => onHlsError(data));
      hls.loadSource(r.url);
      hls.attachMedia(v);
      hls.on(window.Hls.Events.MANIFEST_PARSED, playIt);
    } else {
      v.src = r.url;
      if (pos > 0) v.addEventListener('loadedmetadata', () => { try { v.currentTime = pos; } catch (e) { /* not seekable yet */ } }, { once: true });
      playIt();
    }
  }

  let mediaRecovered = 0;
  function onHlsError(data) {
    metrics.events.push({ t: Math.round(performance.now()), ev: 'hls-error', type: data.type, details: data.details, fatal: data.fatal });
    if (!data.fatal) return;
    if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR && mediaRecovered < 2) { mediaRecovered++; hls.recoverMediaError(); return; }
    if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR && S && S.tries < 2) { S.tries++; hls.startLoad(); return; }
    fallback(data.details || 'Playback error');
  }

  function fallback(why) {
    if (!S || !S.stream) return;
    const pos = v.currentTime || (S.info && S.info.position) || 0;
    metrics.events.push({ t: Math.round(performance.now()), ev: 'fallback', from: S.mode, why });
    if (S.mode === 'direct') { S.force = 'remux'; M.toast('Switching to a stream your phone can play'); startStream(pos); return; }
    if (S.mode === 'remux') { S.force = 'transcode'; startStream(pos); return; }
    const q = S.stream.quality;
    const lower = ORDER[ORDER.indexOf(q) + 1];
    if (lower && S.tries < 3) { S.tries++; S.override = lower; startStream(pos); return; }
    fail(`This video couldn’t play: ${why}`);
  }

  function fail(msg) {
    spin(false);
    $('#p-err-msg').textContent = msg;
    $('#p-err').hidden = false;
  }
  $('#p-err-retry').addEventListener('click', () => { if (!S) return; S.tries = 0; $('#p-err').hidden = true; startStream(v.currentTime || (S.info && S.info.position) || 0, { force: 'transcode' }); });
  $('#p-err-close').addEventListener('click', () => P.close());

  // ---------------------------------------------------------------- video events
  v.addEventListener('playing', () => {
    spin(false);
    el.classList.remove('paused');
    if (S && !S.firstFrame) {
      S.firstFrame = true;
      clearTimeout(watchdog);
      const t = Math.round(performance.now() - S.t0);
      metrics.ttff.push({ id: S.id, mode: S.mode, ms: t });
      S.ttff = t;
    }
    if (stallAt) { const d = performance.now() - stallAt; stallAt = 0; if (d > 3500) stalled('long'); }
    if (seekT0) { metrics.seeks.push({ mode: S && S.mode, ms: Math.round(performance.now() - seekT0) }); seekT0 = 0; }
    hideUISoon();
  });
  v.addEventListener('pause', () => { el.classList.add('paused'); showUI(true); save(); });
  v.addEventListener('play', () => el.classList.remove('paused'));
  let seekT0 = 0, stallAt = 0, lastSeek = 0;
  v.addEventListener('seeking', () => { seekT0 = performance.now(); lastSeek = seekT0; spin(true); });
  v.addEventListener('seeked', () => { if (v.paused) { spin(false); if (seekT0) { metrics.seeks.push({ mode: S && S.mode, ms: Math.round(performance.now() - seekT0), paused: true }); seekT0 = 0; } } });
  v.addEventListener('waiting', () => {
    spin(true);
    if (S && S.firstFrame && !v.seeking && performance.now() - lastSeek > 4000) { stallAt = performance.now(); metrics.stalls++; stalled('waiting'); }
  });
  v.addEventListener('canplay', () => { if (!v.seeking) spin(false); });
  v.addEventListener('error', () => {
    if (!S || !v.getAttribute('src') && !hls) return;
    if (hls) return;   // hls.js reports its own errors
    fallback((v.error && v.error.message) || 'The phone couldn’t decode this');
  });
  v.addEventListener('ended', () => {
    save(true);
    if (S && S.info && S.info.next && M.prefs.autoplay && !S.cancelledNext) playNext();
    else { showUI(true); }
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden && S) save(); });
  window.addEventListener('pagehide', () => { if (S) save(); });

  // Auto quality: step down when the buffer runs dry
  const stalls = [];
  function stalled(kind) {
    if (!S || (S.override || S.quality) !== 'auto' && !S.override) return;
    const now = performance.now();
    stalls.push(now);
    while (stalls.length && now - stalls[0] > 60000) stalls.shift();
    if (kind === 'long' || stalls.length >= 2) {
      const cur = S.stream && S.stream.quality;
      const lower = ORDER[ORDER.indexOf(cur) + 1];
      if (!lower || (S.lastDrop && now - S.lastDrop < 20000)) return;
      S.lastDrop = now;
      stalls.length = 0;
      S.override = lower;
      bw = { bps: 0, at: 0 };
      M.toast(`Switched to ${lower}p to keep playing smoothly`);
      startStream(v.currentTime);
    }
  }
  // the buffer getting thin while playing counts too (before it actually stops)
  setInterval(() => {
    if (!S || !S.firstFrame || v.paused || v.seeking || S.mode === 'direct') { S && (S.thin = 0); return; }
    const ahead = bufferedAhead();
    S.thin = ahead < 3 && performance.now() - lastSeek > 8000 ? (S.thin || 0) + 1 : 0;
    if (S.thin >= 6) { S.thin = 0; stalled('thin'); }
  }, 1000);

  function bufferedAhead() {
    const t = v.currentTime;
    for (let i = 0; i < v.buffered.length; i++) if (v.buffered.start(i) <= t + 0.3 && v.buffered.end(i) >= t) return v.buffered.end(i) - t;
    return 0;
  }

  // ---------------------------------------------------------------- saving the position
  function save(ended) {
    if (!S || !S.info || !S.firstFrame) return;
    const pos = v.currentTime || 0;
    if (pos < 2 && !ended) return;
    M.api('progress', { id: S.info.id, position: pos, duration: S.info.duration, ended: !!ended }, { keepalive: true, quiet: true }).catch(() => {});
  }
  P.save = save;
  P.debug = () => ({ cues: cues.length, cueIdx, mode: S && S.mode, sub: S && S.sub, subUrl: !!(S && S.stream && S.stream.sub_url), raf: rafOn });

  // ---------------------------------------------------------------- controls
  function spin(on) { $('#p-spin').classList.toggle('on', !!on); }
  function showUI(stay) {
    el.classList.add('ui');
    clearTimeout(uiTimer);
    if (!stay) hideUISoon();
  }
  function hideUISoon() {
    clearTimeout(uiTimer);
    uiTimer = setTimeout(() => {
      if (!v.paused && !dragging && $('#p-sheet').hidden) el.classList.remove('ui');
    }, 3200);
  }
  function toggleUI() {
    if (el.classList.contains('ui')) { el.classList.remove('ui'); clearTimeout(uiTimer); } else showUI();
  }
  function togglePlay() {
    M.buzz(8);
    if (v.paused) v.play().catch(() => {}); else v.pause();
    showUI();
  }
  function seekBy(sec) {
    const d = (S && S.info && S.info.duration) || v.duration || 0;
    v.currentTime = Math.max(0, Math.min(d ? d - 1 : Infinity, v.currentTime + sec));
  }
  $('#p-pp').addEventListener('click', togglePlay);
  $$('[data-p]', el).forEach((b) => b.addEventListener('click', () => { M.buzz(8); seekBy(b.dataset.p === 'back' ? -10 : 10); showUI(); }));
  $('#p-close').addEventListener('click', () => P.close());
  $('#p-pc').addEventListener('click', () => {
    if (!S || !S.info) return;
    const id = S.info.id, t = v.currentTime;
    P.close();
    setTimeout(() => M.playOnPC(id, t), 300);
  });

  // gestures: tap shows/hides the controls, double-tap the sides jumps 10 s (keep tapping to jump more)
  const gest = $('#p-gest');
  let lastTap = { t: 0, side: null }, tapTimer = null, streak = 0;
  gest.addEventListener('pointerup', (e) => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    const w = gest.clientWidth, x = e.clientX - gest.getBoundingClientRect().left;
    const side = x < w * 0.38 ? 'l' : x > w * 0.62 ? 'r' : 'c';
    const now = performance.now();
    const quick = now - lastTap.t < 320 && side === lastTap.side;
    if (side !== 'c' && (quick || (streak && now - lastTap.t < 700 && side === lastTap.side))) {
      clearTimeout(tapTimer);
      streak++;
      seekBy(side === 'l' ? -10 : 10);
      const rip = side === 'l' ? $('#rip-l') : $('#rip-r');
      rip.querySelector('span').textContent = `${streak * 10} seconds`;
      rip.classList.remove('go'); void rip.offsetWidth; rip.classList.add('go');
      M.buzz(6);
      lastTap = { t: now, side };
      return;
    }
    streak = 0;
    lastTap = { t: now, side };
    clearTimeout(tapTimer);
    if (side === 'c') toggleUI();
    else tapTimer = setTimeout(toggleUI, 260);
  });
  gest.addEventListener('dblclick', (e) => e.preventDefault());

  // progress bar
  const bar = $('#p-bar');
  let dragging = false, dragTo = 0;
  function barAt(x) {
    const r = bar.getBoundingClientRect();
    const f = Math.min(1, Math.max(0, (x - r.left) / r.width));
    const d = duration();
    dragTo = f * d;
    draw(dragTo);
    const tip = $('#p-tip');
    tip.textContent = M.fmt(dragTo);
    const half = tip.offsetWidth / 2;
    tip.style.left = Math.min(r.width - half, Math.max(half, f * r.width)) + 'px';
  }
  bar.addEventListener('pointerdown', (e) => {
    if (!duration()) return;
    dragging = true;
    bar.classList.add('drag');
    bar.setPointerCapture(e.pointerId);
    barAt(e.clientX);
    showUI(true);
  });
  bar.addEventListener('pointermove', (e) => { if (dragging) barAt(e.clientX); });
  const endDrag = () => {
    if (!dragging) return;
    dragging = false;
    bar.classList.remove('drag');
    v.currentTime = dragTo;
    M.buzz(8);
    hideUISoon();
  };
  bar.addEventListener('pointerup', endDrag);
  bar.addEventListener('pointercancel', endDrag);
  bar.addEventListener('keydown', (e) => { if (e.key === 'ArrowLeft') seekBy(-10); if (e.key === 'ArrowRight') seekBy(10); });

  function duration() { return (S && S.info && S.info.duration) || (isFinite(v.duration) ? v.duration : 0); }
  function draw(t) {
    const d = duration();
    const f = d ? Math.min(1, t / d) : 0;
    $('#p-play').style.width = (f * 100) + '%';
    $('#p-knob').style.left = (f * 100) + '%';
    $('#p-pos').textContent = M.fmt(t);
    $('#p-dur').textContent = M.fmt(d);
  }
  function tick() {
    if (!P.isOpen()) return;
    if (!dragging) draw(v.currentTime || 0);
    const d = duration();
    if (d) $('#p-buf').style.width = Math.min(100, ((v.currentTime + bufferedAhead()) / d) * 100) + '%';
    renderSubs();
    segments();
  }
  function frame() {   // smooth while the page paints; timeupdate + a timer keep it going when it doesn't
    if (!P.isOpen()) { rafOn = false; return; }
    tick();
    requestAnimationFrame(frame);
  }
  v.addEventListener('timeupdate', tick);
  setInterval(() => { if (P.isOpen()) tick(); }, 250);
  function marks() {
    const d = duration();
    $('#p-marks').innerHTML = d ? (S.info.segments || []).map((s) => `<i style="left:${(s.start / d) * 100}%;width:${Math.max(0.4, ((s.end - s.start) / d) * 100)}%"></i>`).join('') : '';
  }

  // ---------------------------------------------------------------- skip intro / credits, up next
  function segments() {
    if (!S || !S.info || !S.firstFrame) return;
    const t = v.currentTime, d = duration();
    const seg = (S.info.segments || []).find((s) => t >= s.start && t < s.end - 0.8);
    const skip = $('#p-skip');
    const next = S.info.next;
    const creditsAt = ((S.info.segments || []).find((s) => s.kind === 'credits') || {}).start;
    const nearEnd = next && (creditsAt ? t >= creditsAt : d && t >= d - 25);
    if (nearEnd && !S.cancelledNext && !S.nextShown) showNext();
    if (!nearEnd && S.nextShown) hideNext();
    if (seg && !(seg.kind === 'credits' && S.nextShown)) {
      skip.hidden = false;
      skip.querySelector('span').textContent = seg.kind === 'intro' ? 'Skip intro' : 'Skip credits';
      skip.onclick = () => {
        M.buzz(10);
        if (seg.kind === 'credits' && next) { playNext(); return; }
        v.currentTime = Math.min(seg.end, d - 0.5);
      };
    } else skip.hidden = true;
  }
  let nextTimer = null;
  function showNext() {
    const n = S.info.next;
    S.nextShown = true;
    $('#p-next').hidden = false;
    $('#p-next-title').textContent = `S${n.season ?? 0}:E${n.episode ?? 0} · ${n.name || ''}`;
    M.loadImg($('#p-next-img'), n.thumb || n.backdrop);
    const box = $('#p-next');
    box.classList.remove('counting');
    clearTimeout(nextTimer);
    if (M.prefs.autoplay) {
      void box.offsetWidth;
      box.style.setProperty('--count', '10s');
      box.classList.add('counting');
      nextTimer = setTimeout(() => { if (S && S.nextShown && !v.paused) playNext(); }, 10000);
    }
  }
  function hideNext() {
    clearTimeout(nextTimer);
    if (S) S.nextShown = false;
    $('#p-next').hidden = true;
    $('#p-next').classList.remove('counting');
  }
  function playNext() {
    if (!S || !S.info || !S.info.next) return;
    const id = S.info.next.id;
    save(true);
    hideNext();
    pending = { t0: performance.now(), quality: S.quality, pushed: S.pushed };
    location.replace('#/play/' + encodeURIComponent(id));
  }
  $('#p-next-go').addEventListener('click', playNext);
  $('#p-next-x').addEventListener('click', () => { if (S) S.cancelledNext = true; hideNext(); });

  // ---------------------------------------------------------------- subtitles (rendered here, so they work everywhere)
  let cues = [], cueIdx = 0, subsShown = '';
  async function subs(r) {
    cues = [];
    renderSubs(true);
    if (!r.sub_url) return;
    const me = S;
    let tries = 0;
    while (S === me && S.stream === r) {
      let res;
      try { res = await fetch(r.sub_url, { cache: 'no-store' }); } catch (e) { return; }
      if (res.status === 202) {
        if (tries++ === 0) M.toast('Getting the subtitles ready…');
        await new Promise((ok) => setTimeout(ok, 1500));
        if (tries > 160) return;
        continue;
      }
      if (!res.ok) { M.toast('Those subtitles couldn’t be read'); return; }
      const text = await res.text();
      if (S !== me) return;
      cues = parseVtt(text);
      cueIdx = 0;
      if (isIOS) {  // the iPhone's own full-screen player shows these
        const tr = document.createElement('track');
        tr.kind = 'subtitles';
        tr.src = r.sub_url;
        tr.default = false;
        v.appendChild(tr);
      }
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
    out.sort((x, y) => x.s - y.s);
    return out;
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

  // ---------------------------------------------------------------- menus
  function menu(title, items, onPick) {
    const w = $('#p-sheet');
    $('#p-sheet-title').textContent = title;
    $('#p-sheet-body').innerHTML = items.map((x, i) => `<button class="opt" data-i="${i}" aria-checked="${!!x.on}"><span class="ck">${x.on ? '<svg width="20" height="20"><use href="#i-check"/></svg>' : ''}</span><span>${esc(x.label)}${x.sub ? `<small>${esc(x.sub)}</small>` : ''}</span></button>`).join('');
    w.hidden = false;
    w.classList.remove('closing');
    showUI(true);
    w.onclick = (e) => {
      if (e.target.closest('[data-close]')) return closeMenu();
      const b = e.target.closest('.opt');
      if (!b) return;
      M.buzz(6);
      closeMenu();
      onPick(items[+b.dataset.i]);
    };
  }
  function closeMenu() {
    const w = $('#p-sheet');
    if (w.hidden) return;
    w.classList.add('closing');
    setTimeout(() => { w.hidden = true; w.classList.remove('closing'); hideUISoon(); }, 260);
  }
  $$('[data-menu]', el).forEach((b) => b.addEventListener('click', () => openMenu(b.dataset.menu)));
  $('#p-gear').addEventListener('click', () => {
    menu('Settings', [{ k: 'quality', label: 'Quality', sub: qualityLabel() }, { k: 'subs', label: 'Captions', sub: subLabel() },
      { k: 'audio', label: 'Audio', sub: audioLabel() }, { k: 'speed', label: 'Playback speed', sub: v.playbackRate === 1 ? 'Normal' : v.playbackRate + '×' },
      { k: 'stats', label: 'Stream details', sub: M.prefs.stats ? 'On' : 'Off' }], (x) => {
      if (x.k === 'stats') { M.prefs.stats = !M.prefs.stats; M.savePrefs(); stats(); return; }
      setTimeout(() => openMenu(x.k), 280);
    });
  });
  const qualityLabel = () => {
    if (!S || !S.stream) return '';
    const q = S.override || S.quality;
    const now = S.stream.mode === 'transcode' ? `${S.stream.label}` : 'Original';
    return q === 'auto' ? `Auto (${now})` : now;
  };
  const subLabel = () => { const s = S && S.info && S.info.subs.find((x) => x.id === S.sub); return s ? s.label : 'Off'; };
  const audioLabel = () => { const a = S && S.info && S.info.audio.find((x) => x.index === (S.stream ? S.stream.audio : S.audio)); return a ? a.label : ''; };
  function openMenu(kind) {
    if (!S || !S.info) return;
    const info = S.info;
    if (kind === 'subs') {
      menu('Captions', [{ id: null, label: 'Off', on: !S.sub }, ...info.subs.map((s) => ({ id: s.id, label: s.label, sub: s.kind === 'image' ? 'Picture subtitles: burned into the video' : '', on: s.id === S.sub }))], (x) => {
        const was = info.subs.find((s) => s.id === S.sub);
        const now = info.subs.find((s) => s.id === x.id);
        S.sub = x.id;
        if ((was && was.kind === 'image') || (now && now.kind === 'image')) { startStream(v.currentTime); return; }
        if (!now) { cues = []; renderSubs(true); $$('track', v).forEach((t) => t.remove()); return; }
        subs({ ...S.stream, sub_url: S.stream.base + `sub/${now.id}.vtt` });
        S.stream = { ...S.stream, sub_url: S.stream.base + `sub/${now.id}.vtt` };
      });
    } else if (kind === 'audio') {
      if (info.audio.length < 2) { M.toast(info.audio.length ? `Audio: ${info.audio[0].label}` : 'No audio track'); return; }
      const cur = S.stream ? S.stream.audio : S.audio;
      menu('Audio', info.audio.map((a) => ({ index: a.index, label: a.label, on: a.index === cur })), (x) => {
        if (x.index === cur) return;
        S.audio = x.index;
        startStream(v.currentTime);
      });
    } else if (kind === 'speed') {
      menu('Playback speed', SPEEDS.map((s) => ({ s, label: s === 1 ? 'Normal' : s + '×', on: v.playbackRate === s })), (x) => { v.playbackRate = x.s; });
    } else if (kind === 'quality') {
      const cur = S.override || S.quality;
      const list = (M.me && M.me.qualities) || [{ key: 'auto', label: 'Auto' }];
      menu('Quality', list.map((q) => ({ key: q.key, label: q.key === 'auto' ? `Auto${S.quality === 'auto' && S.stream ? ` (${S.stream.mode === 'transcode' ? S.stream.label : 'Original'})` : ''}` : q.label, on: q.key === cur })), (x) => {
        S.quality = x.key;
        S.override = null;
        S.force = null;
        startStream(v.currentTime);
      });
    } else if (kind === 'stats') {
      M.prefs.stats = !M.prefs.stats; M.savePrefs(); stats();
    }
  }

  // ---------------------------------------------------------------- picture-in-picture / full screen
  const pipOK = (document.pictureInPictureEnabled && !v.disablePictureInPicture) || typeof v.webkitSetPresentationMode === 'function';
  $('#p-pip').hidden = !pipOK;
  $('#p-pip').addEventListener('click', async () => {
    try {
      if (document.pictureInPictureElement) await document.exitPictureInPicture();
      else if (v.requestPictureInPicture) await v.requestPictureInPicture();
      else v.webkitSetPresentationMode(v.webkitPresentationMode === 'picture-in-picture' ? 'inline' : 'picture-in-picture');
    } catch (e) { M.toast('Picture-in-picture isn’t available here'); }
  });
  $('#p-full').addEventListener('click', async () => {
    M.buzz(8);
    if (document.fullscreenElement) { document.exitFullscreen().catch(() => {}); return; }
    if (el.requestFullscreen) {
      try {
        await el.requestFullscreen({ navigationUI: 'hide' });
        if (screen.orientation && screen.orientation.lock && (v.videoWidth || 16) >= (v.videoHeight || 9)) await screen.orientation.lock('landscape').catch(() => {});
      } catch (e) { /* refused */ }
    } else if (v.webkitEnterFullscreen) {
      v.webkitEnterFullscreen();   // iPhone: the system player (it shows the subtitle track too)
    }
  });
  document.addEventListener('fullscreenchange', () => {
    el.classList.toggle('fs', !!document.fullscreenElement);
    if (!document.fullscreenElement) { try { screen.orientation.unlock(); } catch (e) { /* fine */ } }
  });

  // ---------------------------------------------------------------- lock screen / headphones
  function mediaSession(info) {
    if (!('mediaSession' in navigator)) return;
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: info.type === 'Episode' ? info.subtitle : info.title, artist: info.type === 'Episode' ? info.title : (info.subtitle || ''),
        album: 'Marquee', artwork: info.poster ? [{ src: info.poster, sizes: '600x900', type: 'image/jpeg' }] : [],
      });
      const ms = navigator.mediaSession;
      ms.setActionHandler('play', () => v.play());
      ms.setActionHandler('pause', () => v.pause());
      ms.setActionHandler('seekbackward', () => seekBy(-10));
      ms.setActionHandler('seekforward', () => seekBy(10));
      ms.setActionHandler('nexttrack', info.next ? playNext : null);
    } catch (e) { /* partial support */ }
  }

  // ---------------------------------------------------------------- stream details
  let serverStats = null, statsAt = 0;
  async function stats() {
    const box = $('#p-stats');
    if (!M.prefs.stats || !S || !S.stream) { box.hidden = true; return; }
    box.hidden = false;
    if (S.mode !== 'direct' && Date.now() - statsAt > 2000) {
      statsAt = Date.now();
      M.api(`stream/${S.stream.session}/stats`, undefined, { quiet: true }).then((d) => { serverStats = d.stats; }).catch(() => {});
    }
    const q = v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality() : null;
    const st = serverStats || {};
    const lines = [
      `mode     ${S.stream.mode}${S.stream.reason ? ' (' + S.stream.reason + ')' : ''}`,
      `quality  ${S.stream.label}${S.stream.encoder ? ' · ' + S.stream.encoder : ''}${st.pipeline ? ' · ' + st.pipeline : ''}`,
      `video    ${v.videoWidth}x${v.videoHeight}${q ? ` · dropped ${q.droppedVideoFrames}/${q.totalVideoFrames}` : ''}`,
      `buffer   ${bufferedAhead().toFixed(1)} s ahead · player ${M.player.caps && hls ? 'hls.js' : (S.mode === 'direct' ? 'file' : 'native')}`,
      `network  ${bw.bps ? (bw.bps / 1e6).toFixed(1) + ' Mbps' : 'not measured'}${hls && hls.bandwidthEstimate ? ' · hls ' + (hls.bandwidthEstimate / 1e6).toFixed(1) + ' Mbps' : ''}`,
      `start    ${S.ttff ? S.ttff + ' ms to first frame' : '…'}${metrics.seeks.length ? ' · last seek ' + metrics.seeks[metrics.seeks.length - 1].ms + ' ms' : ''}`,
    ];
    if (st.progress && st.running) lines.push(`encoder  ${(st.progress.speed || 0).toFixed(1)}x · ${Math.round(st.progress.fps || 0)} fps${st.suspended ? ' · paused (far enough ahead)' : ''}`);
    box.textContent = lines.join('\n');
  }

  // keyboard (tablets with keyboards, desktop testing)
  document.addEventListener('keydown', (e) => {
    if (!P.isOpen() || e.target.closest('input')) return;
    if (e.key === ' ' || e.key === 'k') { e.preventDefault(); togglePlay(); } else if (e.key === 'ArrowLeft' || e.key === 'j') seekBy(-10);
    else if (e.key === 'ArrowRight' || e.key === 'l') seekBy(10);
    else if (e.key === 'Escape' && !document.fullscreenElement) P.close();
  });
})();
