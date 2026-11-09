/* NaijaBasket service worker — offline-first shell + catalogue caching.
   Strategy:
     * navigations  -> network first, cached shell fallback, offline page last
     * static assets -> stale-while-revalidate
     * /api/catalog  -> network first with cache fallback (browsable offline)
     * anything else (basket, checkout, merchant) -> always network (never stale
       financial or SLA data).                                                      */

const VERSION = "nb-v1.0.0";
const SHELL_CACHE = `${VERSION}-shell`;
const ASSET_CACHE = `${VERSION}-assets`;
const DATA_CACHE = `${VERSION}-data`;

const SHELL_URLS = [
  "/",
  "/catalog",
  "/offline",
  "/static/css/app.css",
  "/static/js/app.js",
  "/static/manifest.webmanifest",
  "/static/icons/icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_URLS).catch(() => undefined))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((key) => !key.startsWith(VERSION)).map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

function isStatic(url) {
  return url.pathname.startsWith("/static/");
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Never cache or serve stale commercial data.
  const neverCache = ["/checkout", "/orders", "/merchant", "/admin", "/auth", "/webhooks"].some(
    (prefix) => url.pathname.startsWith(prefix)
  );
  if (neverCache) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(SHELL_CACHE).then((cache) => cache.put(request, copy));
          return response;
        })
        .catch(() =>
          caches.match(request).then((cached) => cached || caches.match("/offline"))
        )
    );
    return;
  }

  if (url.pathname.startsWith("/api/catalog")) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(DATA_CACHE).then((cache) => cache.put(request, copy));
          return response;
        })
        .catch(() => caches.match(request))
    );
    return;
  }

  if (isStatic(url)) {
    event.respondWith(
      caches.match(request).then((cached) => {
        const network = fetch(request)
          .then((response) => {
            const copy = response.clone();
            caches.open(ASSET_CACHE).then((cache) => cache.put(request, copy));
            return response;
          })
          .catch(() => cached);
        return cached || network;
      })
    );
  }
});
