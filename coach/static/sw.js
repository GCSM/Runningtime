// Running Coach service worker: makes the app open instantly and show your last plan when offline.
// Static files are cached; pages are network-first (always fresh when online). Never touches POSTs or /api.
const V = "rc-v1", STATIC = V + "-static", PAGES = V + "-pages";
const TABS = ["/", "/plan", "/progress", "/coach", "/me"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(STATIC).then(c => c.addAll(["/static/app.css", "/static/icon-192.png", "/static/manifest.webmanifest"])).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => !k.startsWith(V)).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const r = e.request, u = new URL(r.url);
  if (r.method !== "GET" || u.origin !== location.origin || u.pathname.startsWith("/api") || u.pathname.startsWith("/cal/")) return;
  if (u.pathname.startsWith("/static/")) {
    e.respondWith(caches.match(r).then(hit => {
      const net = fetch(r).then(res => { if (res.ok) { const copy = res.clone(); caches.open(STATIC).then(c => c.put(r, copy)); } return res; }).catch(() => hit);
      return hit || net;
    }));
    return;
  }
  if (r.mode === "navigate" && TABS.includes(u.pathname)) {
    e.respondWith(fetch(r).then(res => {
      if (res.ok && !res.redirected) { const copy = res.clone(); caches.open(PAGES).then(c => c.put(u.pathname, copy)); }
      return res;
    }).catch(() => caches.match(u.pathname, { cacheName: PAGES }).then(hit => hit || new Response("You're offline, and this page hasn't been opened yet.", { status: 503, headers: { "Content-Type": "text/plain" } }))));
  }
});
