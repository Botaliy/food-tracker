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

async function api(path, { method = "GET", json, form } = {}) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (json !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(json); }
  if (form) opts.body = form;
  let res;
  try { res = await fetch(path, opts); } catch { throw new ApiError(0, "Нет связи с сервером"); }
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) {
    let msg = data?.detail;
    if (Array.isArray(msg)) msg = "Проверь введённые значения";
    throw new ApiError(res.status, msg || `Ошибка ${res.status}`);
  }
  return data;
}

let toastTimer;
function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 3500);
}

function openSheet(html) {
  sheetBody.innerHTML = html;
  sheet.hidden = false;
  sheetBody.scrollTop = 0;
}
function closeSheet() { stopScan(); sheet.hidden = true; sheetBody.innerHTML = ""; draft = null; }
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
    <div class="brand"><div class="brand-ico">🥗</div><h1>${invite ? "Регистрация" : "Вход"}</h1><p class="muted small">Калории и БЖУ по фото еды</p></div>
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
      await loadDay();
    } catch (err) {
      renderAuth(err.message);
    }
  });
}

// ---------- day view ----------

async function loadDay() {
  state.view = "day";
  try {
    const [dayData, streaks] = await Promise.all([api(`/api/day?date=${state.day}`), api("/api/streaks").catch(() => null)]);
    state.dayData = dayData;
    state.streaks = streaks;
    renderDay();
  } catch (err) {
    if (err.status === 401) return renderAuth();
    app.innerHTML = `<div class="card error">${esc(err.message)}</div>`;
  }
}

function bar(value, goal, color) {
  const pct = goal > 0 ? Math.min(100, (value / goal) * 100) : 0;
  return `<div class="bar ${value > goal * 1.05 ? "over" : ""}" style="--c:${color}"><i style="width:${pct}%"></i></div>`;
}

// Stroke icons, 24x24, drawn in currentColor.
const svgIcon = (d) => `<svg class="ic" viewBox="0 0 24 24" aria-hidden="true">${d}</svg>`;
const ICON = {
  diary: svgIcon('<path d="M6 3.5h10.5A2.5 2.5 0 0 1 19 6v14.5H7.5A2.5 2.5 0 0 1 5 18V4.5a1 1 0 0 1 1-1z"/><path d="M5 18a2.5 2.5 0 0 1 2.5-2.5H19M9 8h6"/>'),
  chart: svgIcon('<path d="M4 20h16M7 16v-5M12 16V6M17 16v-8"/>'),
  settings: svgIcon('<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>'),
  left: svgIcon('<path d="M14.5 6l-6 6 6 6"/>'),
  right: svgIcon('<path d="M9.5 6l6 6-6 6"/>'),
  plus: svgIcon('<path d="M12 5v14M5 12h14"/>'),
};

function ring(value, goal, color, size, width, inner = "", warnOver = false) {
  const r = (size - width) / 2, c = 2 * Math.PI * r, mid = size / 2;
  const frac = goal > 0 ? Math.min(1, value / goal) : 0;
  const over = warnOver && value > goal * 1.05;
  return `<div class="ring" style="width:${size}px;height:${size}px">
    <svg viewBox="0 0 ${size} ${size}" aria-hidden="true">
      <circle class="track" cx="${mid}" cy="${mid}" r="${r}" stroke-width="${width}"/>
      <circle class="val ${over ? "over" : ""}" cx="${mid}" cy="${mid}" r="${r}" stroke-width="${width}" stroke="${color}"
        stroke-linecap="${frac > 0 ? "round" : "butt"}" stroke-dasharray="${(c * frac).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 ${mid} ${mid})"/>
    </svg>
    <div class="ring-in">${inner}</div>
  </div>`;
}

function topbar(title, subtitle, buttons) {
  return `<header class="top">
    <div class="grow"><h1>${esc(title)}</h1><div class="muted small">${esc(subtitle)}</div></div>
    <div class="row tight">${buttons}</div>
  </header>`;
}

const tabbar = $("#tabbar");
function showTabs(active) {
  tabbar.innerHTML = `
    <button data-tab="day" class="${active === "day" ? "on" : ""}">${ICON.diary}<span>Дневник</span></button>
    <button data-tab="add" class="add" aria-label="Добавить еду">${ICON.plus}</button>
    <button data-tab="reports" class="${active === "reports" ? "on" : ""}">${ICON.chart}<span>Отчёты</span></button>`;
  tabbar.hidden = false;
}
tabbar.addEventListener("click", (e) => {
  const tab = e.target.closest("[data-tab]")?.dataset.tab;
  if (tab === "add") openAdd();
  else if (tab === "day") { state.day = isoDate(new Date()); loadDay(); }
  else if (tab === "reports") loadStats();
});

function renderDay() {
  const { meals, totals: t, goals: g } = state.dayData;
  const isToday = state.day === isoDate(new Date());
  const left = g.kcal - t.kcal;
  const d = new Date(state.day + "T12:00");
  const label = dayLabel(state.day);
  const sameWeekday = label !== "Сегодня" && label !== "Вчера";
  showTabs("day");
  app.innerHTML = `
    ${topbar(sameWeekday ? d.toLocaleDateString("ru-RU", { day: "numeric", month: "long" }) : label,
      d.toLocaleDateString("ru-RU", sameWeekday ? { weekday: "long" } : { weekday: "long", day: "numeric", month: "long" }),
      `<button class="icon" data-act="prev" aria-label="Предыдущий день">${ICON.left}</button>
       <button class="icon" data-act="next" aria-label="Следующий день" ${isToday ? "disabled" : ""}>${ICON.right}</button>
       <button class="icon" data-act="settings" aria-label="Настройки">${ICON.settings}</button>`)}
    <div class="card summary">
      <div class="sum-main">
        ${ring(t.kcal, g.kcal, "var(--accent)", 136, 13,
          `<b>${r0(Math.abs(left))}</b><span class="muted">${left >= 0 ? "осталось" : "перебор"}</span>`, true)}
        <div class="sum-stats">
          <div><span class="muted small">Съедено</span><b>${r0(t.kcal)} <small>ккал</small></b></div>
          <div><span class="muted small">Норма</span><b>${g.kcal} <small>ккал</small></b></div>
          ${activityLine(state.dayData.activity)}
        </div>
      </div>
      <div class="macros">
        ${[["Белки", t.protein, g.protein, "var(--protein)"], ["Жиры", t.fat, g.fat, "var(--fat)"], ["Углеводы", t.carbs, g.carbs, "var(--carbs)"]]
          .map(([n, v, goal, c]) => `<div class="macro">${ring(v, goal, c, 62, 7, `<b>${r0(v)}</b>`)}
            <span>${n}</span><span class="muted small">из ${goal} г</span></div>`).join("")}
      </div>
    </div>
    ${streaksRow(state.streaks)}
    ${meals.length
      ? `<h3 class="sub">Приёмы пищи</h3>${meals.map(mealCard).join("")}`
      : `<div class="empty"><div class="empty-ico">📸</div><b>Пока пусто</b>
          <p>Нажми «+» внизу и сфотографируй еду — посчитаю калории и БЖУ.</p></div>`}`;
}

const fmtInt = (n) => r0(n).toLocaleString("ru-RU");

function streaksRow(st) {
  if (!st?.joined || !st.people.length) return "";
  const chips = st.people.map((p) => `<span class="chip ${p.me ? "me" : ""}" title="${p.today ? "сегодня записано" : "сегодня ещё пусто"}">
    ${p.today ? "✅" : "⏳"} ${esc(p.me ? "ты" : p.name)} <b>${p.streak}</b></span>`).join("");
  return `<div class="streaks"><span class="streaks-title">🔥 Серии</span>${chips}
    ${st.people.length === 1 ? '<span class="muted small">Пока участвуешь только ты</span>' : ""}</div>`;
}

function activityLine(a) {
  if (!a || (a.active_kcal == null && a.steps == null)) return "";
  const parts = [a.active_kcal != null ? `🔥 ${fmtInt(a.active_kcal)} ккал` : "", a.steps != null ? `👣 ${fmtInt(a.steps)}` : ""].filter(Boolean);
  return `<div class="move"><span class="muted small">Движение</span><span>${parts.join(" · ")}</span></div>`;
}

const PART_OF_DAY =[[5, "🌙", "Перекус"], [11, "🍳", "Завтрак"], [16, "🍲", "Обед"], [22, "🍽️", "Ужин"], [24, "🌙", "Перекус"]];

function mealCard(m) {
  const at = new Date(m.eaten_at);
  const time = at.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  const [, icon, name] = PART_OF_DAY.find(([until]) => at.getHours() < until);
  return `<div class="card meal" data-meal="${m.id}">
    <div class="meal-ico">${icon}</div>
    <div class="grow">
      <div class="meal-head"><span class="meal-title">${esc(m.title || name)}</span><span class="meal-kcal">${r0(m.totals.kcal)} <small>ккал</small></span></div>
      <div class="meal-items">${m.items.map((i) => `${esc(i.name)} ${r0(i.grams)} г`).join(" · ")}</div>
      <div class="meal-meta">${time} · <i style="--c:var(--protein)"></i>Б ${r0(m.totals.protein)} <i style="--c:var(--fat)"></i>Ж ${r0(m.totals.fat)} <i style="--c:var(--carbs)"></i>У ${r0(m.totals.carbs)}</div>
    </div>
  </div>`;
}

app.addEventListener("click", (e) => {
  const act = e.target.closest("[data-act]")?.dataset.act;
  const mealEl = e.target.closest("[data-meal]");
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
  } else if (act === "apply-adaptive") {
    applyGoals(state.stats.energy.adaptive.suggested);
  } else if (act === "review") {
    makeReview();
  } else if (act === "save-weight") {
    saveWeight();
  } else if (act === "add") {
    openAdd();
  } else if (act === "settings") {
    openSettings();
  } else if (mealEl) {
    const meal = state.dayData.meals.find((m) => m.id === Number(mealEl.dataset.meal));
    if (meal) openEditor({ id: meal.id, title: meal.title, items: meal.items.map((i) => ({ ...i, options: [] })), comment: "" });
  }
});

// ---------- reports ----------

const PERIODS = [[7, "Неделя"], [30, "Месяц"], [90, "3 месяца"]];
const shortDate = (iso) => new Date(iso + "T12:00").toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
const plural = (n, one, few, many) => {
  const m10 = n % 10, m100 = n % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
};

async function loadStats() {
  state.view = "reports";
  try {
    const [stats, review] = await Promise.all([
      api(`/api/stats?days=${state.statsDays}`),
      state.review ? Promise.resolve(state.review) : api("/api/review"),
    ]);
    state.stats = stats;
    state.review = review;
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
  const bars = s.days.map((d, i) => {
    const x = i * slot + (slot - bw) / 2;
    const over = d.kcal > goal * 1.05;
    const sel = d.date === state.selected;
    const h = d.kcal > 0 ? Math.max(2, H - bottom - y(d.kcal)) : 0;
    // Rounded top, square base: a rounded rect plus a square patch over its bottom corners.
    const mark = h ? `<rect x="${x}" y="${H - bottom - h}" width="${bw}" height="${h}" rx="${rx}" class="${over ? "over" : ""}"/>
      <rect x="${x}" y="${H - bottom - Math.min(h, rx)}" width="${bw}" height="${Math.min(h, rx)}" class="${over ? "over" : ""}"/>` : "";
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
  const kgs = pts.map((p) => p.kg);
  const lo = Math.floor(Math.min(...kgs) - 0.5), hi = Math.ceil(Math.max(...kgs) + 0.5);
  const x = (iso) => left + (W - left - right) * (t1 === t0 ? 0.5 : (new Date(iso + "T12:00").getTime() - t0) / (t1 - t0));
  const y = (kg) => top + (H - top - bottom) * (1 - (kg - lo) / (hi - lo));
  const path = pts.map((p, i) => `${i ? "L" : "M"}${x(p.date).toFixed(1)},${y(p.kg).toFixed(1)}`).join(" ");
  const dots = pts.map((p) => `<circle cx="${x(p.date)}" cy="${y(p.kg)}" r="${pts.length > 40 ? 2 : 4}"><title>${esc(shortDate(p.date))}: ${p.kg} кг</title></circle>`).join("");
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
  const delta = s.weights.length >= 2 ? r1(s.weights.at(-1).kg - s.weights[0].kg) : null;
  showTabs("reports");
  app.innerHTML = `
    ${topbar("Отчёты", `${shortDate(s.from)} — ${shortDate(s.to)}`,
      `<button class="icon" data-act="settings" aria-label="Настройки">${ICON.settings}</button>`)}
    <div class="seg">${PERIODS.map(([d, l]) => `<button data-act="period" data-days="${d}" class="${d === state.statsDays ? "on" : ""}">${l}</button>`).join("")}</div>
    <div class="tiles">
      <div class="tile"><span class="muted small">Серия</span><b>${s.streak}</b><span class="muted small">${plural(s.streak, "день", "дня", "дней")} подряд</span></div>
      <div class="tile"><span class="muted small">В норме</span><b>${s.in_goal_days}<small> / ${s.logged_days}</small></b><span class="muted small">дней с записями</span></div>
      <div class="tile"><span class="muted small">В среднем</span><b>${r0(a.kcal)}</b><span class="muted small">ккал/день</span></div>
    </div>
    ${reviewCard()}
    <div class="card">
      <div class="row between"><b>Калории</b><span class="muted small">${s.logged_days ? `среднее ${r0(a.kcal)} из ${g.kcal}` : "нет записей"}</span></div>
      ${kcalChart(s)}
      <div class="sel-info small">${sel
        ? `<span><b>${esc(dayLabel(sel.date))}</b>: ${r0(sel.kcal)} ккал · Б ${r0(sel.protein)} Ж ${r0(sel.fat)} У ${r0(sel.carbs)}</span><button class="ghost" data-act="open-day">Открыть ›</button>`
        : '<span class="muted">Нажми на столбик, чтобы увидеть день</span>'}</div>
    </div>
    ${energyCard(s)}
    <div class="card">
      <b>БЖУ в среднем за день</b>
      <div class="macros">
        ${[["Белки", a.protein, g.protein, "var(--protein)"], ["Жиры", a.fat, g.fat, "var(--fat)"], ["Углеводы", a.carbs, g.carbs, "var(--carbs)"]]
          .map(([n, v, goal, c]) => `<div class="macro"><span class="muted small">${n}</span><b>${r0(v)} / ${goal} г</b>${bar(v, goal, c)}</div>`).join("")}
      </div>
      <p class="muted small" style="margin:10px 0 0">Среднее считается только по дням, где есть записи.</p>
    </div>
    <div class="card">
      <div class="row between"><b>Вес</b><span class="muted small">${delta === null ? (s.last_weight ? `последний: ${s.last_weight.kg} кг, ${esc(shortDate(s.last_weight.date))}` : "") : `${delta > 0 ? "+" : ""}${delta} кг за период`}</span></div>
      ${weightChart(s)}
      <form class="row" id="weight-form" style="margin-top:10px">
        <input id="weight" type="number" step="0.1" min="20" max="400" inputmode="decimal" placeholder="Вес сегодня, кг" value="${todayW ? todayW.kg : ""}">
        <button class="primary" data-act="save-weight" type="button">${todayW ? "Обновить" : "Записать"}</button>
      </form>
    </div>`;
  $("#weight-form").onsubmit = (e) => { e.preventDefault(); saveWeight(); };
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
    e.activity ? `Часы: ${e.activity.active_kcal != null ? `🔥 ${fmtInt(e.activity.active_kcal)} ккал` : ""}${e.activity.active_kcal != null && e.activity.steps != null ? " · " : ""}${e.activity.steps != null ? `👣 ${fmtInt(e.activity.steps)} шагов` : ""} в среднем за ${e.activity.days} ${plural(e.activity.days, "день", "дня", "дней")}` : "",
  ].filter(Boolean);
  return `<div class="card">
    <b>Расход энергии</b>
    ${main}
    ${extra.length ? `<div class="energy-extra small muted">${extra.map((x) => `<div>${x}</div>`).join("")}</div>` : ""}
  </div>`;
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
    <div class="row between"><b>✨ Обзор недели</b>${r && !state.reviewBusy ? `<span class="muted small">${esc(dayLabel(r.date))}</span>` : ""}</div>
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
    await api("/api/weight", { method: "PUT", json: { kg } });
    toast("Вес записан");
    loadStats();
  } catch (err) { toast(err.message); }
}

// ---------- add: photo / note -> draft ----------

let pendingPhoto = null;

function openAdd() {
  pendingPhoto = null;
  openSheet(`
    <div class="row between"><h2>Что будешь есть?</h2><button class="ghost" data-close>✕</button></div>
    <div class="pick">
      <label class="btn">📷<span>Сфотографировать</span><input type="file" accept="image/*" capture="environment" data-photo></label>
      <label class="btn">🖼️<span>Из галереи</span><input type="file" accept="image/*" data-photo></label>
    </div>
    <img class="preview" id="preview" hidden alt="">
    <label>Уточнение (необязательно)</label>
    <textarea id="note" placeholder="Например: рис 150 г, курица жарена на масле. Или просто текстом: 2 яйца и тост с маслом"></textarea>
    <div class="error" id="add-error"></div>
    <button class="primary block" id="recognize" style="margin-top:12px">Посчитать</button>
    <div class="row" style="margin-top:8px">
      <button class="grow" id="manual">Ввести вручную</button>
      <button class="grow" id="scan">🏷️ Штрихкод</button>
    </div>
    <div id="recent"></div>
    <p class="muted small center" id="spent"></p>`);
  const me = state.me;
  if (me) $("#spent").textContent = `Потрачено сегодня $${me.spent_today.toFixed(2)} из $${me.daily_limit.toFixed(2)}`;
  loadRecent();
}

let recentMeals = [];
async function loadRecent() {
  try { recentMeals = await api("/api/meals/recent"); } catch { return; }
  const box = $("#recent");
  if (!box || !recentMeals.length) return;
  box.innerHTML = `<h3 class="sub">Часто ешь</h3>` + recentMeals.map((m, j) => `
    <button class="recent" data-recent="${j}">
      <span class="grow"><b>${esc(m.title || m.items.map((i) => i.name).join(", "))}</b>
        <span class="small muted">${m.title ? esc(m.items.map((i) => `${i.name} ${r0(i.grams)} г`).join(" · ")) : ""}</span></span>
      <span class="small"><b>${r0(m.totals.kcal)}</b> ккал${m.count > 1 ? `<br><span class="muted">×${m.count}</span>` : ""}</span>
    </button>`).join("");
}

// A copy of an existing meal as a new, unsaved one eaten now.
function repeatMeal(m) {
  // A repeat is the user's own choice, not a recognition: nothing to learn from it.
  openEditor({ title: m.title, items: m.items.map(({ id, ai_grams, ...i }) => ({ ...i, options: [] })), comment: "", today: true });
}

sheetBody.addEventListener("change", async (e) => {
  if (!e.target.matches("[data-photo]")) return;
  const file = e.target.files?.[0];
  if (!file) return;
  pendingPhoto = await shrinkPhoto(file);
  const img = $("#preview");
  img.src = URL.createObjectURL(pendingPhoto);
  img.hidden = false;
});

sheetBody.addEventListener("click", async (e) => {
  if (e.target.closest("[data-close]")) return closeSheet();
  if (e.target.id === "manual") return openEditor({ title: "", items: [], comment: "" });
  if (e.target.id === "scan") return openScanner();
  const rec = e.target.closest("[data-recent]");
  if (rec) return repeatMeal(recentMeals[Number(rec.dataset.recent)]);
  if (e.target.id !== "recognize") return;

  const note = $("#note").value.trim();
  if (!pendingPhoto && !note) { $("#add-error").textContent = "Добавь фото или опиши еду словами"; return; }
  const form = new FormData();
  if (pendingPhoto) form.append("photo", pendingPhoto, "meal.jpg");
  form.append("note", note);
  sheetBody.innerHTML = '<div class="spinner"></div><p class="center muted">Смотрю на тарелку…</p>';
  try {
    const draft = await api("/api/recognize", { method: "POST", form });
    if (state.me) state.me.spent_today = draft.spent_today;
    if (!draft.items.length) {
      openAdd();
      $("#add-error").textContent = draft.comment || "Не нашёл еды на фото";
      return;
    }
    openEditor(draft);
  } catch (err) {
    if (err.status === 401) { closeSheet(); return renderAuth(); }
    openAdd();
    $("#note").value = note;
    $("#add-error").textContent = err.message;
  }
});

// ---------- draft / meal editor ----------

let draft = null;

function openEditor(d) {
  draft = structuredClone(d);
  renderEditor();
}

const SOURCE_LABEL = { usda: "база USDA", off: "Open Food Facts", custom: "мой продукт", estimate: "оценка ИИ", manual: "вручную" };

function itemHtml(it, idx) {
  const k = (Number(it.grams) || 0) / 100;
  const badge = it.source === "estimate" || it.source === "manual" ? "" : "db";
  const opts = (it.options || []).map((o, j) =>
    `<option value="${j}" ${o.food_id === it.food_id && o.source === it.source ? "selected" : ""}>${esc(o.source === "estimate" ? "Оценка ИИ" : o.name_en || o.name)} — ${r0(o.kcal_100)} ккал/100 г</option>`).join("");
  return `<div class="item" data-idx="${idx}">
    <div class="row between">
      <div class="grow">
        <div class="item-name">${esc(it.name)}</div>
        <div class="small muted">
          <span class="badge ${badge}">${SOURCE_LABEL[it.source] || it.source}</span>
          ${it.confidence === "low" ? '<span class="badge low">не уверен</span>' : ""}
          ${r0(it.kcal_100)} ккал/100 г
        </div>
      </div>
      <button class="ghost danger" data-del="${idx}" aria-label="Убрать">✕</button>
    </div>
    <div class="row between" style="margin-top:6px">
      <div class="grams">
        <button data-step="-10" data-i="${idx}">−</button>
        <input type="number" inputmode="decimal" min="1" value="${r0(it.grams)}" data-grams="${idx}"> г
        <button data-step="10" data-i="${idx}">+</button>
      </div>
      <div class="small" data-item-total="${idx}">${itemTotals(it)}</div>
    </div>
    ${opts ? `<select data-opt="${idx}">${opts}</select>` : ""}
  </div>`;
}

function itemTotals(it) {
  const t = totalsOf([it]);
  return `<b>${r0(t.kcal)} ккал</b> · Б ${r1(t.protein)} Ж ${r1(t.fat)} У ${r1(t.carbs)}`;
}

function renderEditor() {
  openSheet(`
    <div class="row between"><h2>${draft.id ? "Редактирование" : "Проверь и поправь"}</h2><button class="ghost" data-close>✕</button></div>
    <input id="title" value="${esc(draft.title)}" placeholder="Название, например «Обед»">
    ${draft.comment ? `<p class="small muted">💡 ${esc(draft.comment)}</p>` : ""}
    ${draft.personal_hints ? '<p class="small muted">🎯 Учтены твои прошлые правки порций</p>' : ""}
    <div class="card" style="margin-top:10px" id="items">${draft.items.map(itemHtml).join("") || '<p class="muted small">Добавь продукты через поиск ниже</p>'}</div>
    <div class="card">
      <input id="food-q" placeholder="Добавить продукт: поиск (рус./англ.)" autocomplete="off">
      <div class="search-results" id="food-results"></div>
    </div>
    <div class="totals">
      <div class="row between"><b id="draft-total"></b><span class="small muted" id="draft-macros"></span></div>
      <button class="primary block" id="save" style="margin-top:8px">${draft.id ? "Сохранить" : "Съел — записать"}</button>
      ${draft.id ? '<button class="block" id="repeat" style="margin-top:8px">Записать ещё раз сегодня</button><button class="block danger" id="delete" style="margin-top:8px">Удалить приём пищи</button>' : ""}
    </div>`);
  updateDraftTotals();
}

function updateDraftTotals() {
  const t = totalsOf(draft.items);
  $("#draft-total").textContent = `${r0(t.kcal)} ккал`;
  $("#draft-macros").textContent = `Б ${r0(t.protein)} · Ж ${r0(t.fat)} · У ${r0(t.carbs)}`;
}

function setGrams(idx, grams) {
  draft.items[idx].grams = Math.max(0, grams);
  const input = sheetBody.querySelector(`[data-grams="${idx}"]`);
  if (input && Number(input.value) !== draft.items[idx].grams) input.value = r0(draft.items[idx].grams);
  sheetBody.querySelector(`[data-item-total="${idx}"]`).innerHTML = itemTotals(draft.items[idx]);
  updateDraftTotals();
}

sheetBody.addEventListener("input", (e) => {
  if (!draft) return;
  if (e.target.dataset.grams !== undefined) setGrams(Number(e.target.dataset.grams), Number(e.target.value) || 0);
  if (e.target.id === "title") draft.title = e.target.value;
  if (e.target.id === "food-q") searchFoods(e.target.value);
});

sheetBody.addEventListener("change", (e) => {
  if (!draft || e.target.dataset.opt === undefined) return;
  const idx = Number(e.target.dataset.opt);
  const it = draft.items[idx];
  const o = it.options[Number(e.target.value)];
  Object.assign(it, { food_id: o.food_id, source: o.source, kcal_100: o.kcal_100, protein_100: o.protein_100, fat_100: o.fat_100, carbs_100: o.carbs_100 });
  renderEditor();
});

let searchTimer, searchResults = [];
function searchFoods(q) {
  clearTimeout(searchTimer);
  const box = $("#food-results");
  if (q.trim().length < 2) { box.innerHTML = ""; return; }
  searchTimer = setTimeout(async () => {
    try {
      searchResults = await api(`/api/foods/search?q=${encodeURIComponent(q)}`);
      box.innerHTML = searchResults.map((f, j) =>
        `<button data-add-food="${j}">${esc(f.name)} <span class="muted">${r0(f.kcal_100)} ккал/100 г</span></button>`).join("")
        + `<button data-custom="1">＋ Свой продукт «${esc(q)}»</button>`;
    } catch (err) { box.innerHTML = `<div class="error">${esc(err.message)}</div>`; }
  }, 250);
}

sheetBody.addEventListener("click", async (e) => {
  if (!draft) return;
  const t = e.target.closest("button");
  if (!t) return;
  if (t.dataset.step) {
    const idx = Number(t.dataset.i);
    setGrams(idx, (Number(draft.items[idx].grams) || 0) + Number(t.dataset.step));
  } else if (t.dataset.del !== undefined) {
    draft.items.splice(Number(t.dataset.del), 1);
    renderEditor();
  } else if (t.dataset.addFood !== undefined) {
    const f = searchResults[Number(t.dataset.addFood)];
    draft.items.push({ ...f, grams: 100, options: [f] });
    renderEditor();
  } else if (t.dataset.custom) {
    openCustomFood($("#food-q").value.trim());
  } else if (t.id === "save") {
    saveDraft(t);
  } else if (t.id === "repeat") {
    repeatMeal(draft);
  } else if (t.id === "delete") {
    if (!confirm("Удалить этот приём пищи?")) return;
    try {
      await api(`/api/meals/${draft.id}`, { method: "DELETE" });
      draft = null; closeSheet(); loadDay();
    } catch (err) { toast(err.message); }
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
      kcal_100: i.kcal_100, protein_100: i.protein_100, fat_100: i.fat_100, carbs_100: i.carbs_100,
      ai_grams: i.ai_grams ?? null,
    })),
  };
  // A new meal lands on the day being viewed: "now" for today, midday for a past day.
  // A repeated meal is always eaten now.
  if (!draft.id && !draft.today && state.day !== isoDate(new Date())) body.eaten_at = `${state.day}T12:00:00`;
  const toToday = !draft.id && draft.today;
  try {
    await api(draft.id ? `/api/meals/${draft.id}` : "/api/meals", { method: draft.id ? "PUT" : "POST", json: body });
    if (toToday) state.day = isoDate(new Date());
    draft = null; closeSheet(); toast("Записано"); loadDay();
  } catch (err) { btn.disabled = false; toast(err.message); }
}

function openCustomFood(name, barcode = null, note = "") {
  const back = draft;
  openSheet(`
    <div class="row between"><h2>Свой продукт</h2><button class="ghost" id="cf-back">${back ? "←" : "✕"}</button></div>
    ${note ? `<p class="small">${esc(note)}</p>` : ""}
    <form id="cf">
      <label>Название</label><input name="name" value="${esc(name)}" required>
      <p class="small muted">Значения на 100 г — обычно есть на упаковке. Продукт сохранится, и его найдут все${barcode ? " — и по штрихкоду тоже" : ""}.</p>
      <div class="row"><div class="grow"><label>Ккал</label><input name="kcal_100" type="number" step="0.1" inputmode="decimal" required></div>
      <div class="grow"><label>Белки</label><input name="protein_100" type="number" step="0.1" inputmode="decimal" required></div></div>
      <div class="row"><div class="grow"><label>Жиры</label><input name="fat_100" type="number" step="0.1" inputmode="decimal" required></div>
      <div class="grow"><label>Углеводы</label><input name="carbs_100" type="number" step="0.1" inputmode="decimal" required></div></div>
      <button class="primary block" style="margin-top:14px">Добавить</button>
    </form>`);
  draft = null; // keep editor handlers quiet while this form is open
  $("#cf-back").onclick = () => { if (back) { draft = back; renderEditor(); } else closeSheet(); };
  $("#cf").onsubmit = async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    try {
      const num = (v) => +String(v).replace(",", ".");
      const food = await api("/api/foods", { method: "POST", json: {
        name: f.name, kcal_100: num(f.kcal_100), protein_100: num(f.protein_100), fat_100: num(f.fat_100), carbs_100: num(f.carbs_100), barcode } });
      if (back) { draft = back; draft.items.push({ ...food, grams: 100, options: [food] }); renderEditor(); }
      else openEditor({ title: food.name, items: [{ ...food, grams: 100, options: [food] }], comment: "" });
    } catch (err) { toast(err.message); }
  };
}

// ---------- barcode ----------

let scanStream = null, scanTimer = null;
function stopScan() {
  clearTimeout(scanTimer);
  scanStream?.getTracks().forEach((t) => t.stop());
  scanStream = null;
}

async function openScanner() {
  const live = "BarcodeDetector" in window && navigator.mediaDevices?.getUserMedia;
  openSheet(`
    <div class="row between"><h2>Штрихкод</h2><button class="ghost" data-close>✕</button></div>
    ${live ? '<div class="scan-box"><video id="scan-video" playsinline muted></video><i class="scan-line"></i></div><p class="small muted center">Наведи камеру на штрихкод</p>' : ""}
    <form id="code-form" class="row" style="margin-top:8px">
      <input name="code" inputmode="numeric" autocomplete="off" placeholder="${live ? "Или введи цифры под штрихкодом" : "Цифры под штрихкодом"}" required>
      <button class="primary">Найти</button>
    </form>
    <div class="error" id="scan-error"></div>`);
  $("#code-form").onsubmit = (e) => { e.preventDefault(); findBarcode(e.target.code.value); };
  if (!live) return;
  try {
    const formats = (await BarcodeDetector.getSupportedFormats()).filter((f) => ["ean_13", "ean_8", "upc_a", "upc_e"].includes(f));
    const detector = new BarcodeDetector({ formats });
    scanStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
    const video = $("#scan-video");
    video.srcObject = scanStream;
    await video.play();
    const tick = async () => {
      if (!scanStream) return;
      try {
        const [hit] = await detector.detect(video);
        if (hit?.rawValue) { stopScan(); navigator.vibrate?.(60); return findBarcode(hit.rawValue); }
      } catch { /* a frame that couldn't be read */ }
      scanTimer = setTimeout(tick, 200);
    };
    tick();
  } catch {
    stopScan();
    $(".scan-box")?.remove();
    $("#scan-error").textContent = "Камера недоступна — введи цифры вручную";
  }
}

async function findBarcode(raw) {
  const code = String(raw).replace(/\D/g, "");
  if (code.length < 6) { $("#scan-error").textContent = "Нужны цифры под штрихкодом"; return; }
  stopScan();
  sheetBody.innerHTML = '<div class="spinner"></div><p class="center muted">Ищу продукт…</p>';
  try {
    const food = await api(`/api/foods/barcode/${code}`);
    openEditor({ title: food.name, items: [{ ...food, grams: 100, options: [food] }], comment: "Проверь вес порции — по умолчанию 100 г." });
  } catch (err) {
    if (err.status === 401) { closeSheet(); return renderAuth(); }
    const note = err.status === 404
      ? `Штрихкода ${code} ещё нет в базе. Перепиши значения с этикетки — в следующий раз продукт найдётся сразу.`
      : err.message;
    openCustomFood("", code, note);
  }
}

// ---------- settings & admin ----------

async function openSettings() {
  const me = state.me || (state.me = await api("/api/me"));
  const g = me.goals;
  openSheet(`
    <div class="row between"><h2>Настройки</h2><button class="ghost" data-close>✕</button></div>
    <form class="card" id="goals">
      <b>Норма в день</b>
      <div class="row"><div class="grow"><label>Ккал</label><input name="kcal" type="number" value="${g.kcal}"></div>
      <div class="grow"><label>Белки, г</label><input name="protein" type="number" value="${g.protein}"></div></div>
      <div class="row"><div class="grow"><label>Жиры, г</label><input name="fat" type="number" value="${g.fat}"></div>
      <div class="grow"><label>Углеводы, г</label><input name="carbs" type="number" value="${g.carbs}"></div></div>
      <button class="primary block" style="margin-top:12px">Сохранить</button>
    </form>
    <div class="card" id="profile-card"><div class="spinner"></div></div>
    <div class="card" id="health-card"></div>
    <div class="card" id="notify-card"></div>
    <div class="card small">
      <div>Вы вошли как <b>${esc(me.username)}</b></div>
      <div class="muted">Распознавания сегодня: $${me.spent_today.toFixed(2)} из $${me.daily_limit.toFixed(2)}</div>
      <button class="block" id="logout" style="margin-top:10px">Выйти</button>
    </div>
    ${me.is_admin ? '<div class="card" id="admin"><div class="spinner"></div></div>' : ""}`);
  $("#goals").onsubmit = async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    try {
      state.me = await api("/api/me", { method: "PUT", json: { kcal: +f.kcal, protein: +f.protein, fat: +f.fat, carbs: +f.carbs, timezone: TZ } });
      closeSheet(); toast("Сохранено"); state.view === "reports" ? loadStats() : loadDay();
    } catch (err) { toast(err.message); }
  };
  $("#logout").onclick = async () => { await api("/api/auth/logout", { method: "POST" }); state.me = null; closeSheet(); renderAuth(); };
  if (me.is_admin) renderAdmin();
  loadProfile();
}

// ---------- profile: goal calculator & Apple Health ----------

const goalsLine = (s) => `<b>${s.kcal} ккал</b> · Б ${s.protein} · Ж ${s.fat} · У ${s.carbs}`;

async function applyGoals(s) {
  try {
    state.me = await api("/api/me", { method: "PUT", json: { kcal: s.kcal, protein: s.protein, fat: s.fat, carbs: s.carbs, timezone: TZ } });
    closeSheet(); toast("Норма обновлена");
    state.view === "reports" ? loadStats() : loadDay();
  } catch (err) { toast(err.message); }
}

async function loadProfile() {
  try { renderProfile(await api("/api/profile")); }
  catch (err) { const b = $("#profile-card"); if (b) b.innerHTML = `<div class="error">${esc(err.message)}</div>`; }
}

function renderProfile(p) {
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
        <div class="grow"><label>Рост, см</label><input name="height_cm" type="number" inputmode="decimal" value="${p.height_cm ?? ""}" required></div>
        <div class="grow"><label>Вес, кг</label><input name="weight_kg" type="number" step="0.1" inputmode="decimal" value="${p.weight_kg ?? ""}" required></div>
      </div>
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
      } }));
    } catch (err) { toast(err.message); }
  };
  if (f) $("#apply-formula").onclick = () => applyGoals(f.suggested);
  renderHealth(p.health);
  renderNotify(p.notify);
}

function renderHealth(h, token = null) {
  const box = $("#health-card");
  if (!box) return;
  const url = `${location.origin}/api/health`;
  if (token) {
    box.innerHTML = `
      <b>⌚ Подключение Apple Watch</b>
      <p class="small">Ключ показывается один раз — скопируй его в команду.</p>
      <label>Адрес</label><div class="row"><input readonly value="${esc(url)}"><button data-copy-text="${esc(url)}">Копировать</button></div>
      <label>Заголовок Authorization</label><div class="row"><input readonly value="Bearer ${esc(token)}"><button data-copy-text="Bearer ${esc(token)}">Копировать</button></div>
      <ol class="howto small">
        <li>«Команды» → «Автоматизация» → «+» → «Время суток»: 23:50, ежедневно, «Запускать сразу».</li>
        <li>«Найти образцы здоровья»: тип «Активная энергия», дата начала — «Сегодня». Затем «Вычислить статистику» → «Сумма».</li>
        <li>То же для типа «Шаги».</li>
        <li>«Получить содержимое URL»: адрес выше, метод POST, заголовок Authorization со значением выше, тело JSON: <code>active_kcal</code> — первая сумма, <code>steps</code> — вторая (оба «Число»).</li>
        <li>Вес можно не отправлять — пиши его в отчётах. Если взвешиваешься в «Здоровье», добавь <code>weight_kg</code>.</li>
      </ol>
      <button class="block" id="health-done">Готово</button>`;
    box.querySelectorAll("[data-copy-text]").forEach((b) => b.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(b.dataset.copyText); toast("Скопировано"); } catch { toast("Не удалось скопировать"); }
    }));
    $("#health-done").onclick = () => loadProfile();
    return;
  }
  box.innerHTML = `
    <b>⌚ Apple Watch и «Здоровье»</b>
    <p class="muted small" style="margin:4px 0 10px">${h.connected
      ? (h.last_day ? `Подключено. Последние данные — ${esc(dayLabel(h.last_day).toLowerCase())}.` : "Ключ выдан, данных пока не было.")
      : "Команда на iPhone раз в день отправляет активные калории и шаги. Они появятся в дневнике, отчётах и обзоре недели."}</p>
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

async function renderNotify(n) {
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
    <b>🔔 Напоминания и серии</b>
    <div style="margin:8px 0 4px">${device}</div>
    <form id="notify-form" class="toggles">
      <label class="check"><input type="checkbox" name="evening" ${n.evening ? "checked" : ""}> Напомнить вечером, если день пустой, в <select name="hour">${hours}</select></label>
      <label class="check"><input type="checkbox" name="weekly" ${n.weekly ? "checked" : ""}> Обзор недели по понедельникам</label>
      <label class="check"><input type="checkbox" name="share_streak" ${n.share_streak ? "checked" : ""}> Участвовать в общих сериях <span class="muted small">— твою серию увидят те, кто тоже участвует</span></label>
    </form>`;
  $("#notify-form").onchange = async (e) => {
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
    renderNotify(n);
  });
  $("#push-test")?.addEventListener("click", async () => {
    try { await api("/api/push/test", { method: "POST" }); toast("Отправил — проверь уведомления"); } catch (err) { toast(err.message); }
  });
  $("#push-off")?.addEventListener("click", async () => {
    const sub = await deviceSubscription();
    if (sub) { await api("/api/push/unsubscribe", { method: "POST", json: { endpoint: sub.endpoint } }).catch(() => {}); await sub.unsubscribe(); }
    renderNotify(n);
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
    state.me = await api("/api/me");
    if (state.me.timezone !== TZ) api("/api/me", { method: "PUT", json: { ...state.me.goals, timezone: TZ } }).then((m) => (state.me = m)).catch(() => {});
    // A tap on the weekly review notification opens /#reports.
    if (location.hash === "#reports") { history.replaceState(null, "", "/"); await loadStats(); }
    else await loadDay();
  } catch (err) {
    if (err.status === 401) renderAuth();
    else app.innerHTML = `<div class="card error">${esc(err.message)}</div>`;
  }
}

// Coming back to the app on a new day should show the new day.
let todayAtStart = isoDate(new Date());
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible" || !state.dayData || !sheet.hidden) return;
  const today = isoDate(new Date());
  if (state.day === todayAtStart) state.day = today;
  if (todayAtStart !== today) state.review = null;
  todayAtStart = today;
  if (state.view === "reports") loadStats(); else loadDay();
});

start();
