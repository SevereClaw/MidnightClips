"use strict";

// ---------------------------------------------------------------- связь с программой
// В самой программе методы даёт pywebview. Если открыть index.html просто в браузере,
// включается демо-режим с выдуманными клипами — удобно править оформление.

const demo = {
  presets: [["Слабый компьютер", 720, 30, 6, 30], ["Для Discord", 720, 60, 6, 30], ["Стандарт", 1080, 60, 12, 60], ["Высокое качество", 0, 60, 20, 60],
    ["Плавность", 0, 120, 50, 60], ["Долгие моменты", 1080, 30, 12, 600]].map((p) => ({ name: p[0], builtin: true, height: p[1], fps: p[2], bitrate_mbps: p[3], buffer_seconds: p[4] }))
    .concat([{ name: "Рейды", builtin: false, height: 1440, fps: 90, bitrate_mbps: 30, buffer_seconds: 120 }]),
  settings: {
    clips_dir: "", clips_dir_resolved: "C:\\Users\\you\\Videos\\MidnightClips", buffer_enabled: true,
    buffer_seconds: 60, fps: 60, height: 0, bitrate_mbps: 12, monitor: 0, encoder: "auto", capture: "auto",
    audio: true, mic: "", audio_offset_ms: 0, hotkey_clip: "Alt+F10", hotkey_record: "Alt+F9", hotkey_overlay: "Alt+Z", hotkey_clip_a: "Alt+F8", clip_a_seconds: 30, hotkey_clip_b: "Alt+F7", clip_b_seconds: 15,
    folder_per_game: true, sound: true, toast: true, autostart: false, auto_update: true, discord_webhook: "", discord_limit_mb: 10, cleanup_days: 0, cleanup_gb: 0, capture_target: "game", window_exe: "p3r.exe", app_volumes: '{"discord.exe": {"on": true, "vol": 60}}', audio_mode: "apps", mic_on: true, separate_tracks: true, game_include: "", game_exclude: "", cursor: true, preset_record: "Высокое качество", buffer_storage: "auto", rec_volume_system: 60, rec_volume_mic: 100, indicator: true, indicator_corner: "tl", sound_volume: 100, sound_saved: "C:\\Sounds\\velvet.mp3", sound_rec_on: "", sound_rec_off: "", sound_error: "",
  },
  clips: [
    ["P3R 2026-11-12 21-40-03", "P3R", 60, 88e6, 0], ["P3R 2026-11-12 21-12-47", "P3R", 60, 91e6, 0],
    ["hl2 запись 2026-11-11 19-05-10", "hl2", 754, 1.1e9, 1], ["Тройное убийство", "cs2", 30, 44e6, 0],
    ["cs2 2026-11-10 23-58-31", "cs2", 30, 46e6, 0], ["Рабочий стол 2026-11-09 14-02-00", "Рабочий стол", 15, 9e6, 0],
  ].map((c, i) => ({ path: "demo" + i, name: c[0], game: c[1], duration: c[2], size: c[3], recording: !!c[4], mtime: Date.now() / 1000 - i * 5400,
    favorite: i === 0 || i === 3, tags: [["босс"], [], [], ["эйс", "смешное"], ["эйс"], []][i] })),
};
const demoApi = {
  get_state: async () => ({ state: "on", message: "", encoder: "NVIDIA NVENC", capture: "Desktop Duplication", audio_note: "", window_active: true, buffer_in_memory: true, buffer_mb: 99, fps_real: 59.8, fps_target: 60, slow: false, fallback: false, recording: !!demo.rec, hotkeys: { clip: true, record: true }, version: "демо", update: { status: "ready", latest: "0.1.7", current: "0.1.5", message: "" } }),
  get_settings: async () => Object.assign(demo.settings, { clips_free: 412e9, moving: false }),
  list_drives: async () => [
    { root: "C:\\", label: "Windows", free: 84e9, total: 512e9, removable: false, network: false, current: !demo.settings.clips_dir },
    { root: "D:\\", label: "Игры", free: 1.2e12, total: 2e12, removable: false, network: false, current: demo.settings.clips_dir.startsWith("D") },
    { root: "E:\\", label: "", free: 30e9, total: 64e9, removable: true, network: false, current: demo.settings.clips_dir.startsWith("E") },
  ],
  use_drive: async (root) => {
    const old = demo.settings.clips_dir_resolved;
    Object.assign(demo.settings, { clips_dir: root + "MidnightClips", clips_dir_resolved: root + "MidnightClips" });
    return { ok: true, error: "", settings: demo.settings, old, old_count: demo.clips.length, old_bytes: 1.4e9 };
  },
  use_default_folder: async () => demoApi.use_drive("C:\\Users\\you\\Videos\\"),
  pick_folder: async () => demoApi.use_drive("D:\\Клипы\\"),
  move_clips: async () => {
    let n = 0;
    const timer = setInterval(() => {
      n += 7;
      window.onBackendEvent("move", { percent: Math.min(n, 100), name: "P3R 2026-11-12 21-40-03.mp4" });
      if (n >= 100) { clearInterval(timer); window.onBackendEvent("move_done", { ok: true, moved: 6, failed: 0, text: "Клипы перенесены: 6" }); }
    }, 200);
    return true;
  },
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
  get_presets: async () => demo.presets,
  list_windows: async () => [{ exe: "p3r.exe", title: "Persona 3 Reload" }, { exe: "chrome.exe", title: "YouTube — Google Chrome" }],
  list_audio_apps: async () => ["chrome.exe", "discord.exe", "p3r.exe", "spotify.exe"],
  save_preset: async (name) => { demo.presets.push({ name, builtin: false, height: demo.settings.height, fps: demo.settings.fps, bitrate_mbps: demo.settings.bitrate_mbps, buffer_seconds: demo.settings.buffer_seconds }); return ""; },
  delete_preset: async (name) => { demo.presets = demo.presets.filter((p) => p.name !== name); return true; },
  delete_clips: async (paths) => { demo.clips = demo.clips.filter((c) => !paths.includes(c.path)); return paths.length; },
  set_favorites: async (paths, v) => { demo.clips.forEach((c) => { if (paths.includes(c.path)) c.favorite = v; }); return paths.length; },
  set_tags: async (p, tags) => { demo.clips.find((c) => c.path === p).tags = tags; return true; },
  cleanup_preview: async (days, gb) => ({ count: days || gb ? 3 : 0, bytes: 1.25e9 }),
  media_url: async () => window.DEMO_VIDEO || "",
  clip_tracks: async () => ["Всё вместе", "Микрофон", "Игра", "Discord", "Остальное"],
  clip_track_urls: async () => window.DEMO_TRACKS || [],
  set_fullscreen: async (on) => on,
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
  setTimeout(() => el.remove(), isError ? Math.max(6000, text.length * 90) : 3200);   // длинную ошибку нужно успеть прочитать
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
    ? [st.encoder, st.capture, st.fps_real != null ? "пишется " + Math.round(st.fps_real) + " из " + st.fps_target + " кадров в секунду" : "",
       ["mux", "enc", "own", "owncpu"].includes(st.capture_kind) && st.screen_fps != null ? "экран обновляется " + Math.round(st.screen_fps) + " раз в секунду" : ""].filter(Boolean).join(", ")
    : (st.message || "");
  $("btnRec").classList.toggle("is-on", !!st.recording);
  $("recTitle").textContent = st.recording ? "Остановить" : "Начать запись";
  if (st.buffer_mb != null) {
    const size = st.buffer_mb >= 1024 ? (st.buffer_mb / 1024).toFixed(1) + " ГБ" : st.buffer_mb + " МБ";
    $("bufferInfo").textContent = st.state === "on"
      ? (st.buffer_in_memory ? "Сейчас в памяти, около " + size + ". На диск ничего не пишется, пока клип не сохранён"
                             : "Сейчас на диске, около " + size + ". «Авто» держит в памяти буфер до 768 МБ")
      : "";
  }
  $("targetInfo").textContent = st.state !== "on" ? ""
    : st.window_active ? "Сейчас записывается окно " + (st.window_name || settings.window_exe || "")
    : settings.capture_target === "window" ? "Сейчас записывается весь монитор: окно программы не открыто"
    : settings.capture_target === "game" ? "Сейчас записывается весь монитор: игра не запущена" : "";
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
  $("warnClipA").hidden = h.clip_a !== false;
  $("warnClipB").hidden = h.clip_b !== false;
  $("warnOverlay").hidden = h.overlay !== false;
}
setInterval(() => {
  if (recSince) $("recTimer").textContent = fmtDuration(Math.max(0, Date.now() / 1000 - recSince));
}, 500);

window.onBackendEvent = (kind, data) => {
  if (kind === "state") applyState(data);
  else if (kind === "hotkeys") applyHotkeyStatus(data);
  else if (kind === "open") { const item = document.querySelector('.nav-item[data-view="' + data.view + '"]'); if (item) item.click(); }
  else if (kind === "update") applyUpdate(data);
  else if (kind === "settings") api.get_settings().then(applySettings);
  else if (kind === "job") showJob(data);
  else if (kind === "cleaned") {
    flash("Автоочистка: в корзину ушло клипов — " + data.count + " (" + fmtSize(data.bytes) + ")");
    loadClips();
  }
  else if (kind === "job_done") finishJob(data);
  else if (kind === "move") showMove(data);
  else if (kind === "move_done") finishMove(data);
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
let edSeekTo = null;   // куда перемотать, когда клип загрузится (открыли кусок из монтажа)
let edVol = 1, edMuted = false;   // громкость плеера — одна на видео и на отдельные дорожки
try { const saved = parseFloat(localStorage.getItem("mc_volume")); if (saved >= 0 && saved <= 1) edVol = saved; } catch (e) { /* без памяти */ }

const ICONS = {
  play: '<svg viewBox="0 0 24 24"><path d="M6 3l15 9-15 9z"/></svg>',
  pause: '<svg viewBox="0 0 24 24"><path d="M5 3h5v18H5zM14 3h5v18h-5z"/></svg>',
  sound: '<svg viewBox="0 0 24 24"><path d="M3 9h4l6-5v16l-6-5H3zM16 8a5 5 0 010 8v-2a3 3 0 000-4zM16 3a10 10 0 010 18v-2a8 8 0 000-14z"/></svg>',
  muted: '<svg viewBox="0 0 24 24"><path d="M3 9h4l6-5v16l-6-5H3zM15.5 9.4l1.4-1.4 2.1 2.1L21.1 8l1.4 1.4-2.1 2.1 2.1 2.1-1.4 1.4-2.1-2.1-2.1 2.1-1.4-1.4 2.1-2.1z"/></svg>',
  full: '<svg viewBox="0 0 24 24"><path d="M3 3h7v3H6v4H3zM14 3h7v7h-3V6h-4zM3 14h3v4h4v3H3zM18 14h3v7h-7v-3h4z"/></svg>',
};
$("pFull").innerHTML = ICONS.full;

function fmtTime(sec) {
  sec = Math.max(0, sec || 0);
  const m = Math.floor(sec / 60), s = sec - m * 60;
  return m + ":" + (s < 9.95 ? "0" : "") + s.toFixed(1);
}
// Куски клипа, которые остаются: [{a, b}] по порядку. Всё между ними вырезано.
let segs = [];
let segSel = -1;        // выбранный кусок (тянуть ручки, убрать, I / O)
let segUndo = [];       // прошлые состояния — для Ctrl+Z
const MIN_SEG = 0.2;    // короче кусок не бывает
function edDur() { return video.duration || 0; }
function segsWhole() {
  const dur = edDur();
  return !dur || (segs.length === 1 && segs[0].a < 0.05 && segs[0].b > dur - 0.05);
}
function trimmed() { return !segsWhole(); }
function segAt(t) { return segs.findIndex((g) => t >= g.a - 0.001 && t < g.b - 0.001); }
function keptLength() { return segs.reduce((sum, g) => sum + g.b - g.a, 0); }
function rememberSegs() {
  segUndo.push(JSON.stringify({ segs, segSel }));
  if (segUndo.length > 60) segUndo.shift();
}
function undoSegs() {
  const last = segUndo.pop();
  if (!last) { osd("Отменять нечего"); return; }
  const state = JSON.parse(last);
  segs = state.segs; segSel = state.segSel;
  drawTrim();
  osd("Отменено");
}
function setSegs(list, sel) {
  segs = list.map((g) => ({ a: g.a, b: g.b })).sort((x, y) => x.a - y.a);
  segSel = sel == null ? -1 : Math.min(sel, segs.length - 1);
  drawTrim();
}
function setTrim(a, b) {   // один кусок [a, b] — как прежняя обрезка
  const dur = edDur();
  a = Math.max(0, Math.min(a, dur - MIN_SEG));
  b = Math.max(a + MIN_SEG, Math.min(b, dur));
  setSegs([{ a, b }], 0);
}
function pct(t) { const dur = edDur(); return dur ? t / dur * 100 : 0; }
function drawTrim() {
  const dur = edDur();
  if (!dur) return;
  // куски и вырезанные промежутки между ними
  const box = $("tlCuts");
  box.innerHTML = "";
  let from = 0;
  const gap = (a, b) => {
    if (b - a < 0.01) return;
    const el = document.createElement("div");
    el.className = "tl-gap";
    el.style.left = pct(a) + "%"; el.style.width = pct(b - a) + "%";
    box.appendChild(el);
  };
  const width = $("edTrack").clientWidth || 1;
  segs.forEach((g, i) => {
    gap(from, g.a);
    from = g.b;
    const el = document.createElement("div");
    el.className = "tl-seg" + (i === segSel ? " is-sel" : "");
    el.dataset.seg = i;
    el.style.left = pct(g.a) + "%"; el.style.width = pct(g.b - g.a) + "%";
    if ((g.b - g.a) / dur * width > 70) {
      const label = document.createElement("b");
      label.textContent = (segs.length > 1 ? (i + 1) + " · " : "") + fmtTime(g.b - g.a);
      el.appendChild(label);
    }
    ["l", "r"].forEach((side) => {
      const h = document.createElement("div");
      h.className = "tl-h " + side;
      h.dataset.seg = i; h.dataset.side = side;
      h.title = side === "l" ? "Начало куска — тяните" : "Конец куска — тяните";
      el.appendChild(h);
    });
    box.appendChild(el);
  });
  gap(from, dur);

  const whole = segsWhole();
  const kept = keptLength();
  $("trimText").textContent = whole ? "Весь клип, " + fmtTime(dur)
    : segs.length === 1 ? "Отрезок " + fmtTime(segs[0].a) + " – " + fmtTime(segs[0].b) + ", длина " + fmtTime(kept)
    : "Кусков: " + segs.length + " · вместе " + fmtTime(kept) + " из " + fmtTime(dur);
  const picked = pickedTracks() !== null;
  $("edTrim").disabled = (whole && !picked) || edBusy;
  $("edTrim").textContent = segs.length > 1 ? "Сохранить склейку из " + segs.length + " " + plural(segs.length, "куска", "кусков", "кусков")
    : whole && picked ? "Сохранить с этими дорожками" : "Сохранить обрезку";
  $("edCompress").textContent = (segs.length > 1 ? "Склеить и сжать до " : "Сжать до ") + settings.discord_limit_mb + " МБ";
  $("cutDelete").disabled = segs.length < 2;
  $("cutUndo").disabled = !segUndo.length;
  $("trimReset").hidden = whole;
  drawRuler();
}
function plural(n, one, few, many) {
  const m = Math.abs(n) % 100, d = m % 10;
  return m >= 11 && m <= 19 ? many : d === 1 ? one : d >= 2 && d <= 4 ? few : many;
}
function drawRuler() {
  const dur = edDur(), box = $("tlRuler");
  const width = box.clientWidth || 1;
  box.innerHTML = "";
  if (!dur) return;
  const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
  const step = steps.find((v) => v / dur * width >= 64) || 3600;
  for (let t = 0; t <= dur + 0.001; t += step) {
    const tick = document.createElement("span");
    tick.style.left = pct(t) + "%";
    const label = document.createElement("em");
    const m = Math.floor(t / 60), sec = t - m * 60;
    label.textContent = m + ":" + String(Math.floor(sec)).padStart(2, "0") + (step < 1 ? "." + Math.round((sec % 1) * 10) : "");
    if (pct(t) < 96) tick.appendChild(label);
    box.appendChild(tick);
  }
}
// ---- монтаж и резка
function splitHere() {
  const t = video.currentTime, k = segAt(t);
  if (k < 0) { osd("Метка во вырезанном месте"); return; }
  const g = segs[k];
  if (t - g.a < MIN_SEG || g.b - t < MIN_SEG) { osd("Слишком близко к краю куска"); return; }
  rememberSegs();
  const list = segs.slice();
  list.splice(k, 1, { a: g.a, b: t }, { a: t, b: g.b });
  setSegs(list, k + 1);
  osd("Разрезано");
}
function deleteSeg() {
  if (segs.length < 2) { osd(segs.length ? "Последний кусок не убрать — сначала разрежьте" : ""); return; }
  let k = segSel >= 0 ? segSel : segAt(video.currentTime);
  if (k < 0) { osd("Выберите кусок — нажмите на него"); return; }
  rememberSegs();
  const list = segs.slice();
  list.splice(k, 1);
  setSegs(list, Math.min(k, list.length - 1));
  const next = segs[Math.min(k, segs.length - 1)];
  if (next && segAt(video.currentTime) < 0) seekTo(next.a);
  osd("Кусок убран");
}
function markStart() {
  const t = video.currentTime;
  let k = segSel >= 0 && t < segs[segSel].b ? segSel : segAt(t);
  if (k < 0) k = segs.findIndex((g) => g.a > t);   // во вырезанном месте — тянем к метке начало следующего куска
  if (k < 0) { osd("Дальше кусков нет"); return; }
  const lo = k > 0 ? segs[k - 1].b : 0;
  const a = Math.max(lo, Math.min(t, segs[k].b - MIN_SEG));
  rememberSegs();
  segs[k].a = a; segSel = k;
  drawTrim();
  osd("Начало куска");
}
function markEnd() {
  const t = video.currentTime;
  let k = segSel >= 0 && t > segs[segSel].a ? segSel : segAt(t);
  if (k < 0) { for (let i = segs.length - 1; i >= 0; i--) if (segs[i].b <= t) { k = i; break; } }
  if (k < 0) { osd("Раньше кусков нет"); return; }
  const hi = k < segs.length - 1 ? segs[k + 1].a : edDur();
  const b = Math.min(hi, Math.max(t, segs[k].a + MIN_SEG));
  rememberSegs();
  segs[k].b = b; segSel = k;
  drawTrim();
  osd("Конец куска");
}
window.addEventListener("resize", () => { if (!$("editor").hidden) drawTrim(); });

// ---- плеер: своя панель вместо встроенной

let osdTimer = null;
function osd(text) {
  $("edOsd").textContent = text;
  $("edOsd").hidden = false;
  clearTimeout(osdTimer);
  osdTimer = setTimeout(() => { $("edOsd").hidden = true; }, 800);
}
function drawPlayer() {
  const dur = video.duration || 0;
  $("pTime").textContent = fmtTime(video.currentTime) + " / " + fmtTime(dur);
  if (dur) $("tlHead").style.left = (video.currentTime / dur * 100) + "%";
  // Значок меняем только когда меняется состояние. Раньше он перерисовывался каждый кадр, и нажатие,
  // попавшее между кадрами, терялось: пауза срабатывала через раз.
  const icon = video.paused ? "play" : "pause";
  if ($("pPlay").dataset.icon !== icon) { $("pPlay").dataset.icon = icon; $("pPlay").innerHTML = ICONS[icon]; }
  $("edBig").hidden = !video.paused || !dur || scrubbing;
}
function playerLoop() {   // пока идёт воспроизведение, метка времени движется плавно
  if ($("editor").hidden) return;
  drawPlayer();
  if (!video.paused) {
    // вырезанное при просмотре пропускается: дошли до конца куска — прыжок к следующему
    if (trimmed() && !scrubbing) {
      const t = video.currentTime;
      const k = segAt(playLast);
      if (k >= 0 && t >= segs[k].b - 0.02) {
        if (k + 1 < segs.length) video.currentTime = segs[k + 1].a;
        else { video.pause(); video.currentTime = segs[0].a; }
      }
    }
    playLast = video.currentTime;
    requestAnimationFrame(playerLoop);
  }
}
let playLast = 0;
function togglePlay() {
  if (!video.duration) return;
  if (video.paused) {
    if (trimmed() && segAt(video.currentTime) < 0) {   // метка во вырезанном месте — играть со следующего куска
      const next = segs.find((g) => g.a > video.currentTime) || segs[0];
      video.currentTime = next.a;
    } else if (video.ended) video.currentTime = trimmed() ? segs[0].a : 0;
    playLast = video.currentTime;
    video.play().catch(() => {});
  } else video.pause();
}
function seekTo(time) {
  const dur = video.duration || 0;
  if (!dur) return;
  video.currentTime = Math.max(0, Math.min(time, dur - 0.01));
  drawPlayer();
}
function seekBy(delta) {
  seekTo(video.currentTime + delta);
  osd((delta > 0 ? "+" : "−") + Math.abs(delta) + " с");
}
function stepFrame(direction) {
  video.pause();
  seekTo(video.currentTime + direction / 60);
}
function applyVolume() {
  if (edSplit) {
    video.muted = true;
    edAudio.forEach((a, i) => { a.muted = edMuted || !edTracks[i].checked; a.volume = edVol; });
  } else {
    video.muted = edMuted;
    video.volume = edVol;
  }
  $("edVolume").value = edMuted ? 0 : edVol;
  $("pMute").innerHTML = edMuted || edVol === 0 ? ICONS.muted : ICONS.sound;
}
function setVolume(value, quiet) {
  edVol = Math.max(0, Math.min(1, value));
  edMuted = false;
  try { localStorage.setItem("mc_volume", String(edVol)); } catch (e) { /* без памяти */ }
  applyVolume();
  if (!quiet) osd("Громкость " + Math.round(edVol * 100) + "%");
}
function toggleMute() {
  edMuted = !edMuted;
  applyVolume();
  osd(edMuted ? "Звук выключен" : "Звук включён");
}
function setSpeed(value) {
  video.playbackRate = value;
  $("pSpeed").value = String(value);
  osd("Скорость " + value + "×");
}
function toggleFull() {
  if (document.fullscreenElement) document.exitFullscreen();
  else if ($("edStage").requestFullscreen) $("edStage").requestFullscreen().catch(() => {});
}
document.addEventListener("fullscreenchange", () => { api.set_fullscreen(!!document.fullscreenElement); });

["play", "pause", "seeked", "loadedmetadata", "ended", "timeupdate"].forEach((name) => video.addEventListener(name, drawPlayer));
video.addEventListener("play", () => requestAnimationFrame(playerLoop));
$("pPlay").onclick = togglePlay;
$("pBack").onclick = (e) => seekBy(e.shiftKey ? -1 : -5);
$("pFwd").onclick = (e) => seekBy(e.shiftKey ? 1 : 5);
$("pPrevF").onclick = () => stepFrame(-1);
$("pNextF").onclick = () => stepFrame(1);
$("pMute").onclick = toggleMute;
$("pFull").onclick = toggleFull;
$("pSpeed").onchange = () => { setSpeed(parseFloat($("pSpeed").value)); $("pSpeed").blur(); };
$("edVolume").addEventListener("input", () => setVolume(parseFloat($("edVolume").value), true));
let screenClick = null;
$("edScreen").addEventListener("click", () => {   // одиночное нажатие — пауза, двойное — на весь экран
  clearTimeout(screenClick);
  screenClick = setTimeout(togglePlay, 220);
});
$("edScreen").addEventListener("dblclick", () => { clearTimeout(screenClick); toggleFull(); });
$("edScreen").addEventListener("wheel", (e) => { e.preventDefault(); setVolume(edVol + (e.deltaY < 0 ? 0.05 : -0.05)); }, { passive: false });

// шкала: нажатие и протяжка перематывают, ручки тянут края кусков, наведение показывает время
let scrubbing = false;
let edgeDrag = null;   // {seg, side}
function trackTime(e) {
  const r = $("edTrack").getBoundingClientRect();
  const part = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  return [part * (video.duration || 0), part * 100];
}
$("edTrack").addEventListener("pointerdown", (e) => {
  if (e.button !== 0 || !video.duration) return;
  $("edTrack").setPointerCapture(e.pointerId);
  const handle = e.target.closest(".tl-h");
  if (handle) {   // тянут край куска
    rememberSegs();
    edgeDrag = { seg: parseInt(handle.dataset.seg, 10), side: handle.dataset.side };
    segSel = edgeDrag.seg;
    video.pause();
    drawTrim();
    return;
  }
  const seg = e.target.closest(".tl-seg");
  const was = segSel;
  segSel = seg ? parseInt(seg.dataset.seg, 10) : -1;
  if (segSel !== was) drawTrim();
  scrubbing = true;
  seekTo(trackTime(e)[0]);
});
$("edTrack").addEventListener("pointermove", (e) => {
  if (!video.duration) return;
  const [time, percent] = trackTime(e);
  $("trimTip").textContent = fmtTime(time);
  $("trimTip").style.left = percent + "%";
  $("trimTip").hidden = false;
  if (edgeDrag) {
    const k = edgeDrag.seg, g = segs[k];
    if (!g) return;
    if (edgeDrag.side === "l") g.a = Math.max(k > 0 ? segs[k - 1].b : 0, Math.min(time, g.b - MIN_SEG));
    else g.b = Math.min(k < segs.length - 1 ? segs[k + 1].a : video.duration, Math.max(time, g.a + MIN_SEG));
    seekTo(edgeDrag.side === "l" ? g.a : g.b);   // видно кадр, на котором встанет край
    drawTrim();
  } else if (scrubbing) seekTo(time);
});
["pointerup", "pointercancel"].forEach((name) => $("edTrack").addEventListener(name, () => {
  scrubbing = false;
  edgeDrag = null;
  drawPlayer();
}));
$("edTrack").addEventListener("pointerleave", () => { if (!scrubbing && !edgeDrag) $("trimTip").hidden = true; });

let edTracks = [];
// Номера оставленных дорожек; null — все на месте (звук как есть).
function pickedTracks() {
  if (!edTracks.length || edTracks.every((b) => b.checked)) return null;
  return edTracks.filter((b) => b.checked).map((b) => parseInt(b.dataset.track, 10));
}
// Отдельные дорожки играют рядом с видео: слышно только включённые — так же, как будет в сохранённом клипе.
let edAudio = [];      // по одному <audio> на каждую отдельную дорожку
let edSplit = false;   // сейчас звук идёт от отдельных дорожек, а само видео без звука
function syncAudio(force) {
  if (!edSplit) return;
  edAudio.forEach((a) => {
    a.playbackRate = video.playbackRate;
    const now = performance.now();   // подравниваем не чаще раза в секунду, иначе звук будет заикаться
    if (force || (Math.abs(a.currentTime - video.currentTime) > 0.05 && now - (a.fixedAt || 0) > 1000)) {
      a.fixedAt = now;
      try { a.currentTime = video.currentTime; } catch (e) { /* дорожка ещё не загрузилась */ }
    }
    const playing = !video.paused && !video.ended && video.readyState > 2 && !video.seeking;
    if (playing && a.paused) a.play().catch(() => {});
    else if (!playing && !a.paused) a.pause();
  });
}
function applyTracks() {
  const want = pickedTracks() !== null && edAudio.length === edTracks.length && edAudio.length > 0;
  $("edTrackNote").hidden = pickedTracks() === null || want;
  if (!want && edSplit) edAudio.forEach((a) => a.pause());
  edSplit = want;
  applyVolume();
  syncAudio(true);
}
function dropAudio() {
  edAudio.forEach((a) => { a.pause(); a.removeAttribute("src"); a.load(); });
  edAudio = [];
  edSplit = false;
  $("edTrackNote").hidden = true;
  applyVolume();
}
["play", "playing", "pause", "waiting", "seeking", "ended", "ratechange", "timeupdate"].forEach((name) => video.addEventListener(name, () => syncAudio(false)));
video.addEventListener("seeked", () => syncAudio(true));

function editorNeighbors() {
  const index = edClip ? shownClips.findIndex((c) => c.path === edClip.path) : -1;
  return [index, index > 0 ? shownClips[index - 1] : null, index >= 0 && index < shownClips.length - 1 ? shownClips[index + 1] : null];
}
function stepClip(direction) {
  if (edBusy) return;
  const [, prev, next] = editorNeighbors();
  const clip = direction < 0 ? prev : next;
  if (clip) openEditor(clip);
}
async function openEditor(clip) {
  edClip = clip;
  $("edName").textContent = clip.name;
  const [index, prev, next] = editorNeighbors();
  $("edCount").textContent = index >= 0 ? (index + 1) + " из " + shownClips.length : "";
  $("edPrev").disabled = !prev;
  $("edNext").disabled = !next;
  $("editor").hidden = false;
  segs = []; segSel = -1; segUndo = [];
  $("tlCuts").innerHTML = ""; $("tlRuler").innerHTML = "";
  $("tlStrip").style.backgroundImage = "";
  api.clip_strip(clip.path).then((res) => { if (edClip === clip && res && res.image) $("tlStrip").style.backgroundImage = 'url("' + res.image + '")'; });
  renderMontage();
  $("edJob").hidden = true;
  $("trimText").textContent = "Загружаю клип…";
  $("pSpeed").value = "1";
  // отдельные звуковые дорожки: переключателями выбирается, что слышно здесь и что останется в сохранённом клипе
  edTracks = [];
  dropAudio();
  $("edTracks").hidden = true;
  video.autoplay = true;   // открыли клип — он сразу играет
  video.src = await api.media_url(clip.path);
  $("edStage").focus();
  drawPlayer();
  const names = (await api.clip_tracks(clip.path)) || [];
  if (edClip !== clip || names.length < 2) return;
  const list = $("edTrackList");
  list.innerHTML = "";
  names.slice(1).forEach((name, i) => {
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox"; box.checked = true; box.dataset.track = String(i + 1); box.tabIndex = -1;
    box.onchange = () => { drawTrim(); applyTracks(); };
    label.append(box, document.createTextNode(name));
    list.appendChild(label);
    edTracks.push(box);
  });
  $("edTracks").hidden = false;
  drawTrim();
  const urls = (await api.clip_track_urls(clip.path)) || [];
  if (edClip !== clip || urls.length !== edTracks.length) return;
  edAudio = urls.map((url) => { const a = new Audio(); a.preload = "auto"; a.src = url; return a; });
  applyTracks();
}
function closeEditor() {
  if (edBusy) api.cancel_job();
  if (document.fullscreenElement) document.exitFullscreen();
  dropAudio();
  video.pause();
  video.removeAttribute("src");
  video.load();
  $("editor").hidden = true;
  edClip = null;
}
video.addEventListener("loadedmetadata", () => {
  segUndo = [];
  setSegs([{ a: 0, b: video.duration }], -1);
  if (edSeekTo != null) { seekTo(edSeekTo); edSeekTo = null; }
  applyVolume();
});
video.addEventListener("error", () => { if (edClip) $("trimText").textContent = "Этот клип не открывается во встроенном плеере — нажмите «Открыть в плеере»."; });
$("trimSetStart").onclick = markStart;
$("trimSetEnd").onclick = markEnd;
$("cutSplit").onclick = splitHere;
$("cutDelete").onclick = deleteSeg;
$("cutUndo").onclick = undoSegs;
$("trimReset").onclick = () => { rememberSegs(); setSegs([{ a: 0, b: video.duration || 0 }], -1); };
$("cutToMontage").onclick = addToMontage;
$("edClose").onclick = closeEditor;
$("edPrev").onclick = () => stepClip(-1);
$("edNext").onclick = () => stepClip(1);
$("edOpen").onclick = () => { video.pause(); if (edClip) api.open_clip(edClip.path); };
$("edCancel").onclick = () => api.cancel_job();
document.addEventListener("keydown", (e) => {
  if ($("editor").hidden || !$("modal").hidden) return;
  const el = e.target, tag = el.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable || (tag === "INPUT" && !["range", "checkbox"].includes(el.type))) return;
  if (e.ctrlKey && e.code === "KeyZ") { e.preventDefault(); undoSegs(); return; }
  if (e.ctrlKey || e.altKey || e.metaKey) return;
  const keys = {
    KeyS: splitHere, Delete: deleteSeg, Backspace: deleteSeg,
    Space: togglePlay, KeyK: togglePlay,
    ArrowLeft: () => seekBy(e.shiftKey ? -1 : -5), ArrowRight: () => seekBy(e.shiftKey ? 1 : 5),
    ArrowUp: () => setVolume(edVol + 0.05), ArrowDown: () => setVolume(edVol - 0.05),
    Comma: () => stepFrame(-1), Period: () => stepFrame(1),
    KeyI: markStart, KeyO: markEnd, KeyM: toggleMute, KeyF: toggleFull,
    Home: () => seekTo(0), End: () => seekTo(video.duration || 0),
    PageUp: () => stepClip(-1), PageDown: () => stepClip(1),
    Escape: () => { if (!document.fullscreenElement) closeEditor(); },
  };
  const action = keys[e.code];
  if (!action) return;
  e.preventDefault();
  if (e.code === "Space" && tag === "BUTTON") el.blur();   // иначе пробел ещё и «нажмёт» кнопку под фокусом
  action();
});
document.addEventListener("keyup", (e) => {
  if (e.code === "Space" && !$("editor").hidden && e.target.tagName === "BUTTON") e.preventDefault();
});

function setEditorBusy(busy) {
  edBusy = busy;
  ["edCompress", "edSend"].forEach((id) => { $(id).disabled = busy; });
  $("mtJoin").disabled = busy || montage.length < 2;
  drawTrim();
}
async function runAction(action) {
  if (!edClip || edBusy) return;
  const dur = video.duration || 0;
  let a = null, b = null, parts = null;
  if (dur && trimmed()) {
    a = segs[0].a; b = segs[segs.length - 1].b;
    if (segs.length > 1) parts = segs.map((g) => [g.a, g.b]);
  }
  if (action === "send" && !(settings.discord_webhook || "").trim()) {
    flash("Сначала вставьте ссылку вебхука: Настройки → Discord", true);
    return;
  }
  video.pause();
  setEditorBusy(true);
  showJob({ stage: "Начинаю…", percent: null });
  if (!(await api.clip_action(edClip.path, action, a, b, pickedTracks(), parts))) {
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
  if (result.action === "montage" && result.ok) { montage = []; renderMontage(); }
  if (result.path || (result.ok && result.action !== "send")) loadClips();   // при отправке рядом остаётся сжатая копия
}
$("edTrim").onclick = () => runAction("trim");
$("edCompress").onclick = () => runAction("compress");
$("edSend").onclick = () => runAction("send");

// ---------------------------------------------------------------- монтаж: куски разных клипов в один ролик

let montage = [];   // [{path, name, a, b, tracks}] — b = null: до конца клипа
let mtDrag = -1;
function pieceLength(p) { return p.b != null ? p.b - p.a : null; }
function addToMontage() {
  if (!edClip || !edDur()) return;
  const tracks = pickedTracks();
  segs.forEach((g) => montage.push({ path: edClip.path, name: edClip.name, a: g.a, b: g.b, tracks }));
  renderMontage();
  flash("В монтаж: " + segs.length + " " + plural(segs.length, "кусок", "куска", "кусков") + " из «" + edClip.name + "»");
}
function renderMontage() {
  const box = $("mtList");
  $("montage").hidden = montage.length === 0;
  box.innerHTML = "";
  let total = 0, known = true;
  montage.forEach((p, i) => {
    const len = pieceLength(p);
    if (len == null) known = false; else total += len;
    const card = document.createElement("div");
    card.className = "mt-card" + (edClip && p.path === edClip.path ? " is-current" : "");
    card.draggable = true;
    card.title = "Открыть этот клип — нажатие. Перетащите, чтобы поменять порядок";
    const thumb = document.createElement("div");
    thumb.className = "mt-thumb";
    thumb.innerHTML = "<em>" + (i + 1) + "</em><b>" + (len != null ? fmtTime(len) : "весь") + "</b>";
    api.get_thumb(p.path).then((res) => { if (res && res.image) thumb.style.backgroundImage = 'url("' + res.image + '")'; });
    const name = document.createElement("div");
    name.className = "mt-name"; name.textContent = p.name;
    const range = document.createElement("div");
    range.className = "mt-range";
    range.textContent = p.b != null ? fmtTime(p.a) + " – " + fmtTime(p.b) : "весь клип";
    const del = document.createElement("button");
    del.className = "mt-x"; del.textContent = "×"; del.title = "Убрать из монтажа";
    del.onclick = (e) => { e.stopPropagation(); montage.splice(i, 1); renderMontage(); };
    card.append(thumb, name, range, del);
    card.onclick = () => {
      const clip = clips.find((c) => c.path === p.path);
      if (!clip) { flash("Этого клипа больше нет в папке", true); return; }
      if (edClip && edClip.path === p.path) seekTo(p.a);
      else { edSeekTo = p.a; openEditor(clip); }
    };
    card.addEventListener("dragstart", (e) => { mtDrag = i; card.classList.add("is-drag"); e.dataTransfer.effectAllowed = "move"; });
    card.addEventListener("dragend", () => { mtDrag = -1; renderMontage(); });
    card.addEventListener("dragover", (e) => { if (mtDrag >= 0) { e.preventDefault(); card.classList.add("is-over"); } });
    card.addEventListener("dragleave", () => card.classList.remove("is-over"));
    card.addEventListener("drop", (e) => {
      e.preventDefault();
      if (mtDrag < 0 || mtDrag === i) return;
      const [moved] = montage.splice(mtDrag, 1);
      montage.splice(mtDrag < i ? i - 1 : i, 0, moved);
      mtDrag = -1;
      renderMontage();
    });
    box.appendChild(card);
  });
  $("mtInfo").textContent = montage.length + " " + plural(montage.length, "кусок", "куска", "кусков") +
    (known ? " · " + fmtTime(total) : "");
  $("mtJoin").disabled = edBusy || montage.length < 2;
}
$("mtClear").onclick = () => { montage = []; renderMontage(); };
$("mtJoin").onclick = async () => {
  if (edBusy || montage.length < 2) return;
  video.pause();
  setEditorBusy(true);
  showJob({ stage: "Начинаю склейку…", percent: null });
  const pieces = montage.map((p) => ({ path: p.path, start: p.a, end: p.b != null ? p.b : 0, tracks: p.tracks }));
  if (!(await api.montage(pieces))) finishJob({ ok: false, text: "Сейчас занято другой задачей — подождите." });
};
$("selJoin").onclick = () => {
  const chosen = clips.filter((c) => picked.has(c.path)).sort((x, y) => x.mtime - y.mtime);   // по порядку записи
  if (chosen.length < 2) { flash("Выберите хотя бы два клипа", true); return; }
  chosen.forEach((c) => montage.push({ path: c.path, name: c.name, a: 0, b: c.duration || null, tracks: null }));
  picked.clear();
  syncPicked();
  openEditor(chosen[0]);
  renderMontage();
  flash("Клипы в монтаже. Подрежьте их, если нужно, и нажмите «Склеить в один ролик»");
};

// ---------------------------------------------------------------- где хранить клипы

async function renderDrives() {
  const list = (await api.list_drives()) || [];
  $("rowDrives").hidden = list.length < 2;   // диск один — выбирать не из чего
  const box = $("drives");
  box.innerHTML = "";
  list.forEach((d) => {
    const b = document.createElement("button");
    b.className = "drive" + (d.current ? " is-current" : "") + (d.free < 10 * 1073741824 ? " is-low" : "");
    const kind = d.network ? "сетевой" : d.removable ? "съёмный" : "";
    const name = document.createElement("span");
    name.textContent = d.root.slice(0, 2) + (d.label ? " " + d.label : "");
    const info = document.createElement("small");
    info.textContent = "свободно " + fmtSize(d.free) + " из " + fmtSize(d.total) + (kind ? " · " + kind : "");
    const fill = document.createElement("span");
    fill.className = "drive-fill";
    fill.innerHTML = "<i></i>";
    fill.firstChild.style.width = Math.round(100 * (1 - d.free / Math.max(1, d.total))) + "%";
    b.append(name, info, fill);
    b.title = d.current ? "Клипы сохраняются на этот диск" : "Сохранять клипы в " + d.target;
    b.onclick = () => { if (!d.current) changeFolder(api.use_drive(d.root)); };
    box.appendChild(b);
  });
}

async function changeFolder(request) {
  const res = await request;
  if (!res) return;   // окно выбора папки закрыли
  applySettings(res.settings);
  renderDrives();
  if (!res.ok) { flash(res.error, true); return; }
  if (!res.old) return;
  loadClips();
  flash("Новые клипы будут сохраняться в " + res.settings.clips_dir_resolved);
  if (res.old_count > 0) {
    const yes = await ask({
      title: "Перенести старые клипы?",
      text: "В прежней папке (" + res.old + ") осталось клипов: " + res.old_count + ", " + fmtSize(res.old_bytes)
        + ". Перенести их в новую папку, чтобы они были в галерее? Избранное и теги сохранятся."
        + (res.old_bytes > 5e9 ? " На другой диск это займёт какое-то время — запись при этом продолжается." : ""),
      okLabel: "Перенести",
    });
    if (yes === true && await api.move_clips(res.old)) showMove({ percent: 0, name: "" });
  }
}

function showMove(m) {
  $("moveBox").hidden = false;
  $("moveText").textContent = "Переношу" + (m.name ? ": " + m.name : " клипы…");
  $("moveFill").style.width = (m.percent || 0) + "%";
}
function finishMove(result) {
  $("moveBox").hidden = true;
  flash(result.text, !result.ok);
  loadClips();
  api.get_settings().then(applySettings);
  renderDrives();
}

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
  $("clipsFree").textContent = s.clips_free != null ? "Свободно на диске: " + fmtSize(s.clips_free) : "";
  $("clipsDirWarn").hidden = !s.clips_dir_missing;
  $("clipsDirWarn").textContent = s.clips_dir_missing ? "Выбранная папка " + s.clips_dir_missing
    + " сейчас недоступна (диск отключён?) — клипы временно сохраняются сюда" : "";
  $("btnDefaultDir").hidden = !s.clips_dir;
  if (s.moving) $("moveBox").hidden = false;
  $("edCompress").textContent = "Сжать до " + s.discord_limit_mb + " МБ";
  $("volText").textContent = s.sound_volume + "%" + (s.sound_volume > 100 ? " — громче исходного файла" : "");
  $("rowWindow").hidden = s.capture_target !== "window";
  $("rowGame").hidden = s.capture_target !== "game";
  const selWin = $("selWindow");
  if (s.window_exe && ![...selWin.options].some((o) => o.value === s.window_exe)) selWin.add(new Option(s.window_exe, s.window_exe));
  selWin.value = s.window_exe || "";
  renderSounds();
  if (s.audio_mode === "apps" && !heardAsked) { heardAsked = true; refreshHeardApps(); }
  renderApps();
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

// Наборы настроек: готовые и свои
let presets = [];
function fmtLen(sec) { return sec >= 60 ? (sec / 60) + " мин" : sec + " с"; }
function presetText(p) {
  return (p.height ? p.height + "p" : "Как на экране") + ", " + p.fps + " кадров, " + p.bitrate_mbps + " Мбит/с, клип " + fmtLen(p.buffer_seconds);
}
const PRESET_KEYS = ["height", "fps", "bitrate_mbps", "buffer_seconds"];
async function loadPresets() {
  presets = (await api.get_presets()) || [];
  renderPresets();
}
function renderPresets() {
  const box = $("presets");
  box.innerHTML = "";
  let activeName = "";
  presets.forEach((p) => {
    const b = document.createElement("button");
    const active = PRESET_KEYS.every((k) => settings[k] === p[k]);
    if (active && !activeName) activeName = p.name;
    b.className = "preset" + (active ? " is-active" : "") + (p.builtin ? "" : " is-custom");
    b.setAttribute("aria-pressed", active ? "true" : "false");
    const strong = document.createElement("strong");
    strong.textContent = p.name;
    const span = document.createElement("span");
    span.textContent = presetText(p);
    b.append(strong, span);
    b.onclick = async () => {
      const values = {};
      PRESET_KEYS.forEach((k) => { values[k] = p[k]; });
      applySettings(await api.set_settings(values));
      flash("Набор «" + p.name + "» включён для клипов");
      applyState(await api.get_state());
    };
    if (!p.builtin) {
      const del = document.createElement("span");
      del.className = "preset-del";
      del.textContent = "×";
      del.title = "Удалить набор";
      del.onclick = async (e) => {
        e.stopPropagation();
        if (!(await ask({ title: "Удалить набор?", text: "«" + p.name + "» — " + presetText(p), okLabel: "Удалить" }))) return;
        await api.delete_preset(p.name);
        applySettings(await api.get_settings());
        loadPresets();
      };
      b.appendChild(del);
    }
    box.appendChild(b);
  });
  $("presetClipText").textContent = activeName ? "Для клипов сейчас включён набор «" + activeName + "»" : "Для клипов сейчас свои настройки, не из набора";
  const sel = $("selPresetRecord");
  sel.innerHTML = "";
  sel.add(new Option("Те же настройки, что и для клипов", ""));
  presets.forEach((p) => sel.add(new Option(p.name, p.name)));
  sel.value = presets.some((p) => p.name === settings.preset_record) ? settings.preset_record : "";
}
$("btnSavePreset").onclick = async () => {
  const name = await ask({ title: "Свой набор", text: "Сохранятся текущие разрешение, кадры, качество и длина клипа. Как назвать набор?", input: "", okLabel: "Сохранить" });
  if (!name) return;
  const error = await api.save_preset(name);
  if (error) { flash(error, true); return; }
  flash("Набор «" + name + "» сохранён");
  loadPresets();
};

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

// Запись окна одной программы
async function fillWindowList() {
  const sel = $("selWindow");
  const current = settings.window_exe || "";
  const wins = (await api.list_windows()) || [];
  sel.innerHTML = "";
  sel.add(new Option("Выберите программу…", ""));
  if (current && !wins.some((w) => w.exe === current)) sel.add(new Option(current + " — сейчас не открыта", current));
  wins.forEach((w) => sel.add(new Option(w.exe + " — " + w.title, w.exe)));
  sel.value = current;
}
$("selWindow").addEventListener("mousedown", fillWindowList);
$("selWindow").addEventListener("focus", fillWindowList);
$("selWindow").addEventListener("change", () => saveSetting("window_exe", $("selWindow").value));

// Что записывать из звука: список как в Medal — галочка, название, громкость в записи.
// В настройке app_volumes лежит {ключ: {on, vol}}; ключ — имя exe, «@game» (игра) или «@self» (сигналы программы).
function appVolumes() {
  let raw = {};
  try { raw = JSON.parse(settings.app_volumes || "{}") || {}; } catch (e) { raw = {}; }
  const rows = {};
  Object.keys(raw).forEach((k) => {
    const v = raw[k];
    rows[k] = (v && typeof v === "object") ? { on: v.on !== false, vol: parseInt(v.vol, 10) || 0 } : { on: true, vol: parseInt(v, 10) || 0 };
  });
  return rows;
}
function saveAppVolumes(obj) { return saveSetting("app_volumes", Object.keys(obj).length ? JSON.stringify(obj) : ""); }
function prettyApp(exe) {
  const base = exe.replace(/\.exe$/i, "");
  return base.charAt(0).toUpperCase() + base.slice(1);
}
function renderApps() {
  const box = $("appRows");
  if (box.contains(document.activeElement) && document.activeElement.type === "range") return;   // не дёргать ползунок под рукой
  box.innerHTML = "";
  const rows = appVolumes();
  const specific = settings.audio_mode === "apps";
  const gameKnown = settings.capture_target === "game" || (settings.capture_target === "window" && settings.window_exe);
  const add = (opt) => {
    const row = document.createElement("div");
    row.className = "audio-row" + (opt.on ? "" : " is-off");
    const check = document.createElement("input");
    check.type = "checkbox"; check.checked = opt.on; check.disabled = !!opt.locked;
    check.setAttribute("aria-label", "Записывать: " + opt.name);
    check.onchange = () => opt.save(check.checked, parseInt(slider.value, 10));
    const name = document.createElement("span");
    name.className = "audio-name"; name.textContent = opt.name;
    if (opt.hint) { const small = document.createElement("small"); small.textContent = opt.hint; name.appendChild(small); }
    const slider = document.createElement("input");
    slider.type = "range"; slider.className = "vol"; slider.min = 0; slider.max = 200; slider.step = 5; slider.value = opt.vol;
    slider.setAttribute("aria-label", "Громкость в записи: " + opt.name);
    const percent = document.createElement("span");
    percent.className = "audio-percent"; percent.textContent = opt.vol + "%";
    slider.oninput = () => { percent.textContent = slider.value + "%"; };
    slider.onchange = () => { slider.blur(); opt.save(check.checked, parseInt(slider.value, 10)); };
    const tail = document.createElement("span");
    if (opt.remove) {
      const del = document.createElement("button");
      del.className = "audio-remove"; del.textContent = "×"; del.title = "Убрать из списка";
      del.setAttribute("aria-label", "Убрать из списка: " + opt.name);
      del.onclick = opt.remove;
      tail.appendChild(del);
    }
    row.append(check, name, slider, percent, tail);
    box.appendChild(row);
  };
  const saveRow = (key) => (on, vol) => { const v = appVolumes(); v[key] = { on, vol }; saveAppVolumes(v); };
  const game = rows["@game"] || { on: true, vol: 100 };
  add({ name: "Игра", on: game.on, vol: game.vol, save: saveRow("@game"),
        hint: gameKnown ? "" : "Работает, когда в «Что записывать» выбрана игра или окно программы" });
  add({ name: "Микрофон", on: settings.mic_on !== false && !!settings.mic, vol: settings.rec_volume_mic,
        hint: settings.mic ? "" : "Устройство не выбрано — выберите его выше",
        save: async (on, vol) => { applySettings(await api.set_settings({ mic_on: on, rec_volume_mic: vol })); applyState(await api.get_state()); } });
  const self = rows["@self"] || { on: true, vol: 100 };
  add({ name: "Сигналы MidnightClips", on: self.on, vol: self.vol, save: saveRow("@self") });
  Object.keys(rows).filter((k) => k.charAt(0) !== "@").sort().forEach((exe) => {
    add({ name: prettyApp(exe), on: rows[exe].on, vol: rows[exe].vol, save: saveRow(exe),
          remove: () => { const v = appVolumes(); delete v[exe]; saveAppVolumes(v); } });
  });
  if (specific) {   // программы, которые звучат сейчас, но в список не добавлены — без галочки
    heardApps.filter((exe) => !(exe in rows)).forEach((exe) => {
      add({ name: prettyApp(exe), on: false, vol: 100, save: saveRow(exe) });
    });
  } else {
    add({ name: "Остальные программы", on: true, locked: true, vol: settings.rec_volume_system,
          save: (on, vol) => saveSetting("rec_volume_system", vol) });
  }
}
let heardApps = [], heardAsked = false;
async function refreshHeardApps() {
  try { heardApps = (await api.list_audio_apps()) || []; } catch (e) { heardApps = []; }
  renderApps();
}
async function fillAppList() {
  const sel = $("selAddApp");
  const have = appVolumes();
  heardApps = (await api.list_audio_apps()) || [];
  const apps = heardApps.filter((a) => !(a in have));
  sel.innerHTML = "";
  sel.add(new Option(apps.length ? "Выбрать…" : "Сейчас больше ничего не звучит", ""));
  apps.forEach((a) => sel.add(new Option(prettyApp(a), a)));
}
$("selAddApp").addEventListener("mousedown", fillAppList);
$("selAddApp").addEventListener("focus", fillAppList);
$("selAddApp").addEventListener("change", () => {
  const exe = $("selAddApp").value;
  if (!exe) return;
  const v = appVolumes();
  v[exe] = { on: true, vol: 100 };
  saveAppVolumes(v);
  $("selAddApp").value = "";
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

// Правая кнопка мыши по «Сохранить клип» и «Начать запись» — сменить клавишу прямо на месте.
let rebinding = false;
function rebindHere(settingKey, label) {
  if (rebinding) return;
  rebinding = true;
  const old = label.textContent;
  label.textContent = "Нажмите сочетание…";
  label.classList.add("is-listening");
  const onKey = (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.key === "Escape") return finish(null);
    if (e.key === "Backspace") return finish("");
    const key = keyName(e.code);
    if (!key) return;   // пока нажаты только Ctrl/Alt/Shift — ждём дальше
    const mods = [e.ctrlKey && "Ctrl", e.altKey && "Alt", e.shiftKey && "Shift"].filter(Boolean);
    if (!mods.length && !/^F\d+$/.test(key)) { flash("Добавьте Ctrl, Alt или Shift — иначе клавиша перестанет печатать", true); return; }
    finish([...mods, key].join("+"));
  };
  const swallow = (e) => { e.preventDefault(); e.stopPropagation(); };
  const onMouse = (e) => {   // щелчок мышью отменяет и не должен нажать саму кнопку
    swallow(e);
    window.addEventListener("click", swallow, { capture: true, once: true });
    setTimeout(() => window.removeEventListener("click", swallow, true), 300);
    finish(null);
  };
  let done = false;
  function finish(combo) {
    done = true;
    window.removeEventListener("keydown", onKey, true);
    window.removeEventListener("mousedown", onMouse, true);
    label.classList.remove("is-listening");
    rebinding = false;
    if (combo == null) label.textContent = old;
    else saveSetting(settingKey, combo).then(() => flash(combo ? "Клавиша изменена: " + combo : "Клавиша снята"));
  }
  window.addEventListener("keydown", onKey, true);
  setTimeout(() => { if (!done) window.addEventListener("mousedown", onMouse, true); }, 0);
}
[["btnClip", "hotkey_clip", "kbdClip"], ["btnRec", "hotkey_record", "kbdRec"]].forEach(([btn, key, kbd]) => {
  $(btn).title = "Правая кнопка мыши — сменить клавишу";
  $(btn).addEventListener("contextmenu", (e) => { e.preventDefault(); rebindHere(key, $(kbd)); });
});

// ---------------------------------------------------------------- кнопки

$("btnClip").onclick = () => api.save_clip();
$("btnRec").onclick = () => api.toggle_record();
$("btnFolder").onclick = () => api.open_clips_folder();
$("btnLog").onclick = () => api.open_log();
$("btnCheckUpdate").onclick = () => api.check_update();
$("btnUpdate").onclick = () => api.apply_update();
$("btnRestart").onclick = () => { api.restart_capture(); flash("Перезапускаю захват"); };
$("btnPick").onclick = () => changeFolder(api.pick_folder());
$("btnDefaultDir").onclick = () => changeFolder(api.use_default_folder());
$("btnMoveCancel").onclick = () => { api.cancel_move(); $("moveText").textContent = "Останавливаю…"; };
$("search").addEventListener("input", renderClips);
document.querySelectorAll(".nav-item").forEach((item) => {
  item.onclick = () => {
    document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("is-active", n === item));
    document.querySelectorAll(".view").forEach((v) => v.classList.toggle("is-active", v.id === "view-" + item.dataset.view));
    if (item.dataset.view === "clips") loadClips();
    if (item.dataset.view === "settings") renderDrives();
    $("tbWhere").textContent = VIEW_NAMES[item.dataset.view] || "";   // флешку могли вставить или вынуть
    syncPicked();
  };
});

// ---------------------------------------------------------------- своя полоса заголовка

const VIEW_NAMES = { clips: "Клипы", settings: "Настройки" };
function setMaxState(max) {
  document.body.classList.toggle("is-max", !!max);
  $("tbMax").title = max ? "Вернуть размер" : "Развернуть";
  $("tbMaxIcon").innerHTML = max
    ? '<path d="M3 1h8v8H9V3H3z M1 3.6h7.4V11H1z M2.8 5.4v3.8h3.8V5.4z" fill-rule="evenodd"/>'
    : '<path d="M1 1h10v10H1z M3.2 3.6v5.2h5.6V3.6z" fill-rule="evenodd"/>';
}
async function toggleMax() { const s = await api.win_maximize(); if (s) setMaxState(s.maximized); }
let tbLastDown = 0, tbLastX = 0, tbLastY = 0;
$("titlebar").addEventListener("mousedown", (e) => {
  if (e.button !== 0 || e.target.closest(".tb-btn")) return;
  const now = performance.now();
  // двойной щелчок по полосе — развернуть; считаем сами: после первого щелчка окно ведёт Windows, и браузер его «не видит»
  if (now - tbLastDown < 420 && Math.abs(e.screenX - tbLastX) < 6 && Math.abs(e.screenY - tbLastY) < 6) {
    tbLastDown = 0;
    toggleMax();
    return;
  }
  tbLastDown = now; tbLastX = e.screenX; tbLastY = e.screenY;
  api.win_drag("move").then(() => api.win_state()).then((s) => { if (s) setMaxState(s.maximized); });
});
document.querySelectorAll(".rz").forEach((el) => el.addEventListener("mousedown", (e) => {
  if (e.button === 0) { e.preventDefault(); api.win_drag(el.dataset.edge); }
}));
$("tbMin").onclick = () => api.win_minimize();
$("tbMax").onclick = toggleMax;
$("tbClose").onclick = () => api.win_close();
let tbResizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(tbResizeTimer);
  tbResizeTimer = setTimeout(async () => { const s = await api.win_state(); if (s) setMaxState(s.maximized); }, 150);
});

async function init() {
  applySettings(await api.get_settings());
  applyState(await api.get_state());
  loadClips();
  loadPresets();
  renderDrives();
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
