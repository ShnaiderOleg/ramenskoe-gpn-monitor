const $ = (selector) => document.querySelector(selector);

function urlBase64ToUint8Array(value) {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  return Uint8Array.from([...raw].map((char) => char.charCodeAt(0)));
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("ru-RU");
}

function setMessage(text, isError = false) {
  const node = $("#message");
  node.textContent = text;
  node.classList.toggle("bad", isError);
}

async function currentSubscription() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return null;
  const registration = await navigator.serviceWorker.ready;
  return registration.pushManager.getSubscription();
}

async function refreshSubscriptionButtons() {
  const subscription = await currentSubscription();
  $("#subscribe").textContent = subscription
    ? "Отключить уведомления"
    : "Включить уведомления";
  $("#test").disabled = !subscription;
}

async function toggleSubscription() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    throw new Error("Этот браузер не поддерживает Web Push");
  }
  const registration = await navigator.serviceWorker.ready;
  let subscription = await registration.pushManager.getSubscription();

  if (subscription) {
    await fetch("/api/unsubscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint: subscription.endpoint }),
    });
    await subscription.unsubscribe();
    setMessage("Уведомления отключены");
  } else {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") throw new Error("Разрешение на уведомления не выдано");
    const keyResponse = await fetch("/api/vapid-public-key");
    const { publicKey } = await keyResponse.json();
    subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(publicKey),
    });
    const response = await fetch("/api/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(subscription.toJSON()),
    });
    if (!response.ok) throw new Error("Сервер не сохранил push-подписку");
    setMessage("Уведомления включены");
  }
  await refreshSubscriptionButtons();
  await loadStatus();
}

function renderStations(stations) {
  const root = $("#stations");
  if (!stations.length) {
    root.innerHTML = '<p class="empty">Станции в Раменском пока не найдены.</p>';
    return;
  }
  root.innerHTML = stations.map((station) => `
    <article class="station-card">
      <div class="station-title">
        <div><span class="eyebrow">Газпромнефть</span><h2>${station.name}</h2><p class="station-city">${station.city}</p></div>
        <a href="${station.map_url}" target="_blank" rel="noreferrer">На карте ↗</a>
      </div>
      <div class="fuel-grid">
        ${station.fuels.map((fuel) => `
          <div class="fuel ${fuel.available ? "available" : "missing"}">
            <span class="dot"></span>
            <div><strong>${fuel.label}</strong><small>${fuel.available ? "Есть" : "Нет"}</small></div>
            <b>${fuel.price ? `${fuel.price} ₽` : "—"}</b>
          </div>
        `).join("")}
      </div>
    </article>
  `).join("");
}

function renderEvents(events) {
  const root = $("#event-list");
  if (!events.length) {
    root.innerHTML = '<p class="empty">Событий пока нет.</p>';
    return;
  }
  root.innerHTML = events.map((event) => `
    <div class="event-row">
      <span>${formatDate(event.created_at)}</span>
      <strong>${event.fuel_label}</strong>
      <span>${event.station_name}${event.price ? ` · ${event.price} ₽` : ""}</span>
    </div>
  `).join("");
}

async function loadStatus() {
  const response = await fetch("/api/status", { cache: "no-store" });
  if (!response.ok) throw new Error("Не удалось получить состояние монитора");
  const data = await response.json();
  $("#last-check").textContent = formatDate(data.last_check);
  $("#source-updated").textContent = data.source_updated || "—";
  $("#subscriptions").textContent = data.subscriptions;
  const error = $("#error");
  error.hidden = !data.last_error;
  error.textContent = data.last_error ? `Ошибка последней проверки: ${data.last_error}` : "";
  renderStations(data.stations);
  renderEvents(data.events);
}

async function postAction(url, pendingText) {
  setMessage(pendingText);
  const response = await fetch(url, { method: "POST" });
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error(result.message || "Операция не выполнена");
  return result;
}

async function boot() {
  try {
    if ("serviceWorker" in navigator) await navigator.serviceWorker.register("/sw.js");
    await refreshSubscriptionButtons();
    await loadStatus();
  } catch (error) {
    setMessage(error.message, true);
  }

  $("#subscribe").addEventListener("click", async () => {
    try { await toggleSubscription(); } catch (error) { setMessage(error.message, true); }
  });
  $("#test").addEventListener("click", async () => {
    try {
      const result = await postAction("/api/test-notification", "Отправляю…");
      setMessage(`Тестовый push отправлен: ${result.delivered}`);
    } catch (error) { setMessage(error.message, true); }
  });
  $("#check").addEventListener("click", async () => {
    try {
      await postAction("/api/check-now", "Проверяю источник…");
      setMessage("Проверка завершена");
      await loadStatus();
    } catch (error) { setMessage(error.message, true); }
  });
  setInterval(() => loadStatus().catch(() => {}), 60_000);
}

boot();

