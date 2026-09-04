// Coil service worker: caches the app shell (static assets) and shows /offline when a page cannot be reached.
// Never caches pages, JSON or anything that is not a GET. Bump CACHE when the shell changes.
var CACHE = 'coil-shell-v1';
var SHELL = ['/static/app.css', '/static/app.js', '/static/offline.html', '/static/icons/icon-192.png',
             '/static/icons/icon-512.png', '/manifest.webmanifest'];

self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

function isShellAsset(url) {
  return url.origin === self.location.origin && (url.pathname.indexOf('/static/') === 0 || url.pathname === '/manifest.webmanifest');
}

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.indexOf('/api/') === 0 || url.pathname.indexOf('/webhooks/') === 0) return;

  if (req.mode === 'navigate') {
    // Pages are always fetched live (they carry per-user data). Only the offline fallback is served from cache.
    e.respondWith(fetch(req).catch(function () {
      return caches.match('/static/offline.html').then(function (r) {
        return r || new Response('You are offline.', { status: 503, headers: { 'Content-Type': 'text/plain' } });
      });
    }));
    return;
  }

  if (isShellAsset(url)) {
    e.respondWith(caches.match(req).then(function (hit) {
      var live = fetch(req).then(function (res) {
        if (res && res.ok) caches.open(CACHE).then(function (c) { c.put(req, res.clone()); });
        return res;
      }).catch(function () { return hit; });
      return hit || live;
    }));
  }
});
