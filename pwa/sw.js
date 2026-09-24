// Service worker: cachea solo el shell de la PWA. La API (/api/) nunca pasa por la caché.
const CACHE = "airwork-shell-v1";
const SHELL = ["/", "/app.js", "/style.css", "/manifest.webmanifest", "/icon.svg", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", (ev) => {
  ev.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (ev) => {
  ev.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (ev) => {
  const url = new URL(ev.request.url);
  if (ev.request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
  // Red primero: así Cloudflare Access puede redirigir al login y el shell siempre está al día.
  ev.respondWith(fetch(ev.request).then((resp) => {
    if (resp.ok && resp.type === "basic") {
      const copy = resp.clone();
      caches.open(CACHE).then((c) => c.put(ev.request.mode === "navigate" ? "/" : ev.request, copy));
    }
    return resp;
  }).catch(() => caches.match(ev.request.mode === "navigate" ? "/" : ev.request)));
});
