/* Ledger service worker — offline-first for the app shell.
   Your financial data lives in localStorage and never touches this cache. */

const VERSION = 'ledger-v1';
const SHELL = [
  './',
  './index.html',
  './manifest.webmanifest',
  './assets/css/app.css',
  './assets/fonts/schibsted-grotesk.woff2',
  './assets/fonts/azeret-mono.woff2',
  './assets/icon.svg',
  './assets/js/app.js',
  './assets/js/util.js',
  './assets/js/store.js',
  './assets/js/model.js',
  './assets/js/charts.js',
  './assets/js/ui.js',
  './assets/js/flows.js',
  './assets/js/views/home.js',
  './assets/js/views/buckets.js',
  './assets/js/views/goals.js',
  './assets/js/views/transactions.js',
  './assets/js/views/investments.js',
  './assets/js/views/history.js',
  './assets/js/views/settings.js',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(VERSION)
      .then((c) => Promise.allSettled(SHELL.map((u) => c.add(u))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (e) => {
  const { request } = e;
  if (request.method !== 'GET' || new URL(request.url).origin !== location.origin) return;

  // Navigations: network first so updates land, cache as the offline fallback.
  if (request.mode === 'navigate') {
    e.respondWith(
      fetch(request)
        .then((res) => {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(request, copy));
          return res;
        })
        .catch(() => caches.match(request).then((r) => r || caches.match('./index.html'))),
    );
    return;
  }

  // Everything else: cache first, refresh in the background.
  e.respondWith(
    caches.match(request).then((cached) => {
      const network = fetch(request).then((res) => {
        if (res && res.status === 200) {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(request, copy));
        }
        return res;
      }).catch(() => cached);
      return cached || network;
    }),
  );
});
