const CACHE_NAME = "seed-church-offline-v7";
const APP_SHELL = [
  "/static/offline.html",
  "/static/styles.css",
  "/static/brand.css",
  "/static/offline-sync.js",
  "/static/brand/official-ministry-seal-original.jpeg",
  "/static/brand/sacred-dove-logo.png",
  "/static/brand/app-icon-192.png",
  "/static/brand/app-icon-512.png",
  "/manifest.webmanifest"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  if (event.request.mode === "navigate") {
    event.respondWith(
      fetch(event.request).catch(() => caches.match("/static/offline.html"))
    );
    return;
  }
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith("/static/")) return;
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).then((response) => {
        if (response.ok && new URL(event.request.url).origin === self.location.origin) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        }
        return response;
      });
    })
  );
});

function openSyncDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("seed-church-offline", 1);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function readQueue() {
  return openSyncDb().then((db) =>
    new Promise((resolve, reject) => {
      const request = db.transaction("queue", "readonly").objectStore("queue").getAll();
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    })
  );
}

function splitSyncBatches(operations) {
  const batches = [];
  let current = [];
  for (const operation of operations) {
    const next = current.concat([operation]);
    const tooLarge = new Blob([JSON.stringify({operations: next})]).size > 7 * 1024 * 1024;
    if (current.length && (next.length > 100 || tooLarge)) {
      batches.push(current);
      current = [operation];
    } else {
      current = next;
    }
    if (new Blob([JSON.stringify({operations: current})]).size > 7 * 1024 * 1024) {
      throw new Error("One offline record is too large to sync");
    }
  }
  if (current.length) batches.push(current);
  return batches;
}

async function syncInBackground() {
  const operations = await readQueue();
  if (!operations.length) return;
  operations.sort((a, b) => (a.kind === "member" ? 0 : 1) - (b.kind === "member" ? 0 : 1));
  const batches = splitSyncBatches(operations);
  for (const batch of batches) {
    const response = await fetch("/api/offline/sync", {
      method: "POST",
      credentials: "include",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({operations: batch})
    });
    if (!response.ok) throw new Error("Offline sync failed");
  }
  const clients = await self.clients.matchAll({type: "window", includeUncontrolled: true});
  // Leave operations in IndexedDB until a page processes the member ID map and
  // removes them. Repeating this idempotent request on next launch is safe.
  clients.forEach((client) => client.postMessage({type: "SYNC_NOW"}));
}

self.addEventListener("sync", (event) => {
  if (event.tag === "seed-church-sync") event.waitUntil(syncInBackground());
});