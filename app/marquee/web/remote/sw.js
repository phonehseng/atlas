/* Marquee Mobile service worker: makes the app installable and opens it instantly. Only the app's own files are
   cached (network first, so updates show at once); the API, artwork and video always come from the PC. */
const CACHE = 'marquee-shell-1';
const SHELL = ['/r', '/r/app.css', '/r/app.js', '/r/player.js', '/r/remote.js', '/r/vendor/hls.min.js',
  '/r/icons/icon-192.png', '/r/icons/icon.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  const path = url.pathname;
  if (!(path === '/r' || path === '/r/' || SHELL.includes(path))) return;  // API, media, artwork: straight through
  e.respondWith(fetch(e.request).then((r) => {
    if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(path === '/r/' ? '/r' : path, copy)); }
    return r;
  }).catch(() => caches.match(path === '/r/' ? '/r' : path)));
});
