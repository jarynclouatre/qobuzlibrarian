const CACHE = 'qobuz-librarian-v2';
const PRECACHE = [
  '/static/dist/app.css',
  '/static/vendor/htmx-1.9.12.min.js',
  '/static/icon.png',
  '/static/icon-192.png',
  '/static/manifest.json',
  '/static/offline.html',
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll(PRECACHE))
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // SSE streams and API calls: always network — never cache dynamic data.
  if (url.pathname.startsWith('/api/')) return;

  // Static assets: cache-first; populate cache on first miss.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(event.request).then(hit => {
        if (hit) return hit;
        return fetch(event.request).then(response => {
          const clone = response.clone();
          caches.open(CACHE).then(cache => cache.put(event.request, clone));
          return response;
        });
      })
    );
    return;
  }

  // Page navigations: network-first; fall back to offline page when the
  // server is unreachable (container stopped, network down).
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request).catch(() => caches.match('/static/offline.html'))
    );
  }
});
