// Minimal service worker: app shell cache + offline page. No offline learning.
const CACHE = "crosspoint-shell-v2";
const SHELL = [
  "/offline",
  "/static/app.css",
  "/static/app.js",
  "/static/htmx.min.js",
  "/static/icons/icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  if (req.mode === "navigate") {
    // Pages always come from the network; offline page as fallback.
    event.respondWith(fetch(req).catch(() => caches.match("/offline")));
  } else if (url.pathname.startsWith("/static/")) {
    // Network first so updates arrive immediately; cache as fallback.
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req))
    );
  }
});
