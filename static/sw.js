// Service worker: щоб архів можна було встановити як застосунок.
//
// Навмисно нічого не кешує зі сторінок, скриптів чи API — все завжди йде
// в мережу, тож після оновлення архіву застосунок одразу показує нове
// (ніяких «застряглих» старих версій). Єдине, що він робить: якщо зв'язку
// немає взагалі — показує зрозумілу сторінку «нема зв'язку» замість
// браузерної помилки.
const VERSION = 'v1';
const OFFLINE_URL = '/offline.html';

self.addEventListener('install', e => {
  e.waitUntil(caches.open(VERSION).then(c => c.add(new Request(OFFLINE_URL, { cache: 'reload' }))));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  if (e.request.mode !== 'navigate') return;          // решта — як без service worker
  e.respondWith(
    fetch(e.request).catch(() => caches.match(OFFLINE_URL))
  );
});
