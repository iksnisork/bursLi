/**
 * Librus Family Hub — Service Worker
 * Handles local notifications / Web Push alerts and lightweight offline shell caching.
 */
const CACHE_NAME = "bursli-hub-v14";
const SHELL_ASSETS = [
  "/",
  "/static/fonts/option-f-fonts.css?v=14",
  "/static/styles.css?v=14",
  "/static/crypto-vault.js?v=14",
  "/static/app.js?v=14",
  "/static/manifest.json"
];

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS).catch(() => {}))
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            return caches.delete(key);
          }
        })
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/api/")) {
    return;
  }
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});

self.addEventListener("push", (event) => {
  let payload = {
    title: "Librus // Family Hub",
    body: "Nowe wydarzenie w dzienniku Librus.",
    url: "/",
  };
  try {
    if (event.data) {
      payload = Object.assign(payload, event.data.json());
    }
  } catch {
    // fallback to default
  }
  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      data: { url: payload.url || "/" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windowClients) => {
      for (const client of windowClients) {
        if ("focus" in client) {
          return client.focus();
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetUrl);
      }
    })
  );
});
