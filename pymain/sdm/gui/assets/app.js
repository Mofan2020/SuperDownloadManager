/* Super Download Manager — 前端逻辑
 *
 * 与后端只通过 window.pywebview.api 通信。
 * 文案全部走 t(key)，不硬编码。
 */
"use strict";

// ============ 状态 ============

const State = {
  locale: "system",
  strings: {},
  tasks: [],
  history: [],
  schedules: [],
  siteRules: [],
  settings: {},
  view: "downloads",
  filter: "all",
  categoryFilter: null,
  selectedTaskId: null,
  clipboardCandidate: null,
  showToolbarLabels: true,
  booted: false,
};

const api = () => window.pywebview?.api;

// ============ 文案 ============

function t(key, fallback) {
  const s = State.strings[key];
  if (s !== undefined && s !== null) return s;
  if (fallback !== undefined) return fallback;
  return key;
}

function applyStaticI18n(root) {
  const scope = root || document;
  scope.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n, el.textContent);
  });
  scope.querySelectorAll("[data-i18n-title]").forEach((el) => {
    el.title = t(el.dataset.i18nTitle, el.title);
  });
}

// ============ 格式化 ============

function fmtSize(bytes) {
  if (bytes === null || bytes === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = Number(bytes);
  let i = 0;
  while (Math.abs(v) >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return i === 0 ? `${Math.round(v)} ${units[i]}` : `${v.toFixed(1)} ${units[i]}`;
}

function fmtRate(bytesPerSec) {
  if (!bytesPerSec || bytesPerSec <= 0) return "—";
  const units = ["B/s", "KB/s", "MB/s"];
  let v = Number(bytesPerSec);
  let i = 0;
  while (Math.abs(v) >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return i === 0 ? `${Math.round(v)} ${units[i]}` : `${v.toFixed(1)} ${units[i]}`;
}

function fmtDuration(seconds) {
  if (!seconds || seconds < 0 || !isFinite(seconds)) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  if (m < 60) return `${m}m ${s}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

function fmtDate(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// ============ 工具 ============

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (k === "dataset") Object.assign(node.dataset, v);
      else node.setAttribute(k, v);
    }
  }
  (Array.isArray(children) ? children : children ? [children] : []).forEach((c) => {
    if (c === null || c === undefined) return;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  });
  return node;
}

function icon(name, cls) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon" + (cls ? " " + cls : ""));
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#i-" + name);
  svg.appendChild(use);
  return svg;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function toast(message, kind) {
  const stack = document.getElementById("toastStack");
  const type = kind || "ok";
  const names = { ok: "check", err: "alert", info: "info" };
  const node = el("div", { class: "toast " + type }, [
    icon(names[type] || "info"),
    el("span", { class: "toast-text", text: message }),
  ]);
  stack.appendChild(node);
  // 最多同时 3 条
  while (stack.children.length > 3) stack.removeChild(stack.firstChild);
  setTimeout(() => node.remove(), 2400);
}

// ============ 状态文案与颜色 ============

const STATE_META = {
  queued:           { key: "stateQueued",           cls: "badge-queued",           icon: "clock" },
  downloading:      { key: "stateDownloading",      cls: "badge-downloading",     icon: "download" },
  paused:           { key: "statePaused",           cls: "badge-paused",           icon: "pause" },
  completed:        { key: "stateCompleted",        cls: "badge-completed",        icon: "check" },
  failed:           { key: "stateFailed",           cls: "badge-failed",           icon: "alert" },
  cancelled:        { key: "stateCancelled",        cls: "badge-cancelled",        icon: "x" },
  awaiting_new_link:{ key: "stateAwaitingLink",     cls: "badge-awaiting_new_link",icon: "refresh" },
};

const CATEGORY_META = {
  video:     { icon: "video",    color: "#DB2777" },
  music:     { icon: "music",    color: "#9333EA" },
  pictures:  { icon: "image",    color: "#0D9488" },
  archives:  { icon: "archive",  color: "#EA580C" },
  documents: { icon: "file",     color: "#2563EB" },
  software:  { icon: "package",  color: "#4F46E5" },
  other:     { icon: "help",     color: "#64748B" },
};

// ============ 侧边栏 ============

const SIDEBAR_ITEMS = [
  { id: "all",         icon: "list",    key: "sidebarAllDownloads" },
  { id: "downloading", icon: "download",key: "sidebarDownloading" },
  { id: "queued",      icon: "clock",   key: "sidebarQueued" },
  { id: "paused",      icon: "pause",   key: "sidebarPaused" },
  { id: "completed",   icon: "check",   key: "sidebarCompleted" },
  { id: "failed",      icon: "alert",   key: "sidebarFailed" },
];

function renderSidebar() {
  const nav = document.getElementById("sidebarNav");
  clear(nav);

  const counts = {
    all: State.tasks.length,
    downloading: State.tasks.filter((x) => x.state === "downloading").length,
    queued: State.tasks.filter((x) => x.state === "queued").length,
    paused: State.tasks.filter((x) => x.state === "paused").length,
    completed: State.tasks.filter((x) => x.state === "completed").length,
    failed: State.tasks.filter((x) => x.state === "failed").length,
  };

  SIDEBAR_ITEMS.forEach((item) => {
    const btn = el("button", {
      class: "nav-item" + (State.filter === item.id && State.view === "downloads" ? " active" : ""),
      title: t(item.key),
      onclick: () => { setView("downloads"); State.filter = item.id; State.categoryFilter = null; render(); },
    }, [
      icon(item.icon),
      el("span", { class: "nav-text", text: t(item.key) }),
      el("span", { class: "nav-count", text: String(counts[item.id] || 0) }),
    ]);
    nav.appendChild(btn);
  });

  // 分类
  nav.appendChild(el("div", { class: "nav-group-label", text: t("sidebarCategories") }));
  Object.keys(CATEGORY_META).forEach((cat) => {
    const n = State.tasks.filter((x) => x.category === cat).length;
    if (n === 0) return;
    const meta = CATEGORY_META[cat];
    const btn = el("button", {
      class: "nav-item" + (State.categoryFilter === cat ? " active" : ""),
      title: cat,
      onclick: () => { setView("downloads"); State.filter = "all"; State.categoryFilter = cat; render(); },
    }, [
      icon(meta.icon),
      el("span", { class: "nav-text", text: cat.charAt(0).toUpperCase() + cat.slice(1) }),
      el("span", { class: "nav-count", text: String(n) }),
    ]);
    nav.appendChild(btn);
  });

  // 其它
  nav.appendChild(el("div", { class: "nav-group-label", text: t("sidebarOther") }));
  [
    { id: "history",  icon: "clock", key: "sidebarHistory" },
    { id: "schedule", icon: "alarm", key: "sidebarSchedule" },
  ].forEach((item) => {
    nav.appendChild(el("button", {
      class: "nav-item" + (State.view === item.id ? " active" : ""),
      title: t(item.key),
      onclick: () => { State.filter = "all"; State.categoryFilter = null; setView(item.id); },
    }, [icon(item.icon), el("span", { class: "nav-text", text: t(item.key) })]));
  });
}

// ============ 工具栏 ============

function renderToolbar() {
  const title = document.getElementById("toolbarTitle");
  const actions = document.getElementById("toolbarActions");
  clear(title); clear(actions);

  if (State.view === "downloads") {
    title.appendChild(el("span", { class: "toolbar-title", text: t("mainDownloads") }));
    actions.appendChild(el("button", {
      class: "btn btn-primary", title: t("toolbarAddURL"),
      onclick: openAddDialog,
    }, [icon("plus"), el("span", { class: "btn-label", text: t("toolbarAddURL") })]));

    actions.appendChild(el("button", {
      class: "btn", title: t("toolbarPauseAll"),
      onclick: async () => { await api()?.pause_all(); toast(t("toolbarPauseAll")); },
    }, [icon("pause"), el("span", { class: "btn-label", text: t("toolbarPauseAll") })]));

    actions.appendChild(el("button", {
      class: "btn", title: t("toolbarResumeAll"),
      onclick: async () => { await api()?.resume_all(); toast(t("toolbarResumeAll")); },
    }, [icon("play"), el("span", { class: "btn-label", text: t("toolbarResumeAll") })]));

    actions.appendChild(el("button", {
      class: "btn", title: t("toolbarClearCompleted"),
      onclick: async () => { await api()?.clear_completed(); toast(t("toolbarClearCompleted")); },
    }, [icon("trash"), el("span", { class: "btn-label", text: t("toolbarClearCompleted") })]));
  } else if (State.view === "history") {
    title.appendChild(el("span", { class: "toolbar-title", text: t("sidebarHistory") }));
    actions.appendChild(el("button", {
      class: "btn btn-danger", title: t("historyClearAll"),
      onclick: async () => { await api()?.clear_history(); toast(t("historyClearAll")); },
    }, [icon("trash"), el("span", { class: "btn-label", text: t("historyClearAll") })]));
  } else if (State.view === "schedule") {
    title.appendChild(el("span", { class: "toolbar-title", text: t("scheduleTitle") }));
  } else if (State.view === "settings") {
    title.appendChild(el("span", { class: "toolbar-title", text: t("settingsWindowTitle") }));
  }
}

// ============ 下载列表 ============

function filteredTasks() {
  let list = State.tasks.slice();
  if (State.filter && State.filter !== "all") {
    list = list.filter((x) => x.state === State.filter);
  }
  if (State.categoryFilter) {
    list = list.filter((x) => x.category === State.categoryFilter);
  }
  // 下载中的排前面，其余按加入时间倒序
  const order = { downloading: 0, queued: 1, paused: 2, awaiting_new_link: 3, failed: 4, completed: 5, cancelled: 6 };
  return list.sort((a, b) => (order[a.state] ?? 9) - (order[b.state] ?? 9));
}

// 建一行（只做一次）。返回 {node, refs}，refs 里存着要更新的元素引用。
function buildRow(task) {
  const catMeta = CATEGORY_META[task.category] || CATEGORY_META.other;

  const catIcon = el("span", { class: "row-cat", style: `color:${catMeta.color}` }, [icon(catMeta.icon)]);
  const title = el("div", { class: "row-title trunc", text: task.filename || task.url });
  const sub = el("div", { class: "row-sub trunc", text: hostOf(task.url) });
  const chip = el("span", { class: "limit-chip", hidden: true }, [
    icon("gauge", "icon-sm"),
    el("span", { text: "" }),
  ]);
  const nameBox = el("div", { class: "row-name" }, [
    catIcon,
    el("div", { class: "trunc", style: "min-width:0;flex:1" }, [title, sub, chip]),
  ]);

  const badgeIcon = icon("clock");
  const badgeText = el("span", { text: "" });
  const badge = el("span", { class: "badge" }, [badgeIcon, badgeText]);

  const fill = el("div", { class: "progress-fill" });
  const pctText = el("div", { class: "progress-text" });
  const progress = el("div", { class: "progress-cell cell-progress" }, [
    el("div", { class: "progress-track" }, [fill]),
    pctText,
  ]);

  const sizeCell = el("div", { class: "cell-num cell-size" });
  const speedCell = el("div", { class: "cell-num cell-speed" });
  const etaCell = el("div", { class: "cell-num cell-eta cell-muted" });

  // 操作按钮：三个固定的槽位，按状态换图标与行为，
  // 避免每次重建按钮节点。
  const btnA = el("button", { class: "btn" }, [icon("pause")]);
  const btnB = el("button", { class: "btn danger" }, [icon("x")]);
  const btnC = el("button", { class: "btn" }, [icon("more")]);
  const actions = el("div", { class: "row-actions" }, [btnA, btnB, btnC]);

  const row = el("div", { class: "row" }, [
    nameBox, badge, progress, sizeCell, speedCell, etaCell, actions,
  ]);

  const refs = {
    node: row, title, sub, chip, badge, badgeIcon, badgeText,
    fill, pctText, sizeCell, speedCell, etaCell,
    btnA, btnB, btnC, catIcon,
    state: null, taskId: task.id,
  };

  btnA.addEventListener("click", (e) => { e.stopPropagation(); onActionA(refs); });
  btnB.addEventListener("click", (e) => { e.stopPropagation(); onActionB(refs); });
  btnC.addEventListener("click", (e) => { e.stopPropagation(); openContextMenu(e, currentTask(refs.taskId)); });
  row.addEventListener("click", () => selectTask(refs.taskId));
  row.addEventListener("contextmenu", (e) => { e.preventDefault(); openContextMenu(e, currentTask(refs.taskId)); });

  updateRow(refs, task);
  return refs;
}

function currentTask(id) {
  return State.tasks.find((x) => x.id === id) || { id, filename: "", url: "" };
}

// 只更新变化的内容，不重建节点。
function updateRow(refs, task) {
  const meta = STATE_META[task.state] || STATE_META.queued;
  const catMeta = CATEGORY_META[task.category] || CATEGORY_META.other;

  if (refs.state !== task.state) {
    refs.state = task.state;
    refs.badge.className = "badge " + meta.cls;
    refs.badgeIcon.replaceWith(icon(meta.icon));
    refs.badgeText.textContent = t(meta.key);
    refs.badgeIcon = refs.badge.firstChild;
  }
  if (refs.catIcon.style.color !== catMeta.color) {
    refs.catIcon.style.color = catMeta.color;
    refs.catIcon.firstChild.replaceWith(icon(catMeta.icon));
  }

  refs.title.textContent = task.filename || task.url;
  refs.sub.textContent = hostOf(task.url);

  const pct = task.total_bytes > 0
    ? Math.min(100, Math.max(0, (task.bytes_done / task.total_bytes) * 100))
    : 0;
  const done = task.state === "completed";
  const failed = task.state === "failed";
  refs.fill.className = "progress-fill" + (done ? " done" : "") + (failed ? " error" : "");
  refs.fill.style.width = (done ? 100 : pct) + "%";
  refs.pctText.textContent = done ? "100%" : pct.toFixed(1) + "%";

  refs.sizeCell.textContent = fmtSize(task.total_bytes);
  refs.speedCell.textContent = fmtRate(task.speed_bytes_per_sec);

  const eta = task.total_bytes && task.speed_bytes_per_sec > 0 && !done
    ? (task.total_bytes - task.bytes_done) / task.speed_bytes_per_sec
    : null;
  refs.etaCell.textContent = eta !== null ? fmtDuration(eta) : "—";

  // 单任务限速标记
  if (task.speed_limit_bytes_per_sec > 0) {
    refs.chip.hidden = false;
    refs.chip.lastChild.textContent = fmtRate(task.speed_limit_bytes_per_sec);
  } else {
    refs.chip.hidden = true;
  }

  // 选中态
  refs.node.classList.toggle("selected", State.selectedTaskId === task.id);

  // 操作按钮
  const isActive = task.state === "downloading" || task.state === "queued";
  const canResume = task.state === "paused" || task.state === "failed"
                 || task.state === "cancelled" || task.state === "awaiting_new_link";
  refs.btnA.hidden = !(isActive || canResume);
  if (isActive) {
    refs.btnA.title = t("rowPause");
    refs.btnA.firstChild.replaceWith(icon("pause"));
  } else if (canResume) {
    refs.btnA.title = task.state === "failed" ? t("rowRetry") : t("rowResume");
    refs.btnA.firstChild.replaceWith(icon("play"));
  }
  refs.btnB.hidden = !isActive;
}

function onActionA(refs) {
  const task = currentTask(refs.taskId);
  const isActive = task.state === "downloading" || task.state === "queued";
  if (isActive) api()?.pause_task(task.id);
  else api()?.resume_task(task.id);
}

function onActionB(refs) {
  api()?.cancel_task(refs.taskId);
}

function hostOf(url) {
  try { return new URL(url).host; } catch (e) { return url; }
}

// 列表渲染：按任务 id 复用行节点。
//
// 之前每 700ms 把整个列表拆了重建——任务多的时候这是内存和
// GC 压力的主要来源（每个行节点都带事件监听器和一堆子节点）。
// 现在只新建「新增的行」和删除「消失的行」，其余只更新文本。
const _rowCache = new Map();   // task_id -> {node, refs}

function renderDownloads() {
  const view = document.getElementById("viewDownloads");
  const tasks = filteredTasks();

  // 空状态
  let wrap = view.querySelector(".list-wrap");
  if (tasks.length === 0) {
    if (wrap) { wrap.remove(); wrap = null; }
    if (!view.querySelector(".empty")) {
      clear(view);
      view.appendChild(el("div", { class: "empty" }, [
        el("div", { class: "empty-icon" }, [icon("inbox", "icon-xl")]),
        el("div", { class: "empty-title", text: t("emptyTitle") }),
        el("div", { class: "empty-hint", text: t("emptyHint") }),
        el("div", { class: "empty-tip", text: t("emptyTipLang") }),
      ]));
    }
    _rowCache.clear();
    return;
  }

  // 有内容
  if (!wrap) {
    clear(view);
    wrap = el("div", { class: "list-wrap" });
    wrap.appendChild(el("div", { class: "list" }));
    view.appendChild(wrap);
  }
  const list = wrap.querySelector(".list");
  const emptyEl = view.querySelector(".empty");
  if (emptyEl) emptyEl.remove();

  const seen = new Set();
  let prev = null;
  for (const task of tasks) {
    seen.add(task.id);
    let entry = _rowCache.get(task.id);
    if (!entry) {
      const built = buildRow(task);
      entry = built;
      _rowCache.set(task.id, built);
    } else {
      updateRow(entry, task);
    }
    // 保证顺序
    const next = prev ? prev.nextSibling : list.firstChild;
    if (next !== entry.node) list.insertBefore(entry.node, next);
    prev = entry.node;
  }
  // 删掉不再显示的行
  for (const [id, entry] of _rowCache) {
    if (!seen.has(id)) {
      entry.node.remove();
      _rowCache.delete(id);
    }
  }
}

// ============ 过期链接横幅 ============
// Swift 版有这个视图但从没被任何地方引用（风险项 R-31）——
// 任务根本不会进 awaiting_new_link 状态。现在 Python 版真接上了，
// 所以横幅也必须真的显示。

function renderBanner() {
  const slot = document.getElementById("bannerSlot");
  clear(slot);
  const waiting = State.tasks.filter((x) => x.state === "awaiting_new_link");
  if (!waiting.length) return;

  const task = waiting[0];
  const input = el("input", { class: "input", placeholder: "https://..." });
  const submit = async () => {
    const url = input.value.trim();
    if (!url) return;
    const res = await api()?.relink_task(task.id, url);
    if (!res) { toast(t("expiredRefresh"), "err"); return; }
    if (res.decision === "unsafeReject") {
      toast(res.reason || t("expiredRefresh"), "err");
      return;
    }
    input.value = "";
    toast(t("expiredRefresh"), "ok");
    await refreshTasks();
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });

  slot.appendChild(el("div", { class: "banner" }, [
    icon("refresh"),
    el("span", { class: "banner-text", text: `${t("stateAwaitingLink")}：${task.filename || task.url}` }),
    input,
    el("button", { class: "btn btn-sm btn-primary", onclick: submit },
      [el("span", { text: t("expiredRefresh") })]),
  ]));
  if (waiting.length > 1) {
    slot.appendChild(el("div", { class: "banner", style: "background:var(--neutral-soft);color:var(--text-secondary);border-color:transparent" }, [
      el("span", { class: "banner-text", text: `另有 ${waiting.length - 1} 个任务等待新链接` }),
    ]));
  }
}

// ============ 详情抽屉 ============

function selectTask(taskId) {
  State.selectedTaskId = State.selectedTaskId === taskId ? null : taskId;
  render();
  // 分片明细单独取——列表接口不带它（省内存 + 省序列化）
  if (State.selectedTaskId) loadTaskChunks(State.selectedTaskId);
}

async function loadTaskChunks(taskId) {
  const chunks = await api()?.get_task_chunks(taskId);
  if (!chunks || !chunks.length) return;
  const task = State.tasks.find((x) => x.id === taskId);
  if (!task) return;
  task.chunks = chunks;
  if (State.selectedTaskId === taskId) renderDetail();
}

function renderDetail() {
  const detail = document.getElementById("detail");
  const app = document.getElementById("app");
  const task = State.tasks.find((x) => x.id === State.selectedTaskId);

  if (!task) {
    detail.hidden = true;
    app.classList.remove("with-detail");
    return;
  }
  detail.hidden = false;
  app.classList.add("with-detail");
  clear(detail);

  const meta = STATE_META[task.state] || STATE_META.queued;
  const pct = task.total_bytes > 0 ? (task.bytes_done / task.total_bytes) * 100 : 0;

  detail.appendChild(el("div", { class: "detail-head" }, [
    el("span", { class: "detail-title", text: task.filename || task.url }),
    el("button", {
      class: "btn btn-ghost btn-icon", title: t("detailClose"),
      onclick: () => { State.selectedTaskId = null; render(); },
    }, [icon("x")]),
  ]));

  const body = el("div", { class: "detail-body" });

  // 元信息
  const metaList = el("div", { class: "meta-list" });
  const addMeta = (key, value, mono) => {
    metaList.appendChild(el("div", { class: "meta-row" }, [
      el("span", { class: "meta-key", text: key }),
      el("span", { class: "meta-val" + (mono ? " mono" : ""), text: value }),
    ]));
  };
  addMeta(t("detailTaskState"), t(meta.key));
  addMeta(t("detailDestination"), task.destination || "—", true);
  addMeta(t("detailSize"), fmtSize(task.total_bytes));
  addMeta(t("detailSpeed"), fmtRate(task.speed_bytes_per_sec));
  addMeta(t("detailETA"), task.total_bytes && task.speed_bytes_per_sec > 0
    ? fmtDuration((task.total_bytes - task.bytes_done) / task.speed_bytes_per_sec) : "—");
  if (task.speed_limit_bytes_per_sec > 0) {
    addMeta(t("detailPerTaskLimit"), fmtRate(task.speed_limit_bytes_per_sec));
  }
  if (task.error_message) {
    addMeta(t("detailError"), task.error_message);
  }
  body.appendChild(metaList);

  // 分片
  if (task.chunks && task.chunks.length) {
    // 分片最多 128 个，全渲染会让 DOM 很大。只显示前 64 个，
    // 其余用一行汇总——用户要的是「看分片状态」，不是逐个核对。
    const CHUNK_VIEW_LIMIT = 64;
    body.appendChild(el("div", { class: "section-title" }, [
      icon("layers", "icon-sm"),
      el("span", { text: `${t("detailShowChunks")} (${task.completed_chunks}/${task.chunk_count})` }),
    ]));
    const table = el("table", { class: "chunk-table" }, [
      el("thead", {}, [el("tr", {}, [
        el("th", { text: t("taskDetailChunkHeader") }),
        el("th", { text: t("taskDetailChunkBytes") }),
        el("th", { text: t("taskDetailChunkStatus") }),
      ])]),
    ]);
    const tbody = el("tbody");
    task.chunks.slice(0, CHUNK_VIEW_LIMIT).forEach((c) => {
      const st = c.bytes_done >= c.size ? "done" : (c.bytes_done > 0 ? "downloading" : "pending");
      tbody.appendChild(el("tr", {}, [
        el("td", { class: "mono", text: `#${c.id}` }),
        el("td", { class: "mono", text: `${fmtSize(c.bytes_done)} / ${fmtSize(c.size)}` }),
        el("td", {}, [
          el("span", { class: "chunk-dot chunk-" + st }),
          el("span", { text: t("taskDetailChunk" + st.charAt(0).toUpperCase() + st.slice(1)) }),
        ]),
      ]));
    });
    if (task.chunks.length > CHUNK_VIEW_LIMIT) {
      tbody.appendChild(el("tr", {}, [
        el("td", { colspan: "3", class: "cell-muted",
          text: `… 另有 ${task.chunks.length - CHUNK_VIEW_LIMIT} 个分片` }),
      ]));
    }
    table.appendChild(tbody);
    body.appendChild(table);

    body.appendChild(el("button", {
      class: "btn btn-sm", style: "margin-top:12px",
      onclick: async () => {
        const ok = await api()?.copy_to_clipboard(JSON.stringify(task, null, 2));
        toast(ok?.ok ? t("detailCopyJSON") : t("detailCopyJSON"), ok?.ok ? "ok" : "err");
      },
    }, [icon("copy", "icon-sm"), el("span", { text: t("detailCopyJSON") })]));
  }

  detail.appendChild(body);
}

// ============ 历史 ============

function renderHistory() {
  const view = document.getElementById("viewHistory");
  clear(view);

  const search = el("div", { class: "search-bar" }, [
    el("div", { class: "search-wrap" }, [
      icon("search", "icon-sm"),
      el("input", { class: "input", placeholder: t("historyFilter"), oninput: renderHistory }),
    ]),
    el("span", { class: "search-count", text: String(State.history.length) }),
  ]);
  view.appendChild(search);

  if (!State.history.length) {
    view.appendChild(el("div", { class: "empty" }, [
      el("div", { class: "empty-icon" }, [icon("clock", "icon-xl")]),
      el("div", { class: "empty-title", text: t("historyNoEntries") }),
      el("div", { class: "empty-hint", text: t("historyExplanation") }),
    ]));
    return;
  }

  const table = el("table", { class: "data" }, [
    el("thead", {}, [el("tr", {}, [
      el("th", { text: t("historyColumnFile") }),
      el("th", { text: t("historyColumnSize") }),
      el("th", { text: t("historyColumnState") }),
      el("th", { text: t("historyColumnDate") }),
      el("th", { text: "" }),
    ])]),
  ]);
  const tbody = el("tbody");
  State.history.forEach((entry) => {
    tbody.appendChild(el("tr", {}, [
      el("td", {}, [
        el("div", { class: "trunc", text: entry.filename }),
        el("div", { class: "trunc row-sub", text: entry.url }),
      ]),
      el("td", { class: "num", text: fmtSize(entry.total_bytes) }),
      el("td", {}, [el("span", { class: "badge badge-" + entry.state, text: entry.state })]),
      el("td", { class: "num", text: fmtDate(entry.finished_at) }),
      el("td", {}, [
        el("button", {
          class: "btn btn-ghost btn-icon", title: t("historyActionRedownload"),
          onclick: () => api()?.add_url(entry.url),
        }, [icon("download")]),
        el("button", {
          class: "btn btn-ghost btn-icon", title: t("historyActionReveal"),
          onclick: () => api()?.reveal_in_finder(entry.destination_path),
        }, [icon("folder")]),
        el("button", {
          class: "btn btn-ghost btn-icon", title: t("historyActionRemove"),
          onclick: async () => { await api()?.remove_history_entry(entry.id); await refreshHistory(); },
        }, [icon("trash")]),
      ]),
    ]));
  });
  table.appendChild(tbody);
  view.appendChild(el("div", { class: "table-wrap" }, [table]));
}

// ============ 计划任务 ============

function renderSchedule() {
  const view = document.getElementById("viewSchedule");
  clear(view);

  const urlInput = el("input", { class: "input", placeholder: "https://example.com/file.zip" });
  const timeInput = el("input", { class: "input", type: "datetime-local" });
  const noteInput = el("input", { class: "input", placeholder: t("scheduleNote") });
  const err = el("div", { class: "modal-error" });

  const submit = async () => {
    err.textContent = "";
    const ts = new Date(timeInput.value).getTime() / 1000;
    const res = await api()?.add_schedule(urlInput.value, ts, noteInput.value);
    if (!res?.ok) { err.textContent = res?.error || "失败"; return; }
    urlInput.value = ""; noteInput.value = "";
    await refreshSchedules();
    render();
  };

  view.appendChild(el("div", { class: "card", style: "margin:16px 20px 0" }, [
    el("div", { class: "form" }, [
      el("div", { class: "field" }, [
        el("label", { class: "field-label", text: t("scheduleURL") }),
        urlInput,
      ]),
      el("div", { class: "field-row" }, [
        el("div", { class: "field" }, [
          el("label", { class: "field-label", text: t("scheduleFireAt") }),
          timeInput,
        ]),
        el("div", { class: "field" }, [
          el("label", { class: "field-label", text: t("scheduleNote") }),
          noteInput,
        ]),
        el("button", { class: "btn btn-primary", style: "align-self:flex-end", onclick: submit },
          [icon("plus"), el("span", { text: t("scheduleAdd") })]),
      ]),
      err,
    ]),
  ]));

  if (!State.schedules.length) {
    view.appendChild(el("div", { class: "empty" }, [
      el("div", { class: "empty-icon" }, [icon("alarm", "icon-xl")]),
      el("div", { class: "empty-title", text: t("scheduleEmpty") }),
      el("div", { class: "empty-hint", text: t("scheduleHint") }),
    ]));
    return;
  }

  const table = el("table", { class: "data" }, [
    el("thead", {}, [el("tr", {}, [
      el("th", { text: t("scheduleColumnFireAt") }),
      el("th", { text: t("scheduleColumnURL") }),
      el("th", { text: t("scheduleColumnNote") }),
      el("th", { text: "" }),
    ])]),
  ]);
  const tbody = el("tbody");
  State.schedules.forEach((item) => {
    tbody.appendChild(el("tr", {}, [
      el("td", { class: "num", text: fmtDate(item.fire_at) }),
      el("td", { class: "trunc mono", text: item.url }),
      el("td", { class: "trunc", text: item.note || "—" }),
      el("td", {}, [
        el("button", {
          class: "btn btn-ghost btn-icon", title: t("scheduleColumnRemove"),
          onclick: async () => { await api()?.remove_schedule(item.id); await refreshSchedules(); render(); },
        }, [icon("x")]),
      ]),
    ]));
  });
  table.appendChild(tbody);
  view.appendChild(el("div", { class: "table-wrap" }, [table]));
}

// ============ 设置 ============

function renderSettings() {
  const view = document.getElementById("viewSettings");
  clear(view);
  const s = State.settings;

  const wrap = el("div", { class: "list-wrap" });

  // 通用
  const general = el("div", { class: "setting-section" }, [
    el("div", { class: "setting-section-title", text: t("settingsSectionGeneral") }),
    el("div", { class: "card" }, [
      toggleRow(t("settingsLanguage"), t("settingsLanguageHint"), "locale", ["system", "en", "zh-Hans"], s.locale),
      toggleRow(t("settingsToggleAutoCategorize"), "", "classify_enabled", null, s.classify_enabled),
      toggleRow(t("settingsToggleClipboardCapture"), "", "capture_clipboard", null, s.capture_clipboard),
      toggleRow(t("settingsToggleSystemNotifications"), "", "notify_on_complete", null, s.notify_on_complete),
      toggleRow(t("settingsToggleHideDockWhenWindowClosed"), "", "hide_dock_when_window_closed", null, s.hide_dock_when_window_closed),
      toggleRow(t("settingsToggleShowToolbarLabels"), "", "toolbar_show_labels", null, s.toolbar_show_labels),
      toggleRow(t("settingsToggleIgnoreSSL"), t("settingsIgnoreSSLHint"), "network_ignore_ssl", null, s.network_ignore_ssl),
    ]),
  ]);
  wrap.appendChild(general);

  // 默认
  const threadsInput = el("input", { class: "input", type: "number", min: "1", max: "128", value: String(s.threads_default) });
  threadsInput.addEventListener("change", () => {
    api()?.update_settings({ threads_default: parseInt(threadsInput.value, 10) || 32 });
  });
  const bandwidthInput = el("input", { class: "input", type: "number", min: "0", value: String(s.bandwidth_bytes_per_sec) });
  bandwidthInput.addEventListener("change", () => {
    api()?.update_settings({ bandwidth_bytes_per_sec: parseInt(bandwidthInput.value, 10) || 0 });
  });

  wrap.appendChild(el("div", { class: "setting-section" }, [
    el("div", { class: "setting-section-title", text: t("settingsSectionDefaults") }),
    el("div", { class: "card" }, [
      el("div", { class: "field", style: "margin-bottom:16px" }, [
        el("label", { class: "field-label", text: t("settingsDefaultThreadsLabel") }),
        el("div", { class: "slider-row" }, [
          el("input", {
            class: "slider", type: "range", min: "1", max: "128", value: String(s.threads_default),
            oninput: (e) => { threadsInput.value = e.target.value; },
            onchange: (e) => api()?.update_settings({ threads_default: parseInt(e.target.value, 10) }),
          }),
          threadsInput,
        ]),
      ]),
      el("div", { class: "field" }, [
        el("label", { class: "field-label", text: t("settingsBandwidth") }),
        el("div", { class: "field-row" }, [
          bandwidthInput,
          el("span", { class: "field-hint", text: t("settingsKbps") }),
        ]),
        el("div", { class: "field-hint", text: t("settingsBandwidthHint") }),
      ]),
    ]),
  ]));

  // 分类
  const catCard = el("div", { class: "card" });
  Object.entries(CATEGORY_META).forEach(([key, meta]) => {
    catCard.appendChild(el("div", { class: "setting-toggle" }, [
      el("div", { class: "setting-toggle-text" }, [
        el("div", { class: "setting-toggle-label cat-label" }, [
          icon(meta.icon, "icon-sm"),
          el("span", { text: key.charAt(0).toUpperCase() + key.slice(1) }),
        ]),
      ]),
    ]));
  });
  wrap.appendChild(el("div", { class: "setting-section" }, [
    el("div", { class: "setting-section-title", text: t("settingsSectionCategories") }),
    el("div", { class: "setting-section-hint", text: t("settingsCategoriesPlaceholder") }),
    catCard,
  ]));

  // 站点规则
  wrap.appendChild(renderSiteRulesSection());

  // 启动
  wrap.appendChild(el("div", { class: "setting-section" }, [
    el("div", { class: "setting-section-title", text: t("settingsSectionStartup") }),
    el("div", { class: "card" }, [
      toggleRow(t("settingsOpenAtLogin"), t("settingsOpenAtLoginHint"), "__openAtLogin", null, s.open_at_login),
      el("div", { class: "field" }, [
        el("label", { class: "field-label", text: t("settingsUpdateChannel") }),
        el("select", {
          class: "select",
          onchange: (e) => api()?.update_settings({ update_channel: e.target.value }),
        }, [
          el("option", { value: "stable", text: t("settingsUpdateStable"), selected: s.update_channel === "stable" }),
          el("option", { value: "pre", text: t("settingsUpdatePrerelease"), selected: s.update_channel === "pre" }),
        ]),
      ]),
    ]),
  ]));

  // 日志
  wrap.appendChild(renderLogsSection());

  // 关于
  wrap.appendChild(el("div", { class: "setting-section" }, [
    el("div", { class: "setting-section-title", text: t("settingsSidebarAbout") }),
    el("div", { class: "card", style: "text-align:center" }, [
      el("div", { class: "empty-title", text: t("settingsAboutTitle") }),
      el("div", { class: "field-hint", text: t("settingsAboutVersion") + " " + (State.appInfo?.version || "") }),
      el("div", { class: "field-hint", text: t("settingsAboutLicense") }),
      el("div", { class: "field-hint", text: t("settingsAboutCopyright") }),
    ]),
  ]));

  view.appendChild(wrap);
}

function toggleRow(label, hint, key, options, value) {
  const row = el("div", { class: "setting-toggle" }, [
    el("div", { class: "setting-toggle-text" }, [
      el("div", { class: "setting-toggle-label", text: label }),
      hint ? el("div", { class: "setting-toggle-hint", text: hint }) : null,
    ]),
  ]);

  if (options) {
    const sel = el("select", {
      class: "select", style: "width:auto;min-width:120px",
      onchange: async (e) => {
        if (key === "locale") {
          const res = await api()?.set_language(e.target.value);
          if (res?.translations) { State.strings = res.translations; State.locale = e.target.value; applyStaticI18n(); }
        } else {
          api()?.update_settings({ [key]: e.target.value });
        }
        render();
      },
    }, options.map((o) => el("option", { value: o, text: o === "system" ? t("settingsLanguageSystem") : (o === "zh-Hans" ? "简体中文" : "English"), selected: o === value })));
    row.appendChild(sel);
  } else {
    const input = el("input", { type: "checkbox", checked: !!value });
    input.addEventListener("change", async () => {
      if (key === "__openAtLogin") {
        await api()?.set_open_at_login(input.checked);
      } else {
        await api()?.update_settings({ [key]: input.checked });
        if (key === "toolbar_show_labels") {
          State.showToolbarLabels = input.checked;
          document.body.classList.toggle("hide-toolbar-labels", !input.checked);
        }
      }
      State.settings[key] = input.checked;
    });
    row.appendChild(el("label", { class: "switch" }, [input, el("span", { class: "switch-track" }), el("span", { class: "switch-thumb" })]));
  }
  return row;
}

function renderSiteRulesSection() {
  const host = el("input", { class: "input", placeholder: t("siteRulesHost") });
  const referer = el("input", { class: "input", placeholder: t("siteRulesReferer") });
  const ua = el("input", { class: "input", placeholder: t("siteRulesUserAgent") });
  const cookie = el("input", { class: "input", placeholder: t("siteRulesCookie") });
  const user = el("input", { class: "input", placeholder: t("siteRulesBasicAuthUser") });
  const pass = el("input", { class: "input", type: "password", placeholder: t("siteRulesBasicAuthPassword") });
  const err = el("div", { class: "modal-error" });

  const add = async () => {
    err.textContent = "";
    const res = await api()?.add_site_rule(host.value, referer.value, ua.value, cookie.value, user.value, pass.value);
    if (!res?.ok) { err.textContent = res?.error || "失败"; return; }
    host.value = referer.value = ua.value = cookie.value = user.value = pass.value = "";
    State.siteRules = res.rules || [];
    render();
  };

  const card = el("div", { class: "card" }, [
    el("div", { class: "setting-section-hint", text: t("siteRulesExplanation") }),
    el("div", { class: "form" }, [
      el("div", { class: "field-row" }, [host, referer]),
      el("div", { class: "field-row" }, [ua, cookie]),
      el("div", { class: "field-row" }, [user, pass]),
      el("div", { class: "field-row" }, [
        el("button", { class: "btn btn-primary", onclick: add }, [icon("plus"), el("span", { text: t("siteRulesAdd") })]),
        el("button", {
          class: "btn btn-danger",
          onclick: async () => { const r = await api()?.clear_site_rules(); State.siteRules = r?.rules || []; render(); },
        }, [icon("trash"), el("span", { text: t("siteRulesClearAll") })]),
      ]),
      err,
    ]),
  ]);

  if (State.siteRules.length) {
    const list = el("div", { style: "margin-top:12px" });
    State.siteRules.forEach((rule) => {
      const bits = [];
      if (rule.referer) bits.push(t("siteRulesRefererShort") + ": " + rule.referer);
      if (rule.user_agent) bits.push(t("siteRulesUserAgentShort") + ": " + rule.user_agent);
      if (rule.cookies) bits.push(t("siteRulesCookieShort") + ": " + rule.cookies);
      if (rule.basic_auth_user) bits.push(t("siteRulesBasicAuthShort") + ": " + rule.basic_auth_user);
      list.appendChild(el("div", { class: "setting-toggle" }, [
        el("div", { class: "setting-toggle-text" }, [
          el("div", { class: "setting-toggle-label", text: rule.host }),
          bits.length ? el("div", { class: "setting-toggle-hint trunc", text: bits.join(" · ") }) : null,
        ]),
        el("button", {
          class: "btn btn-ghost btn-icon",
          onclick: async () => { const r = await api()?.remove_site_rule(rule.id); State.siteRules = r?.rules || []; render(); },
        }, [icon("trash")]),
      ]));
    });
    card.appendChild(list);
  } else {
    card.appendChild(el("div", { class: "setting-section-hint", style: "margin-top:12px", text: t("siteRulesEmpty") }));
  }

  return el("div", { class: "setting-section" }, [
    el("div", { class: "setting-section-title", text: t("settingsSiteRulesLink") }),
    card,
  ]);
}

function renderLogsSection() {
  const s = State.settings;
  const card = el("div", { class: "card" });

  const levelSel = el("select", {
    class: "select",
    onchange: (e) => api()?.update_settings({ logs_level: parseInt(e.target.value, 10) }),
  }, [
    ["DEBUG", 0], ["INFO", 1], ["NOTICE", 2], ["WARNING", 3], ["ERROR", 4],
  ].map(([name, val]) => el("option", { value: String(val), text: name, selected: s.logs_level === val })));

  card.appendChild(el("div", { class: "field", style: "margin-bottom:12px" }, [
    el("label", { class: "field-label", text: t("settingsLogsLevelLabel") }),
    levelSel,
  ]));
  card.appendChild(toggleRow(t("settingsLogsPersistenceLabel"), "", "logs_persistence", null, s.logs_persistence));

  const numField = (label, key, value, min) => {
    const input = el("input", { class: "input", type: "number", min: String(min), value: String(value) });
    input.addEventListener("change", () => api()?.update_settings({ [key]: parseInt(input.value, 10) || min }));
    return el("div", { class: "field", style: "margin-top:12px" }, [
      el("label", { class: "field-label", text: label }),
      input,
    ]);
  };
  card.appendChild(numField(t("settingsLogsRetentionLabel"), "logs_retention_days", s.logs_retention_days, 1));
  card.appendChild(numField(t("settingsLogsMaxBytesLabel"), "logs_max_bytes_kb", s.logs_max_bytes_kb, 32));
  card.appendChild(numField(t("settingsLogsMaxLinesLabel"), "logs_max_lines", s.logs_max_lines, 100));

  card.appendChild(el("div", { class: "field-row", style: "margin-top:16px" }, [
    el("button", { class: "btn", onclick: openLogPreview }, [icon("file"), el("span", { text: t("settingsLogsPreviewButton") })]),
    el("button", {
      class: "btn",
      onclick: async () => { const r = await api()?.get_log_file_path(); if (r?.path) api()?.reveal_in_finder(r.path); },
    }, [icon("folder"), el("span", { text: t("settingsLogsRevealInFinder") })]),
  ]));

  return el("div", { class: "setting-section" }, [
    el("div", { class: "setting-section-title", text: t("settingsSectionLogs") }),
    card,
  ]);
}

// ============ 模态 ============

function openModal(titleKey, bodyNodes, footNodes) {
  const root = document.getElementById("modalRoot");
  const modal = document.getElementById("modal");
  clear(modal);
  modal.appendChild(el("div", { class: "modal-head" }, [
    el("div", { class: "modal-title", text: t(titleKey) }),
    el("button", { class: "btn btn-ghost btn-icon", onclick: closeModal }, [icon("x")]),
  ]));
  const body = el("div", { class: "modal-body" });
  (Array.isArray(bodyNodes) ? bodyNodes : [bodyNodes]).forEach((n) => n && body.appendChild(n));
  modal.appendChild(body);
  if (footNodes) {
    const foot = el("div", { class: "modal-foot" });
    (Array.isArray(footNodes) ? footNodes : [footNodes]).forEach((n) => n && foot.appendChild(n));
    modal.appendChild(foot);
  }
  root.hidden = false;
  document.getElementById("modalBackdrop").onclick = closeModal;
  const firstInput = modal.querySelector("input,select,textarea");
  if (firstInput) setTimeout(() => firstInput.focus(), 30);
}

function closeModal() {
  document.getElementById("modalRoot").hidden = true;
}

function openAddDialog() {
  const url = el("input", { class: "input", placeholder: "https://example.com/file.zip" });
  const name = el("input", { class: "input", placeholder: t("addSheetFilenameHint") });
  const err = el("div", { class: "modal-error" });

  const submit = async () => {
    err.textContent = "";
    const res = await api()?.add_url(url.value, name.value || null);
    if (!res?.ok) { err.textContent = res?.error || t("addSheetErrorBadURL"); return; }
    closeModal();
    toast(t("toolbarAddURL"));
    await refreshTasks();
  };

  openModal("addSheetTitle", [
    el("div", { class: "form" }, [
      el("div", { class: "field" }, [
        el("label", { class: "field-label", text: t("addSheetURLField") }),
        url,
      ]),
      el("div", { class: "field" }, [
        el("label", { class: "field-label", text: t("addSheetFilenameField") }),
        name,
        el("div", { class: "field-hint", text: t("addSheetFilenameHint") }),
      ]),
      err,
    ]),
  ], [
    el("button", { class: "btn", onclick: closeModal }, [el("span", { text: t("addSheetCancel") })]),
    el("button", { class: "btn btn-primary", onclick: submit }, [el("span", { text: t("addSheetAdd") })]),
  ]);
}

async function openLogPreview() {
  const logs = await api()?.get_logs(500);
  const filter = el("input", { class: "input", placeholder: t("settingsLogsPreviewFilter") });
  const body = el("div", { style: "max-height:46vh;overflow:auto" });
  const pathInfo = await api()?.get_log_file_path();

  const draw = () => {
    clear(body);
    const q = (filter.value || "").toLowerCase();
    const rows = (logs || []).filter((l) =>
      !q || l.message.toLowerCase().includes(q) || l.category.toLowerCase().includes(q));
    if (!rows.length) {
      body.appendChild(el("div", { class: "empty-hint", text: t("settingsLogsPreviewEmpty") }));
      return;
    }
    const table = el("table", { class: "data log-table" }, [
      el("thead", {}, [el("tr", {}, [
        el("th", { text: "Time" }), el("th", { text: "Level" }),
        el("th", { text: "Category" }), el("th", { text: "Message" }),
      ])]),
    ]);
    const tbody = el("tbody");
    rows.forEach((l) => {
      tbody.appendChild(el("tr", {}, [
        el("td", { class: "mono", text: (l.timestamp || "").replace("T", " ").slice(0, 23) }),
        el("td", { class: "log-lvl-" + l.level, text: l.level_tag }),
        el("td", { class: "mono", text: l.category }),
        el("td", { class: "msg", text: l.message }),
      ]));
    });
    table.appendChild(tbody);
    body.appendChild(table);
  };
  filter.addEventListener("input", draw);
  draw();

  openModal("settingsLogsPreviewTitle", [
    el("div", { class: "field-hint mono", style: "margin-bottom:8px", text: pathInfo?.path || "" }),
    filter, body,
  ], [
    el("button", {
      class: "btn",
      onclick: async () => {
        const text = (logs || []).map((l) => `${l.timestamp} [${l.level_tag}] [${l.category}] ${l.message}`).join("\n");
        await api()?.copy_to_clipboard(text);
        toast(t("settingsLogsPreviewCopy"));
      },
    }, [icon("copy"), el("span", { text: t("settingsLogsPreviewCopy") })]),
    el("button", { class: "btn btn-primary", onclick: closeModal }, [el("span", { text: t("settingsLogsPreviewClose") })]),
  ]);
}

function openSpeedLimitDialog(task) {
  const input = el("input", { class: "input", type: "number", min: "0", value: String(Math.round((task.speed_limit_bytes_per_sec || 0) / 1024)) });
  const submit = async () => {
    const kb = parseInt(input.value, 10) || 0;
    await api()?.set_task_speed_limit(task.id, kb * 1024);
    closeModal();
    toast(t("rowLimitSpeed"));
    await refreshTasks();
  };
  openModal("rowLimitSpeed", [
    el("div", { class: "form" }, [
      el("div", { class: "field" }, [
        el("label", { class: "field-label", text: t("settingsKbps") }),
        input,
        el("div", { class: "field-hint", text: "0 = 使用全局限制" }),
      ]),
    ]),
  ], [
    el("button", { class: "btn", onclick: closeModal }, [el("span", { text: t("addSheetCancel") })]),
    el("button", { class: "btn btn-primary", onclick: submit }, [el("span", { text: t("addSheetAdd") })]),
  ]);
}

function openRelinkDialog(task) {
  const input = el("input", { class: "input", placeholder: "https://..." });
  const err = el("div", { class: "modal-error" });
  const submit = async () => {
    err.textContent = "";
    const res = await api()?.relink_task(task.id, input.value);
    if (!res) { err.textContent = "失败"; return; }
    if (res.decision === "unsafeReject") {
      err.textContent = res.reason || t("expiredRefresh");
      return;
    }
    closeModal();
    toast(t("expiredRefresh"));
    await refreshTasks();
  };
  openModal("expiredRefresh", [
    el("div", { class: "form" }, [
      el("div", { class: "field" }, [
        el("label", { class: "field-label", text: t("expiredRefresh") }),
        input,
      ]),
      err,
    ]),
  ], [
    el("button", { class: "btn", onclick: closeModal }, [el("span", { text: t("addSheetCancel") })]),
    el("button", { class: "btn btn-primary", onclick: submit }, [el("span", { text: t("addSheetAdd") })]),
  ]);
}

// ============ 右键菜单 ============

function openContextMenu(event, task) {
  const menu = document.getElementById("ctxmenu");
  clear(menu);
  const isActive = task.state === "downloading" || task.state === "queued";

  const item = (labelKey, iconName, action, danger) => {
    menu.appendChild(el("button", {
      class: "ctxmenu-item" + (danger ? " danger" : ""),
      onclick: () => { closeContextMenu(); action(); },
    }, [icon(iconName, "icon-sm"), el("span", { class: "ctx-label", text: t(labelKey) })]));
  };

  if (isActive) item("rowPause", "pause", () => api()?.pause_task(task.id));
  else item("rowResume", "play", () => api()?.resume_task(task.id));
  if (task.state === "failed") item("rowRetry", "refresh", () => api()?.retry_task(task.id));
  if (isActive) item("rowCancel", "x", () => api()?.cancel_task(task.id));

  menu.appendChild(el("div", { class: "ctxmenu-sep" }));
  item("rowOpen", "external", () => api()?.open_file(task.destination));
  item("rowReveal", "folder", () => api()?.reveal_in_finder(task.destination));
  item("rowOpenSource", "link", () => api()?.open_source_url(task.url));
  item("rowCopyURL", "copy", async () => {
    await api()?.copy_to_clipboard(task.url);
    toast(t("rowCopyURL"));
  });
  item("rowLimitSpeed", "gauge", () => openSpeedLimitDialog(task));

  if (task.state === "completed") {
    item("rowChecksum", "hash", async () => {
      const res = await api()?.compute_checksum(task.destination);
      if (res?.ok) {
        await api()?.copy_to_clipboard(res.sha256);
        toast(res.sha256.slice(0, 16) + "…", "ok");
      } else {
        toast(res?.error || "失败", "err");
      }
    });
  }

  menu.appendChild(el("div", { class: "ctxmenu-sep" }));
  item("rowRemove", "x", async () => { await api()?.remove_task(task.id); toast(t("rowRemove")); await refreshTasks(); });
  if (task.state === "completed") {
    item("rowDelete", "trash", async () => { await api()?.delete_file(task.destination); await api()?.remove_task(task.id); await refreshTasks(); });
  }

  menu.hidden = false;
  const rect = menu.getBoundingClientRect();
  let x = event.clientX;
  let y = event.clientY;
  if (x + rect.width > window.innerWidth - 8) x = window.innerWidth - rect.width - 8;
  if (y + rect.height > window.innerHeight - 8) y = window.innerHeight - rect.height - 8;
  menu.style.left = Math.max(8, x) + "px";
  menu.style.top = Math.max(8, y) + "px";
}

function closeContextMenu() {
  document.getElementById("ctxmenu").hidden = true;
}

document.addEventListener("click", (e) => {
  const menu = document.getElementById("ctxmenu");
  if (!menu.hidden && !menu.contains(e.target)) closeContextMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeContextMenu(); closeModal(); }
});

// ============ 视图切换 ============

function setView(view) {
  State.view = view;
  document.querySelectorAll(".view").forEach((v) => {
    v.hidden = v.dataset.view !== view;
  });
  if (view === "history") refreshHistory();
  if (view === "schedule") refreshSchedules();
  render();
}

// ============ 渲染 ============

function render() {
  // 视图显隐统一在这里处理。只改 State.view 而不走 setView
  // 也不会漏切 DOM。
  document.querySelectorAll(".view").forEach((v) => {
    v.hidden = v.dataset.view !== State.view;
  });
  renderSidebar();
  renderToolbar();
  renderBanner();
  if (State.view === "downloads") renderDownloads();
  else if (State.view === "history") renderHistory();
  else if (State.view === "schedule") renderSchedule();
  else if (State.view === "settings") renderSettings();
  renderDetail();
}

// ============ 数据刷新 ============

async function refreshTasks() {
  const tasks = await api()?.get_tasks();
  if (tasks) { State.tasks = tasks; render(); }
}

async function refreshHistory() {
  const h = await api()?.get_history(500);
  if (h) State.history = h;
}

async function refreshSchedules() {
  const s = await api()?.get_schedules();
  if (s) State.schedules = s;
}

// ============ 后端推送 ============

function onBootstrap(state) {
  State.locale = state.language || "system";
  State.strings = state.translations || {};
  State.tasks = state.tasks || [];
  State.settings = state.settings || {};
  State.appInfo = state.app;
  State.showToolbarLabels = State.settings.toolbar_show_labels !== false;
  document.body.classList.toggle("hide-toolbar-labels", !State.showToolbarLabels);
  applyStaticI18n();
  State.booted = true;
  render();
  refreshHistory();
  refreshSchedules();
  const rules = api()?.get_site_rules();
  if (rules) { rules.then((r) => { State.siteRules = r || []; render(); }); }
}

function onClipboardCandidate(candidate) {
  State.clipboardCandidate = candidate;
  openClipboardDialog(candidate);
}

function openClipboardDialog(candidate) {
  const catSel = el("select", { class: "select" },
    Object.keys(CATEGORY_META).map((c) => el("option", { value: c, text: c, selected: c === candidate.category })));
  const dirInput = el("input", { class: "input", value: "" });
  const threadInput = el("input", { class: "input", type: "number", min: "1", max: "128", value: String(candidate.default_threads || 32) });

  const submit = async (skip) => {
    if (!skip) {
      await api()?.add_url(candidate.url, null, catSel.value, dirInput.value || null, parseInt(threadInput.value, 10) || null);
    } else {
      await api()?.dismiss_clipboard_candidate();
    }
    closeModal();
    await refreshTasks();
  };

  openModal("clipboardSheetTitle", [
    el("div", { class: "form" }, [
      el("div", { class: "field-hint mono", style: "overflow-wrap:anywhere", text: candidate.url }),
      el("div", { class: "field" }, [
        el("label", { class: "field-label", text: t("clipboardCategory") }), catSel,
      ]),
      el("div", { class: "field" }, [
        el("label", { class: "field-label", text: t("clipboardSavePath") }),
        el("div", { class: "field-row" }, [
          dirInput,
          el("button", {
            class: "btn",
            onclick: async () => { const r = await api()?.choose_directory(); if (r?.path) dirInput.value = r.path; },
          }, [icon("folder"), el("span", { text: t("clipboardChoose") })]),
        ]),
      ]),
      el("div", { class: "field" }, [
        el("label", { class: "field-label", text: t("clipboardMaxThreads") }),
        threadInput,
        el("div", { class: "field-hint", text: t("clipboardMaxThreadsHint") }),
      ]),
    ]),
  ], [
    el("button", { class: "btn", onclick: () => submit(true) }, [el("span", { text: t("clipboardSkip") })]),
    el("button", { class: "btn btn-primary", onclick: () => submit(false) }, [el("span", { text: t("clipboardDownload") })]),
  ]);
}

function onScheduleFired() { refreshTasks(); }

// ============ 启动 ============

async function boot() {
  // pywebview 的 API 注入是异步的，等它就绪。
  for (let i = 0; i < 100; i++) {
    if (api() && typeof api().get_tasks === "function") break;
    await new Promise((r) => setTimeout(r, 50));
  }
  const state = await api()?.bootstrap();
  if (state) onBootstrap(state);
}

document.addEventListener("DOMContentLoaded", () => {
  // 拖拽 URL 入队
  document.addEventListener("dragover", (e) => { e.preventDefault(); });
  document.addEventListener("drop", async (e) => {
    e.preventDefault();
    const text = e.dataTransfer?.getData("text/plain") || "";
    const url = text.trim();
    if (!url.toLowerCase().startsWith("http")) return;
    const res = await api()?.add_url(url);
    if (res?.ok) { toast(t("toolbarAddURL")); await refreshTasks(); }
  });

  boot();
  // 定时刷新任务状态
  // 轮询：只在下载视图、且窗口可见时进行。
  // 窗口最小化/隐藏时还按 700ms 拉数据是纯浪费。
  setInterval(() => {
    if (State.view !== "downloads") return;
    if (document.hidden) return;
    refreshTasks();
  }, 700);
});
