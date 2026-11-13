"use strict";

// ---------------------------------------------------------------- связь с программой
// В самой программе методы даёт pywebview. Если открыть index.html просто в браузере,
// включается демо-режим с выдуманными клипами — удобно править оформление.

const demo = {
  settings: {
    clips_dir: "", clips_dir_resolved: "C:\\Users\\you\\Videos\\MidnightClips", buffer_enabled: true,
    buffer_seconds: 60, fps: 60, height: 0, bitrate_mbps: 12, monitor: 0, encoder: "auto", capture: "auto",
    audio: true, mic: "", audio_offset_ms: 0, hotkey_clip: "Alt+F10", hotkey_record: "Alt+F9",
    folder_per_game: true, sound: true, toast: true, autostart: false, auto_update: true, discord_webhook: "", discord_limit_mb: 10, cleanup_days: 0, cleanup_gb: 0, rec_volume_system: 60, rec_volume_mic: 100, indicator: true, indicator_corner: "tl", sound_volume: 100, sound_saved: "C:\\Sounds\\velvet.mp3", sound_rec_on: "", sound_rec_off: "", sound_error: "",
  },
  clips: [
    ["P3R 2026-11-12 21-40-03", "P3R", 60, 88e6, 0], ["P3R 2026-11-12 21-12-47", "P3R", 60, 91e6, 0],
    ["hl2 запись 2026-11-11 19-05-10", "hl2", 754, 1.1e9, 1], ["Тройное убийство", "cs2", 30, 44e6, 0],
    ["cs2 2026-11-10 23-58-31", "cs2", 30, 46e6, 0], ["Рабочий стол 2026-11-09 14-02-00", "Рабочий стол", 15, 9e6, 0],
  ].map((c, i) => ({ path: "demo" + i, name: c[0], game: c[1], duration: c[2], size: c[3], recording: !!c[4], mtime: Date.now() / 1000 - i * 5400,
    favorite: i === 0 || i === 3, tags: [["босс"], [], [], ["эйс", "смешное"], ["эйс"], []][i] })),
};
const demoApi = {
  get_state: async () => ({ state: "on", message: "", encoder: "NVIDIA NVENC", capture: "Desktop Duplication", audio_note: "", fps_real: 59.8, fps_target: 60, slow: false, fallback: false, recording: !!demo.rec, hotkeys: { clip: true, record: true }, version: "демо", update: { status: "ready", latest: "0.1.7", current: "0.1.5", message: "" } }),
  get_settings: async () => demo.settings,
  set_settings: async (v) => Object.assign(demo.settings, v),
  get_options: async () => ({ monitors: ["Монитор 1 — 2560×1440 (основной)", "Монитор 2 — 1920×1080"], mics: ["Микрофон (USB Audio)"] }),
  list_clips: async () => demo.clips,
  get_thumb: async (path) => {
    const hue = (parseInt(path.slice(4), 10) * 67) % 360;
    const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='400' height='225'><rect width='400' height='225' fill='hsl(${hue},45%,28%)'/><circle cx='300' cy='70' r='90' fill='hsl(${hue + 40},60%,45%)'/><rect y='150' width='400' height='75' fill='hsl(${hue},40%,16%)'/></svg>`;
    return { image: "data:image/svg+xml," + encodeURIComponent(svg), duration: null };
  },
  save_clip: async () => { setTimeout(() => window.onBackendEvent("saving", {}), 50); setTimeout(() => window.onBackendEvent("saved", { kind: "clip", path: "Клип.mp4" }), 900); },
  toggle_record: async () => { demo.rec = !demo.rec; window.onBackendEvent("state", { state: "on", encoder: "NVIDIA NVENC", capture: "Desktop Duplication", recording: demo.rec, recording_since: Date.now() / 1000 }); },
  rename_clip: async (p, n) => { demo.clips.find((c) => c.path === p).name = n; return p; },
  delete_clip: async (p) => { demo.clips = demo.clips.filter((c) => c.path !== p); return true; },
  copy_clip: async () => true,
  pick_sound: async () => demo.settings,
  delete_clips: async (paths) => { demo.clips = demo.clips.filter((c) => !paths.includes(c.path)); return paths.length; },
  set_favorites: async (paths, v) => { demo.clips.forEach((c) => { if (paths.includes(c.path)) c.favorite = v; }); return paths.length; },
  set_tags: async (p, tags) => { demo.clips.find((c) => c.path === p).tags = tags; return true; },
  cleanup_preview: async (days, gb) => ({ count: days || gb ? 3 : 0, bytes: 1.25e9 }),
  media_url: async () => window.DEMO_VIDEO || "",
  clip_action: async (p, action) => {
    let n = 0;
    const timer = setInterval(() => {
      n += 20;
      window.onBackendEvent("job", { stage: action === "send" ? "Отправляю в Discord…" : "Сжимаю до 10 МБ…", percent: action === "send" ? null : n });
      if (n >= 100) { clearInterval(timer); window.onBackendEvent("job_done", { ok: true, action, text: "Готово (демо)", path: "" }); }
    }, 300);
    return true;
  },
};
const api = new Proxy({}, {
  get: (_t, name) => (...args) => {
    const real = window.pywebview && window.pywebview.api && window.pywebview.api[name];
    if (real) return real(...args);
    return demoApi[name] ? demoApi[name](...args) : Promise.resolve(true);
  },
});

// ---------------------------------------------------------------- мелочи

const $ = (id) => document.getElementById(id);
let settings = {};
let clips = [];
let gameFilter = "";
let favOnly = false;
let tagFilter = "";
let shownClips = [];
const picked = new Set();   // пути выбранных клипов
let recSince = null;

function flash(text, isError) {
  const el = document.createElement("div");
  el.className = "flash" + (isError ? " is-error" : "");
  el.textContent = text;
  $("flashes").appendChild(el);
  setTimeout(() => el.remove(), isError ? 6000 : 3200);
}

function fmtDuration(sec) {
  if (sec == null) return "";
  sec = Math.round(sec);
  const m = Math.floor(sec / 60), s = sec % 60;
  return (m >= 60 ? Math.floor(m / 60) + ":" + String(m % 60).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
}
function fmtSize(bytes) {
  return bytes >= 1e9 ? (bytes / 1073741824).toFixed(1) + " ГБ" : Math.max(1, Math.round(bytes / 1048576)) + " МБ";
}
function fmtDate(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const time = d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return "сегодня, " + time;
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "long" }) + ", " + time;
}
function fmtBuffer(sec) {
  return sec >= 60 ? "последние " + sec / 60 + " мин" : "последние " + sec + " с";
}

function ask({ title, text, input, okLabel }) {
  return new Promise((resolve) => {
    $("modalTitle").textContent = title;
    $("modalText").textContent = text || "";
    const field = $("modalInput");
    field.hidden = input == null;
    field.value = input || "";
    $("modalOk").textContent = okLabel || "Ок";
    $("modal").hidden = false;
    (input == null ? $("modalOk") : field).focus();
    if (input != null) field.select();
    const done = (value) => { $("modal").hidden = true; cleanup(); resolve(value); };
    const onOk = () => done(input == null ? true : field.value.trim());
    const onCancel = () => done(null);
    const onKey = (e) => { if (e.key === "Enter") onOk(); if (e.key === "Escape") onCancel(); };
    function cleanup() {
      $("modalOk").removeEventListener("click", onOk);
      $("modalCancel").removeEventListener("click", onCancel);
      $("modal").removeEventListener("keydown", onKey);
    }
    $("modalOk").addEventListener("click", onOk);
    $("modalCancel").addEventListener("click", onCancel);
    $("modal").addEventListener("keydown", onKey);
  });
}

// ---------------------------------------------------------------- состояние эфира

function applyState(st) {
  const tv = $("tv");
  recSince = st.recording ? (st.recording_since || Date.now() / 1000) : null;
  tv.dataset.state = st.recording ? "rec" : st.state;
  const titles = { on: "В эфире", starting: "Настраиваю эфир…", off: "Эфир выключен", error: "Нет сигнала" };
  $("statusLine").textContent = st.recording ? "Идёт запись" : (titles[st.state] || "");
  let sub = st.message || "";
  if (st.state === "on") {
    sub = [st.encoder, st.capture].filter(Boolean).join(", ");
    if (!settings.buffer_enabled) sub = "Буфер повтора выключен. " + sub;
    if (st.audio_note) sub = st.audio_note;
  }
  let warn = false;
  if (st.state === "on" && st.slow) {
    sub = "Запись не успевает: " + Math.round(st.fps_real) + " из " + st.fps_target + " кадров в секунду. Выберите набор попроще в настройках.";
    warn = true;
  } else if (st.state === "on" && st.fallback && !st.audio_note) {
    sub += ". Запасной режим — возможны рывки";
    warn = true;
  }
  $("statusSub").textContent = sub;
  $("statusSub").classList.toggle("is-warn", warn);
  $("captureInfo").textContent = st.state === "on"
    ? [st.encoder, st.capture, st.fps_real != null ? "пишется " + Math.round(st.fps_real) + " из " + st.fps_target + " кадров в секунду" : ""].filter(Boolean).join(", ")
    : (st.message || "");
  $("btnRec").classList.toggle("is-on", !!st.recording);
  $("recTitle").textContent = st.recording ? "Остановить" : "Начать запись";
  if (st.hotkeys) applyHotkeyStatus(st.hotkeys);
  if (st.version) $("version").textContent = "MidnightClips " + st.version;
  if (st.update) applyUpdate(st.update);
}
function applyUpdate(u) {
  const texts = {
    disabled: "Обновления работают только в собранной программе (exe)",
    checking: "Проверяю…", downloading: "Скачиваю версию " + u.latest + "…",
    ready: "Версия " + u.latest + " скачана и готова к установке",
    current: "Установлена последняя версия", error: u.message, idle: "",
  };
  $("updateInfo").textContent = "Сейчас " + u.current + ". " + (texts[u.status] || "");
  $("updateBar").hidden = u.status !== "ready";
  $("updateBarText").textContent = "Готово обновление " + u.latest;
  $("btnCheckUpdate").disabled = u.status === "checking" || u.status === "downloading" || u.status === "disabled";
}
function applyHotkeyStatus(h) {
  $("warnClip").hidden = h.clip !== false;
  $("warnRec").hidden = h.record !== false;
}
setInterval(() => {
  if (recSince) $("recTimer").textContent = fmtDuration(Math.max(0, Date.now() / 1000 - recSince));
}, 500);

window.onBackendEvent = (kind, data) => {
  if (kind === "state") applyState(data);
  else if (kind === "hotkeys") applyHotkeyStatus(data);
  else if (kind === "update") applyUpdate(data);
  else if (kind === "job") showJob(data);
  else if (kind === "cleaned") {
    flash("Автоочистка: в корзину ушло клипов — " + data.count + " (" + fmtSize(data.bytes) + ")");
    loadClips();
  }
  else if (kind === "job_done") finishJob(data);
  else if (kind === "saving") $("btnClip").classList.add("is-busy");
  else if (kind === "saved") {
    $("btnClip").classList.remove("is-busy");
    flash(data.kind === "clip" ? "Клип сохранён" : "Запись сохранена");
    loadClips();
  } else if (kind === "error") { $("btnClip").classList.remove("is-busy"); flash(data.text, true); }
  else if (kind === "warning") flash(data.text, true);
};

// ---------------------------------------------------------------- клипы

const thumbObserver = new IntersectionObserver((entries) => {
  entries.forEach((entry) => {
    if (!entry.isIntersecting) return;
    thumbObserver.unobserve(entry.target);
    const el = entry.target;
    api.get_thumb(el.dataset.path).then((res) => {
      if (!res) return;
      if (res.image) { el.style.backgroundImage = `url("${res.image}")`; el.classList.add("has-image"); }
      if (res.duration != null) {
        const badge = el.querySelector(".badge-time");
        if (badge) { badge.textContent = fmtDuration(res.duration); badge.hidden = false; }
      }
    });
  });
}, { rootMargin: "200px" });

function renderChips() {
  const games = [...new Set(clips.map((c) => c.game).filter(Boolean))];
  const tags = [...new Set(clips.flatMap((c) => c.tags || []))].sort((a, b) => a.localeCompare(b, "ru"));
  const hasFav = clips.some((c) => c.favorite);
  const box = $("chips");
  box.innerHTML = "";
  if (games.length < 2) gameFilter = "";
  if (gameFilter && !games.includes(gameFilter)) gameFilter = "";
  if (tagFilter && !tags.includes(tagFilter)) tagFilter = "";
  if (!hasFav) favOnly = false;
  if (games.length < 2 && !tags.length && !hasFav) return;
  const chip = (label, active, cls, fn) => {
    const b = document.createElement("button");
    b.className = "chip" + (cls ? " " + cls : "") + (active ? " is-active" : "");
    b.textContent = label;
    b.onclick = () => { fn(); renderChips(); renderClips(); };
    box.appendChild(b);
  };
  chip("Все", !gameFilter && !favOnly && !tagFilter, "", () => { gameFilter = ""; favOnly = false; tagFilter = ""; });
  if (hasFav) chip("★ Избранное", favOnly, "chip-fav", () => { favOnly = !favOnly; });
  if (games.length >= 2) games.forEach((g) => chip(g, g === gameFilter, "", () => { gameFilter = g === gameFilter ? "" : g; }));
  tags.forEach((t) => chip("#" + t, t === tagFilter, "chip-tag", () => { tagFilter = t === tagFilter ? "" : t; }));
}

function renderClips() {
  const query = $("search").value.trim().toLowerCase();
  const grid = $("grid");
  grid.innerHTML = "";
  const shown = clips.filter((c) => (!gameFilter || c.game === gameFilter)
    && (!favOnly || c.favorite) && (!tagFilter || (c.tags || []).includes(tagFilter))
    && (!query || c.name.toLowerCase().includes(query) || (c.tags || []).some((t) => t.toLowerCase().includes(query))));
  $("empty").hidden = clips.length > 0;
  shownClips = shown;
  shown.forEach((clip) => {
    const card = document.createElement("article");
    card.className = "card";
    card.dataset.path = clip.path;

    const pick = document.createElement("button");
    pick.className = "pick";
    pick.textContent = "✓";
    pick.title = "Выбрать";
    pick.setAttribute("aria-label", "Выбрать клип " + clip.name);
    pick.onclick = (e) => togglePick(clip.path, e.shiftKey);

    const thumb = document.createElement("button");
    thumb.className = "thumb";
    thumb.dataset.path = clip.path;
    thumb.title = "Смотреть и обрезать";
    thumb.innerHTML = '<span class="play"></span><span class="badge badge-time" hidden></span>' + (clip.recording ? '<span class="badge badge-rec">запись</span>' : "");
    if (clip.duration != null) { const b = thumb.querySelector(".badge-time"); b.textContent = fmtDuration(clip.duration); b.hidden = false; }
    thumb.onclick = (e) => {   // пока что-то выбрано, нажатие отмечает клип — как в галерее телефона
      if (picked.size || e.ctrlKey || e.shiftKey) togglePick(clip.path, e.shiftKey);
      else openEditor(clip);
    };
    thumbObserver.observe(thumb);

    const star = document.createElement("button");
    star.className = "star" + (clip.favorite ? " is-on" : "");
    star.textContent = "★";
    star.title = clip.favorite ? "Убрать из избранного" : "В избранное — автоочистка такие клипы не трогает";
    star.setAttribute("aria-pressed", clip.favorite ? "true" : "false");
    star.onclick = async () => {
      clip.favorite = !clip.favorite;
      await api.set_favorite(clip.path, clip.favorite);
      renderChips();
      renderClips();
    };

    const tagBox = document.createElement("div");
    tagBox.className = "card-tags";
    (clip.tags || []).forEach((t) => {
      const b = document.createElement("button");
      b.className = "tag";
      b.textContent = "#" + t;
      b.title = "Показать клипы с этим тегом";
      b.onclick = () => { tagFilter = t; renderChips(); renderClips(); };
      tagBox.appendChild(b);
    });

    const name = document.createElement("div");
    name.className = "card-name";
    name.textContent = name.title = clip.name;
    const meta = document.createElement("div");
    meta.className = "card-meta";
    meta.textContent = fmtDate(clip.mtime) + ", " + fmtSize(clip.size);

    const actions = document.createElement("div");
    actions.className = "card-actions";
    const action = (label, fn, cls) => {
      const b = document.createElement("button");
      b.className = "link" + (cls ? " " + cls : "");
      b.textContent = label;
      b.onclick = fn;
      actions.appendChild(b);
    };
    action("Скопировать", async () => {
      flash((await api.copy_clip(clip.path)) ? "Скопировано — вставьте через Ctrl+V" : "Не удалось скопировать", false);
    });
    action("Показать в папке", () => api.reveal_clip(clip.path));
    action("Теги", async () => {
      const value = await ask({ title: "Теги клипа", text: "Через запятую, например: эйс, смешное, босс", input: (clip.tags || []).join(", "), okLabel: "Сохранить" });
      if (value == null) return;
      await api.set_tags(clip.path, value.split(",").map((t) => t.trim()).filter(Boolean));
      loadClips();
    });
    action("Переименовать", async () => {
      const value = await ask({ title: "Переименовать клип", input: clip.name, okLabel: "Переименовать" });
      if (!value || value === clip.name) return;
      if (await api.rename_clip(clip.path, value)) loadClips();
      else flash("Не удалось переименовать: такое имя уже есть или в нём недопустимые знаки", true);
    });
    action("Удалить", async () => {
      const ok = await ask({ title: "Удалить клип?", text: clip.name + " отправится в корзину.", okLabel: "Удалить" });
      if (ok && (await api.delete_clip(clip.path))) loadClips();
    }, "link-danger");

    card.append(thumb, pick, star, name, meta, tagBox, actions);
    grid.appendChild(card);
  });
  syncPicked();
}

// ---------------------------------------------------------------- выбор нескольких клипов

let lastPicked = null;
function syncPicked() {
  const existing = new Set(clips.map((c) => c.path));
  [...picked].forEach((p) => { if (!existing.has(p)) picked.delete(p); });
  document.querySelectorAll("#grid .card").forEach((card) => {
    const on = picked.has(card.dataset.path);
    card.classList.toggle("is-picked", on);
    card.querySelector(".pick").setAttribute("aria-pressed", on ? "true" : "false");
  });
  $("grid").classList.toggle("is-selecting", picked.size > 0);
  $("selectBar").hidden = picked.size === 0 || !$("view-clips").classList.contains("is-active");
  if (picked.size) {
    const size = clips.filter((c) => picked.has(c.path)).reduce((sum, c) => sum + c.size, 0);
    $("selectText").textContent = "Выбрано: " + picked.size + ", " + fmtSize(size);
    const allFav = clips.filter((c) => picked.has(c.path)).every((c) => c.favorite);
    $("selFav").textContent = allFav ? "Убрать из избранного" : "В избранное";
  }
}
function togglePick(path, range) {
  if (range && lastPicked && lastPicked !== path) {   // с Shift — весь промежуток
    const order = shownClips.map((c) => c.path);
    const a = order.indexOf(lastPicked), b = order.indexOf(path);
    if (a >= 0 && b >= 0) order.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((p) => picked.add(p));
  } else if (picked.has(path)) picked.delete(path);
  else picked.add(path);
  lastPicked = path;
  syncPicked();
}
function clearPicked() { picked.clear(); lastPicked = null; syncPicked(); }

$("selCancel").onclick = clearPicked;
$("selAll").onclick = () => { shownClips.forEach((c) => picked.add(c.path)); syncPicked(); };
$("selFav").onclick = async () => {
  const chosen = clips.filter((c) => picked.has(c.path));
  const value = !chosen.every((c) => c.favorite);
  await api.set_favorites(chosen.map((c) => c.path), value);
  chosen.forEach((c) => { c.favorite = value; });
  flash(value ? "Добавлено в избранное: " + chosen.length : "Убрано из избранного: " + chosen.length);
  clearPicked();
  loadClips();
};
async function deletePicked() {
  const paths = [...picked];
  if (!paths.length) return;
  const size = clips.filter((c) => picked.has(c.path)).reduce((sum, c) => sum + c.size, 0);
  const ok = await ask({ title: "Удалить выбранные клипы?", text: "В корзину отправится клипов: " + paths.length + " (" + fmtSize(size) + ").", okLabel: "Удалить" });
  if (!ok) return;
  const count = await api.delete_clips(paths);
  flash("Удалено клипов: " + count, count !== paths.length);
  clearPicked();
  loadClips();
}
$("selDelete").onclick = deletePicked;

document.addEventListener("keydown", (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (!$("view-clips").classList.contains("is-active") || !$("editor").hidden || !$("modal").hidden || typing) return;
  if (e.key === "Escape" && picked.size) clearPicked();
  else if (e.key === "Delete" && picked.size) deletePicked();
  else if ((e.ctrlKey || e.metaKey) && e.code === "KeyA" && shownClips.length) {
    e.preventDefault();
    shownClips.forEach((c) => picked.add(c.path));
    syncPicked();
  }
});

// Обводка рамкой: зажать мышь на свободном месте и протянуть по клипам.
(() => {
  const view = $("view-clips"), box = $("lasso");
  let start = null, base = null, active = false;
  const hit = (r, x1, y1, x2, y2) => r.left < x2 && r.right > x1 && r.top < y2 && r.bottom > y1;
  function update(e) {
    const x1 = Math.min(start.x, e.clientX), y1 = Math.min(start.y, e.clientY);
    const x2 = Math.max(start.x, e.clientX), y2 = Math.max(start.y, e.clientY);
    Object.assign(box.style, { left: x1 + "px", top: y1 + "px", width: (x2 - x1) + "px", height: (y2 - y1) + "px" });
    picked.clear();
    base.forEach((p) => picked.add(p));
    document.querySelectorAll("#grid .card").forEach((card) => {
      if (hit(card.querySelector(".thumb").getBoundingClientRect(), x1, y1, x2, y2)) picked.add(card.dataset.path);
    });
    syncPicked();
  }
  view.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || e.target.closest("button, input, select, a, .view-head")) return;
    if (e.clientX > view.getBoundingClientRect().right - 14) return;   // полоса прокрутки
    start = { x: e.clientX, y: e.clientY };
    base = e.ctrlKey || e.shiftKey ? new Set(picked) : new Set();
    active = false;
  });
  window.addEventListener("mousemove", (e) => {
    if (!start) return;
    if (!active && Math.hypot(e.clientX - start.x, e.clientY - start.y) < 6) return;
    active = true;
    box.hidden = false;
    const r = view.getBoundingClientRect();   // у края списка — прокручиваем
    if (e.clientY > r.bottom - 30) view.scrollBy(0, 18);
    else if (e.clientY < r.top + 30) view.scrollBy(0, -18);
    update(e);
  });
  window.addEventListener("mouseup", () => {
    if (start && !active && picked.size && !base.size) clearPicked();   // клик по пустому месту снимает выбор
    start = null;
    active = false;
    box.hidden = true;
  });
})();

async function loadClips() {
  clips = (await api.list_clips()) || [];
  renderChips();
  renderClips();
}

// ---------------------------------------------------------------- просмотр и обрезка

const video = $("edVideo");
let edClip = null;
let edBusy = false;

function fmtTime(sec) {
  const m = Math.floor(sec / 60), s = sec - m * 60;
  return m + ":" + (s < 10 ? "0" : "") + s.toFixed(1);
}
function trimValues() {
  return [parseFloat($("trimStart").value), parseFloat($("trimEnd").value)];
}
function drawTrim() {
  const dur = video.duration || 0;
  if (!dur) return;
  const [a, b] = trimValues();
  $("trimRange").style.left = (a / dur * 100) + "%";
  $("trimRange").style.width = ((b - a) / dur * 100) + "%";
  const whole = a < 0.05 && b > dur - 0.05;
  $("trimText").textContent = whole
    ? "Весь клип, " + fmtTime(dur)
    : "Отрезок " + fmtTime(a) + " – " + fmtTime(b) + ", длина " + fmtTime(b - a);
  $("edTrim").disabled = whole || edBusy;
}
function setTrim(a, b) {
  const dur = video.duration || 0;
  a = Math.max(0, Math.min(a, dur - 0.5));
  b = Math.max(a + 0.5, Math.min(b, dur));
  $("trimStart").value = a;
  $("trimEnd").value = b;
  drawTrim();
}
async function openEditor(clip) {
  edClip = clip;
  $("edName").textContent = clip.name;
  $("editor").hidden = false;
  $("edJob").hidden = true;
  $("trimText").textContent = "Загружаю клип…";
  video.src = await api.media_url(clip.path);
  $("edClose").focus();
}
function closeEditor() {
  if (edBusy) api.cancel_job();
  video.pause();
  video.removeAttribute("src");
  video.load();
  $("editor").hidden = true;
  edClip = null;
}
video.addEventListener("loadedmetadata", () => {
  const dur = video.duration;
  ["trimStart", "trimEnd"].forEach((id) => { $(id).max = dur; });
  setTrim(0, dur);
});
video.addEventListener("error", () => { if (edClip) $("trimText").textContent = "Этот клип не открывается во встроенном плеере — нажмите «Открыть в плеере»."; });
video.addEventListener("timeupdate", () => {
  const dur = video.duration || 0;
  if (!dur) return;
  const [a, b] = trimValues();
  $("trimPlay").style.left = (video.currentTime / dur * 100) + "%";
  if (!video.paused && b < dur - 0.05 && video.currentTime >= b) { video.pause(); video.currentTime = a; }
});
$("trimStart").addEventListener("input", () => { const [a, b] = trimValues(); setTrim(Math.min(a, b - 0.5), b); video.currentTime = trimValues()[0]; });
$("trimEnd").addEventListener("input", () => { const [a, b] = trimValues(); setTrim(a, Math.max(b, a + 0.5)); video.currentTime = trimValues()[1]; });
$("trimSetStart").onclick = () => setTrim(video.currentTime, trimValues()[1]);
$("trimSetEnd").onclick = () => setTrim(trimValues()[0], video.currentTime);
$("trimReset").onclick = () => setTrim(0, video.duration || 0);
$("edClose").onclick = closeEditor;
$("edOpen").onclick = () => edClip && api.open_clip(edClip.path);
$("edCancel").onclick = () => api.cancel_job();
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("editor").hidden && $("modal").hidden) closeEditor();
});

function setEditorBusy(busy) {
  edBusy = busy;
  ["edCompress", "edSend"].forEach((id) => { $(id).disabled = busy; });
  drawTrim();
}
async function runAction(action) {
  if (!edClip || edBusy) return;
  const dur = video.duration || 0;
  let [a, b] = dur ? trimValues() : [null, null];
  if (dur && a < 0.05 && b > dur - 0.05) { a = null; b = null; }
  if (action === "send" && !(settings.discord_webhook || "").trim()) {
    flash("Сначала вставьте ссылку вебхука: Настройки → Discord", true);
    return;
  }
  video.pause();
  setEditorBusy(true);
  showJob({ stage: "Начинаю…", percent: null });
  if (!(await api.clip_action(edClip.path, action, a, b))) {
    finishJob({ ok: false, text: "Сейчас занято другой задачей — подождите." });
  }
}
function showJob(job) {
  $("edJob").hidden = false;
  $("edJobText").textContent = job.stage + (job.percent != null ? " " + job.percent + "%" : "");
  $("edJobFill").parentElement.classList.toggle("is-unknown", job.percent == null);
  $("edJobFill").style.width = job.percent != null ? job.percent + "%" : "";
}
function finishJob(result) {
  setEditorBusy(false);
  $("edJob").hidden = true;
  flash(result.text, !result.ok);
  if (result.ok && result.action !== "send") loadClips();
}
$("edTrim").onclick = () => runAction("trim");
$("edCompress").onclick = () => runAction("compress");
$("edSend").onclick = () => runAction("send");

// ---------------------------------------------------------------- настройки

function applySettings(s) {
  settings = s;
  document.querySelectorAll("[data-key]").forEach((el) => {
    const value = s[el.dataset.key];
    if (el.type === "checkbox") el.checked = !!value;
    else {
      if (el.tagName === "SELECT" && ![...el.options].some((o) => o.value === String(value))) {
        el.add(new Option(String(value), String(value)));
      }
      el.value = value;
    }
  });
  document.querySelectorAll("[data-cleanup]").forEach((el) => { el.value = String(s[el.dataset.cleanup] || 0); });
  document.querySelectorAll("[data-hotkey]").forEach((el) => { el.textContent = s[el.dataset.hotkey] || "Не задано"; });
  $("kbdClip").textContent = $("kbdEmpty").textContent = s.hotkey_clip || "кнопкой";
  $("kbdRec").textContent = s.hotkey_record || "кнопкой";
  $("clipLen").textContent = fmtBuffer(s.buffer_seconds);
  $("clipsDir").textContent = s.clips_dir_resolved || "";
  $("edCompress").textContent = "Сжать до " + s.discord_limit_mb + " МБ";
  $("volText").textContent = s.sound_volume + "%" + (s.sound_volume > 100 ? " — громче исходного файла" : "");
  const recVol = (v) => v + "% — " + (v === 100 ? "как есть" : v === 0 ? "в записи не слышно" : v < 100 ? "в записи тише" : "в записи громче")
    + ". Меняет только запись: в наушниках всё звучит как раньше";
  $("volSysText").textContent = recVol(s.rec_volume_system);
  $("volMicText").textContent = recVol(s.rec_volume_mic);
  renderSounds();
  renderPresets();
}
async function saveSetting(key, value) {
  applySettings(await api.set_settings({ [key]: value }));
  applyState(await api.get_state());
}
document.querySelectorAll("[data-key]").forEach((el) => {
  el.addEventListener("change", () => {
    let value = el.type === "checkbox" ? el.checked : el.value;
    if (el.dataset.type === "int") value = parseInt(value, 10) || 0;
    saveSetting(el.dataset.key, value);
  });
});

// Готовые наборы настроек
const PRESETS = [
  ["Слабый компьютер", "720p, 30 кадров, клип 30 секунд. Меньше всего нагрузки", { height: 720, fps: 30, bitrate_mbps: 6, buffer_seconds: 30 }],
  ["Для Discord", "720p, 60 кадров, клип 30 секунд. Лёгкие файлы", { height: 720, fps: 60, bitrate_mbps: 6, buffer_seconds: 30 }],
  ["Стандарт", "1080p, 60 кадров, клип 1 минута", { height: 1080, fps: 60, bitrate_mbps: 12, buffer_seconds: 60 }],
  ["Высокое качество", "Как на экране, 60 кадров, клип 1 минута", { height: 0, fps: 60, bitrate_mbps: 20, buffer_seconds: 60 }],
  ["Плавность", "Как на экране, 120 кадров. Нужны мощная видеокарта и быстрый монитор", { height: 0, fps: 120, bitrate_mbps: 50, buffer_seconds: 60 }],
  ["Долгие моменты", "1080p, 30 кадров, клип 10 минут. Для длинных записей", { height: 1080, fps: 30, bitrate_mbps: 12, buffer_seconds: 600 }],
];
function renderPresets() {
  const box = $("presets");
  box.innerHTML = "";
  PRESETS.forEach(([title, text, values]) => {
    const b = document.createElement("button");
    const active = Object.keys(values).every((k) => settings[k] === values[k]);
    b.className = "preset" + (active ? " is-active" : "");
    b.setAttribute("aria-pressed", active ? "true" : "false");
    const strong = document.createElement("strong");
    strong.textContent = title;
    const span = document.createElement("span");
    span.textContent = text;
    b.append(strong, span);
    b.onclick = async () => {
      applySettings(await api.set_settings(values));
      flash("Набор «" + title + "» включён");
      applyState(await api.get_state());
    };
    box.appendChild(b);
  });
}

// Автоочистка: перед включением показываем, сколько клипов уйдёт прямо сейчас.
document.querySelectorAll("[data-cleanup]").forEach((el) => {
  el.addEventListener("change", async () => {
    const key = el.dataset.cleanup;
    const value = parseInt(el.value, 10) || 0;
    const days = key === "cleanup_days" ? value : settings.cleanup_days || 0;
    const gb = key === "cleanup_gb" ? value : settings.cleanup_gb || 0;
    if (value > 0) {
      const plan = await api.cleanup_preview(days, gb);
      if (plan && plan.count > 0) {
        const ok = await ask({
          title: "Включить автоочистку?",
          text: "Сразу после включения в корзину уйдёт клипов: " + plan.count + " (" + fmtSize(plan.bytes) + "). Избранное останется.",
          okLabel: "Включить",
        });
        if (!ok) { el.value = String(settings[key] || 0); return; }
      }
    }
    saveSetting(key, value);
  });
});

// Свои звуки сигналов
const SOUNDS = [["saved", "Клип или запись сохранены"], ["rec_on", "Запись началась"], ["rec_off", "Запись остановлена"], ["error", "Что-то не получилось"]];
function renderSounds() {
  const box = $("soundRows");
  box.innerHTML = "";
  SOUNDS.forEach(([event, label]) => {
    const file = settings["sound_" + event] || "";
    const row = document.createElement("div");
    row.className = "row";
    const text = document.createElement("span");
    text.textContent = label;
    const small = document.createElement("small");
    small.textContent = file ? file.split(/[\\/]/).pop() : "Стандартный звук";
    text.appendChild(small);
    const buttons = document.createElement("span");
    buttons.className = "row-buttons";
    const add = (title, fn) => { const b = document.createElement("button"); b.className = "mini"; b.textContent = title; b.onclick = fn; buttons.appendChild(b); };
    add("Прослушать", () => api.test_sound(event));
    add("Выбрать файл", async () => applySettings(await api.pick_sound(event)));
    if (file) add("Вернуть стандартный", () => saveSetting("sound_" + event, ""));
    row.append(text, buttons);
    box.appendChild(row);
  });
}
document.querySelector('[data-key="sound_volume"]').addEventListener("change", () => setTimeout(() => api.test_sound("saved"), 250));

// Назначение горячей клавиши: нажали кнопку — ждём сочетание.
const KEY_NAMES = { Space: "Space", Enter: "Enter", Tab: "Tab", Insert: "Insert", Delete: "Delete", Home: "Home", End: "End",
  PageUp: "PageUp", PageDown: "PageDown", ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right",
  Pause: "Pause", ScrollLock: "ScrollLock", Backquote: "`", Minus: "-", Equal: "=", BracketLeft: "[", BracketRight: "]",
  Backslash: "\\", Semicolon: ";", Quote: "'", Comma: ",", Period: ".", Slash: "/" };
function keyName(code) {
  if (/^F\d{1,2}$/.test(code)) return code;
  if (/^Key[A-Z]$/.test(code)) return code.slice(3);
  if (/^Digit\d$/.test(code)) return code.slice(5);
  if (/^Numpad\d$/.test(code)) return "Num" + code.slice(6);
  return KEY_NAMES[code] || null;
}
document.querySelectorAll("[data-hotkey]").forEach((btn) => {
  btn.addEventListener("click", () => {
    btn.classList.add("is-listening");
    btn.textContent = "Нажмите сочетание…";
    const onKey = (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (e.key === "Escape") return finish(null);
      if (e.key === "Backspace") return finish("");
      const key = keyName(e.code);
      if (!key) return;  // пока нажаты только Ctrl/Alt/Shift — ждём дальше
      const mods = [e.ctrlKey && "Ctrl", e.altKey && "Alt", e.shiftKey && "Shift"].filter(Boolean);
      if (!mods.length && !/^F\d+$/.test(key)) { flash("Добавьте Ctrl, Alt или Shift — иначе клавиша перестанет печатать", true); return; }
      finish([...mods, key].join("+"));
    };
    function finish(combo) {
      window.removeEventListener("keydown", onKey, true);
      btn.classList.remove("is-listening");
      if (combo == null) applySettings(settings); else saveSetting(btn.dataset.hotkey, combo);
    }
    window.addEventListener("keydown", onKey, true);
  });
});

// ---------------------------------------------------------------- кнопки

$("btnClip").onclick = () => api.save_clip();
$("btnRec").onclick = () => api.toggle_record();
$("btnFolder").onclick = () => api.open_clips_folder();
$("btnLog").onclick = () => api.open_log();
$("btnCheckUpdate").onclick = () => api.check_update();
$("btnUpdate").onclick = () => api.apply_update();
$("btnRestart").onclick = () => { api.restart_capture(); flash("Перезапускаю захват"); };
$("btnPick").onclick = async () => { applySettings(await api.pick_folder()); loadClips(); };
$("search").addEventListener("input", renderClips);
document.querySelectorAll(".nav-item").forEach((item) => {
  item.onclick = () => {
    document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("is-active", n === item));
    document.querySelectorAll(".view").forEach((v) => v.classList.toggle("is-active", v.id === "view-" + item.dataset.view));
    if (item.dataset.view === "clips") loadClips();
    syncPicked();
  };
});

async function init() {
  applySettings(await api.get_settings());
  applyState(await api.get_state());
  loadClips();
  const options = await api.get_options();
  const mon = $("selMonitor");
  mon.innerHTML = "";
  options.monitors.forEach((label, i) => mon.add(new Option(label, String(i))));
  const mic = $("selMic");
  options.mics.forEach((name) => mic.add(new Option(name, name)));
  applySettings(settings);
}
setInterval(async () => { if (started && !document.hidden) applyState(await api.get_state()); }, 5000);
let started = false;
function start() { if (!started) { started = true; init(); } }
window.addEventListener("pywebviewready", start);
window.addEventListener("load", () => setTimeout(() => { if (!window.pywebview) start(); }, 400));
