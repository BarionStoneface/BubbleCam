// BubbleCam service worker. Its one job: when the Pi can't be reached (Tailscale off, Pi off, out of
// range), the app still opens, from the last copy, and says what's wrong instead of a browser error.
// Everything is fetched fresh whenever the Pi answers; nothing live (pictures, data, video) is cached.
const CACHE = 'bubblecam-shell-v1';
const SHELL = ['/', '/static/app.js', '/static/style.css', '/manifest.webmanifest', '/static/icon-192.png', '/static/icon.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  const shell = e.request.mode === 'navigate' || url.pathname.startsWith('/static/') || url.pathname === '/manifest.webmanifest';
  if (!shell) return;                                   // API, stream, video: straight to the Pi
  e.respondWith(fetch(e.request).then(r => {
    if (r.ok) {
      const copy = r.clone();
      const key = e.request.mode === 'navigate' ? '/' : url.pathname;
      caches.open(CACHE).then(c => c.put(key, copy));
    }
    return r;
  }).catch(() => caches.match(e.request.mode === 'navigate' ? '/' : url.pathname)
    .then(r => r || new Response('BubbleCam is not reachable right now.', { status: 503, headers: { 'Content-Type': 'text/plain' } }))));
});
