/* GHOST como app: el "service worker" que la deja abrir a pantalla completa.
   - Los datos (/api/...) siempre se piden a la Mac: nunca se muestran viejos.
   - La página se pide siempre a la Mac; sin conexión sale la pantalla
     "sin conexión con la Mac".
   - Íconos, letras y librerías se guardan para que abra rápido. */
const CACHE = "ghost-v1";
const OFFLINE = "/static/offline.html";
const PRECACHE = [OFFLINE, "/static/icon-192.png", "/static/ghost.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  const same = url.origin === self.location.origin;
  if (same && (url.pathname.startsWith("/api/") || url.pathname === "/sw.js")) return;   // siempre en vivo
  if (req.mode === "navigate") {           // sin la Mac no hay datos: mejor decirlo claro
    e.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
    return;
  }
  const keep = (same && url.pathname.startsWith("/static/")) ||
    /^(fonts\.googleapis\.com|fonts\.gstatic\.com|unpkg\.com|cdn\.jsdelivr\.net|cdnjs\.cloudflare\.com)$/.test(url.hostname);
  if (!keep) return;
  // lo guardado al instante, y se refresca por detrás
  e.respondWith(caches.open(CACHE).then(async c => {
    const hit = await c.match(req);
    const net = fetch(req).then(res => { if (res.ok || res.type === "opaque") c.put(req, res.clone()); return res; })
      .catch(() => hit);
    return hit || net;
  }));
});
