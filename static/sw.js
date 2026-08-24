self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) {}
  event.waitUntil(self.registration.showNotification(data.title || "Монитор бензина", {
    body: data.body || "Обновилось наличие топлива",
    tag: data.tag || "fuel-monitor",
    icon: "/static/icon.svg",
    badge: "/static/icon.svg",
    data: { url: data.url || "/" },
    requireInteraction: true,
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/", self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) {
      if ("focus" in client && client.url === target) return client.focus();
    }
    return clients.openWindow(target);
  })());
});

