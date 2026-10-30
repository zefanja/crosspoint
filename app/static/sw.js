// Minimal service worker: app shell cache + offline page + daily reminder (push).
// No offline learning.
const CACHE = "crosspoint-shell-v3";
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
        .catch(() => caches.match(req, { ignoreSearch: true }))  // versioned URLs (?v=)
    );
  }
});

// Daily reminder: payload {title, body, url} from app/reminders.py.
self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { /* plain text */ }
  event.waitUntil(self.registration.showNotification(data.title || "CrossPoint", {
    body: data.body || "",
    icon: "/static/icons/icon-192.png",
    tag: "daily-reminder",
    data: { url: data.url || "/" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data.url || "/", self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      const win = wins.find((w) => w.url.startsWith(self.location.origin));
      return win ? win.focus().then((w) => w.navigate(url)).catch(() => {}) : self.clients.openWindow(url);
    })
  );
});
