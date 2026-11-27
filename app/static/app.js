"use strict";

// ---------- helpers ----------

const $ = (sel, root = document) => root.querySelector(sel);
const app = $("#app");
const sheet = $("#sheet");
const sheetBody = $("#sheet-body");
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const r0 = (n) => Math.round(n || 0);
const r1 = (n) => Math.round((n || 0) * 10) / 10;

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, { method = "GET", json, form, headers = {} } = {}) {
  const opts = { method, headers: { ...headers }, credentials: "same-origin" };
  if (json !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(json); }
  if (form) opts.body = form;
  let res;
  try { res = await fetch(path, opts); } catch { setNetDown(true); throw new ApiError(0, "Нет связи с сервером"); }
  setNetDown(false);
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) {
    let msg = data?.detail;
    if (Array.isArray(msg)) msg = "Проверь введённые значения";
    throw new ApiError(res.status, msg || `Ошибка ${res.status}`);
  }
  return data;
}

let toastTimer;
function toast(text, action = null) {
  const t = $("#toast");
  t.textContent = text;
  if (action) {
    const b = document.createElement("button");
    b.textContent = action.label;
    b.onclick = () => { t.hidden = true; action.run(); };
    t.append(" ", b);
  }
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), action ? 6000 : 3500);
}

// ---------- offline: a queue for the quick writes, the last good day to read from ----------
// Only water, weight and new meals are queued; recognition, the coach, search and reports need the network.

const QUEUE_MAX = 50;
const userKey = (kind) => `ft-${kind}:${state.me?.username || ""}`;
function readStored(kind, fallback) {
  try { return JSON.parse(localStorage.getItem(userKey(kind)) || "null") ?? fallback; } catch { return fallback; }
}
function store(kind, value) {
  try { localStorage.setItem(userKey(kind), JSON.stringify(value)); return true; } catch { return false; }
}
const readQueue = () => readStored("queue", []);

let netDown = false; // the last request failed for lack of a connection
function setNetDown(v) {
  if (netDown === v) return;
  netDown = v;
  updateNetBanner();
}
const isOffline = () => netDown || navigator.onLine === false;

const newKey = () => (crypto.randomUUID ? crypto.randomUUID()
  : Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join(""));

// A write that may wait for the network. Resolves with the server's answer, or with
// { queued: true } when it went to the queue: the caller then updates the screen itself.
// HTTP errors are thrown as usual: only a missing connection queues.
async function quickWrite(path, { method, json, label }) {
  const entry = { key: newKey(), method, path, json, at: Date.now(), label };
  if (navigator.onLine !== false) {
    try {
      return await api(path, { method, json, headers: { "Idempotency-Key": entry.key } });
    } catch (err) {
      if (err.status !== 0) throw err;
    }
  }
  // The request may have reached the server before the connection dropped: the same key makes the retry harmless.
  const q = readQueue();
  if (q.length >= QUEUE_MAX) throw new ApiError(0, `Нет сети, а ${QUEUE_MAX} записей уже ждут отправки — дождись связи`);
  q.push(entry);
  if (!store("queue", q)) throw new ApiError(0, "Нет сети, и записать в память телефона не вышло");
  toast("Нет сети — запишу, когда появится");
  updateNetBanner();
  return { queued: true };
}

// Strictly in order, one flush at a time. 2xx and 4xx leave the queue (a 4xx would fail
// forever); no connection, 5xx and 401 stop and keep everything for the next try.
let flushing = null;
function flushQueue() {
  if (!flushing) flushing = doFlush().finally(() => { flushing = null; });
  return flushing;
}
async function doFlush() {
  if (!state.me) return;
  let sent = 0;
  for (let q = readQueue(); q.length; q = readQueue()) {
    const e = q[0];
    let res;
    try {
      res = await fetch(e.path, { method: e.method, credentials: "same-origin", body: JSON.stringify(e.json),
        headers: { "Content-Type": "application/json", "Idempotency-Key": e.key } });
    } catch { setNetDown(true); break; }
    setNetDown(false);
    if (res.status === 401) { renderAuth(); break; }
    if (res.status >= 500) break;
    store("queue", readQueue().filter((x) => x.key !== e.key));
    if (res.ok) sent++; else toast(`Не удалось записать: ${e.label}`);
  }
  updateNetBanner();
  if (!sent) return;
  if (state.view === "day") loadDay();
  else if (state.view === "reports") loadStats();
}
window.addEventListener("online", () => { updateNetBanner(); flushQueue(); });
window.addEventListener("offline", updateNetBanner);

// The day as the server last showed it, with what still waits in the queue on top.
const localNow = () => { const d = new Date(); return `${isoDate(d)}T${d.toTimeString().slice(0, 8)}`; };
function dayWithQueue(day) {
  const d = structuredClone(day);
  for (const e of readQueue()) {
    const onDay = isoDate(new Date(e.at)) === d.date;
    if (e.path === "/api/water" && onDay && d.water) {
      d.water.ml = Math.max(0, Math.min(10000, d.water.ml + e.json.delta_ml));
    } else if (e.path === "/api/weight" && onDay) {
      d.weight = d.weight ? { ...d.weight, today: e.json.kg } : { today: e.json.kg, last: { kg: e.json.kg }, trend: e.json.kg, kg_per_week: null };
    } else if (e.path === "/api/meals" && e.json.eaten_at?.startsWith(d.date)) {
      const items = e.json.items.map((i) => ({ ...i }));
      d.meals.push({ id: `q:${e.key}`, queued: true, title: e.json.title, eaten_at: e.json.eaten_at, photo: e.json.photo ?? null, items, totals: totalsOf(items) });
    }
  }
  d.meals.sort((a, b) => new Date(a.eaten_at) - new Date(b.eaten_at));
  const all = d.meals.flatMap((m) => m.items);
  const extra = (k) => (all.some((i) => i[`${k}_100`] != null) ? all.reduce((s, i) => s + ((i[`${k}_100`] || 0) * i.grams) / 100, 0) : null);
  d.totals = { ...totalsOf(all), fiber: extra("fiber"), sugar: extra("sugar"), salt: extra("salt") };
  return d;
}

function netBannerHtml() {
  const n = readQueue().length;
  if (!isOffline() && !n) return "";
  const waits = n ? `${n} ${plural(n, "запись", "записи", "записей")} ${n % 10 === 1 && n % 100 !== 11 ? "ждёт" : "ждут"} отправки` : "";
  return `<div class="net-banner with-ico" role="status">${ICONS.cloudOff}<span>${[isOffline() ? "Нет сети" : "", waits].filter(Boolean).join(" · ")}</span></div>`;
}
function updateNetBanner() {
  const slot = $("#net-slot");
  if (slot) slot.innerHTML = netBannerHtml();
}

function openSheet(html) {
  stopVoice(true); // the textarea it was typing into is about to go
  sheetBody.innerHTML = html;
  sheet.hidden = false;
  document.documentElement.classList.add("sheet-open");
  sheetBody.scrollTop = 0;
}
function closeSheet() {
  stopScan();
  stopVoice(true);
  if (draft && !draft.id && draft.items.length) {
    stashDraft();
    toast("Черновик сохранён — вернёшь его через «+»");
  }
  sheet.hidden = true;
  document.documentElement.classList.remove("sheet-open");
  sheetBody.innerHTML = "";
  draft = null;
  dish = null;
}

// An unsaved new meal survives closing the sheet and the app itself: recognition costs money.
const draftKey = () => `ft-draft:${state.me?.username || ""}`;
function stashDraft() {
  if (!draft || draft.id || !draft.items.length) return;
  try { localStorage.setItem(draftKey(), JSON.stringify({ at: Date.now(), day: state.day, draft })); } catch { /* storage full or off */ }
}
function stashedDraft() {
  try {
    const v = JSON.parse(localStorage.getItem(draftKey()) || "null");
    return v && Date.now() - v.at < 24 * 3600e3 ? v : null;
  } catch { return null; }
}
const dropStash = () => { try { localStorage.removeItem(draftKey()); } catch { /* ignore */ } };
sheet.addEventListener("click", (e) => { if (e.target === sheet) closeSheet(); });

function totalsOf(items) {
  const t = { kcal: 0, protein: 0, fat: 0, carbs: 0 };
  for (const i of items) {
    const k = (Number(i.grams) || 0) / 100;
    t.kcal += i.kcal_100 * k; t.protein += i.protein_100 * k; t.fat += i.fat_100 * k; t.carbs += i.carbs_100 * k;
  }
  return t;
}

const isoDate = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
function dayLabel(iso) {
  const today = isoDate(new Date());
  const y = new Date(); y.setDate(y.getDate() - 1);
  if (iso === today) return "Сегодня";
  if (iso === isoDate(y)) return "Вчера";
  return new Date(iso + "T12:00").toLocaleDateString("ru-RU", { day: "numeric", month: "long", weekday: "short" });
}

// Shrink on the phone: a 4 MB photo becomes ~200 KB before upload.
async function shrinkPhoto(file, maxEdge = 1280) {
  try {
    const bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
    const scale = Math.min(1, maxEdge / Math.max(bmp.width, bmp.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bmp.width * scale);
    canvas.height = Math.round(bmp.height * scale);
    canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
    bmp.close?.();
    return await new Promise((ok) => canvas.toBlob((b) => ok(b || file), "image/jpeg", 0.85));
  } catch {
    return file; // the server shrinks it anyway
  }
}

// ---------- state ----------

const state = { me: null, view: "day", day: isoDate(new Date()), dayData: null, statsDays: 7, stats: null, selected: null, review: null, reviewBusy: false, reviewError: "" };

// ---------- auth ----------

function inviteFromUrl() {
  const m = location.hash.match(/invite=([\w-]+)/);
  return m ? m[1] : null;
}

function renderAuth(error = "") {
  const invite = inviteFromUrl();
  $("#tabbar").hidden = true;
  app.innerHTML = `
    <div class="brand"><div class="brand-ico">${ICONS.leaf}</div><h1>${invite ? "Регистрация" : "Вход"}</h1><p class="muted small">Калории и БЖУ по фото еды</p></div>
    <form class="card" id="auth-form">
      <label>Логин</label>
      <input name="username" autocomplete="username" autocapitalize="none" required>
      <label>Пароль${invite ? " (не короче 8 символов)" : ""}</label>
      <input name="password" type="password" autocomplete="${invite ? "new-password" : "current-password"}" required>
      <div class="error">${esc(error)}</div>
      <button class="primary block" style="margin-top:14px">${invite ? "Создать аккаунт" : "Войти"}</button>
    </form>
    ${invite ? "" : '<p class="muted small center">Аккаунт создаётся по ссылке-приглашению.</p>'}`;
  $("#auth-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = { username: f.get("username"), password: f.get("password"), timezone: TZ };
    try {
      state.me = invite
        ? await api("/api/auth/register", { method: "POST", json: { ...body, invite } })
        : await api("/api/auth/login", { method: "POST", json: body });
      history.replaceState(null, "", "/");
      try { localStorage.setItem("ft-me", JSON.stringify(state.me)); } catch { /* ignore */ }
      flushQueue(); // this user's writes queued before a sign-out
      if (!state.me.onboarded) renderOnboarding(1); else await loadDay();
    } catch (err) {
      renderAuth(err.message);
    }
  });
}

// ---------- day view ----------

async function loadDay() {
  leaveProfileHash();
  showTabs("day");
  showSkeleton("day");
  state.view = "day";
  const today = isoDate(new Date());
  try {
    const [dayData, streaks] = await Promise.all([api(`/api/day?date=${state.day}`), api("/api/streaks").catch(() => null)]);
    if (dayData.date === today) store("day", dayData);
    state.dayData = dayData.date === today ? dayWithQueue(dayData) : dayData;
    state.streaks = streaks;
    renderDay();
    celebrate(dayData.streaks);
  } catch (err) {
    if (err.status === 401) return renderAuth();
    const cached = err.status === 0 && state.day === today ? readStored("day", null) : null;
    if (cached?.date === today) {
      state.dayData = dayWithQueue(cached);
      renderDay();
      return;
    }
    app.innerHTML = `<div id="net-slot">${netBannerHtml()}</div><div class="card error">${esc(err.message)}</div>`;
  }
}

function bar(value, goal, color = "var(--accent)", warnOver = true) {
  const pct = goal > 0 ? Math.min(100, (value / goal) * 100) : 0;
  return `<div class="bar ${warnOver && isOver(value, goal) ? "over" : ""}" style="--c:${color}"><i style="width:${pct}%"></i></div>`;
}

// Stroke icons, 24x24, drawn in currentColor: emoji look different on every platform.
const svgIcon = (d) => `<svg class="ic" viewBox="0 0 24 24" aria-hidden="true">${d}</svg>`;
const ICONS = Object.fromEntries(Object.entries({
  diary: '<path d="M6 3.5h10.5A2.5 2.5 0 0 1 19 6v14.5H7.5A2.5 2.5 0 0 1 5 18V4.5a1 1 0 0 1 1-1z"/><path d="M5 18a2.5 2.5 0 0 1 2.5-2.5H19M9 8h6"/>',
  chart: '<path d="M4 20h16M7 16v-5M12 16V6M17 16v-8"/>',
  settings: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  left: '<path d="M14.5 6l-6 6 6 6"/>',
  right: '<path d="M9.5 6l6 6-6 6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  close: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
  up: '<path d="M12 19V5.5M6.5 11L12 5.5l5.5 5.5"/>',
  undo: '<path d="M9 6.5L5 10.5l4 4"/><path d="M5 10.5h9.5a4.5 4.5 0 0 1 0 9H12"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  clock: '<circle cx="12" cy="12" r="8"/><path d="M12 7.5V12l3 2"/>',
  user: '<circle cx="12" cy="8.5" r="3.5"/><path d="M5 20c1.2-3.6 4-5.5 7-5.5s5.8 1.9 7 5.5"/>',
  chat: '<path d="M4.5 6.5A2.5 2.5 0 0 1 7 4h10a2.5 2.5 0 0 1 2.5 2.5v7A2.5 2.5 0 0 1 17 16h-6l-4.5 3.5V16H7a2.5 2.5 0 0 1-2.5-2.5z"/><path d="M8.5 9h7M8.5 12h4"/>',
  leaf: '<path d="M5 19c0-8 5-13.5 14-14 .2 9-5.5 14-14 14z"/><path d="M5 19l7.5-7.5"/>',
  flame: '<path d="M12 21c-3.9 0-6.5-2.6-6.5-6.2 0-2.9 1.8-5 3.4-6.6.3 1.9 1.2 3 2.3 3.4C11 8.6 12.4 5.4 15 3c.2 3 1.6 4.6 2.7 6.2 1 1.5 1.8 3 1.8 5 0 3.8-3.2 6.8-7.5 6.8z"/>',
  steps: '<path d="M8 3.5c1.8 0 2.8 2 2.8 4.6 0 2.2-.8 3.9-1.2 5.4H6.4C6 12 5.2 10.3 5.2 8.1 5.2 5.5 6.2 3.5 8 3.5zM6.4 16.5h3.2v1a1.6 1.6 0 0 1-3.2 0z"/><path d="M16 7.5c-1.8 0-2.8 2-2.8 4.6 0 2.2.8 3.9 1.2 5.4h3.2c.4-1.5 1.2-3.2 1.2-5.4 0-2.6-1-4.6-2.8-4.6zM14.4 19.5h3.2v.5a1.6 1.6 0 0 1-3.2 0z"/>',
  scale: '<rect x="4" y="4" width="16" height="16" rx="4"/><path d="M8.5 10.5a5 5 0 0 1 7 0M12 11l1.5-2"/>',
  drop: '<path d="M12 3.5c3 3.6 6 7 6 10.5a6 6 0 0 1-12 0c0-3.5 3-6.9 6-10.5z"/>',
  camera: '<path d="M4 8.5a2 2 0 0 1 2-2h2l1.5-2h5l1.5 2h2a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><circle cx="12" cy="13" r="3.5"/>',
  image: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><circle cx="9" cy="9.5" r="1.5"/><path d="M20.5 15.5L16 11l-9.5 8.5"/>',
  barcode: '<path d="M4 7V5.5A1.5 1.5 0 0 1 5.5 4H7M17 4h1.5A1.5 1.5 0 0 1 20 5.5V7M20 17v1.5a1.5 1.5 0 0 1-1.5 1.5H17M7 20H5.5A1.5 1.5 0 0 1 4 18.5V17M8 8v8M11 8v8M14 8v8M16.5 8v8"/>',
  pencil: '<path d="M15.5 4.5l4 4L9 19H5v-4z"/><path d="M13.5 6.5l4 4"/>',
  sparkle: '<path d="M11 3.5l1.8 5 5 1.8-5 1.8-1.8 5-1.8-5-5-1.8 5-1.8z"/><path d="M18 15.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z"/>',
  search: '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/>',
  bulb: '<path d="M9.5 17.5h5M10.5 20.5h3M12 3.5a6 6 0 0 0-3.5 10.9c.5.4.5.9.5 1.6v1h5v-1c0-.7 0-1.2.5-1.6A6 6 0 0 0 12 3.5z"/>',
  target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><path d="M12 12h.01"/>',
  dumbbell: '<path d="M6.5 7.5v9M17.5 7.5v9M3.5 10v4M20.5 10v4M6.5 12h11"/>',
  snow: '<path d="M12 3v18M4.2 7.5l15.6 9M4.2 16.5l15.6-9M9.5 4.5L12 6l2.5-1.5M9.5 19.5L12 18l2.5 1.5"/>',
  flag: '<path d="M5.5 21V4M5.5 4.5h12l-2.5 3.5 2.5 3.5h-12"/>',
  trash: '<path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l.8 12a1.5 1.5 0 0 0 1.5 1.4h6.4a1.5 1.5 0 0 0 1.5-1.4l.8-12M10 11v5.5M14 11v5.5"/>',
  bell: '<path d="M6.5 16.5V11a5.5 5.5 0 0 1 11 0v5.5l1.5 2h-14z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
  heart: '<path d="M12 19.5s-7.5-4.4-7.5-9.7A4.3 4.3 0 0 1 12 7.2a4.3 4.3 0 0 1 7.5 2.6c0 5.3-7.5 9.7-7.5 9.7z"/>',
  shield: '<path d="M12 3.5l7 2.5v5.5c0 4.5-3 7.8-7 9-4-1.2-7-4.5-7-9V6z"/><path d="M9 12l2 2 4-4"/>',
  logout: '<path d="M14 4.5H7A1.5 1.5 0 0 0 5.5 6v12A1.5 1.5 0 0 0 7 19.5h7M10.5 12h9M16.5 8.5l3.5 3.5-3.5 3.5"/>',
  replay: '<path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3L4.5 9"/><path d="M4.5 4.5V9H9"/>',
  download: '<path d="M12 4v11M7.5 10.5L12 15l4.5-4.5M5 19.5h14"/>',
  mic: '<rect x="9" y="3.5" width="6" height="11" rx="3"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v2.5"/>',
  pot: '<path d="M4.5 10h15v6.5a3 3 0 0 1-3 3h-9a3 3 0 0 1-3-3z"/><path d="M2.5 10h19M9 6.5c0-1 .8-1.5.8-2.5M14 6.5c0-1 .8-1.5.8-2.5"/>',
  // Parts of the day for meals without a photo, and the observations from /api/insights.
  cloudOff: '<path d="M7.5 18.5h9.5a3.5 3.5 0 0 0 1-6.86A6 6 0 0 0 7.6 9.1 4.75 4.75 0 0 0 7.5 18.5z"/><path d="M4 4l16 16"/>',
  cup: '<path d="M5 9h11v4.5a5 5 0 0 1-5 5h-1a5 5 0 0 1-5-5z"/><path d="M16 10.5h1.5a2.5 2.5 0 0 1 0 5H16M8.5 3.5v2.5M12 3.5v2.5"/>',
  bowl: '<path d="M4 11h16a8 8 0 0 1-16 0z"/><path d="M9.5 21h5M9 7.5c0-1.5 1-1.5 1-3M13.5 7.5c0-1.5 1-1.5 1-3"/>',
  plate: '<circle cx="12" cy="12" r="4.8"/><path d="M3.5 4v4.5a1.5 1.5 0 0 0 3 0V4M5 4v16M20.5 4c-1.2 1-1.8 3-1.8 5.5h1.8V20"/>',
  apple: '<path d="M12 7.5c-1.5-1-3-1.2-4.5-.8C5 7.4 4 10 4.5 13c.6 3.8 3 7 5.2 7 .9 0 1.4-.4 2.3-.4s1.4.4 2.3.4c2.2 0 4.6-3.2 5.2-7 .5-3-.5-5.6-3-6.3-1.5-.4-3-.2-4.5.8z"/><path d="M12 7.5c0-2 1-3.5 3-4"/>',
  moon: '<path d="M19.5 14.5A7.5 7.5 0 0 1 9.5 4.5a7.5 7.5 0 1 0 10 10z"/>',
  egg: '<path d="M12 3.5c3.3 0 6 5 6 9.5a6 6 0 0 1-12 0c0-4.5 2.7-9.5 6-9.5z"/>',
  wheat: '<path d="M12 21V8"/><path d="M12 12.5c-2.6 0-4-1.6-4-4 2.6 0 4 1.6 4 4zM12 12.5c2.6 0 4-1.6 4-4-2.6 0-4 1.6-4 4zM12 17c-2.6 0-4-1.6-4-4 2.6 0 4 1.6 4 4zM12 17c2.6 0 4-1.6 4-4-2.6 0-4 1.6-4 4zM12 8c-1.2-1-1.2-3.2 0-4.5 1.2 1.3 1.2 3.5 0 4.5z"/>',
  party: '<path d="M4 20l4.5-11 6.5 6.5z"/><path d="M13.5 4.5c.6 1.2.4 2.5-.5 3.5M19.5 10.5c-1.2-.6-2.5-.4-3.5.5M17 3.5h.01M20.5 7h.01M10.5 3.5h.01M20.5 14h.01"/>',
}).map(([k, d]) => [k, svgIcon(d)]));
// A server-sent icon key may be unknown to an old cached shell: draw nothing rather than break.
const iconOf = (key) => (Object.hasOwn(ICONS, key) ? ICONS[key] : "");

// Within 5% of the goal is still "in goal": a few kcal over shouldn't paint the day as a failure.
const OVER_TOLERANCE = 1.05;
const isOver = (value, goal) => goal > 0 && value > goal * OVER_TOLERANCE;

// Rings start from what they showed last time (by key), so a re-render animates the change
// instead of appearing at the final value.
const ringShown = new Map();
function ring(key, value, goal, color, size, width, inner = "", warnOver = false) {
  const r = (size - width) / 2, c = 2 * Math.PI * r, mid = size / 2;
  const frac = goal > 0 ? Math.min(1, value / goal) : 0;
  const over = warnOver && isOver(value, goal);
  const to = `${(c * frac).toFixed(1)} ${c.toFixed(1)}`;
  const from = ringShown.get(key) ?? `0 ${c.toFixed(1)}`;
  ringShown.set(key, to);
  return `<div class="ring" style="width:${size}px;height:${size}px">
    <svg viewBox="0 0 ${size} ${size}" aria-hidden="true">
      <circle class="track" cx="${mid}" cy="${mid}" r="${r}" stroke-width="${width}"/>
      <circle class="val ${over ? "over" : ""}" cx="${mid}" cy="${mid}" r="${r}" stroke-width="${width}" stroke="${color}"
        stroke-linecap="${frac > 0 ? "round" : "butt"}" style="stroke-dasharray:${from}" data-to="${to}" transform="rotate(-90 ${mid} ${mid})"/>
    </svg>
    <div class="ring-in">${inner}</div>
  </div>`;
}

function animateRings(root = app) {
  const circles = [...root.querySelectorAll("circle[data-to]")];
  circles.forEach((el) => getComputedStyle(el).strokeDasharray); // commit the start value first
  requestAnimationFrame(() => circles.forEach((el) => (el.style.strokeDasharray = el.dataset.to)));
}

function topbar(title, subtitle, buttons, back = "") {
  return `<header class="top">
    ${back ? `<button class="icon back" data-act="${back}" aria-label="Назад">${ICONS.left}</button>` : ""}
    <div class="grow"><h1>${esc(title)}</h1><div class="muted small">${esc(subtitle)}</div></div>
    <div class="row tight">${buttons}</div>
  </header>`;
}

// Grey blocks shaped like the screen that is loading, instead of a "Loading…" line.
function skeleton(kind) {
  const b = (h, extra = "") => `<div class="skeleton" style="height:${h}px${extra}"></div>`;
  const head = `<div class="sk-head">${b(30, ";width:46%")}${b(14, ";width:30%;margin-top:8px")}</div>`;
  const body = {
    day: `${b(250, ";border-radius:var(--radius)")}<div class="sk-row">${b(72)}${b(72)}${b(72)}</div>${b(16, ";width:36%;margin:22px 4px 10px")}${b(76)}${b(76)}`,
    reports: `${b(40)}<div class="sk-row">${b(84)}${b(84)}${b(84)}</div>${b(220, ";border-radius:var(--radius)")}${b(180, ";border-radius:var(--radius)")}`,
    coach: `<div class="sk-chat">${b(64, ";width:78%")}${b(44, ";width:55%;align-self:flex-end")}${b(88, ";width:82%")}</div>`,
    profile: `${b(300, ";border-radius:var(--radius)")}${b(56, ";border-radius:var(--radius);margin-top:12px")}`,
  }[kind] || "";
  return `<div class="sk" aria-busy="true" aria-label="Загрузка">${head}${body}</div>`;
}
// Placeholders only when arriving from another screen: a refresh of the same one keeps its content.
function showSkeleton(view) {
  if (state.view === view && app.querySelector(".top")) return;
  app.innerHTML = skeleton(view);
}

const tabbar = $("#tabbar");
function showTabs(active) {
  stopVoice(true);
  const tab = (id, icon, label) => `<button data-tab="${id}" class="${active === id ? "on" : ""}">${icon}<span>${label}</span></button>`;
  tabbar.innerHTML = `
    ${tab("day", ICONS.diary, "Дневник")}
    ${tab("reports", ICONS.chart, "Отчёты")}
    <button data-tab="add" class="add" aria-label="Добавить еду">${ICONS.plus}</button>
    ${tab("coach", ICONS.chat, "Коуч")}
    ${tab("profile", ICONS.user, "Профиль")}`;
  tabbar.hidden = false;
}
tabbar.addEventListener("click", (e) => {
  const tab = e.target.closest("[data-tab]")?.dataset.tab;
  if (tab === "add") openAdd();
  else if (tab === "day") { state.day = isoDate(new Date()); loadDay(); }
  else if (tab === "reports") loadStats();
  else if (tab === "coach") loadCoach();
  else if (tab === "profile") loadProfileView();
});

// Re-render whatever screen is open after something changed underneath it.
function refreshView() {
  if (state.view === "reports") return loadStats();
  if (state.view === "profile") return loadProfileView(state.profileSection);
  if (state.view === "onboarding") return renderOnboarding(state.onboardingStep + 1);
  return loadDay();
}

function renderDay() {
  const { meals, totals: t, goals: g } = state.dayData;
  const isToday = state.day === isoDate(new Date());
  const left = g.kcal - t.kcal;
  const d = new Date(state.day + "T12:00");
  const label = dayLabel(state.day);
  const sameWeekday = label !== "Сегодня" && label !== "Вчера";
  showTabs("day");
  openMeal = null;
  app.innerHTML = `
    <div id="net-slot">${netBannerHtml()}</div>
    ${topbar(sameWeekday ? d.toLocaleDateString("ru-RU", { day: "numeric", month: "long" }) : label,
      d.toLocaleDateString("ru-RU", sameWeekday ? { weekday: "long" } : { weekday: "long", day: "numeric", month: "long" }),
      `<button class="icon" data-act="prev" aria-label="Предыдущий день">${ICONS.left}</button>
       <button class="icon" data-act="next" aria-label="Следующий день" ${isToday ? "disabled" : ""}>${ICONS.right}</button>`)}
    <div class="card summary">
      <div class="sum-main">
        ${ring("kcal", t.kcal, g.kcal, "var(--accent)", 136, 13,
          `<b>${r0(Math.abs(left))}</b><span class="muted">${left >= 0 ? "осталось" : "перебор"}</span>`, true)}
        <div class="sum-stats">
          <div><span class="muted small">Съедено</span><b>${r0(t.kcal)} <small>ккал</small></b></div>
          <div><span class="muted small">Норма</span><b>${g.kcal} <small>ккал</small></b></div>
        </div>
      </div>
      <div class="macros">
        ${[["p", "Белки", t.protein, g.protein, "var(--protein)"], ["f", "Жиры", t.fat, g.fat, "var(--fat)"], ["c", "Углеводы", t.carbs, g.carbs, "var(--carbs)"]]
          .map(([k, n, v, goal, c]) => `<div class="macro">${ring(k, v, goal, c, 62, 7, `<b>${r0(v)}</b>`)}
            <span>${n}</span><span class="muted small">из ${goal} г</span></div>`).join("")}
      </div>
      ${nutrientLine(t, state.dayData.refs)}
      ${activityLine(state.dayData.activity)}
      ${workoutLines(state.dayData.workouts || [])}
    </div>
    ${isToday ? quickTiles(state.dayData) : ""}
    ${meals.length
      ? `<h3 class="sub">Приёмы пищи</h3>${meals.map(mealCard).join("")}`
      : `<div class="empty"><div class="empty-ico">${ICONS.camera}</div><b>Пока пусто</b>
          <p>Нажми «+» внизу и сфотографируй еду — посчитаю калории и БЖУ.</p></div>`}
    ${isToday ? suggestRow(state.dayData.suggest, g.kcal - t.kcal) : ""}
    ${isToday ? streaksRow(state.streaks, { others: true }) : ""}`;
  animateRings();
}

const fmtInt = (n) => r0(n).toLocaleString("ru-RU");

const kgFmt = (n) => String(r1(n)).replace(".", ",");

// One row of small tiles: weight, water, streak. Water drops out when it isn't tracked.
function quickTiles(day) {
  const tiles = [weightTile(day.weight), waterTile(day.water), streakTile(day.streaks)].filter(Boolean);
  return `<div class="qtiles" style="--n:${tiles.length}">${tiles.join("")}</div>`;
}

const tile = (act, icon, value, caption, { tint = "", label = "", extra = "", on = false } = {}) => `
  <div class="qt ${tint} ${on ? "on" : ""}">
    <button class="qt-main" data-act="${act}" ${label ? `aria-label="${esc(label)}"` : ""}>
      <span class="qt-top"><span class="qt-ico">${icon}</span><b>${value}</b></span>
      ${caption}
    </button>${extra}
  </div>`;

const paceText = (w) => w.kg_per_week == null ? "" : `${w.kg_per_week < 0 ? "↓" : w.kg_per_week > 0 ? "↑" : "→"}${kgFmt(Math.abs(w.kg_per_week))}/нед`;

function weightTile(w) {
  if (!w) return tile("weigh", ICONS.scale, "Вес", '<span class="qt-cap">взвеситься</span>', { label: "Записать вес" });
  const kg = w.today ?? w.last.kg;
  return tile("weigh", ICONS.scale, `${kgFmt(kg)} кг`, `<span class="qt-cap">${paceText(w) || `тренд ${kgFmt(w.trend)}`}</span>`,
    { label: `Вес ${kgFmt(kg)} кг — записать` });
}

const GLASS_ML = 250;

function waterTile(w) {
  if (!w) return "";
  const pct = w.goal > 0 ? Math.min(100, (100 * w.ml) / w.goal) : 0;
  return tile("water-add", ICONS.drop, `${kgFmt(w.ml / 1000)} / ${kgFmt(w.goal / 1000)} л`,
    `<span class="qt-bar"><i style="width:${pct}%"></i></span>`,
    { tint: "water", label: "Добавить стакан воды", extra: `<button class="qt-more" data-act="water-sheet" aria-label="Вода за сегодня: убрать или добавить стакан">${ICONS.minus}</button>` });
}

function streakTile(st) {
  if (!st) return "";
  const m = st.next_milestone;
  const cap = st.frozen ? "заморозка спасла" : st.days && !st.today ? "ждёт записи" : m && st.days ? `до ${m}: ещё ${m - st.days}` : st.days ? "дней подряд" : "начни серию";
  return tile("streak", ICONS.flame, `${st.days} ${plural(st.days, "день", "дня", "дней")}`, `<span class="qt-cap">${cap}</span>`,
    { tint: "streak", label: `Серия ${st.days} ${plural(st.days, "день", "дня", "дней")} — подробнее`, on: st.today && st.days > 0 });
}

async function addWater(delta) {
  try {
    const r = await quickWrite("/api/water", { method: "POST", json: { delta_ml: delta }, label: `вода ${delta > 0 ? "+" : "−"}${Math.abs(delta)} мл` });
    const w = state.dayData.water;
    state.dayData.water = r.queued ? { ...w, ml: Math.max(0, Math.min(10000, w.ml + delta)) } : r;
    const y = window.scrollY;
    renderDay();
    window.scrollTo(0, y);
    if ($("#water-sheet")) renderWaterSheet();
  } catch (err) { toast(err.message); }
}

// −/+ for the day's water: the tile itself only adds, this is where a glass comes off.
function renderWaterSheet() {
  const w = state.dayData?.water;
  if (!w) return closeSheet();
  const glasses = Math.round(w.ml / GLASS_ML), goal = Math.round(w.goal / GLASS_ML);
  const cells = Array.from({ length: Math.max(goal, glasses) }, (_, i) => `<i class="${i < glasses ? "on" : ""}"></i>`).join("");
  const html = `
    <div class="row between"><h2>Вода сегодня</h2><button class="ghost" data-close aria-label="Закрыть">${ICONS.close}</button></div>
    <div class="card water" id="water-sheet">
      <div class="grow"><div><b>${kgFmt(w.ml / 1000)} л</b> <span class="muted small">из ${kgFmt(w.goal / 1000)} л · стакан ${GLASS_ML} мл</span></div>
        <div class="glasses" aria-hidden="true">${cells}</div></div>
      <button class="icon-btn" data-water="-1" aria-label="Убрать стакан" ${w.ml ? "" : "disabled"}>${ICONS.minus}</button>
      <button class="icon-btn primary" data-water="1" aria-label="Добавить стакан воды">${ICONS.plus}</button>
    </div>
    <p class="muted small">Цель и отключение — в «Профиль → Вода».</p>`;
  if ($("#water-sheet")) sheetBody.innerHTML = html; else openSheet(html);
}

function suggestRow(list, left) {
  if (!list?.length) return "";
  return `<div class="suggest">
    <div class="small muted">Осталось ~${fmtInt(left)} ккал — подойдёт из твоего обычного:</div>
    <div class="suggest-list">${list.map((m, j) => `<button class="chip" data-suggest="${j}">
      ${esc(m.title || m.items.map((i) => i.name).join(", "))} <b>${r0(m.totals.kcal)}</b></button>`).join("")}</div>
  </div>`;
}

function openWeigh() {
  const w = state.dayData?.weight;
  const goal = !w?.goal ? "" : w.eta === "reached" ? " · цель достигнута"
    : w.eta ? ` · цель ${kgFmt(w.goal)} к ${new Date(w.eta + "T12:00").toLocaleDateString("ru-RU", { day: "numeric", month: "short" })}` : ` · цель ${kgFmt(w.goal)}`;
  openSheet(`
    <div class="row between"><h2>Вес сегодня</h2><button class="ghost" data-close aria-label="Закрыть">${ICONS.close}</button></div>
    ${w ? `<p class="small" style="margin:0 0 10px">Тренд <b>${kgFmt(w.trend)} кг</b> ${paceText(w)}<span class="muted">${goal}</span></p>`
      : '<p class="muted small" style="margin:0 0 10px">Запиши вес — покажу тренд и посчитаю реальный расход.</p>'}
    <form id="weigh-form" class="row">
      <input id="weigh-kg" type="text" inputmode="decimal" autocomplete="off" placeholder="кг" value="${w?.today ?? w?.last?.kg ?? ""}" autofocus>
      <button class="primary">Сохранить</button>
    </form>
    <p class="muted small">Лучше взвешиваться утром, после туалета и до еды — так точки ровнее.</p>
    ${w?.today != null ? '<button class="block danger" id="weigh-del">Удалить сегодняшнее взвешивание</button>' : ""}`);
  $("#weigh-form").onsubmit = async (e) => {
    e.preventDefault();
    const kg = Number($("#weigh-kg").value.replace(",", "."));
    if (!(kg >= 20 && kg <= 400)) return toast("Вес — число от 20 до 400 кг");
    try {
      const r = await quickWrite("/api/weight", { method: "PUT", json: { kg }, label: `вес ${kgFmt(kg)} кг` });
      closeSheet();
      if (!r.queued) toast("Вес записан");
      loadDay();
    } catch (err) { toast(err.message); }
  };
  $("#weigh-del")?.addEventListener("click", async () => {
    try { await api("/api/weight", { method: "DELETE" }); closeSheet(); loadDay(); } catch (err) { toast(err.message); }
  });
}

const MILESTONES = [7, 14, 30, 50, 100, 200, 365];

// Everything about runs that used to sit inline on the day screen.
function openStreak() {
  const st = state.dayData?.streaks;
  if (!st) return;
  const days = (n) => `${n} ${plural(n, "день", "дня", "дней")}`;
  const m = st.next_milestone;
  const line = (icon, text, hint = "") => `<li><span class="li-ico">${icon}</span><span>${text}${hint ? `<span class="muted small">${hint}</span>` : ""}</span></li>`;
  const freeze = st.frozen
    ? line(ICONS.snow, `Заморозка спасла ${esc(dayLabel(st.frozen).toLowerCase())}`, "Следующая — через неделю после неё")
    : line(ICONS.snow, st.freeze_ready ? "Заморозка готова" : "Заморозка уже потрачена", "Один пропуск в неделю не обрывает серию");
  openSheet(`
    <div class="row between"><h2>Серия</h2><button class="ghost" data-close aria-label="Закрыть">${ICONS.close}</button></div>
    <div class="card">
      <div class="streak-big"><b>${days(st.days)}</b> <span class="muted">подряд с записями${st.today ? "" : " · сегодня ещё пусто"}</span></div>
      ${m && st.days ? `<div class="need"><span>До ${days(m)}</span><span>ещё ${m - st.days}</span></div>${bar(st.days, m)}` : ""}
      <ul class="insights">
        ${freeze}
        ${line(ICONS.target, `${days(st.in_goal)} в норме`, "Подряд в коридоре 85–105% нормы")}
        ${line(ICONS.dumbbell, `${days(st.protein)} с белком`, "Подряд с белком от 90% нормы")}
      </ul>
    </div>
    ${streaksRow(state.streaks) || '<p class="muted small">Общие серии с друзьями включаются в «Профиль → Уведомления».</p>'}`);
}

// A milestone reached today gets a moment, once.
function celebrate(st) {
  if (!st?.today || !MILESTONES.includes(st.days) || state.day !== isoDate(new Date())) return;
  const key = `ft-milestone:${state.me?.username}`;
  try {
    if (localStorage.getItem(key) === String(st.days)) return;
    localStorage.setItem(key, String(st.days));
  } catch { return; }
  setTimeout(() => toast(`${st.days} ${plural(st.days, "день", "дня", "дней")} подряд! Так держать`), 400);
}

// Shared streaks. On the day screen only when someone besides me takes part.
function streaksRow(st, { others = false } = {}) {
  if (!st?.joined || !st.people.length) return "";
  if (others && !st.people.some((p) => !p.me)) return "";
  const chips = st.people.map((p) => `<span class="chip ${p.me ? "me" : ""}" title="${p.today ? "сегодня записано" : "сегодня ещё пусто"}">
    <span class="chip-ico ${p.today ? "ok" : ""}">${p.today ? ICONS.check : ICONS.clock}</span>${esc(p.me ? "ты" : p.name)} <b>${p.streak}</b></span>`).join("");
  return `<div class="streaks"><span class="streaks-title">Общие серии</span>${chips}
    ${st.people.length === 1 ? '<span class="muted small">Пока участвуешь только ты</span>' : ""}</div>`;
}

// Fibre, sugar, salt: a quiet reference line, not a goal to fail. Only the salt limit gets a colour.
const gFmt = (n) => String(r1(n)).replace(".", ",");
function nutrientLine(t, refs) {
  if (t.fiber == null && t.sugar == null && t.salt == null) return "";
  const item = (label, value, cls = "") => `<span class="nut-item ${cls}">${label} <b>${value}</b></span>`;
  return `<div class="nut" aria-label="Клетчатка, сахар и соль">
    ${t.fiber != null ? item("Клетчатка", `${r0(t.fiber)}${refs ? ` / ${refs.fiber}` : ""} г`) : ""}
    ${t.sugar != null ? item("Сахар", `${r0(t.sugar)} г`) : ""}
    ${t.salt != null ? item("Соль", `${gFmt(t.salt)}${refs ? ` / ${refs.salt}` : ""} г`, refs && t.salt > refs.salt ? "over" : "") : ""}
  </div>`;
}

// Footer of the summary card: each value stays whole, the pair may wrap between items.
function activityLine(a) {
  if (!a || (a.active_kcal == null && a.steps == null)) return "";
  const item = (icon, text) => `<span class="move-item"><span class="move-ico">${icon}</span>${text}</span>`;
  return `<div class="move" aria-label="Движение">
    ${a.active_kcal != null ? item(ICONS.flame, `<b>${fmtInt(a.active_kcal)}</b> ккал активности`) : ""}
    ${a.steps != null ? item(ICONS.steps, `<b>${fmtInt(a.steps)}</b> ${plural(r0(a.steps), "шаг", "шага", "шагов")}`) : ""}
  </div>`;
}

// Workouts confirmed in the coach chat, under the movement line. Read-only offline: never queued.
// Each value stays whole (a no-break space before its unit); lines wrap only between them.
const workoutText = (w) => [esc(w.kind), `${r0(w.minutes)}\u00a0мин`, w.distance_km != null && `${gFmt(w.distance_km)}\u00a0км`,
  w.kcal != null && `${fmtInt(w.kcal)}\u00a0ккал`, w.avg_hr != null && `пульс\u00a0${r0(w.avg_hr)}`].filter(Boolean).join(" · ");
function workoutLines(list) {
  if (!list.length) return "";
  return `<div class="workouts" aria-label="Тренировки">${list.map((w) => `<div class="wk">
    <span class="move-ico">${ICONS.dumbbell}</span>
    <span class="grow">${w.time ? `<span class="muted">${esc(w.time)}</span> ` : ""}${workoutText(w)}</span>
    <button class="ghost wk-del" data-workout-del="${w.id}" aria-label="Удалить тренировку">${ICONS.trash}</button>
  </div>`).join("")}</div>`;
}

async function deleteWorkout(id) {
  if (!confirm("Удалить эту тренировку?")) return;
  try {
    await api(`/api/workouts/${id}`, { method: "DELETE" });
    loadDay();
  } catch (err) { toast(err.message); }
}

const PART_OF_DAY = [[5, "moon", "Перекус"], [11, "cup", "Завтрак"], [16, "bowl", "Обед"], [22, "plate", "Ужин"], [24, "apple", "Перекус"]];

// The card slides over a delete button: swipe left reveals it, the editor keeps its own.
function mealCard(m) {
  const at = new Date(m.eaten_at);
  const time = at.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  const [, icon, name] = PART_OF_DAY.find(([until]) => at.getHours() < until);
  // A queued meal isn't on the server yet: nothing to edit or delete until it is.
  if (m.queued) {
    return `<div class="meal-wrap"><div class="card meal queued" aria-label="${esc(m.title || name)}, ${r0(m.totals.kcal)} ккал — ждёт сети">
      <div class="meal-ico">${ICONS[icon]}</div>
      <div class="grow">
        <div class="meal-head"><span class="meal-title">${esc(m.title || name)}</span><span class="meal-kcal">${r0(m.totals.kcal)} <small>ккал</small></span></div>
        <div class="meal-items">${m.items.map((i) => `${esc(i.name)} ${r0(i.grams)} г`).join(" · ")}</div>
        <div class="meal-meta">${time} · <span class="badge wait">ждёт сети</span></div>
      </div>
    </div></div>`;
  }
  return `<div class="meal-wrap">
    <button class="meal-del" data-meal-del="${m.id}" aria-label="Удалить приём пищи" tabindex="-1">${ICONS.trash}<span>Удалить</span></button>
    <div class="card meal" data-meal="${m.id}" role="button" tabindex="0" aria-label="${esc(m.title || name)}, ${r0(m.totals.kcal)} ккал — изменить">
      <div class="meal-ico">${ICONS[icon]}${m.photo ? `<img src="/api/photos/${encodeURIComponent(m.photo)}" loading="lazy" decoding="async" alt="">` : ""}</div>
      <div class="grow">
        <div class="meal-head"><span class="meal-title">${esc(m.title || name)}</span><span class="meal-kcal">${r0(m.totals.kcal)} <small>ккал</small></span></div>
        <div class="meal-items">${m.items.map((i) => `${esc(i.name)} ${r0(i.grams)} г`).join(" · ")}</div>
        <div class="meal-meta">${time} · <i style="--c:var(--protein)"></i>Б ${r0(m.totals.protein)} <i style="--c:var(--fat)"></i>Ж ${r0(m.totals.fat)} <i style="--c:var(--carbs)"></i>У ${r0(m.totals.carbs)}</div>
      </div>
    </div>
  </div>`;
}

// The one delete flow, from the editor and from the swipe: confirm, delete, reload the day.
async function deleteMeal(id) {
  if (!confirm("Удалить этот приём пищи?")) return false;
  try {
    await api(`/api/meals/${id}`, { method: "DELETE" });
    loadDay();
    return true;
  } catch (err) { toast(err.message); return false; }
}

// ---------- day gestures: swipe between days, swipe a meal to delete ----------

const SWIPE_MIN = 60, MEAL_OPEN = -88;
let touch = null, openMeal = null;

function closeOpenMeal() {
  if (!openMeal) return;
  openMeal.style.transform = "";
  openMeal.closest(".meal-wrap")?.classList.remove("open");
  openMeal = null;
}

app.addEventListener("touchstart", (e) => {
  if (state.view !== "day" || e.touches.length !== 1) { touch = null; return; }
  const p = e.touches[0];
  // Leave the screen edges to the system back gesture.
  if (p.clientX < 20 || p.clientX > innerWidth - 20) { touch = null; return; }
  const meal = e.target.closest(".meal");
  touch = { x: p.clientX, y: p.clientY, t: Date.now(), meal, base: meal && meal === openMeal ? MEAL_OPEN : 0, axis: null, dx: 0 };
}, { passive: true });

app.addEventListener("touchmove", (e) => {
  if (!touch) return;
  const p = e.touches[0];
  const dx = p.clientX - touch.x, dy = p.clientY - touch.y;
  // Decide once, after a few pixels: vertical movement is a scroll and is left alone.
  if (!touch.axis && Math.hypot(dx, dy) > 8) touch.axis = Math.abs(dx) > Math.abs(dy) * 1.2 ? "x" : "y";
  if (touch.axis !== "x") return;
  touch.dx = dx;
  if (touch.meal) {
    touch.meal.classList.add("dragging");
    touch.meal.style.transform = `translateX(${Math.max(MEAL_OPEN - 24, Math.min(0, touch.base + dx))}px)`;
  }
}, { passive: true });

app.addEventListener("touchend", () => {
  const t = touch;
  touch = null;
  if (!t || t.axis !== "x") return;
  if (t.meal) {
    t.meal.classList.remove("dragging");
    const pos = t.base + t.dx;
    if (pos < MEAL_OPEN / 2) {
      if (openMeal !== t.meal) closeOpenMeal();
      openMeal = t.meal;
      openMeal.style.transform = `translateX(${MEAL_OPEN}px)`;
      openMeal.closest(".meal-wrap").classList.add("open");
    } else {
      t.meal.style.transform = "";
      if (openMeal === t.meal) closeOpenMeal();
    }
    t.meal.dataset.swiped = "1"; // the click that follows a drag isn't a tap
    setTimeout(() => delete t.meal.dataset.swiped, 50);
    return;
  }
  if (Math.abs(t.dx) < SWIPE_MIN || Date.now() - t.t > 700) return;
  const btn = $(`[data-act="${t.dx < 0 ? "next" : "prev"}"]`, app);
  if (btn && !btn.disabled) { closeOpenMeal(); btn.click(); }
});

app.addEventListener("touchcancel", () => {
  if (touch?.meal) { touch.meal.classList.remove("dragging"); touch.meal.style.transform = touch.meal === openMeal ? `translateX(${MEAL_OPEN}px)` : ""; }
  touch = null;
});

// Long press on the water tile opens the −/+ sheet, like its small button does.
let pressTimer = null, pressFired = false;
app.addEventListener("pointerdown", (e) => {
  if (!e.target.closest('[data-act="water-add"]')) return;
  pressFired = false;
  clearTimeout(pressTimer);
  pressTimer = setTimeout(() => { pressFired = true; navigator.vibrate?.(15); renderWaterSheet(); }, 500);
});
["pointerup", "pointercancel", "pointerleave"].forEach((ev) => app.addEventListener(ev, () => clearTimeout(pressTimer)));
app.addEventListener("touchmove", () => clearTimeout(pressTimer), { passive: true });
app.addEventListener("contextmenu", (e) => { if (e.target.closest('[data-act="water-add"]')) e.preventDefault(); });

app.addEventListener("click", (e) => {
  const act = e.target.closest("[data-act]")?.dataset.act;
  const mealEl = e.target.closest("[data-meal]");
  const sug = e.target.closest("[data-suggest]");
  const del = e.target.closest("[data-meal-del]");
  const wdel = e.target.closest("[data-workout-del]");
  if (wdel) return deleteWorkout(Number(wdel.dataset.workoutDel));
  if (sug) return repeatMeal(state.dayData.suggest[Number(sug.dataset.suggest)]);
  if (del) return deleteMeal(Number(del.dataset.mealDel)).then((done) => { if (!done) closeOpenMeal(); });
  if (mealEl?.dataset.swiped) return;
  // A tap anywhere while a card shows its delete button just closes it.
  if (openMeal) { closeOpenMeal(); if (mealEl) return; }
  if (act === "water-add" && pressFired) { pressFired = false; return; } // the long press already opened the sheet
  if (act === "prev" || act === "next") {
    const d = new Date(state.day + "T12:00");
    d.setDate(d.getDate() + (act === "prev" ? -1 : 1));
    state.day = isoDate(d);
    loadDay();
  } else if (act === "reports") {
    loadStats();
  } else if (act === "back") {
    loadDay();
  } else if (act === "period") {
    state.statsDays = Number(e.target.closest("[data-days]").dataset.days);
    state.selected = null;
    loadStats();
  } else if (act === "bar") {
    state.selected = e.target.closest("[data-date]").dataset.date;
    renderStats();
  } else if (act === "open-day") {
    state.day = state.selected;
    loadDay();
  } else if (act === "coach-clear") {
    if (!confirm("Очистить переписку с коучем?")) return;
    api("/api/coach", { method: "DELETE" }).then(() => loadCoach()).catch((err) => toast(err.message));
  } else if (act === "weigh") {
    openWeigh();
  } else if (act === "water-add") {
    addWater(GLASS_ML);
  } else if (act === "water-sheet") {
    renderWaterSheet();
  } else if (act === "streak") {
    openStreak();
  } else if (act === "profile-back") {
    profileBack();
  } else if (act?.startsWith("profile:")) {
    openProfileSection(act.slice(8));
  } else if (act === "apply-adaptive") {
    applyGoals(state.stats.energy.adaptive.suggested);
  } else if (act === "review") {
    makeReview();
  } else if (act === "save-weight") {
    saveWeight();
  } else if (act === "add") {
    openAdd();
  } else if (act === "settings") {
    loadProfileView();
  } else if (mealEl) {
    const meal = state.dayData.meals.find((m) => m.id === Number(mealEl.dataset.meal));
    if (meal) openEditor({ id: meal.id, title: meal.title, eaten_at: meal.eaten_at, photo: meal.photo, items: meal.items.map((i) => ({ ...i, options: [] })), comment: "" });
  }
});
app.addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches(".meal[data-meal]")) { e.preventDefault(); e.target.click(); }
});
// A thumbnail that is gone (kept N days) or fails to load leaves the icon under it visible.
document.addEventListener("error", (e) => {
  if (e.target instanceof HTMLImageElement && e.target.matches(".meal-ico img, img.preview")) e.target.remove();
}, true);

// ---------- reports ----------

const PERIODS = [[7, "Неделя"], [30, "Месяц"], [90, "3 месяца"]];
const shortDate = (iso) => new Date(iso + "T12:00").toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
const plural = (n, one, few, many) => {
  const m10 = n % 10, m100 = n % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
};

async function loadStats() {
  leaveProfileHash();
  showTabs("reports");
  showSkeleton("reports");
  state.view = "reports";
  try {
    const [stats, review, found] = await Promise.all([
      api(`/api/stats?days=${state.statsDays}`),
      state.review ? Promise.resolve(state.review) : api("/api/review"),
      api("/api/insights").catch(() => []),
    ]);
    state.stats = stats;
    state.review = review;
    state.insights = found;
    renderStats();
  } catch (err) {
    if (err.status === 401) return renderAuth();
    app.innerHTML = `<div class="card error">${esc(err.message)}</div>`;
  }
}

function kcalChart(s) {
  const W = 320, H = 150, top = 8, bottom = 18, n = s.days.length;
  const goal = s.goals.kcal;
  const max = Math.max(goal * 1.15, ...s.days.map((d) => d.kcal));
  const y = (v) => top + (H - top - bottom) * (1 - v / max);
  const slot = W / n, gap = n > 40 ? 1 : 2, bw = Math.min(28, Math.max(1, slot - gap));
  const rx = Math.min(4, bw / 2);
  // Rounded top, square base: a rounded rect plus a square patch over its bottom corners.
  const column = (x, yTop, h, cls = "") => `<rect x="${x}" y="${yTop}" width="${bw}" height="${h}" rx="${Math.min(rx, h / 2)}" class="${cls}"/>
      <rect x="${x}" y="${yTop + h - Math.min(h, rx)}" width="${bw}" height="${Math.min(h, rx)}" class="${cls}"/>`;
  const base = H - bottom, yGoal = y(goal);
  const bars = s.days.map((d, i) => {
    const x = i * slot + (slot - bw) / 2;
    const sel = d.date === state.selected;
    const h = d.kcal > 0 ? Math.max(2, base - y(d.kcal)) : 0;
    // Over the tolerance only the part above the goal line changes colour: the day still had its norm.
    const mark = !h ? "" : isOver(d.kcal, goal)
      ? `<rect x="${x}" y="${yGoal}" width="${bw}" height="${base - yGoal}"/>${column(x, base - h, yGoal - (base - h), "over")}`
      : column(x, base - h, h);
    return `<g class="b ${sel ? "sel" : ""}" data-act="bar" data-date="${d.date}">
      <rect class="hit" x="${i * slot}" y="0" width="${slot}" height="${H}"/>${mark}</g>`;
  }).join("");
  const ticks = [0, Math.floor((n - 1) / 2), n - 1].filter((v, i, a) => a.indexOf(v) === i)
    .map((i) => `<text x="${(i + 0.5) * slot}" y="${H - 4}" text-anchor="${i === 0 ? "start" : i === n - 1 ? "end" : "middle"}">${esc(shortDate(s.days[i].date))}</text>`).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Калории по дням">
    <line class="base" x1="0" x2="${W}" y1="${H - bottom}" y2="${H - bottom}"/>
    ${bars}
    <line class="goal" x1="0" x2="${W}" y1="${y(goal)}" y2="${y(goal)}"/>
    <text class="goal-label" x="${W}" y="${y(goal) - 4}" text-anchor="end">норма ${goal}</text>
    ${ticks}
  </svg>`;
}

function weightChart(s) {
  const pts = s.weights;
  if (pts.length < 2) return `<p class="muted small">${pts.length ? "Ещё одно взвешивание — и появится график." : "За этот период взвешиваний нет."}</p>`;
  const W = 320, H = 120, top = 10, bottom = 18, left = 4, right = 4;
  const t0 = new Date(s.from + "T12:00").getTime(), t1 = new Date(s.to + "T12:00").getTime();
  const kgs = pts.flatMap((p) => [p.kg, p.trend ?? p.kg]);
  const lo = Math.floor(Math.min(...kgs) - 0.5), hi = Math.ceil(Math.max(...kgs) + 0.5);
  const x = (iso) => left + (W - left - right) * (t1 === t0 ? 0.5 : (new Date(iso + "T12:00").getTime() - t0) / (t1 - t0));
  const y = (kg) => top + (H - top - bottom) * (1 - (kg - lo) / (hi - lo));
  const path = pts.map((p, i) => `${i ? "L" : "M"}${x(p.date).toFixed(1)},${y(p.trend ?? p.kg).toFixed(1)}`).join(" ");
  const dots = pts.map((p) => `<circle cx="${x(p.date)}" cy="${y(p.kg)}" r="${pts.length > 40 ? 2 : 3}"><title>${esc(shortDate(p.date))}: ${p.kg} кг, тренд ${r1(p.trend ?? p.kg)}</title></circle>`).join("");
  return `<svg class="chart weight" viewBox="0 0 ${W} ${H}" role="img" aria-label="Вес по дням">
    <line class="grid" x1="0" x2="${W}" y1="${y(hi)}" y2="${y(hi)}"/><line class="grid" x1="0" x2="${W}" y1="${y(lo)}" y2="${y(lo)}"/>
    <text x="${W}" y="${y(hi) + 12}" text-anchor="end">${hi} кг</text><text x="${W}" y="${y(lo) - 4}" text-anchor="end">${lo} кг</text>
    <path d="${path}"/>${dots}
    <text x="0" y="${H - 4}">${esc(shortDate(s.from))}</text><text x="${W}" y="${H - 4}" text-anchor="end">${esc(shortDate(s.to))}</text>
  </svg>`;
}

function renderStats() {
  const s = state.stats;
  const g = s.goals, a = s.avg;
  const sel = s.days.find((d) => d.date === state.selected);
  const todayW = s.weights.find((w) => w.date === s.to);
  const tr = (w) => w.trend ?? w.kg;
  const delta = s.weights.length >= 2 ? r1(tr(s.weights.at(-1)) - tr(s.weights[0])) : null;
  showTabs("reports");
  app.innerHTML = `
    ${topbar("Отчёты", `${shortDate(s.from)} — ${shortDate(s.to)}`, "")}
    <div class="seg">${PERIODS.map(([d, l]) => `<button data-act="period" data-days="${d}" class="${d === state.statsDays ? "on" : ""}">${l}</button>`).join("")}</div>
    <div class="tiles">
      <div class="tile"><span class="muted small">Серия</span><b>${s.streak}</b><span class="muted small">${plural(s.streak, "день", "дня", "дней")} подряд</span></div>
      <div class="tile"><span class="muted small">В норме</span><b>${s.in_goal_days}<small> / ${s.logged_days}</small></b><span class="muted small">дней с записями</span></div>
      <div class="tile"><span class="muted small">В среднем</span><b>${r0(a.kcal)}</b><span class="muted small">ккал/день</span></div>
    </div>
    ${insightsCard(state.insights)}
    ${reviewCard()}
    <div class="card">
      <div class="row between"><b>Калории</b><span class="muted small">${s.logged_days ? `среднее ${r0(a.kcal)} из ${g.kcal}` : "нет записей"}</span></div>
      ${kcalChart(s)}
      <div class="sel-info small">${sel
        ? `<span><b>${esc(dayLabel(sel.date))}</b>: ${r0(sel.kcal)} ккал · Б ${r0(sel.protein)} Ж ${r0(sel.fat)} У ${r0(sel.carbs)}</span><button class="ghost" data-act="open-day">Открыть ›</button>`
        : '<span class="muted">Нажми на столбик, чтобы увидеть день</span>'}</div>
    </div>
    <div class="card">
      <div class="row between"><b>Вес</b><span class="muted small">${delta === null ? (s.last_weight ? `последний: ${s.last_weight.kg} кг, ${esc(shortDate(s.last_weight.date))}` : "") : `тренд ${delta > 0 ? "+" : delta < 0 ? "−" : ""}${String(Math.abs(delta)).replace(".", ",")} кг за период`}</span></div>
      ${weightChart(s)}
      <p class="muted small" style="margin:6px 0 0">Точки — взвешивания, линия — сглаженный тренд: суточные скачки воды почти не двигают её.</p>
      <form class="row" id="weight-form" style="margin-top:10px">
        <input id="weight" type="text" inputmode="decimal" autocomplete="off" placeholder="Вес сегодня, кг" value="${todayW ? todayW.kg : ""}">
        <button class="primary" data-act="save-weight" type="button">${todayW ? "Обновить" : "Записать"}</button>
      </form>
    </div>
    ${energyCard(s)}
    <div class="card">
      <b>БЖУ в среднем за день</b>
      <div class="macros">
        ${[["Белки", a.protein, g.protein, "var(--protein)"], ["Жиры", a.fat, g.fat, "var(--fat)"], ["Углеводы", a.carbs, g.carbs, "var(--carbs)"]]
          .map(([n, v, goal, c]) => `<div class="macro"><span class="muted small">${n}</span><b>${r0(v)} / ${goal} г</b>${bar(v, goal, c)}</div>`).join("")}
      </div>
      ${extraAverages(a, s.refs)}
      <p class="muted small" style="margin:10px 0 0">Среднее считается только по дням, где есть записи.</p>
    </div>
`;
  $("#weight-form").onsubmit = (e) => { e.preventDefault(); saveWeight(); };
}

// Fibre and salt against their reference points, sugar as a bare number; each only with data.
function extraAverages(a, refs) {
  if (!refs || (a.fiber == null && a.sugar == null && a.salt == null)) return "";
  const cells = [
    a.fiber != null ? `<div class="macro"><span class="muted small">Клетчатка</span><b>${r0(a.fiber)} / ${refs.fiber} г</b>${bar(a.fiber, refs.fiber, "var(--fiber)", false)}</div>` : "",
    a.salt != null ? `<div class="macro"><span class="muted small">Соль</span><b>${gFmt(a.salt)} / ${refs.salt} г</b>${bar(a.salt, refs.salt, "var(--salt)")}</div>` : "",
    a.sugar != null ? `<div class="macro"><span class="muted small">Сахар</span><b>${r0(a.sugar)} г</b><span class="muted small">весь, не только добавленный</span></div>` : "",
  ].join("");
  return `<div class="macros extra">${cells}</div>`;
}

function energyCard(s) {
  const e = s.energy, ad = e.adaptive, g = s.goals;
  let main;
  if (ad.ready) {
    const kgw = ad.kg_per_week;
    const sug = ad.suggested;
    const differs = sug && Math.abs(sug.kcal - g.kcal) >= 50;
    main = `<div class="energy-big"><b>~${fmtInt(ad.tdee)}</b> <span class="muted">ккал в день</span></div>
      <p class="small muted" style="margin:2px 0 0">Ты ел в среднем ${fmtInt(ad.avg_intake)} ккал, вес ${kgw > 0 ? "+" : kgw < 0 ? "−" : ""}${String(Math.abs(kgw)).replace(".", ",")} кг в неделю — значит, тратишь примерно столько.</p>
      ${differs ? `<div class="calc"><div class="small muted">Норма под твою цель по этому расходу:</div><div>${goalsLine(sug)}</div>
        <button class="block" data-act="apply-adaptive" style="margin-top:8px">Обновить норму</button></div>` : ""}`;
  } else {
    const pct = (have, need) => Math.min(100, Math.round((100 * have) / need));
    main = `<p class="small muted" style="margin:4px 0 8px">Посчитаю твой реальный расход по дневнику и весу, когда наберётся данных за последние 4 недели:</p>
      <div class="need"><span>Дни с записями</span><span>${ad.logged_days} из ${ad.need_logged_days}</span></div>${bar(pct(ad.logged_days, ad.need_logged_days), 100)}
      <div class="need"><span>Взвешивания: разброс дат</span><span>${ad.weigh_in_span_days} из ${ad.need_span_days} дн.</span></div>${bar(pct(ad.weigh_in_span_days, ad.need_span_days), 100)}`;
  }
  const extra = [
    e.formula ? `По формуле: ~${fmtInt(e.formula.tdee)} ккал` : "",
    e.activity ? `Часы: ${[e.activity.active_kcal != null ? `${fmtInt(e.activity.active_kcal)} ккал активности` : "",
      e.activity.steps != null ? `${fmtInt(e.activity.steps)} ${plural(r0(e.activity.steps), "шаг", "шага", "шагов")}` : ""].filter(Boolean).join(" · ")} в среднем за ${e.activity.days} ${plural(e.activity.days, "день", "дня", "дней")}` : "",
  ].filter(Boolean);
  return `<div class="card">
    <b>Расход энергии</b>
    ${main}
    ${extra.length ? `<div class="energy-extra small muted">${extra.map((x) => `<div>${x}</div>`).join("")}</div>` : ""}
  </div>`;
}

function insightsCard(list) {
  if (!list?.length) return "";
  return `<div class="card"><b>Наблюдения за 2 недели</b>
    <ul class="insights">${list.map((i) => `<li><span class="li-ico">${iconOf(i.icon)}</span><span>${esc(i.text)}</span></li>`).join("")}</ul></div>`;
}

function reviewCard() {
  const r = state.review?.review;
  const list = (title, items) => items?.length ? `<div class="rv-sec"><span class="muted small">${title}</span><ul>${items.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></div>` : "";
  let body;
  if (state.reviewBusy) {
    body = '<div class="spinner"></div><p class="center muted small">Смотрю на неделю…</p>';
  } else if (r) {
    body = `<p class="rv-summary">${esc(r.summary)}</p>
      ${list("Получается", r.good)}${list("Мешает", r.improve)}${list("Советы на неделю", r.tips)}
      ${state.review.fresh ? "" : '<button class="block" data-act="review">Обновить обзор</button>'}`;
  } else {
    body = `<p class="muted small" style="margin:6px 0 10px">ИИ посмотрит на последние 7 дней и подскажет, что поправить. Стоит пару центов из дневного лимита.</p>
      <button class="primary block" data-act="review">Получить обзор</button>`;
  }
  return `<div class="card review">
    <div class="row between"><b class="with-ico">${ICONS.sparkle}Обзор недели</b>${r && !state.reviewBusy ? `<span class="muted small">${esc(dayLabel(r.date))}</span>` : ""}</div>
    ${body}
    ${state.reviewError ? `<div class="error">${esc(state.reviewError)}</div>` : ""}
  </div>`;
}

async function makeReview() {
  state.reviewBusy = true;
  state.reviewError = "";
  renderStats();
  try {
    state.review = await api("/api/review", { method: "POST" });
    state.me = await api("/api/me");
  } catch (err) {
    state.reviewError = err.message;
  }
  state.reviewBusy = false;
  if (state.view === "reports") renderStats();
}

async function saveWeight() {
  const kg = Number($("#weight").value.replace(",", "."));
  if (!(kg >= 20 && kg <= 400)) return toast("Вес — число от 20 до 400 кг");
  try {
    const r = await quickWrite("/api/weight", { method: "PUT", json: { kg }, label: `вес ${kgFmt(kg)} кг` });
    if (r.queued) return;
    toast("Вес записан");
    loadStats();
  } catch (err) { toast(err.message); }
}

// ---------- add: photo / note -> draft ----------

let pendingPhoto = null;
// The just-taken photo, for a draft that has no stored thumbnail (photos switched off on the server).
let localPreview = null;
function dropLocalPreview() {
  if (localPreview) URL.revokeObjectURL(localPreview.url);
  localPreview = null;
}

function openAdd() {
  pendingPhoto = null;
  dropLocalPreview();
  const stash = stashedDraft();
  const stashKcal = stash ? r0(totalsOf(stash.draft.items).kcal) : 0;
  openSheet(`
    <div class="row between"><h2>Что съел?</h2><button class="ghost" data-close aria-label="Закрыть">${ICONS.close}</button></div>
    ${stash ? `<div class="resume"><button class="grow with-ico" id="resume">${ICONS.undo}<span>Вернуться к черновику: ${esc(stash.draft.title || "без названия")} · ${stashKcal} ккал</span></button>
      <button class="ghost" id="resume-drop" aria-label="Выбросить черновик">${ICONS.close}</button></div>` : ""}
    <div class="row add-search">
      <span class="search-field">${ICONS.search}<input id="add-q" type="search" placeholder="Найти продукт" aria-label="Найти продукт" autocomplete="off" enterkeyhint="search"></span>
      <button id="scan" aria-label="Сканировать штрихкод">${ICONS.barcode}</button>
    </div>
    <div class="search-results" id="add-results"></div>
    <div class="pick">
      <label class="btn"><span class="pick-ico">${ICONS.camera}</span><span>Сфотографировать</span><input type="file" accept="image/*" capture="environment" data-photo></label>
      <label class="btn"><span class="pick-ico">${ICONS.image}</span><span>Из галереи</span><input type="file" accept="image/*" data-photo></label>
    </div>
    <details class="describe" id="describe">
      <summary class="with-ico">${ICONS.pencil}Описать словами или уточнить к фото</summary>
      <textarea id="note" placeholder="Например: 2 яйца и тост с маслом. К фото: рис 150 г, жарено на масле"></textarea>
      <div class="row voice-row">${micButton("note")}<button class="primary grow" id="recognize">Посчитать</button></div>
    </details>
    <div class="error" id="add-error"></div>
    <div id="dishes"></div>
    <div id="recent"><h3 class="sub">Часто ешь</h3><div class="skeleton"></div><div class="skeleton"></div></div>
    <div class="row add-more">
      <button class="grow ghost-line" id="manual">Собрать вручную</button>
      <button class="grow ghost-line with-ico" id="dish-new">${ICONS.pot}Новое блюдо</button>
    </div>
    <p class="muted small center" id="spent"></p>`);
  const me = state.me;
  if (me) $("#spent").textContent = `Распознавания сегодня: $${me.spent_today.toFixed(2)} из $${me.daily_limit.toFixed(2)}`;
  loadRecent();
  loadDishes();
}

let recentMeals = [];
async function loadRecent() {
  try { recentMeals = await api("/api/meals/recent"); store("recent", recentMeals); }
  catch (err) { recentMeals = err.status === 0 ? readStored("recent", []) : []; }
  const box = $("#recent");
  if (!box) return;
  if (!recentMeals.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<h3 class="sub">Часто ешь <span class="muted">— тап записывает сразу</span></h3>` + recentMeals.map((m, j) => `
    <div class="recent">
      <button class="recent-main" data-recent="${j}">
        <span class="grow"><b>${esc(m.title || m.items.map((i) => i.name).join(", "))}</b>
          <span class="small muted">${m.title ? esc(m.items.map((i) => `${i.name} ${r0(i.grams)} г`).join(" · ")) : ""}</span></span>
        <span class="small"><b>${r0(m.totals.kcal)}</b> ккал${m.count > 1 ? `<br><span class="muted">×${m.count}</span>` : ""}</span>
      </button>
      <button class="recent-edit" data-recent-edit="${j}" aria-label="Изменить перед записью">${ICONS.pencil}</button>
    </div>`).join("");
}

// One tap: the meal is written as it was, with an undo.
async function logAgain(m) {
  const body = {
    title: m.title,
    eaten_at: localNow(), // now, not whenever a queued copy reaches the server
    items: m.items.map((i) => ({ name: i.name, grams: i.grams, food_id: i.food_id ?? null, source: i.source,
      kcal_100: i.kcal_100, protein_100: i.protein_100, fat_100: i.fat_100, carbs_100: i.carbs_100, ...extrasOf(i) })),
  };
  const label = m.title || m.items[0]?.name || "приём пищи";
  try {
    const saved = await quickWrite("/api/meals", { method: "POST", json: body, label });
    closeSheet();
    state.day = isoDate(new Date());
    await loadDay();
    if (saved.queued) return;
    toast(`Записано: ${label}`, {
      label: "Отменить",
      run: async () => { await api(`/api/meals/${saved.id}`, { method: "DELETE" }).catch(() => {}); loadDay(); },
    });
  } catch (err) { toast(err.message); }
}

const EXTRA_KEYS = ["fiber_100", "sugar_100", "salt_100"];
const extrasOf = (i) => Object.fromEntries(EXTRA_KEYS.map((k) => [k, i[k] ?? null]));

// A copy of an existing meal as a new, unsaved one eaten now. The photo stays with the original.
function repeatMeal(m) {
  // A repeat is the user's own choice, not a recognition: nothing to learn from it.
  openEditor({ title: m.title, items: m.items.map(({ id, ai_grams, ...i }) => ({ ...i, options: [] })), comment: "", today: true });
}

sheetBody.addEventListener("change", async (e) => {
  if (e.target.matches("[data-dish-photo]") && dish) {
    const file = e.target.files?.[0];
    const mine = dish;
    if (!file) return;
    mine.photo = await shrinkPhoto(file);
    mine.note = $("#dish-note")?.value || mine.note;
    if (dish === mine) renderDish();
    return;
  }
  if (!e.target.matches("[data-photo]")) return;
  const file = e.target.files?.[0];
  if (!file) return;
  pendingPhoto = await shrinkPhoto(file);
  recognizeNow();
});

sheetBody.addEventListener("click", async (e) => {
  if (e.target.closest("[data-close]")) return closeSheet();
  const water = e.target.closest("[data-water]");
  if (water) return addWater(Number(water.dataset.water) * GLASS_ML);
  if (e.target.id === "manual") return openEditor({ title: "", items: [], comment: "" });
  if (e.target.closest("#scan")) return openScanner();
  if (e.target.closest("#resume")) { const st = stashedDraft(); if (st) openEditor(st.draft); return; }
  if (e.target.closest("#resume-drop")) { dropStash(); e.target.closest(".resume").remove(); return; }
  const rec = e.target.closest("[data-recent]");
  if (rec) { rec.disabled = true; return logAgain(recentMeals[Number(rec.dataset.recent)]); }
  const recEdit = e.target.closest("[data-recent-edit]");
  if (recEdit) return repeatMeal(recentMeals[Number(recEdit.dataset.recentEdit)]);
  const found = e.target.closest("[data-add-found]");
  if (found) {
    const f = addResults[Number(found.dataset.addFound)];
    return openEditor({ title: "", items: [{ ...f, grams: startGrams(f), options: [f] }], comment: "" });
  }
  if (e.target.closest("[data-add-custom]")) return openCustomFood($("#add-q").value.trim());
  if (e.target.id === "recognize") recognizeNow();
});

sheetBody.addEventListener("input", (e) => {
  if (e.target.id === "add-q") searchFoodsInto(e.target.value, "#add-results", "add");
});

let addResults = [];
async function recognizeNow() {
  const note = $("#note")?.value.trim() || "";
  if (!pendingPhoto && !note) { $("#describe").open = true; $("#add-error").textContent = "Добавь фото или опиши еду словами"; return; }
  const form = new FormData();
  if (pendingPhoto) form.append("photo", pendingPhoto, "meal.jpg");
  form.append("note", note);
  stopVoice(true);
  sheetBody.innerHTML = '<div class="spinner"></div><p class="center muted">Смотрю на тарелку…</p>';
  try {
    const d = await api("/api/recognize", { method: "POST", form });
    if (state.me) state.me.spent_today = d.spent_today;
    if (!d.items.length) {
      openAdd();
      $("#add-error").textContent = d.comment || "Не нашёл еды на фото";
      return;
    }
    openEditor(d);
    if (!d.photo && pendingPhoto) { localPreview = { draft, url: URL.createObjectURL(pendingPhoto) }; renderEditor(); }
  } catch (err) {
    if (err.status === 401) { closeSheet(); return renderAuth(); }
    openAdd();
    $("#note").value = note;
    if (note) $("#describe").open = true;
    $("#add-error").textContent = err.message;
  }
}

// ---------- draft / meal editor ----------

let draft = null;

function openEditor(d) {
  draft = structuredClone(d);
  if (!draft.time) {
    const at = draft.eaten_at ? new Date(draft.eaten_at) : new Date();
    const isPast = !draft.id && !draft.today && state.day !== isoDate(new Date());
    draft.time = isPast ? "13:00" : at.toTimeString().slice(0, 5);
    draft.day = draft.eaten_at ? isoDate(at) : (draft.today ? isoDate(new Date()) : state.day);
  }
  renderEditor();
}

const MEAL_TYPES = [["Завтрак", "08:30"], ["Обед", "13:00"], ["Ужин", "19:00"], ["Перекус", null]];
const typeOf = (time) => { const h = Number(time.slice(0, 2)); return h < 11 ? "Завтрак" : h < 16 ? "Обед" : h < 22 ? "Ужин" : "Перекус"; };

const SOURCE_LABEL = { usda: "база USDA", off: "Open Food Facts", custom: "мой продукт", recipe: "моё блюдо", estimate: "оценка ИИ", manual: "вручную" };
// Where the grams start for a food just picked: last time's helping, one pack, a dish's usual portion, else 100 g.
const startGrams = (f) => r0(f.last_grams || f.package_grams || f.portion_grams || (f.source === "recipe" ? DISH_PORTION : 100));

// A packaged food is counted in packs: «1,5 уп.» of a 500 g can.
const PACK_COUNTS = [0.5, 1, 2, 3];
const packCount = (it) => (Number(it.grams) || 0) / it.package_grams;
const packLabel = (it) => `${String(Math.round(packCount(it) * 100) / 100).replace(".", ",")} уп. по ${r0(it.package_grams)} г`;
// Whoever logs a scanned or hand-added food first can say «this was the whole pack», for everyone.
const canSetPack = (it) => it.food_id != null && (it.source === "off" || it.source === "custom");

function multHtml(it, idx) {
  if (it.package_grams > 0) {
    return `<div class="mult">${PACK_COUNTS.map((k) => `<button data-pack="${k}" data-i="${idx}">${k === 0.5 ? "½" : k} уп.</button>`).join("")}
      <span class="small muted pack-count" data-pack-count="${idx}">${packLabel(it)}</span></div>`;
  }
  return `<div class="mult">${[["½", 0.5], ["×1,5", 1.5], ["×2", 2]].map(([l, k]) => `<button data-mult="${k}" data-i="${idx}">${l}</button>`).join("")}
    ${canSetPack(it) ? `<button class="set-pack" data-set-pack="${idx}">Это вся упаковка</button>` : ""}</div>`;
}

function itemHtml(it, idx) {
  const k = (Number(it.grams) || 0) / 100;
  const badge = it.source === "estimate" || it.source === "manual" ? "" : "db";
  const opts = (it.options || []).map((o, j) =>
    `<option value="${j}" ${o.food_id === it.food_id && o.source === it.source ? "selected" : ""}>${esc(o.source === "estimate" ? "Оценка ИИ" : o.name || o.name_en)} — ${r0(o.kcal_100)} ккал/100 г</option>`).join("");
  return `<div class="item" data-idx="${idx}">
    <div class="row between">
      <div class="grow">
        <input class="item-name" data-name="${idx}" value="${esc(it.name)}" aria-label="Название продукта">
        <div class="small muted">
          <span class="badge ${badge}">${SOURCE_LABEL[it.source] || it.source}</span>
          ${it.confidence === "low" ? '<span class="badge low">не уверен</span>' : ""}
          ${r0(it.kcal_100)} ккал/100 г
        </div>
      </div>
      <button class="ghost danger" data-del="${idx}" aria-label="Убрать">${ICONS.close}</button>
    </div>
    <div class="row between" style="margin-top:6px">
      <div class="grams">
        <button data-step="-10" data-i="${idx}">−</button>
        <input type="number" inputmode="numeric" pattern="[0-9]*" min="1" value="${r0(it.grams)}" data-grams="${idx}"> г
        <button data-step="10" data-i="${idx}">+</button>
      </div>
      <div class="small" data-item-total="${idx}">${itemTotals(it)}</div>
    </div>
    ${multHtml(it, idx)}
    ${opts ? `<select data-opt="${idx}">${opts}</select>` : ""}
  </div>`;
}

function itemTotals(it) {
  const t = totalsOf([it]);
  return `<b>${r0(t.kcal)} ккал</b> · Б ${r1(t.protein)} Ж ${r1(t.fat)} У ${r1(t.carbs)}`;
}

function renderEditor() {
  const photo = draft.photo ? `/api/photos/${encodeURIComponent(draft.photo)}` : localPreview?.draft === draft ? localPreview.url : "";
  openSheet(`
    <div class="row between"><h2>${draft.id ? "Редактирование" : "Проверь и поправь"}</h2><button class="ghost" data-close aria-label="Закрыть">${ICONS.close}</button></div>
    ${photo ? `<img class="preview" src="${esc(photo)}" alt="" decoding="async">` : ""}
    <div class="when">
      ${MEAL_TYPES.map(([name]) => `<button class="chip-btn ${(draft.title || typeOf(draft.time)) === name ? "on" : ""}" data-type="${name}">${name}</button>`).join("")}
      <input type="time" id="time" value="${draft.time}" aria-label="Время">
    </div>
    <input id="title" value="${esc(draft.title)}" placeholder="Название (необязательно)">
    ${draft.comment ? `<p class="small muted note">${ICONS.bulb}<span>${esc(draft.comment)}</span></p>` : ""}
    ${draft.personal_hints ? `<p class="small muted note">${ICONS.target}<span>Учтены твои прошлые правки порций</span></p>` : ""}
    <div class="card" style="margin-top:10px" id="items">${draft.items.map(itemHtml).join("") || '<p class="muted small">Добавь продукты через поиск ниже</p>'}</div>
    <div class="card">
      <input id="food-q" placeholder="Добавить продукт: поиск (рус./англ.)" autocomplete="off">
      <div class="search-results" id="food-results"></div>
    </div>
    ${draft.items.length >= 2 ? `<button class="block ghost-line with-ico as-dish" id="as-dish">${ICONS.pot}Сохранить как блюдо</button>` : ""}
    <div class="totals">
      <div class="row between"><b id="draft-total"></b><span class="small muted" id="draft-macros"></span></div>
      <button class="primary block" id="save" style="margin-top:8px">${draft.id ? "Сохранить" : "Съел — записать"}</button>
      ${draft.id ? '<button class="block" id="repeat" style="margin-top:8px">Записать ещё раз сегодня</button><button class="block danger" id="delete" style="margin-top:8px">Удалить приём пищи</button>' : ""}
    </div>`);
  updateDraftTotals();
  stashDraft();
}

function updateDraftTotals() {
  const t = totalsOf(draft.items);
  $("#draft-total").textContent = `${r0(t.kcal)} ккал`;
  $("#draft-macros").textContent = `Б ${r0(t.protein)} · Ж ${r0(t.fat)} · У ${r0(t.carbs)}`;
}

// The item list (itemHtml) is shared by the meal editor and the dish editor: one of them is open.
const editing = () => draft || dish;
const rerender = () => (draft ? renderEditor() : renderDish());

function setGrams(idx, grams) {
  const items = editing().items;
  items[idx].grams = Math.max(0, grams);
  const input = sheetBody.querySelector(`[data-grams="${idx}"]`);
  if (input && Number(input.value) !== items[idx].grams) input.value = r0(items[idx].grams);
  sheetBody.querySelector(`[data-item-total="${idx}"]`).innerHTML = itemTotals(items[idx]);
  const packs = sheetBody.querySelector(`[data-pack-count="${idx}"]`);
  if (packs) packs.textContent = packLabel(items[idx]);
  if (draft) updateDraftTotals(); else updateDishResult();
  stashDraft();
}

sheetBody.addEventListener("input", (e) => {
  const ed = editing();
  if (!ed) return;
  if (e.target.dataset.grams !== undefined) setGrams(Number(e.target.dataset.grams), Number(e.target.value) || 0);
  if (e.target.dataset.name !== undefined) ed.items[Number(e.target.dataset.name)].name = e.target.value;
  if (e.target.id === "food-q") searchFoods(e.target.value);
  if (draft) {
    if (e.target.id === "title") draft.title = e.target.value;
    if (e.target.id === "time") draft.time = e.target.value || draft.time;
    stashDraft();
  }
});

sheetBody.addEventListener("change", (e) => {
  if (!editing() || e.target.dataset.opt === undefined) return;
  const idx = Number(e.target.dataset.opt);
  const it = editing().items[idx];
  const o = it.options[Number(e.target.value)];
  // Same rule as the server's: a database value wins, else the model's estimate stays.
  const est = it.options.find((x) => x.source === "estimate");
  Object.assign(it, { package_grams: o.package_grams || it.package_grams || null, food_id: o.food_id, source: o.source, kcal_100: o.kcal_100, protein_100: o.protein_100, fat_100: o.fat_100, carbs_100: o.carbs_100,
    ...Object.fromEntries(EXTRA_KEYS.map((k) => [k, o[k] ?? est?.[k] ?? null])) });
  rerender();
});

let searchTimer, searchResults = [];
const searchFoods = (q) => searchFoodsInto(q, "#food-results", "editor");

function searchFoodsInto(q, sel, where) {
  clearTimeout(searchTimer);
  const box = $(sel);
  if (q.trim().length < 2) { box.innerHTML = ""; return; }
  searchTimer = setTimeout(async () => {
    try {
      const res = await api(`/api/foods/search?q=${encodeURIComponent(q)}`);
      if (where === "add") addResults = res; else searchResults = res;
      const attr = where === "add" ? "data-add-found" : "data-add-food";
      box.innerHTML = res.map((f, j) =>
        `<button ${attr}="${j}">${esc(f.name)}${f.source === "recipe" ? ' <span class="badge db">блюдо</span>' : ""} <span class="muted">${r0(f.kcal_100)} ккал/100 г${f.last_grams ? ` · обычно ${r0(f.last_grams)} г` : ""}</span></button>`).join("")
        + `<button class="with-ico" ${where === "add" ? "data-add-custom" : "data-custom"}="1">${ICONS.plus}Свой продукт «${esc(q)}»</button>`;
    } catch (err) { box.innerHTML = `<div class="error">${esc(err.message)}</div>`; }
  }, 250);
}

sheetBody.addEventListener("click", async (e) => {
  if (!editing()) return;
  const t = e.target.closest("button");
  if (!t) return;
  if (t.dataset.type) {
    const [name, time] = MEAL_TYPES.find(([n]) => n === t.dataset.type);
    if (time) draft.time = time;
    // The chip names the meal unless the user typed a name of their own.
    if (!draft.title || MEAL_TYPES.some(([n]) => n === draft.title)) draft.title = name === typeOf(draft.time) ? "" : name;
    renderEditor();
  } else if (t.dataset.mult) {
    const idx = Number(t.dataset.i);
    setGrams(idx, Math.round((Number(editing().items[idx].grams) || 0) * Number(t.dataset.mult)));
  } else if (t.dataset.step) {
    const idx = Number(t.dataset.i);
    setGrams(idx, (Number(editing().items[idx].grams) || 0) + Number(t.dataset.step));
  } else if (t.dataset.pack) {
    const idx = Number(t.dataset.i);
    setGrams(idx, Math.round(editing().items[idx].package_grams * Number(t.dataset.pack)));
  } else if (t.dataset.setPack !== undefined) {
    const it = editing().items[Number(t.dataset.setPack)];
    const grams = Number(it.grams) || 0;
    if (grams < 1) return;
    t.disabled = true;
    try {
      await api(`/api/foods/${it.food_id}/package`, { method: "PUT", json: { grams } });
      it.package_grams = grams;
      stashDraft();
      rerender();
    } catch (err) { t.disabled = false; toast(err.message); }
  } else if (t.dataset.del !== undefined) {
    editing().items.splice(Number(t.dataset.del), 1);
    rerender();
  } else if (t.dataset.addFood !== undefined) {
    const f = searchResults[Number(t.dataset.addFood)];
    editing().items.push({ ...f, grams: startGrams(f), options: [f] });
    rerender();
  } else if (t.dataset.custom) {
    openCustomFood($("#food-q").value.trim());
  } else if (t.id === "as-dish" && draft) {
    saveAsDish();
  } else if (t.id === "save") {
    saveDraft(t);
  } else if (t.id === "repeat") {
    repeatMeal(draft);
  } else if (t.id === "delete") {
    const id = draft.id;
    if (await deleteMeal(id) && draft?.id === id) { draft = null; closeSheet(); }
  }
});

async function saveDraft(btn) {
  const items = draft.items.filter((i) => Number(i.grams) > 0);
  if (!items.length) return toast("Добавь хотя бы один продукт");
  btn.disabled = true;
  const body = {
    title: draft.title || "",
    items: items.map((i) => ({
      name: i.name, grams: Number(i.grams), food_id: i.food_id ?? null, source: i.source,
      kcal_100: i.kcal_100, protein_100: i.protein_100, fat_100: i.fat_100, carbs_100: i.carbs_100, ...extrasOf(i),
      ai_grams: i.ai_grams ?? null,
    })),
  };
  // Sent only when there is one: an edit without "photo" keeps the meal's own.
  if (draft.photo) body.photo = draft.photo;
  // The day the meal belongs to (the viewed day for a new one) at the time in the editor.
  body.eaten_at = `${draft.day}T${draft.time}:00`;
  try {
    // A new meal may wait for the network; an edit of a saved one needs it.
    const r = draft.id ? await api(`/api/meals/${draft.id}`, { method: "PUT", json: body })
      : await quickWrite("/api/meals", { method: "POST", json: body, label: body.title || items[0].name });
    if (!draft.id) { dropStash(); state.day = draft.day; }
    draft = null; dropLocalPreview(); closeSheet(); if (!r?.queued) toast("Записано"); loadDay();
  } catch (err) { btn.disabled = false; toast(err.message); }
}

function openCustomFood(name, barcode = null, note = "") {
  const back = draft, backDish = dish;
  openSheet(`
    <div class="row between"><h2>Свой продукт</h2><button class="ghost" id="cf-back" aria-label="${back || backDish ? "Назад" : "Закрыть"}">${back || backDish ? ICONS.left : ICONS.close}</button></div>
    ${note ? `<p class="small">${esc(note)}</p>` : ""}
    <form id="cf">
      <label>Название</label><input name="name" value="${esc(name)}" required>
      <p class="small muted">Значения на 100 г — обычно есть на упаковке. Продукт сохранится, и его найдут все${barcode ? " — и по штрихкоду тоже" : ""}.</p>
      <div class="row"><div class="grow"><label>Ккал</label><input name="kcal_100" type="text" inputmode="decimal" autocomplete="off" required></div>
      <div class="grow"><label>Белки</label><input name="protein_100" type="text" inputmode="decimal" autocomplete="off" required></div></div>
      <div class="row"><div class="grow"><label>Жиры</label><input name="fat_100" type="text" inputmode="decimal" autocomplete="off" required></div>
      <div class="grow"><label>Углеводы</label><input name="carbs_100" type="text" inputmode="decimal" autocomplete="off" required></div></div>
      <label>Вес упаковки, г или мл</label><input name="package_grams" type="text" inputmode="decimal" autocomplete="off" placeholder="Необязательно: банка 500, батончик 50">
      <details class="describe cf-more">
        <summary>Ещё: клетчатка, сахар, соль</summary>
        <div class="row"><div class="grow"><label>Клетчатка</label><input name="fiber_100" type="text" inputmode="decimal" autocomplete="off"></div>
        <div class="grow"><label>Сахар</label><input name="sugar_100" type="text" inputmode="decimal" autocomplete="off"></div>
        <div class="grow"><label>Соль</label><input name="salt_100" type="text" inputmode="decimal" autocomplete="off"></div></div>
        <p class="small muted">Необязательно. Пустое поле — «не знаю», а не ноль.</p>
      </details>
      <button class="primary block" style="margin-top:14px">Добавить</button>
    </form>`);
  draft = null; // keep editor handlers quiet while this form is open
  dish = null;
  $("#cf-back").onclick = () => { if (back) { draft = back; renderEditor(); } else if (backDish) { dish = backDish; renderDish(); } else closeSheet(); };
  $("#cf").onsubmit = async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    try {
      const num = (v) => +String(v).replace(",", ".");
      const opt = (v) => (String(v ?? "").trim() === "" ? null : num(v));
      const food = await api("/api/foods", { method: "POST", json: {
        name: f.name, kcal_100: num(f.kcal_100), protein_100: num(f.protein_100), fat_100: num(f.fat_100), carbs_100: num(f.carbs_100),
        fiber_100: opt(f.fiber_100), sugar_100: opt(f.sugar_100), salt_100: opt(f.salt_100), package_grams: opt(f.package_grams), barcode } });
      if (back) { draft = back; draft.items.push({ ...food, grams: startGrams(food), options: [food] }); renderEditor(); }
      else if (backDish) { dish = backDish; dish.items.push({ ...food, grams: startGrams(food), options: [food] }); renderDish(); }
      else openEditor({ title: food.name, items: [{ ...food, grams: startGrams(food), options: [food] }], comment: "" });
    } catch (err) { toast(err.message); }
  };
}

// ---------- own dishes (recipes) ----------
// A dish is a food of the user's own whose numbers come from its raw ingredients and the
// weight of the finished pot; logging it is logging a food, with its usual portion.

const DISH_PORTION = 300; // grams, when the dish has no usual portion
const ADD_DISHES = 5; // shown in the add sheet before «Все блюда»
let dish = null; // the dish editor's state while it is open (draft is null then)
let myDishes = [];

const dishGrams = (d) => r0(d.portion_grams || DISH_PORTION);
const dishItem = (d) => ({ food_id: d.id, source: "recipe", name: d.name, kcal_100: d.kcal_100, protein_100: d.protein_100,
  fat_100: d.fat_100, carbs_100: d.carbs_100, ...extrasOf(d), portion_grams: d.portion_grams, grams: dishGrams(d), options: [] });

function dishRow(d, j) {
  const n = d.ingredient_count;
  return `<div class="recent">
    <button class="recent-main" data-dish="${j}">
      <span class="grow"><b>${esc(d.name)}</b>
        <span class="small muted">порция ${dishGrams(d)} г · ${n} ${plural(n, "продукт", "продукта", "продуктов")}</span></span>
      <span class="small"><b>${r0((d.kcal_100 * dishGrams(d)) / 100)}</b> ккал</span>
    </button>
    <button class="recent-edit" data-dish-edit="${j}" aria-label="Изменить блюдо «${esc(d.name)}»">${ICONS.pencil}</button>
  </div>`;
}

async function loadDishes() {
  try { myDishes = await api("/api/recipes"); store("dishes", myDishes); }
  catch (err) { myDishes = err.status === 0 ? readStored("dishes", []) : []; }
  const box = $("#dishes");
  if (!box || !myDishes.length) return;
  box.innerHTML = `<h3 class="sub">Мои блюда <span class="muted">— тап открывает порцию</span></h3>`
    + myDishes.slice(0, ADD_DISHES).map(dishRow).join("")
    + (myDishes.length > ADD_DISHES ? `<button class="block ghost-line" id="dish-all">Все блюда · ${myDishes.length}</button>` : "");
}

async function openDishList() {
  openSheet(`
    <div class="row between"><h2>Мои блюда</h2><button class="ghost" id="dishes-back" aria-label="Назад">${ICONS.left}</button></div>
    <div id="dish-list"><div class="skeleton"></div><div class="skeleton"></div></div>
    <button class="primary block with-ico center-ico" id="dish-new" style="margin-top:14px">${ICONS.plus}Новое блюдо</button>`);
  try { myDishes = await api("/api/recipes"); store("dishes", myDishes); } catch (err) {
    if (err.status === 0 && (myDishes = readStored("dishes", [])).length) { const box = $("#dish-list"); if (box) box.innerHTML = myDishes.map(dishRow).join(""); return; }
    const box = $("#dish-list");
    if (box) box.innerHTML = `<div class="error">${esc(err.message)}</div>`;
    return;
  }
  const box = $("#dish-list");
  if (!box) return;
  box.innerHTML = myDishes.length ? myDishes.map(dishRow).join("")
    : '<p class="muted small">Пока нет блюд. Сохрани суп, плов или запеканку один раз — дальше записывай порцию в одно касание.</p>';
}

// The dish at its usual portion in the meal editor: grams adjusted there, saved like any meal.
const logDish = (d) => openEditor({ title: d.name, items: [dishItem(d)], comment: "" });

function openDish(d, back) {
  draft = null;
  dish = { id: null, name: "", items: [], cooked_grams: null, portion_grams: null, note: "", photo: null, comment: "", error: "", ...d, back };
  renderDish();
}

async function editDish(id, back) {
  openSheet('<div class="spinner"></div>');
  try {
    const d = await api(`/api/recipes/${id}`);
    openDish({ id: d.id, name: d.name, cooked_grams: d.cooked_grams, portion_grams: d.portion_grams,
      items: d.items.map(({ id: _, ...i }) => ({ ...i, options: [] })) }, back);
  } catch (err) {
    if (err.status === 401) { closeSheet(); return renderAuth(); }
    back ? back() : closeSheet();
    toast(err.message);
  }
}

// A meal of several parts becomes a dish: what was eaten is one portion of it.
function saveAsDish() {
  const back = draft;
  const items = structuredClone(back.items.filter((i) => Number(i.grams) > 0));
  const sum = r0(items.reduce((s, i) => s + Number(i.grams), 0));
  openDish({ name: MEAL_TYPES.some(([n]) => n === back.title) ? "" : back.title || "", items, cooked_grams: sum, portion_grams: sum },
    () => { dish = null; draft = back; renderEditor(); });
}

const rawGrams = (d) => d.items.reduce((s, i) => s + (Number(i.grams) || 0), 0);

function dishPer100(d) {
  const weight = Number(d.cooked_grams) || rawGrams(d);
  if (!(weight > 0)) return null;
  const t = totalsOf(d.items);
  return { kcal: (t.kcal / weight) * 100, protein: (t.protein / weight) * 100, fat: (t.fat / weight) * 100, carbs: (t.carbs / weight) * 100 };
}

function updateDishResult() {
  const box = $("#dish-result");
  if (!box || !dish) return;
  const v = dishPer100(dish);
  const cooked = $("#dish-cooked");
  if (cooked) cooked.placeholder = rawGrams(dish) ? `${r0(rawGrams(dish))} — сумма продуктов` : "";
  if (!v) { box.innerHTML = '<span class="muted small">Добавь продукты — посчитаю блюдо</span>'; return; }
  const portion = dishGrams(dish);
  box.innerHTML = `<div><b>На 100 г: ${r0(v.kcal)} ккал</b> <span class="muted small">· Б ${r0(v.protein)} · Ж ${r0(v.fat)} · У ${r0(v.carbs)}</span></div>
    <div class="small muted">Порция ${portion} г: <b class="ink">${r0((v.kcal * portion) / 100)} ккал</b>${dish.portion_grams ? "" : " (обычная порция не задана)"}</div>`;
}

function renderDish() {
  const d = dish;
  openSheet(`
    <div class="row between"><h2>${d.id ? "Блюдо" : "Новое блюдо"}</h2>
      <button class="ghost" id="dish-back" aria-label="${d.back ? "Назад" : "Закрыть"}">${d.back ? ICONS.left : ICONS.close}</button></div>
    <input id="dish-name" value="${esc(d.name)}" placeholder="Название, например «Борщ»" aria-label="Название блюда" maxlength="200" autocomplete="off">
    <details class="card dish-ai" ${d.items.length ? "" : "open"}>
      <summary class="with-ico">${ICONS.sparkle}Собрать по описанию или фото</summary>
      <textarea id="dish-note" placeholder="Опиши рецепт: продукты и сколько" maxlength="2000" aria-label="Опиши рецепт: продукты и сколько">${esc(d.note)}</textarea>
      ${d.photo ? `<div class="row small dish-photo"><span class="li-ico">${ICONS.image}</span><span class="grow">Фото добавлено</span>
        <button class="ghost" id="dish-photo-drop" aria-label="Убрать фото">${ICONS.close}</button></div>` : ""}
      <div class="row voice-row">
        <label class="btn icon-pick" aria-label="${d.photo ? "Заменить фото" : "Добавить фото ингредиентов или рецепта"}">${ICONS.camera}<input type="file" accept="image/*" data-dish-photo></label>
        ${micButton("dish-note")}
        <button class="primary grow" id="dish-ai">Собрать</button>
      </div>
      <div class="error" id="dish-error">${esc(d.error)}</div>
    </details>
    ${d.comment ? `<p class="small muted note">${ICONS.bulb}<span>${esc(d.comment)}</span></p>` : ""}
    <h3 class="sub">Продукты <span class="muted">— сырые, на всё блюдо</span></h3>
    <div class="card" id="items">${d.items.map(itemHtml).join("") || '<p class="muted small">Найди продукты ниже или собери их из описания</p>'}</div>
    <div class="card">
      <input id="food-q" placeholder="Добавить продукт: поиск (рус./англ.)" autocomplete="off" aria-label="Добавить продукт">
      <div class="search-results" id="food-results"></div>
    </div>
    <div class="card">
      <label for="dish-cooked" style="margin-top:0">Вес готового блюда, г</label>
      <input id="dish-cooked" type="number" inputmode="numeric" min="1" value="${d.cooked_grams ? r0(d.cooked_grams) : ""}">
      <p class="small muted" style="margin:6px 0 0">Взвесь кастрюлю с едой и вычти вес пустой. Пусто — считаю по сумме продуктов</p>
      <label for="dish-portion">Обычная порция, г</label>
      <input id="dish-portion" type="number" inputmode="numeric" min="1" value="${d.portion_grams ? r0(d.portion_grams) : ""}" placeholder="${DISH_PORTION}">
    </div>
    <div class="totals">
      <div id="dish-result"></div>
      <button class="primary block" id="dish-save" style="margin-top:8px">Сохранить блюдо</button>
      ${d.id ? '<button class="block danger" id="dish-delete" style="margin-top:8px">Удалить блюдо</button>' : ""}
    </div>`);
  updateDishResult();
}

function dishBack() {
  const back = dish?.back;
  dish = null;
  back ? back() : closeSheet();
}

async function buildDishWithAi() {
  const mine = dish;
  mine.note = $("#dish-note")?.value.trim() || "";
  if (!mine.note && !mine.photo) { mine.error = "Опиши рецепт словами или добавь фото"; return renderDish(); }
  if (mine.items.length && !confirm("Заменить продукты в списке собранными заново?")) return;
  const form = new FormData();
  if (mine.photo) form.append("photo", mine.photo, "recipe.jpg");
  form.append("note", mine.note);
  stopVoice(true);
  sheetBody.innerHTML = '<div class="spinner"></div><p class="center muted">Собираю рецепт…</p>';
  try {
    const r = await api("/api/recipes/draft", { method: "POST", form });
    if (state.me) state.me.spent_today = r.spent_today;
    if (dish !== mine) return; // the sheet was closed meanwhile
    if (!r.items.length) mine.error = r.comment || "Не нашёл в описании продуктов";
    else {
      Object.assign(mine, { items: r.items, comment: r.comment, error: "", photo: null });
      if (!mine.name.trim()) mine.name = r.title;
      if (r.cooked_grams) mine.cooked_grams = r.cooked_grams;
    }
  } catch (err) {
    if (err.status === 401) { closeSheet(); return renderAuth(); }
    if (dish !== mine) return;
    mine.error = err.message;
  }
  renderDish();
}

async function saveDish(btn) {
  const d = dish;
  const items = d.items.filter((i) => Number(i.grams) > 0);
  if (!d.name.trim()) { toast("Назови блюдо"); return $("#dish-name")?.focus(); }
  if (!items.length) return toast("Добавь хотя бы один продукт");
  const num = (v) => (Number(v) > 0 ? Number(v) : null);
  const body = {
    name: d.name.trim(), cooked_grams: num(d.cooked_grams), portion_grams: num(d.portion_grams),
    items: items.map((i) => ({ name: i.name, grams: Number(i.grams), food_id: i.food_id ?? null, source: i.source,
      kcal_100: i.kcal_100, protein_100: i.protein_100, fat_100: i.fat_100, carbs_100: i.carbs_100, ...extrasOf(i) })),
  };
  btn.disabled = true;
  try {
    await api(d.id ? `/api/recipes/${d.id}` : "/api/recipes", { method: d.id ? "PUT" : "POST", json: body });
    toast(d.id ? "Блюдо сохранено" : "Блюдо сохранено — найдёшь его в «Мои блюда»");
    dishBack();
  } catch (err) { btn.disabled = false; toast(err.message); }
}

async function deleteDish(btn) {
  if (!confirm("Удалить это блюдо? Уже записанные приёмы пищи останутся.")) return;
  btn.disabled = true;
  try { await api(`/api/recipes/${dish.id}`, { method: "DELETE" }); toast("Блюдо удалено"); dishBack(); }
  catch (err) { btn.disabled = false; toast(err.message); }
}

sheetBody.addEventListener("input", (e) => {
  if (!dish) return;
  const id = e.target.id;
  if (id === "dish-name") dish.name = e.target.value;
  else if (id === "dish-note") dish.note = e.target.value;
  else if (id === "dish-cooked" || id === "dish-portion") {
    dish[id === "dish-cooked" ? "cooked_grams" : "portion_grams"] = Number(e.target.value) > 0 ? Number(e.target.value) : null;
    updateDishResult();
  }
});

sheetBody.addEventListener("click", (e) => {
  if (!dish) return;
  const t = e.target.closest("button");
  if (!t) return;
  if (t.id === "dish-back") dishBack();
  else if (t.id === "dish-ai") buildDishWithAi();
  else if (t.id === "dish-save") saveDish(t);
  else if (t.id === "dish-delete") deleteDish(t);
  else if (t.id === "dish-photo-drop") { dish.photo = null; renderDish(); }
});

// Opening a dish from the add sheet or the full list, and coming back to where it was opened.
sheetBody.addEventListener("click", (e) => {
  if (dish || draft) return;
  const fromList = !!$("#dish-list");
  const back = fromList ? openDishList : openAdd;
  const row = e.target.closest("[data-dish]");
  if (row) return logDish(myDishes[Number(row.dataset.dish)]);
  const edit = e.target.closest("[data-dish-edit]");
  if (edit) return editDish(myDishes[Number(edit.dataset.dishEdit)].id, back);
  if (e.target.closest("#dish-all")) return openDishList();
  if (e.target.closest("#dishes-back")) return openAdd();
  if (e.target.closest("#dish-new")) return openDish({}, back);
});

// ---------- voice input ----------
// The browser's own speech recognition, where there is one: nothing is recorded or sent by us.
// Without it there is no button; the keyboard's dictation still works.

const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
let voiceDenied = false; // no microphone access: the buttons stay hidden for the session
let voice = null; // { rec, btn, ta }

const micButton = (targetId) => (SpeechRec && !voiceDenied
  ? `<button type="button" class="mic" data-mic="${targetId}" aria-label="Диктовать" aria-pressed="false">${ICONS.mic}</button>` : "");

function setMicState(btn, on) {
  btn.classList.toggle("on", on);
  btn.setAttribute("aria-pressed", String(on));
  btn.setAttribute("aria-label", on ? "Остановить диктовку" : "Диктовать");
}

// abort drops what is still being recognized (the field is going away); stop keeps it.
function stopVoice(abort = false) {
  if (!voice) return;
  const v = voice;
  voice = null;
  setMicState(v.btn, false);
  try { abort ? v.rec.abort() : v.rec.stop(); } catch { /* already ended */ }
}

function startVoice(btn) {
  const ta = document.getElementById(btn.dataset.mic);
  if (!ta || ta.disabled) return;
  const rec = new SpeechRec();
  rec.lang = "ru-RU";
  rec.interimResults = true;
  rec.continuous = false;
  // Appended to what is already typed; interim words show live and are replaced by the final ones.
  const base = ta.value;
  const sep = base && !/\s$/.test(base) ? " " : "";
  const v = { rec, btn, ta };
  rec.onresult = (e) => {
    if (!ta.isConnected) return stopVoice(true);
    const said = Array.from(e.results, (r) => r[0].transcript).join("").trim();
    ta.value = base + (said ? sep + said : "");
    ta.dispatchEvent(new Event("input", { bubbles: true }));
  };
  rec.onerror = (e) => {
    if (e.error === "not-allowed" || e.error === "service-not-allowed") {
      voiceDenied = true;
      document.querySelectorAll("[data-mic]").forEach((b) => b.remove());
      toast("Нет доступа к микрофону");
    } else if (e.error === "network") toast("Диктовка не работает без сети");
    // no-speech, aborted: just stop
  };
  rec.onend = () => { if (voice === v) { voice = null; setMicState(btn, false); } };
  try { rec.start(); } catch { return; }
  voice = v;
  setMicState(btn, true);
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-mic]");
  if (!btn) return;
  e.preventDefault();
  if (voice?.btn === btn) return stopVoice();
  stopVoice(true);
  startVoice(btn);
});
document.addEventListener("visibilitychange", () => { if (document.visibilityState !== "visible") stopVoice(true); });

// ---------- barcode ----------

let scanStream = null, scanTimer = null;
function stopScan() {
  clearTimeout(scanTimer);
  scanStream?.getTracks().forEach((t) => t.stop());
  scanStream = null;
}

const ZXING_SRC = "/static/vendor/zxing-0.23.0.min.js";
let zxingLoading = null;
function loadZxing() {
  if (window.ZXing) return Promise.resolve();
  zxingLoading ??= new Promise((ok, fail) => {
    const el = document.createElement("script");
    el.src = ZXING_SRC;
    el.onload = ok;
    el.onerror = () => { zxingLoading = null; fail(new Error("zxing")); };
    document.head.appendChild(el);
  });
  return zxingLoading;
}

// A function that reads one video frame: the browser's own detector where there is one,
// else zxing on a canvas (Safari on iPhone has no BarcodeDetector).
async function frameReader() {
  if ("BarcodeDetector" in window) {
    const formats = (await BarcodeDetector.getSupportedFormats()).filter((f) => ["ean_13", "ean_8", "upc_a", "upc_e"].includes(f));
    if (formats.length) {
      const detector = new BarcodeDetector({ formats });
      return async (video) => (await detector.detect(video))[0]?.rawValue || null;
    }
  }
  await loadZxing();
  const Z = window.ZXing;
  const reader = new Z.MultiFormatReader();
  reader.setHints(new Map([
    [Z.DecodeHintType.POSSIBLE_FORMATS, [Z.BarcodeFormat.EAN_13, Z.BarcodeFormat.EAN_8, Z.BarcodeFormat.UPC_A, Z.BarcodeFormat.UPC_E]],
    [Z.DecodeHintType.TRY_HARDER, true],
  ]));
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  return async (video) => {
    const w = video.videoWidth, h = video.videoHeight;
    if (!w || !h) return null;
    // The middle band of the frame, where the guide line is, at most 800 px wide.
    const bandH = Math.round(h * 0.4), scale = Math.min(1, 800 / w);
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(bandH * scale);
    ctx.drawImage(video, 0, Math.round((h - bandH) / 2), w, bandH, 0, 0, canvas.width, canvas.height);
    try {
      const bitmap = new Z.BinaryBitmap(new Z.HybridBinarizer(new Z.HTMLCanvasElementLuminanceSource(canvas)));
      return reader.decodeWithState(bitmap).getText();
    } catch {
      return null; // nothing readable in this frame
    }
  };
}

async function openScanner() {
  const live = !!navigator.mediaDevices?.getUserMedia;
  openSheet(`
    <div class="row between"><h2>Штрихкод</h2><button class="ghost" data-close aria-label="Закрыть">${ICONS.close}</button></div>
    ${live ? '<div class="scan-box"><video id="scan-video" playsinline muted autoplay></video><i class="scan-line"></i></div><p class="small muted center">Наведи камеру на штрихкод</p>' : ""}
    <form id="code-form" class="row" style="margin-top:8px">
      <input name="code" inputmode="numeric" autocomplete="off" placeholder="${live ? "Или введи цифры под штрихкодом" : "Цифры под штрихкодом"}" required>
      <button class="primary">Найти</button>
    </form>
    <div class="error" id="scan-error"></div>`);
  $("#code-form").onsubmit = (e) => { e.preventDefault(); findBarcode(e.target.code.value); };
  if (!live) return;
  try {
    const [read, stream] = await Promise.all([
      frameReader(),
      navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: { ideal: 1280 } } }),
    ]);
    scanStream = stream;
    const video = $("#scan-video");
    if (!video) return stopScan(); // the sheet was closed while the camera started
    video.srcObject = stream;
    await video.play();
    const tick = async () => {
      if (!scanStream) return;
      let code = null;
      try { code = await read(video); } catch { /* a frame that couldn't be read */ }
      if (code) { stopScan(); navigator.vibrate?.(60); return findBarcode(code); }
      scanTimer = setTimeout(tick, 150);
    };
    tick();
  } catch {
    stopScan();
    $(".scan-box")?.remove();
    const err = $("#scan-error");
    if (err) err.textContent = "Камера недоступна — введи цифры вручную";
  }
}

async function findBarcode(raw) {
  const code = String(raw).replace(/\D/g, "");
  if (code.length < 6) { $("#scan-error").textContent = "Нужны цифры под штрихкодом"; return; }
  stopScan();
  sheetBody.innerHTML = '<div class="spinner"></div><p class="center muted">Ищу продукт…</p>';
  try {
    const food = await api(`/api/foods/barcode/${code}`);
    openEditor({ title: food.name, items: [{ ...food, grams: startGrams(food), options: [food] }], comment: food.package_grams || food.last_grams ? "" : "Проверь вес порции — по умолчанию 100 г." });
  } catch (err) {
    if (err.status === 401) { closeSheet(); return renderAuth(); }
    const note = err.status === 404
      ? `Штрихкода ${code} ещё нет в базе. Перепиши значения с этикетки — в следующий раз продукт найдётся сразу.`
      : err.message;
    openCustomFood("", code, note);
  }
}

// ---------- settings & admin ----------

// The profile is a list of sections; each opens as a sub-screen at #profile/<key>,
// so the browser's Back returns to the list.
const IS_ANDROID = /Android/i.test(navigator.userAgent);
const PROFILE_SECTIONS = {
  goals: { icon: "target", title: "Норма и цель" },
  water: { icon: "drop", title: "Вода" },
  health: { icon: "heart", title: "Apple Health" },
  notify: { icon: "bell", title: "Уведомления" },
  data: { icon: "download", title: "Данные" },
  admin: { icon: "shield", title: "Админка", admin: true },
};
const profileHash = () => location.hash.match(/^#profile\/(\w+)/)?.[1] || null;

function leaveProfileHash() {
  if (profileHash()) history.replaceState(null, "", "/");
}

function openProfileSection(key) {
  if (!PROFILE_SECTIONS[key]) return;
  history.pushState({ profileSub: key }, "", `#profile/${key}`);
  renderProfileScreen(key);
}

function profileBack() {
  // Our own entry sits on top of the list: going back keeps the history tidy.
  if (history.state?.profileSub) return history.back();
  history.replaceState(null, "", "/");
  renderProfileScreen(null);
}

window.addEventListener("popstate", () => {
  if (state.view !== "profile") return;
  if (!sheet.hidden) closeSheet();
  loadProfileView(profileHash());
});

async function loadProfileView(section = null) {
  if (!section) leaveProfileHash();
  showTabs("profile");
  showSkeleton("profile");
  state.view = "profile";
  try {
    [state.me, state.profile] = await Promise.all([api("/api/me"), api("/api/profile")]);
  } catch (err) {
    if (err.status === 401) return renderAuth();
    app.innerHTML = `<div class="card error">${esc(err.message)}</div>`;
    return;
  }
  if (state.view !== "profile") return;
  renderProfileScreen(section);
}

function profileValue(key, me, p) {
  const g = me.goals, n = p.notify;
  if (key === "goals") return `${g.kcal} ккал · Б ${g.protein} · Ж ${g.fat} · У ${g.carbs}`;
  if (key === "water") return p.track_water ? `${kgFmt(p.water_goal_ml / 1000)} л в день` : "выключено";
  if (key === "health") return p.health.connected ? "подключено" : "не подключено";
  if (key === "notify") {
    const on = [n.evening ? `вечером в ${n.hour}:00` : "", n.weekly ? "обзор недели" : "", n.share_streak ? "серии" : ""].filter(Boolean);
    return on.length ? on.join(" · ") : "выключены";
  }
  if (key === "data") return "выгрузка в CSV";
  return "пользователи, приглашения, лимиты";
}

function renderProfileScreen(section) {
  const me = state.me, p = state.profile;
  if (!me || !p) return loadProfileView(section);
  state.profileSection = section && PROFILE_SECTIONS[section] && (!PROFILE_SECTIONS[section].admin || me.is_admin) ? section : null;
  showTabs("profile");
  if (!state.profileSection) return renderProfileList(me, p);
  const key = state.profileSection;
  const body = {
    goals: () => `
      <form class="card" id="goals">
        <b>Норма в день</b>
        <div class="row"><div class="grow"><label>Ккал</label><input name="kcal" type="number" inputmode="numeric" value="${me.goals.kcal}"></div>
        <div class="grow"><label>Белки, г</label><input name="protein" type="number" inputmode="numeric" value="${me.goals.protein}"></div></div>
        <div class="row"><div class="grow"><label>Жиры, г</label><input name="fat" type="number" inputmode="numeric" value="${me.goals.fat}"></div>
        <div class="grow"><label>Углеводы, г</label><input name="carbs" type="number" inputmode="numeric" value="${me.goals.carbs}"></div></div>
        <button class="primary block" style="margin-top:12px">Сохранить</button>
      </form>
      <div class="card" id="profile-card"></div>`,
    water: () => '<div class="card" id="water-card"></div>',
    health: () => '<div class="card" id="health-card"></div>',
    notify: () => '<div class="card" id="notify-card"></div>',
    // A plain link: the session cookie goes with it, and an iPhone home-screen app can download it.
    data: () => `<div class="card">
        <b>Выгрузка дневника</b>
        <p class="muted small" style="margin:4px 0 12px">Архив с таблицами CSV: все приёмы пищи по продуктам (граммы, КБЖУ, клетчатка, сахар, соль), вес, вода и активность. Открывается в Excel, Numbers и Google Таблицах.</p>
        <a class="btn primary block with-ico" href="/api/export" download>${ICONS.download}Скачать архив CSV</a>
      </div>`,
    admin: () => '<div class="card" id="admin"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div>',
  }[key]();
  app.innerHTML = `${topbar(PROFILE_SECTIONS[key].title, me.username, "", "profile-back")}${body}`;
  window.scrollTo(0, 0);
  if (key === "goals") {
    $("#goals").onsubmit = async (e) => {
      e.preventDefault();
      const f = Object.fromEntries(new FormData(e.target));
      try {
        state.me = await api("/api/me", { method: "PUT", json: { kcal: +f.kcal, protein: +f.protein, fat: +f.fat, carbs: +f.carbs, timezone: TZ } });
        toast("Норма сохранена");
      } catch (err) { toast(err.message); }
    };
  }
  if (key === "admin") renderAdmin();
  else renderProfile(p);
}

function renderProfileList(me, p) {
  const row = (key) => {
    const s = PROFILE_SECTIONS[key];
    return `<button class="lrow" data-act="profile:${key}">
      <span class="lrow-ico">${ICONS[s.icon]}</span>
      <span class="grow"><span class="lrow-title">${s.title}</span><span class="lrow-value">${esc(profileValue(key, me, p))}</span></span>
      <span class="lrow-chev">${ICONS.right}</span>
    </button>`;
  };
  app.innerHTML = `
    ${topbar("Профиль", me.username, "")}
    <div class="card list">${Object.keys(PROFILE_SECTIONS).filter((k) => !PROFILE_SECTIONS[k].admin || me.is_admin).map(row).join("")}</div>
    <div class="card list">
      ${p.android_apk ? `<button class="lrow" id="android-apk"><span class="lrow-ico">${ICONS.download}</span><span class="grow"><span class="lrow-title">Приложение для Android</span><span class="lrow-value">${IS_ANDROID ? "Скачать APK" : "Отправить ссылку на APK"}</span></span></button>` : ""}
      <button class="lrow" id="replay-onboarding"><span class="lrow-ico">${ICONS.replay}</span><span class="grow"><span class="lrow-title">Пройти первые шаги заново</span></span></button>
      <button class="lrow danger" id="logout"><span class="lrow-ico">${ICONS.logout}</span><span class="grow"><span class="lrow-title">Выйти</span></span></button>
    </div>
    <p class="muted small center">Распознавания и ИИ сегодня: $${me.spent_today.toFixed(2)} из $${me.daily_limit.toFixed(2)}</p>`;
  // The offline queue stays (it is per user and goes out after this user's next sign-in); the device forgets who was here.
  $("#logout").onclick = async () => {
    await api("/api/auth/logout", { method: "POST" });
    try { localStorage.removeItem("ft-me"); } catch { /* ignore */ }
    state.me = null; renderAuth();
  };
  $("#replay-onboarding").onclick = () => renderOnboarding(1);
  // On Android the file itself; anywhere else the link, to pass on to a friend with an Android phone.
  if ($("#android-apk")) $("#android-apk").onclick = async () => {
    const url = location.origin + "/app.apk";
    if (IS_ANDROID) { location.href = url; return; }
    try {
      if (navigator.share) await navigator.share({ title: "КБЖУ по фото для Android", url });
      else { await navigator.clipboard.writeText(url); toast("Ссылка на APK скопирована"); }
    } catch { /* the share sheet was closed */ }
  };
}

// ---------- coach ----------

const COACH_STARTERS = ["Что съесть на ужин, чтобы добрать белок?", "Оцени мой сегодняшний день", "Запиши: омлет из 3 яиц и кофе с молоком", "Запиши тренировку: 40 минут в зале",
  "Почему вес стоит на месте?", "Как мне меньше есть вечером?"];

// What the coach proposes to write: a card under its reply; nothing is saved until the tap.
const ACTION_UI = {
  log_meal: { icon: "plate", title: "Записать приём пищи" },
  set_goals: { icon: "target", title: "Изменить норму" },
  add_water: { icon: "drop", title: "Добавить воду" },
  log_weight: { icon: "scale", title: "Записать вес" },
  log_workout: { icon: "dumbbell", title: "Записать тренировку" },
  log_activity: { icon: "steps", title: "Записать активность" },
};

function actionBody(a) {
  const s = a.summary;
  const macros = (t) => `Б ${r0(t.protein)} · Ж ${r0(t.fat)} · У ${r0(t.carbs)}`;
  if (a.type === "log_meal") {
    return `<div class="act-sub">${esc(s.title || "Приём пищи")}${s.time ? ` · ${esc(s.time)}` : ""}</div>
      <ul class="act-items">${s.items.map((i) => `<li><span>${esc(i.name)}</span><span class="muted">${r0(i.grams)} г</span></li>`).join("")}</ul>
      <div class="act-total"><b>${r0(s.totals.kcal)} ккал</b> · ${macros(s.totals)}</div>`;
  }
  if (a.type === "set_goals") {
    return `<div class="act-total">${r0(s.old.kcal)} → <b>${r0(s.new.kcal)} ккал</b> · ${macros(s.new)}</div>
      ${s.reason ? `<p class="small muted act-reason">${esc(s.reason)}</p>` : ""}`;
  }
  if (a.type === "add_water") return `<div class="act-total"><b>+${r0(s.ml)} мл</b></div>`;
  if (a.type === "log_weight") return `<div class="act-total"><b>${kgFmt(s.kg)} кг</b></div>`;
  if (a.type === "log_workout") {
    const rest = [s.distance_km != null && `${gFmt(s.distance_km)} км`, s.kcal != null && `${fmtInt(s.kcal)} ккал`,
      s.avg_hr != null && `пульс ${r0(s.avg_hr)}`].filter(Boolean);
    return `<div class="act-sub">${esc(s.kind)}${s.time ? ` · ${esc(s.time)}` : ""}</div>
      <div class="act-total"><b>${r0(s.minutes)} мин</b>${rest.map((x) => ` · ${x}`).join("")}</div>`;
  }
  if (a.type === "log_activity") {
    return `<div class="act-total">${[
      s.steps != null && `<b>${fmtInt(s.steps)}</b> ${plural(r0(s.steps), "шаг", "шага", "шагов")}`,
      s.active_kcal != null && `<b>${fmtInt(s.active_kcal)}</b> ккал активности`].filter(Boolean).join(" · ")}</div>`;
  }
  return "";
}

function actionCard(m, a) {
  const ui = ACTION_UI[a.type];
  if (!ui) return ""; // a type an old cached shell doesn't know
  const foot = a.status === "pending"
    ? `<div class="act-btns"><button class="primary grow" data-action-go="1">Записать</button><button class="grow" data-action-go="0">Не надо</button></div>`
    : a.status === "done"
      ? `<div class="act-status"><span class="with-ico">${ICONS.check}Записано в дневник</span>
          <button class="link" data-action-day="${esc(a.summary.date || isoDate(new Date()))}">Открыть день</button></div>`
      : '<div class="act-status"><span>Отклонено</span></div>';
  return `<div class="act ${a.status}" data-msg="${m.id}" data-action="${esc(a.id)}">
    <div class="act-head"><span class="act-ico">${ICONS[ui.icon]}</span><b>${ui.title}</b></div>
    ${actionBody(a)}${foot}
  </div>`;
}

async function decideAction(card, accept) {
  const msgId = Number(card.dataset.msg), actionId = card.dataset.action;
  card.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    const m = await api(`/api/coach/actions/${msgId}/${encodeURIComponent(actionId)}`, { method: "POST", json: { accept } });
    const list = state.coach?.messages || [];
    const i = list.findIndex((x) => x.id === msgId);
    if (i >= 0) list[i] = m;
    const a = m.actions.find((x) => x.id === actionId);
    if (a && card.isConnected) card.outerHTML = actionCard(m, a);
    if (a?.status === "done") {
      // The diary and the norm changed underneath: the other tabs load fresh, the header numbers too.
      state.dayData = null; state.stats = null; state.profile = null;
      api("/api/me").then((me) => (state.me = me)).catch(() => {});
    }
  } catch (err) {
    if (err.status === 401) return renderAuth();
    card.querySelectorAll("button").forEach((b) => (b.disabled = false));
    toast(err.message);
  }
}

app.addEventListener("click", (e) => {
  if (state.view !== "coach") return;
  const go = e.target.closest("[data-action-go]");
  if (go) return decideAction(go.closest(".act"), go.dataset.actionGo === "1");
  const day = e.target.closest("[data-action-day]");
  if (day) { state.day = day.dataset.actionDay; loadDay(); }
});

// Coach replies use **bold** and "- " lists; everything else is plain text.
function coachHtml(text) {
  const lines = esc(text).split("\n");
  let html = "", inList = false;
  for (const raw of lines) {
    const line = raw.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
    const item = line.match(/^\s*[-•]\s+(.*)/);
    if (item) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${item[1]}</li>`; continue; }
    if (inList) { html += "</ul>"; inList = false; }
    if (line.trim()) html += `<p>${line}</p>`;
  }
  return html + (inList ? "</ul>" : "");
}

async function loadCoach() {
  leaveProfileHash();
  showTabs("coach");
  showSkeleton("coach");
  state.view = "coach";
  try { state.coach = await api("/api/coach"); } catch (err) {
    if (err.status === 401) return renderAuth();
    app.innerHTML = `${topbar("Коуч", "чат о питании и привычках", "")}<div class="card error">${esc(err.message)}</div>`;
    return;
  }
  renderCoach();
}

// A screenshot to go with the next message: shrunk on the phone, sent once, never kept on the server.
let coachImage = null; // { data: data: URL, url: object URL for the preview }
const MAX_COACH_IMAGE = 2_400_000; // the server takes up to 2.5 MB of base64

function dropCoachImage() {
  if (coachImage) URL.revokeObjectURL(coachImage.url);
  coachImage = null;
}

// What a picture is usually for: one tap sends it with the question.
const ATTACH_ASKS = [["Что взять из меню?", "Что взять из этого меню под мой остаток на сегодня?"], ["Запиши это", "Запиши это"]];
const attachChip = () => coachImage
  ? `<span class="chip attach-chip"><img src="${coachImage.url}" alt="">Фото
      <button type="button" class="ghost" data-attach-drop aria-label="Убрать фото">${ICONS.close}</button></span>
      ${ATTACH_ASKS.map(([label], j) => `<button type="button" class="chip attach-ask" data-attach-ask="${j}">${label}</button>`).join("")}` : "";

const imageMark = `<span class="bubble-img">${ICONS.image}фото</span>`;
const userBubble = (text, hasImage) => `<div class="bubble user">${hasImage ? imageMark : ""}${text ? `<p>${esc(text)}</p>` : ""}</div>`;

async function attachCoachImage(file) {
  const blob = await shrinkPhoto(file, 1568); // the small print of a menu needs more pixels than a plate
  const data = await new Promise((ok, fail) => {
    const r = new FileReader();
    r.onload = () => ok(r.result);
    r.onerror = () => fail(r.error);
    r.readAsDataURL(blob);
  }).catch(() => null);
  if (!data || data.length > MAX_COACH_IMAGE) return toast(data ? "Картинка слишком большая" : "Не получилось прочитать картинку");
  dropCoachImage();
  coachImage = { data, url: URL.createObjectURL(blob) };
  const slot = $("#coach-attach");
  if (slot) slot.innerHTML = attachChip();
}

// pending: the message on its way, { text, image }.
function renderCoach(pending = null) {
  stopVoice(true);
  const c = state.coach;
  const left = Math.max(0, c.per_day - c.used_today);
  const open = left && !pending;
  const bubbles = c.messages.map((m) => (m.role === "assistant" ? `<div class="bubble assistant">${coachHtml(m.text)}</div>` : userBubble(m.text, m.has_image))
    + (m.actions || []).map((a) => actionCard(m, a)).join("")).join("");
  app.innerHTML = `
    ${topbar("Коуч", left ? `осталось ${left} ${plural(left, "сообщение", "сообщения", "сообщений")} сегодня` : "сообщения на сегодня закончились",
      c.messages.length ? `<button class="icon" data-act="coach-clear" aria-label="Очистить чат">${ICONS.trash}</button>` : "")}
    <div class="chat" id="chat">
      ${c.messages.length ? bubbles : `<div class="empty"><div class="empty-ico">${ICONS.chat}</div><b>Спроси что угодно о питании</b>
        <p>Коуч видит твой дневник: норму, что съедено сегодня, неделю, вес, тренировки и серии. Можно приложить фото меню в кафе — подскажет, что взять, или скриншот тренировки из приложения часов — предложит её записать.</p></div>
        <div class="starters">${COACH_STARTERS.map((q, j) => `<button class="chip" data-starter="${j}">${esc(q)}</button>`).join("")}</div>`}
      ${pending ? `${userBubble(pending.text, pending.image)}<div class="bubble assistant typing"><i></i><i></i><i></i></div>` : ""}
    </div>
    <form class="composer" id="coach-form">
      <div id="coach-attach">${open ? attachChip() : ""}</div>
      ${open ? `<label class="btn icon-pick" aria-label="Приложить фото или скриншот">${ICONS.image}<input type="file" accept="image/*" id="coach-file"></label>` : ""}
      <textarea id="coach-text" rows="1" placeholder="${left ? "Напиши коучу…" : "Продолжим завтра"}" ${open ? "" : "disabled"} maxlength="2000"></textarea>
      ${open ? micButton("coach-text") : ""}
      <button class="primary" ${open ? "" : "disabled"} aria-label="Отправить">${ICONS.up}</button>
    </form>
    <div id="coach-error" class="error"></div>`;
  window.scrollTo(0, document.body.scrollHeight);
  const ta = $("#coach-text");
  ta.oninput = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 140) + "px"; };
  ta.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing && matchMedia("(hover: hover)").matches) { e.preventDefault(); $("#coach-form").requestSubmit(); } };
  $("#coach-form").onsubmit = (e) => { e.preventDefault(); sendCoach(ta.value); };
  const file = $("#coach-file");
  if (file) file.onchange = () => { if (file.files[0]) attachCoachImage(file.files[0]); file.value = ""; };
  $("#coach-attach").onclick = (e) => {
    const ask = e.target.closest("[data-attach-ask]");
    if (ask) return sendCoach(ATTACH_ASKS[Number(ask.dataset.attachAsk)][1]);
    if (!e.target.closest("[data-attach-drop]")) return;
    dropCoachImage();
    $("#coach-attach").innerHTML = "";
  };
  app.querySelectorAll("[data-starter]").forEach((b) => b.addEventListener("click", () => sendCoach(COACH_STARTERS[Number(b.dataset.starter)])));
}

async function sendCoach(text) {
  text = text.trim();
  const image = coachImage;
  if (!text && !image) return;
  coachImage = null; // the composer is cleared while the message is on its way; restored if it fails
  renderCoach({ text, image: !!image });
  try {
    const r = await api("/api/coach", { method: "POST", json: image ? { text, image: image.data } : { text } });
    if (image) URL.revokeObjectURL(image.url);
    state.coach.messages.push(...r.messages);
    state.coach.used_today = r.used_today;
    if (state.me) state.me.spent_today = r.spent_today;
    if (state.view === "coach") renderCoach();
  } catch (err) {
    if (image && !coachImage) coachImage = image;
    if (state.view !== "coach") return toast(err.message);
    renderCoach();
    $("#coach-text").value = text;
    $("#coach-error").textContent = err.message;
  }
}

// ---------- first run: goal, Home Screen, reminders ----------

async function renderOnboarding(step = 1) {
  state.view = "onboarding";
  state.onboardingStep = step;
  if (step === 2 && (isStandalone() || !isIOS)) step = state.onboardingStep = 3; // already installed, or not an iPhone
  if (step > 3) return finishOnboarding();
  tabbar.hidden = true;
  const dots = [1, 2, 3].map((n) => `<i class="${n === step ? "on" : n < step ? "done" : ""}"></i>`).join("");
  const head = (title, text) => `<div class="onb-head"><div class="onb-dots">${dots}</div><h1>${title}</h1><p class="muted">${text}</p></div>`;
  const foot = (next = "Дальше") => `<div class="onb-foot"><button class="primary block" id="onb-next">${next}</button>
    <button class="block ghost-line" id="onb-skip">Пропустить настройку</button></div>`;
  if (step === 1) {
    app.innerHTML = `${head("Твоя норма", "Посчитаю калории и БЖУ под твой вес и цель. Потом их уточнит твой же дневник.")}
      <div class="card" id="profile-card"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div>${foot()}`;
    loadProfile();
  } else if (step === 2) {
    app.innerHTML = `${head("Поставь на экран «Домой»", "Так приложение открывается как обычное и может присылать напоминания.")}
      <div class="card"><ol class="howto">
        <li>Открой этот сайт в Safari.</li>
        <li>Нажми «Поделиться» <span class="muted">(квадрат со стрелкой вверх)</span>.</li>
        <li>Выбери «На экран «Домой»» → «Добавить».</li>
        <li>Дальше открывай приложение с иконки на экране «Домой».</li>
      </ol></div>${foot("Сделал, дальше")}`;
  } else {
    let n = { evening: true, hour: 21, weekly: true, share_streak: false };
    try { n = (await api("/api/profile")).notify; } catch { /* defaults */ }
    app.innerHTML = `${head("Напоминания и серии", "Вечером напомню, если день пустой. А в общих сериях видно, кто сегодня уже записал — так держаться проще.")}
      <div class="card" id="notify-card"></div>${foot("Готово")}`;
    await renderNotify({ ...n, share_streak: true }, { autosave: false });
  }
  $("#onb-next").onclick = async () => {
    if (step === 3) {
      const f = $("#notify-form");
      await api("/api/profile/notify", { method: "PUT", json: {
        evening: f.evening.checked, hour: +f.hour.value, weekly: f.weekly.checked, share_streak: f.share_streak.checked } }).catch(() => {});
    }
    renderOnboarding(step + 1);
  };
  $("#onb-skip").onclick = finishOnboarding;
}

async function finishOnboarding() {
  try { state.me = await api("/api/profile/onboarded", { method: "POST" }); } catch { /* not critical */ }
  state.day = isoDate(new Date());
  loadDay();
}

// ---------- profile: goal calculator & Apple Health ----------

const goalsLine = (s) => `<b>${s.kcal} ккал</b> · Б ${s.protein} · Ж ${s.fat} · У ${s.carbs}`;

async function applyGoals(s) {
  try {
    state.me = await api("/api/me", { method: "PUT", json: { kcal: s.kcal, protein: s.protein, fat: s.fat, carbs: s.carbs, timezone: TZ } });
    toast("Норма обновлена");
    refreshView();
  } catch (err) { toast(err.message); }
}

async function loadProfile() {
  try { renderProfile(await api("/api/profile")); }
  catch (err) { const b = $("#profile-card"); if (b) b.innerHTML = `<div class="error">${esc(err.message)}</div>`; }
}

// Fills whichever profile boxes the current screen has.
function renderProfile(p) {
  state.profile = p;
  renderCalculator(p);
  renderHealth(p.health);
  renderNotify(p.notify);
  renderWaterSettings(p);
}

function renderCalculator(p) {
  const box = $("#profile-card");
  if (!box) return;
  const opt = (list, cur) => list.map((o) => `<option value="${o.value}" ${o.value === cur ? "selected" : ""}>${esc(o.label)}</option>`).join("");
  const f = p.formula;
  box.innerHTML = `
    <b>Рассчитать норму</b>
    <p class="muted small" style="margin:4px 0 0">По формуле Миффлина — Сан Жеора. Потом её уточнит твой дневник: см. «Расход энергии» в отчётах.</p>
    <form id="profile-form">
      <div class="row">
        <div class="grow"><label>Пол</label><select name="sex"><option value="f" ${p.sex === "f" ? "selected" : ""}>Женский</option><option value="m" ${p.sex === "m" ? "selected" : ""}>Мужской</option></select></div>
        <div class="grow"><label>Год рождения</label><input name="birth_year" type="number" inputmode="numeric" value="${p.birth_year ?? ""}" required></div>
      </div>
      <div class="row">
        <div class="grow"><label>Рост, см</label><input name="height_cm" type="text" inputmode="decimal" value="${p.height_cm ?? ""}" required></div>
        <div class="grow"><label>Вес, кг</label><input name="weight_kg" type="text" inputmode="decimal" autocomplete="off" value="${p.weight_kg ?? ""}" required></div>
      </div>
      <label>Цель по весу, кг <span class="muted">(необязательно)</span></label>
      <input name="goal_weight_kg" type="text" inputmode="decimal" autocomplete="off" value="${p.goal_weight_kg ?? ""}">
      <label>Активность</label><select name="activity">${opt(p.activity_levels, p.activity)}</select>
      <label>Цель</label><select name="rate_kg_week">${opt(p.rates, p.rate_kg_week)}</select>
      <button class="block" style="margin-top:12px">Рассчитать</button>
    </form>
    ${f ? `<div class="calc">
      <div class="small muted">Базовый обмен ~${f.bmr} ккал, расход с активностью ~${f.tdee} ккал</div>
      <div style="margin-top:4px">${goalsLine(f.suggested)}</div>
      ${f.suggested.capped ? '<div class="small muted">Норма поднята до безопасного минимума — худеть быстрее не стоит без врача.</div>' : ""}
      <button class="primary block" id="apply-formula" style="margin-top:10px">Сделать моей нормой</button>
    </div>` : ""}`;
  $("#profile-form").onsubmit = async (e) => {
    e.preventDefault();
    const v = Object.fromEntries(new FormData(e.target));
    try {
      renderProfile(await api("/api/profile", { method: "PUT", json: {
        sex: v.sex, birth_year: +v.birth_year, height_cm: +String(v.height_cm).replace(",", "."),
        weight_kg: +String(v.weight_kg).replace(",", "."), activity: +v.activity, rate_kg_week: +v.rate_kg_week,
        goal_weight_kg: v.goal_weight_kg ? +String(v.goal_weight_kg).replace(",", ".") : null,
      } }));
    } catch (err) { toast(err.message); }
  };
  if (f) $("#apply-formula").onclick = () => applyGoals(f.suggested);
}

function renderWaterSettings(p) {
  const box = $("#water-card");
  if (!box) return;
  const goals = [1500, 2000, 2500, 3000].map((ml) => `<option value="${ml}" ${ml === p.water_goal_ml ? "selected" : ""}>${kgFmt(ml / 1000)} л</option>`).join("");
  box.innerHTML = `<b>Вода на экране дня</b>
    <form id="water-form" class="toggles">
      <label class="check"><input type="checkbox" name="track" ${p.track_water ? "checked" : ""}> Считать стаканы на экране дня, цель <select name="goal">${goals}</select></label>
    </form>`;
  $("#water-form").onchange = async (e) => {
    const f = e.currentTarget;
    try { await api("/api/profile/water", { method: "PUT", json: { track_water: f.track.checked, water_goal_ml: +f.goal.value } }); toast("Сохранено"); }
    catch (err) { toast(err.message); }
  };
}

function renderHealth(h, token = null) {
  const box = $("#health-card");
  if (!box) return;
  const url = `${location.origin}/api/health`;
  if (token) {
    box.innerHTML = `
      <b>Подключение Apple Watch</b>
      <p class="small">Ключ показывается один раз — скопируй его в команду.</p>
      <label>Адрес</label><div class="row"><input readonly value="${esc(url)}"><button data-copy-text="${esc(url)}">Копировать</button></div>
      <label>Заголовок Authorization</label><div class="row"><input readonly value="Bearer ${esc(token)}"><button data-copy-text="Bearer ${esc(token)}">Копировать</button></div>
      <ol class="howto small">
        <li>«Команды» → «Автоматизация» → «+» → «Время суток»: 23:50, ежедневно, «Запускать сразу».</li>
        <li>«Найти образцы здоровья»: тип «Активная энергия», дата начала — «Сегодня». Затем «Вычислить статистику» → «Сумма».</li>
        <li>То же для типа «Шаги».</li>
        <li>«Получить содержимое URL»: адрес выше, метод POST, заголовок Authorization со значением выше, тело JSON: <code>active_kcal</code> — первая сумма, <code>steps</code> — вторая (оба «Число»).</li>
      </ol>
      <button class="block" id="health-done">Готово</button>`;
    box.querySelectorAll("[data-copy-text]").forEach((b) => b.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(b.dataset.copyText); toast("Скопировано"); } catch { toast("Не удалось скопировать"); }
    }));
    $("#health-done").onclick = () => loadProfile();
    return;
  }
  box.innerHTML = `
    <b>Apple Watch и «Здоровье»</b>
    <p class="muted small" style="margin:4px 0 10px">${h.connected
      ? (h.last_day ? `Подключено. Последние данные — ${esc(dayLabel(h.last_day).toLowerCase())}.` : "Ключ выдан, данных пока не было.")
      : "Команда на iPhone раз в день отправляет активные калории и шаги. Они появятся в дневнике, отчётах и обзоре недели. Вес пиши в приложении."}</p>
    <button class="block" id="health-token">${h.connected ? "Выдать новый ключ" : "Подключить"}</button>
    ${h.connected ? '<button class="block danger" id="health-off" style="margin-top:8px">Отключить</button>' : ""}`;
  $("#health-token").onclick = async () => {
    try { renderHealth(h, (await api("/api/profile/health-token", { method: "POST" })).token); } catch (err) { toast(err.message); }
  };
  if (h.connected) $("#health-off").onclick = async () => {
    if (!confirm("Отключить? Команда на телефоне перестанет работать.")) return;
    try { await api("/api/profile/health-token", { method: "DELETE" }); renderHealth({ connected: false, last_day: h.last_day }); } catch (err) { toast(err.message); }
  };
}

// ---------- push reminders ----------

const pushSupported = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent);
const isStandalone = () => matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;

function b64ToBytes(b64) {
  const s = atob(b64.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (b64.length % 4)) % 4));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}

async function deviceSubscription() {
  if (!pushSupported()) return null;
  const reg = await navigator.serviceWorker.ready;
  return reg.pushManager.getSubscription();
}

async function enablePush() {
  const { public_key: key } = await api("/api/push/key");
  if (!key) throw new Error("Уведомления на сервере пока не настроены");
  if ((await Notification.requestPermission()) !== "granted") throw new Error("Уведомления запрещены — разреши их в настройках телефона");
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(key) });
  await api("/api/push/subscribe", { method: "POST", json: sub.toJSON() });
}

async function renderNotify(n, { autosave = true } = {}) {
  const box = $("#notify-card");
  if (!box) return;
  let device;
  if (!pushSupported()) {
    device = isIOS && !isStandalone()
      ? '<p class="small">Чтобы получать напоминания на iPhone, добавь приложение на экран «Домой»: «Поделиться» → «На экран «Домой»», и открой его оттуда.</p>'
      : '<p class="small muted">Этот браузер не поддерживает уведомления.</p>';
  } else {
    const sub = await deviceSubscription().catch(() => null);
    device = sub
      ? `<p class="small">Уведомления на этом устройстве включены.</p>
         <div class="row"><button class="grow" id="push-test">Проверить</button><button class="grow danger" id="push-off">Выключить</button></div>`
      : '<button class="primary block" id="push-on">Включить уведомления</button>';
  }
  const hours = [17, 18, 19, 20, 21, 22, 23].map((h) => `<option value="${h}" ${h === n.hour ? "selected" : ""}>${h}:00</option>`).join("");
  box.innerHTML = `
    <b>Напоминания и серии</b>
    <div style="margin:8px 0 4px">${device}</div>
    <form id="notify-form" class="toggles">
      <label class="check"><input type="checkbox" name="evening" ${n.evening ? "checked" : ""}> Напомнить вечером, если день пустой, в <select name="hour">${hours}</select></label>
      <label class="check"><input type="checkbox" name="weekly" ${n.weekly ? "checked" : ""}> Обзор недели по понедельникам</label>
      <label class="check"><input type="checkbox" name="share_streak" ${n.share_streak ? "checked" : ""}> Участвовать в общих сериях <span class="muted small">— твою серию увидят те, кто тоже участвует</span></label>
    </form>`;
  $("#notify-form").onchange = async (e) => {
    if (!autosave) return;
    const f = e.currentTarget;
    try {
      const p = await api("/api/profile/notify", { method: "PUT", json: {
        evening: f.evening.checked, hour: +f.hour.value, weekly: f.weekly.checked, share_streak: f.share_streak.checked } });
      toast("Сохранено");
      if (e.target.name === "share_streak") state.streaks = await api("/api/streaks").catch(() => null);
      n = p.notify;
    } catch (err) { toast(err.message); }
  };
  $("#push-on")?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { await enablePush(); toast("Уведомления включены"); } catch (err) { toast(err.message); }
    renderNotify(n, { autosave });
  });
  $("#push-test")?.addEventListener("click", async () => {
    try { await api("/api/push/test", { method: "POST" }); toast("Отправил — проверь уведомления"); } catch (err) { toast(err.message); }
  });
  $("#push-off")?.addEventListener("click", async () => {
    const sub = await deviceSubscription();
    if (sub) { await api("/api/push/unsubscribe", { method: "POST", json: { endpoint: sub.endpoint } }).catch(() => {}); await sub.unsubscribe(); }
    renderNotify(n, { autosave });
  });
}

async function renderAdmin() {
  const box = $("#admin");
  if (!box) return;
  try {
    const [u, inv] = await Promise.all([api("/api/admin/users"), api("/api/admin/invites")]);
    const free = inv.filter((i) => !i.used_by);
    box.innerHTML = `
      <b>Админка</b>
      <p class="small muted">За месяц: $${u.month_total.toFixed(2)} из $${u.monthly_limit.toFixed(2)}</p>
      <table><tr><th>Кто</th><th>Сегодня</th><th>Месяц</th><th>Лимит/день</th></tr>
        ${u.users.map((x) => `<tr><td>${esc(x.username)}</td><td>$${x.spent_today.toFixed(2)}</td><td>$${x.spent_month.toFixed(2)}</td>
          <td><input style="width:72px;padding:4px" type="number" step="0.05" value="${x.daily_limit}" data-limit="${x.id}"></td></tr>`).join("")}
      </table>
      <button class="block" id="new-invite" style="margin-top:12px">Создать приглашение</button>
      ${free.map((i) => `<div class="row" style="margin-top:6px"><input readonly value="${location.origin}/#invite=${esc(i.code)}"><button data-copy="${esc(i.code)}">Копировать</button></div>`).join("")}`;
    box.querySelectorAll("[data-limit]").forEach((el) => el.addEventListener("change", async () => {
      try { await api(`/api/admin/users/${el.dataset.limit}`, { method: "PATCH", json: { daily_limit_usd: el.value === "" ? null : +el.value } }); toast("Лимит обновлён"); }
      catch (err) { toast(err.message); }
    }));
    $("#new-invite").onclick = async () => { await api("/api/admin/invites", { method: "POST" }); renderAdmin(); };
    box.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
      const link = `${location.origin}/#invite=${b.dataset.copy}`;
      try { await navigator.clipboard.writeText(link); toast("Ссылка скопирована"); } catch { toast(link); }
    }));
  } catch (err) { box.innerHTML = `<div class="error">${esc(err.message)}</div>`; }
}

// ---------- start ----------

async function start() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  if (inviteFromUrl()) return renderAuth();
  try {
    try {
      state.me = await api("/api/me");
      try { localStorage.setItem("ft-me", JSON.stringify(state.me)); } catch { /* ignore */ }
    } catch (err) {
      // No network: open the diary as last seen, if someone was signed in on this device.
      let last = null;
      try { last = JSON.parse(localStorage.getItem("ft-me") || "null"); } catch { /* ignore */ }
      if (err.status !== 0 || !last) throw err;
      state.me = last;
      return await loadDay();
    }
    flushQueue();
    if (state.me.timezone !== TZ) api("/api/me", { method: "PUT", json: { ...state.me.goals, timezone: TZ } }).then((m) => (state.me = m)).catch(() => {});
    // A tap on the weekly review notification opens /#reports.
    if (location.hash === "#reports") { history.replaceState(null, "", "/"); await loadStats(); }
    else if (profileHash()) await loadProfileView(profileHash());
    else if (!state.me.onboarded) renderOnboarding(1);
    else await loadDay();
  } catch (err) {
    if (err.status === 401) renderAuth();
    else app.innerHTML = `<div class="card error">${esc(err.message)}</div>`;
  }
}

// Coming back to the app on a new day should show the new day.
let todayAtStart = isoDate(new Date());
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") flushQueue();
  if (document.visibilityState !== "visible" || !state.dayData || !sheet.hidden) return;
  const today = isoDate(new Date());
  if (state.day === todayAtStart) state.day = today;
  if (todayAtStart !== today) state.review = null;
  todayAtStart = today;
  // Only the live screens refresh, and they stay where they were scrolled.
  if (state.view !== "day" && state.view !== "reports") return;
  const y = window.scrollY;
  (state.view === "reports" ? loadStats() : loadDay()).then(() => window.scrollTo(0, y));
});

start();
