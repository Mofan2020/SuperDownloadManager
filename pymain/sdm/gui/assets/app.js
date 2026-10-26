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
  expandedChunks: null,
  clipboardCandidate: null,
  showToolbarLabels: true,
  booted: false,
  // 下载列表的排序。sortKey 为 null 时用默认排序（下载中优先）。
  // 可选：filename / size / progress / speed / eta / state / category。
  sortKey: null,
  sortAsc: true,
  // 主题：system / light / dark（规格 §2.5）。
  theme: "system",
  // 用户在列选择器里关掉的列（列 id）。存进 settings.hidden_columns。
  hiddenColumns: [],
  // 列表容器的宽度——窄屏该收哪些列靠它判断。
  listWidth: 1200,
  // 当前列组合的指纹。变了就得重建所有行（列的数量/顺序变了）。
  columnSignature: "",
  // 全局速度（推送通道给的）。received = false 表示还没收到过推送。
  globalSpeed: { bytes_per_sec: 0, limit_bytes_per_sec: 0, active_count: 0, received: false },
  // 每个任务最近 60 秒的速度采样——详情面板的速度曲线用它。
  speedSamples: {},
  // 后端给的速度历史（有 get_speed_history 时优先用它）。
  speedHistory: {},
  // 每个任务的错误详情（get_task_error 的结果）。
  taskErrors: {},
};

const api = () => window.pywebview?.api;

// ============ 文案 ============

function t(key, fallback) {
  const s = State.strings[key];
  if (s !== undefined && s !== null) return s;
  if (fallback !== undefined) return fallback;
  return key;
}

// 带占位符的文案。
//
// **占位符是 Swift 风格的 \(0)**——文案表（契约里的键）一直这么写，
// 前端原来没有替换过，所以这类文案在前端是显示不出来的。
// 这里按位置替换：tf("addSheetProbeSize", fmtSize(size))。
function tf(key, ...args) {
  const s = State.strings[key];
  if (s === undefined || s === null) return key;
  return s.replace(/\\\(\d+\)/g, (m) => {
    const idx = Number(m.slice(2, -1));
    const value = args[idx];
    return value === undefined || value === null ? m : String(value);
  });
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

// 分片静默时长：比任务级的 ETA 细。
// 任务级用整秒够了（ETA 差 1 秒无所谓），但分片可能 0.1 秒就下完，
// 取整会显示成 "0s"——一列全是 0s 等于没信息。
function fmtChunkElapsed(seconds) {
  if (seconds == null || seconds < 0 || !isFinite(seconds)) return "—";
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  return fmtDuration(seconds);
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

// 分类的显示元数据。
//
// **分类名走 i18n**（key 字段）——原来直接把英文枚举值首字母大写
// 显示（"Video" / "Music"），中文界面下夹着英文很难看，而且
// 换语言时那几行不变。
const CATEGORY_META = {
  video:     { icon: "video",    color: "#DB2777", key: "categoryVideo" },
  music:     { icon: "music",    color: "#9333EA", key: "categoryMusic" },
  pictures:  { icon: "image",    color: "#0D9488", key: "categoryPictures" },
  archives:  { icon: "archive",  color: "#EA580C", key: "categoryArchives" },
  documents: { icon: "file",     color: "#2563EB", key: "categoryDocuments" },
  software:  { icon: "package",  color: "#4F46E5", key: "categorySoftware" },
  other:     { icon: "help",     color: "#64748B", key: "categoryOther" },
};

// 分类的本地化名。
function categoryLabel(cat) {
  const meta = CATEGORY_META[cat];
  return meta ? t(meta.key) : (cat || "");
}

// ============ 任务列表的列 ============
//
// **列的显隐是用户可控的**（规格 §2.1）：列选择器里勾掉的列不渲染，
// 选择存进 settings.hidden_columns，下次启动还在。
//
// 窄屏自动收起的列写在 responsiveHiddenColumns() 里——它跟
// style.css 的容器查询阈值是一套的，改一边要改另一边。
const COLUMN_DEFS = [
  { id: "name",     labelKey: "listColumnName",     widthVar: "--w-name",     sortKey: "filename" },
  { id: "category", labelKey: "listColumnCategory", widthVar: "--w-category", sortKey: "category" },
  { id: "state",    labelKey: "listColumnState",    widthVar: "--w-state",    sortKey: "state" },
  { id: "progress", labelKey: "listColumnProgress", widthVar: "--w-progress", sortKey: "progress" },
  { id: "size",     labelKey: "listColumnSize",     widthVar: "--w-size",     sortKey: "size" },
  { id: "speed",    labelKey: "listColumnSpeed",    widthVar: "--w-speed",    sortKey: "speed" },
  { id: "eta",      labelKey: "listColumnEta",      widthVar: "--w-eta",      sortKey: "eta" },
  { id: "actions",  labelKey: null,                 widthVar: "--w-actions" },
];

// 窄屏自动收起的列。跟 style.css 的 @container 阈值一致。
function responsiveHiddenColumns(width) {
  const hidden = new Set();
  if (width <= 1179) hidden.add("eta");
  if (width <= 1039) hidden.add("size");
  if (width <= 879) { hidden.add("speed"); hidden.add("size"); hidden.add("eta"); }
  if (width <= 639) {
    hidden.add("progress"); hidden.add("size");
    hidden.add("speed"); hidden.add("eta"); hidden.add("category");
  }
  return hidden;
}

// 当前应该渲染哪些列。
function visibleColumns() {
  const responsive = responsiveHiddenColumns(State.listWidth);
  const userHidden = State.hiddenColumns || [];
  return COLUMN_DEFS.filter((c) => !responsive.has(c.id) && !userHidden.includes(c.id));
}

// 列模板（grid-template-columns）。宽度用 CSS 变量表达，
// 窄屏时容器查询改那些变量，表头和行一起变，不会错位。
function columnTemplate(columns) {
  return columns.map((c) => `var(${c.widthVar})`).join(" ");
}

// 列组合变了没。变了就得重建所有行——列的数量/顺序变了，
// 光更新文本不够。
function columnSignature(columns) {
  return columns.map((c) => c.id).join("|");
}

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

  // 分类。
  //
  // **计数是 0 的分类也要显示**（规格 §2.1 把分类列成固定条目）。
  // 原来「0 个就藏起来」看着清爽，但用户想找「我的视频在哪」时，
  // 列表里空着一条分类不见了，反而得猜是不是分类功能坏了。
  nav.appendChild(el("div", { class: "nav-group-label", text: t("sidebarCategories") }));
  Object.keys(CATEGORY_META).forEach((cat) => {
    const n = State.tasks.filter((x) => x.category === cat).length;
    const meta = CATEGORY_META[cat];
    const btn = el("button", {
      class: "nav-item" + (State.categoryFilter === cat ? " active" : ""),
      title: categoryLabel(cat),
      onclick: () => { setView("downloads"); State.filter = "all"; State.categoryFilter = cat; render(); },
    }, [
      icon(meta.icon),
      el("span", { class: "nav-text", text: categoryLabel(cat) }),
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

  // 设置和主题是全局的——哪个视图都放得下，也省得用户为了改个
  // 设置先翻到侧栏底部找按钮（那个按钮原来藏在侧栏脚注里）。
  const settingsBtn = el("button", {
    class: "btn", title: t("settingsWindowTitle"),
    onclick: () => setView("settings"),
  }, [icon("settings"), el("span", { class: "btn-label", text: t("settingsWindowTitle") })]);

  // 主题：system → light → dark → system。
  const themeBtn = el("button", {
    class: "btn", title: t("themeToggle"),
    onclick: async () => {
      const next = nextTheme(State.theme);
      applyTheme(next);
      await saveSettings({ theme: next });
      render();
    },
  }, [icon("theme"), el("span", { class: "btn-label", text: themeLabel(State.theme) })]);

  if (State.view === "downloads") {
    title.appendChild(el("span", { class: "toolbar-title", text: t("mainDownloads") }));

    actions.appendChild(el("button", {
      class: "btn btn-primary", title: t("toolbarNewTask"),
      onclick: openAddDialog,
    }, [icon("plus"), el("span", { class: "btn-label", text: t("toolbarNewTask") })]));

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
    }, [icon("check"), el("span", { class: "btn-label", text: t("toolbarClearCompleted") })]));

    // 删除：删掉当前选中的任务。没选中就禁用——
    // 让用户点一个不知道会删什么的按钮更糟。
    const selected = State.tasks.find((x) => x.id === State.selectedTaskId);
    actions.appendChild(el("button", {
      class: "btn", title: t("toolbarDeleteSelected"),
      disabled: !selected,
      onclick: async () => {
        if (!selected) return;
        await api()?.remove_task(selected.id, false);
        State.selectedTaskId = null;
        toast(t("toolbarDeleteSelected"));
        await refreshTasks();
      },
    }, [icon("trash"), el("span", { class: "btn-label", text: t("toolbarDeleteSelected") })]));

    // 剪贴板监控开关。开着的时候按钮高亮，用户一眼能看出
    // 「链接还是会被自动接管的」。
    const capture = State.settings.capture_clipboard !== false;
    actions.appendChild(el("button", {
      class: "btn" + (capture ? " btn-active" : ""),
      title: t("toolbarClipboardMonitor"),
      onclick: async () => {
        const next = !capture;
        await saveSettings({ capture_clipboard: next });
        render();
      },
    }, [icon("clipboard"), el("span", { class: "btn-label", text: t("toolbarClipboardMonitor") })]));

    actions.appendChild(settingsBtn);
    actions.appendChild(themeBtn);
  } else if (State.view === "history") {
    title.appendChild(el("span", { class: "toolbar-title", text: t("sidebarHistory") }));
    actions.appendChild(el("button", {
      class: "btn btn-danger", title: t("historyClearAll"),
      onclick: async () => { await api()?.clear_history(); toast(t("historyClearAll")); },
    }, [icon("trash"), el("span", { class: "btn-label", text: t("historyClearAll") })]));
    actions.appendChild(settingsBtn);
    actions.appendChild(themeBtn);
  } else if (State.view === "schedule") {
    title.appendChild(el("span", { class: "toolbar-title", text: t("scheduleTitle") }));
    actions.appendChild(settingsBtn);
    actions.appendChild(themeBtn);
  } else if (State.view === "settings") {
    title.appendChild(el("span", { class: "toolbar-title", text: t("settingsWindowTitle") }));
    actions.appendChild(themeBtn);
  }
}

// ============ 状态栏 ============

// 全局速度 / 活跃任务数 / 限速状态（规格 §2.1）。
//
// **推送没来的时候用任务列表自己算**——不然推送通道不可用时
// 状态栏永远显示 0，看着像坏了。
function globalSpeed() {
  const pushed = State.globalSpeed;
  if (pushed && pushed.received) return pushed;
  let speed = 0;
  let active = 0;
  for (const task of State.tasks) {
    if (task.state === "downloading") {
      speed += Number(task.speed_bytes_per_sec) || 0;
      active += 1;
    } else if (task.state === "queued") {
      active += 1;
    }
  }
  return {
    bytes_per_sec: speed,
    limit_bytes_per_sec: Number(State.settings.bandwidth_bytes_per_sec) || 0,
    active_count: active,
  };
}

function renderStatusBar() {
  const speedEl = document.getElementById("statusSpeed");
  const activeEl = document.getElementById("statusActive");
  const limitEl = document.getElementById("statusLimit");
  if (!speedEl || !activeEl || !limitEl) return;

  const g = globalSpeed();
  clear(speedEl);
  speedEl.appendChild(el("span", { text: t("statusBarSpeed") + " " }));
  speedEl.appendChild(el("b", { text: fmtRate(g.bytes_per_sec) }));
  clear(activeEl);
  activeEl.appendChild(el("span", { text: t("statusBarActive") + " " }));
  activeEl.appendChild(el("b", { text: String(g.active_count || 0) }));

  const limit = Number(g.limit_bytes_per_sec || 0);
  limitEl.textContent = limit > 0
    ? `${t("statusBarLimit")} ${fmtRate(limit)}`
    : t("statusBarNoLimit");
  limitEl.className = "status-item" + (limit > 0 ? " status-limit-on" : "");
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
  if (State.sortKey) {
    return sortTasks(list, State.sortKey, State.sortAsc);
  }
  // 默认排序：下载中的排前面，其余按加入时间倒序。
  //
  // **跟 Swift 不同，保留现有的「下载中优先」。** Swift 版默认按
  // state 升序（App/MainWindow/DownloadListView.swift:78-80），
  // 但下载中优先对下载器更实用——用户关心的是「现在在跑哪些」，
  // 不是「按状态字母排」。用户点表头就能切到 state 排序。
  const order = { downloading: 0, queued: 1, paused: 2, awaiting_new_link: 3, failed: 4, completed: 5, cancelled: 6 };
  return list.sort((a, b) => (order[a.state] ?? 9) - (order[b.state] ?? 9));
}

// 按指定键排序。
//
// Swift 对照：App/MainWindow/DownloadListView.swift:254-256 的
// KeyPathComparator。Python 版多排几列（规格 §2.1）：名称 / 大小 /
// 进度 / 速度 / 剩余时间 / 状态 / 分类。
//
// **未知大小永远排最后**（不管升序还是降序）。Swift 的
// `totalBytesOrZero` 用 0 代表「不知道」，但 0 在升序时排最前，
// 跟注释写的 "so columns sort unknown to the end" 不符——这里按
// 注释的意图实现：未知大小的沉到底。
//
// 这个函数被 Node 侧的测试单独抠出来跑（tests/test_sort_js.mjs），
// 所以**不能引用函数外面的东西**——表都写在函数里。
function sortTasks(list, key, asc) {
  const dir = asc ? 1 : -1;
  const stateOrder = { downloading: 0, queued: 1, paused: 2, awaiting_new_link: 3, failed: 4, completed: 5, cancelled: 6 };
  const categoryOrder = { documents: 0, music: 1, video: 2, archives: 3, software: 4, other: 5, pictures: 6 };
  const nameOf = (t) => (t.filename || t.url || "");
  // 剩余时间。算不出来（不知道大小 / 没速度 / 已完成）的沉底——
  // 跟「未知大小沉底」一个道理，不然一列 "—" 混在中间没法看。
  const etaOf = (t) => (t.total_bytes && t.speed_bytes_per_sec > 0 && t.state !== "completed")
    ? (t.total_bytes - t.bytes_done) / t.speed_bytes_per_sec : null;
  const progressOf = (t) => (t.total_bytes > 0 ? (t.bytes_done || 0) / t.total_bytes : 0);
  return list.slice().sort((a, b) => {
    if (key === "size") {
      const av = a.total_bytes;
      const bv = b.total_bytes;
      const aUnknown = av === null || av === undefined;
      const bUnknown = bv === null || bv === undefined;
      // 未知大小沉底——两个都未知时保持原顺序
      if (aUnknown && bUnknown) return 0;
      if (aUnknown) return 1;
      if (bUnknown) return -1;
      return (Number(av) - Number(bv)) * dir;
    }
    if (key === "speed") {
      return ((a.speed_bytes_per_sec || 0) - (b.speed_bytes_per_sec || 0)) * dir;
    }
    if (key === "state") {
      const av = stateOrder[a.state] ?? 9;
      const bv = stateOrder[b.state] ?? 9;
      return (av - bv) * dir;
    }
    if (key === "category") {
      const av = categoryOrder[a.category] ?? 9;
      const bv = categoryOrder[b.category] ?? 9;
      // 同一分类里按名字排——不然同一类的任务顺序会乱跳。
      if (av === bv) return nameOf(a).localeCompare(nameOf(b));
      return (av - bv) * dir;
    }
    if (key === "progress") {
      return (progressOf(a) - progressOf(b)) * dir;
    }
    if (key === "eta") {
      const av = etaOf(a);
      const bv = etaOf(b);
      if (av === null && bv === null) return 0;
      if (av === null) return 1;
      if (bv === null) return -1;
      return (av - bv) * dir;
    }
    // filename
    const av = nameOf(a);
    const bv = nameOf(b);
    return av.localeCompare(bv) * dir;
  });
}

// 点表头切排序。同一个键再点一次换升降序。
function toggleSort(key) {
  if (State.sortKey === key) {
    State.sortAsc = !State.sortAsc;
  } else {
    State.sortKey = key;
    State.sortAsc = true;
  }
  renderDownloads();
}

// 重绘排序表头。
//
// 表头不是一次性的——排序状态会变（用户点了别的列、或者又点了
// 同一个列换升降序），列的显隐也会变，所以每次渲染列表都重画一遍。
function updateSortHeader(container) {
  if (!container) return;
  clear(container);
  visibleColumns().forEach((col) => {
    if (col.id === "actions") { container.appendChild(el("div")); return; }
    // 大小 / 速度 / 剩余时间右对齐，跟行里的数字列对齐。
    const right = col.id === "size" || col.id === "speed" || col.id === "eta";
    container.appendChild(sortHeaderCell(col.sortKey, t(col.labelKey), right));
  });
}

// 列选择器：勾掉的列不渲染。
//
// 选择存进 settings.hidden_columns——用户下次打开还想这么看。
function renderColumnPicker(host) {
  let panel = document.getElementById("colPicker");
  if (!panel) {
    panel = el("div", { class: "col-picker", id: "colPicker" });
    host.appendChild(panel);
  }
  // 只在勾选状态变了才重画内容——推送每 0.7 秒来一次，
  // 每次都重建复选框会打断用户正在点的那一个。
  const signature = (State.hiddenColumns || []).slice().sort().join("|");
  if (panel.dataset.signature === signature) return panel;
  panel.dataset.signature = signature;
  clear(panel);
  COLUMN_DEFS.filter((c) => c.labelKey).forEach((col) => {
    const checked = !(State.hiddenColumns || []).includes(col.id);
    const box = el("input", { type: "checkbox", checked });
    box.addEventListener("change", () => {
      const hidden = new Set(State.hiddenColumns || []);
      if (box.checked) hidden.delete(col.id);
      else hidden.add(col.id);
      State.hiddenColumns = Array.from(hidden);
      saveSettings({ hidden_columns: State.hiddenColumns });
      render();
    });
    panel.appendChild(el("label", { class: "col-picker-item" }, [
      box, el("span", { text: t(col.labelKey) }),
    ]));
  });
  return panel;
}

// 切主题：system → light → dark → system。
//
// 主题存进 settings.theme。**"system" 时把 data-theme 属性删掉**——
// 让 prefers-color-scheme 说话，比写死一个值更对：系统半夜切成
// 深色，应用跟着变。
const THEME_CYCLE = ["system", "light", "dark"];

function applyTheme(theme) {
  const value = theme || "system";
  State.theme = value;
  if (value === "system") {
    delete document.documentElement.dataset.theme;
  } else {
    document.documentElement.dataset.theme = value;
  }
}

function nextTheme(theme) {
  const idx = THEME_CYCLE.indexOf(theme || "system");
  return THEME_CYCLE[(idx + 1) % THEME_CYCLE.length];
}

function themeLabel(theme) {
  const value = theme || "system";
  if (value === "light") return t("themeLight");
  if (value === "dark") return t("themeDark");
  return t("themeSystem");
}

// 一个可点击的表头单元格。
function sortHeaderCell(key, label, rightAlign) {
  const active = State.sortKey === key;
  const arrow = active ? (State.sortAsc ? "↑" : "↓") : "";
  const cell = el("div", {
    class: "sh" + (active ? " active" : "") + (rightAlign ? " right" : ""),
    title: label,
  }, [
    el("span", { text: label }),
    el("span", { class: "arrow", text: arrow }),
  ]);
  cell.addEventListener("click", () => toggleSort(key));
  return cell;
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

  // 分类列。分类名走 i18n（categoryLabel），不显示英文枚举值。
  const catLabel = el("span", { text: categoryLabel(task.category) });
  const categoryCell = el("div", { class: "cell-category" }, [
    el("span", { style: `color:${catMeta.color};display:flex;flex:none` }, [icon(catMeta.icon)]),
    catLabel,
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

  // 列的显隐是用户定的——按当前可见的列拼格子。
  // 列集合变了整行都要重建（列的顺序/数量变了），所以这里每次
  // 都按当前可见列表重新拼。
  const cells = {
    name: nameBox,
    category: categoryCell,
    state: badge,
    progress: progress,
    size: sizeCell,
    speed: speedCell,
    eta: etaCell,
    actions: actions,
  };
  const row = el("div", { class: "row" }, visibleColumns().map((c) => cells[c.id]));

  const refs = {
    node: row, title, sub, chip, badge, badgeIcon, badgeText,
    fill, pctText, sizeCell, speedCell, etaCell,
    categoryCell, catLabel,
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
  // 分类列。分类名是 i18n 的——换语言后这一格也得跟着变。
  const label = categoryLabel(task.category);
  if (refs.catLabel.textContent !== label) {
    refs.catLabel.textContent = label;
    refs.categoryCell.firstChild.firstChild.replaceWith(icon(catMeta.icon));
    refs.categoryCell.firstChild.style.color = catMeta.color;
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
  const columns = visibleColumns();

  // 列组合变了（用户改了列的显隐，或者窗口窄了）——所有行都得
  // 重建：格子的数量和顺序都变了，光改文本对不上。
  const signature = columnSignature(columns);
  if (signature !== State.columnSignature) {
    State.columnSignature = signature;
    for (const entry of _rowCache.values()) entry.node.remove();
    _rowCache.clear();
  }

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
    const head = el("div", { class: "list-head" }, [el("div", { class: "sort-header" })]);
    const pickerBtn = el("button", {
      class: "col-picker-btn", title: t("columnPickerTitle"),
      onclick: (e) => {
        e.stopPropagation();
        const panel = document.getElementById("colPicker");
        if (panel) panel.hidden = !panel.hidden;
      },
    }, [icon("columns", "icon-sm")]);
    head.appendChild(pickerBtn);
    wrap.appendChild(head);
    wrap.appendChild(el("div", { class: "list" }));
    view.appendChild(wrap);
    // 容器宽度变了要重算「窄屏收哪些列」。
    observeListWidth(wrap);
  }

  renderColumnPicker(wrap);

  // 列模板：表头和行必须用同一个，否则对不齐。
  wrap.style.setProperty("--row-cols", columnTemplate(columns));

  // 表头每次重绘——排序状态（哪一列、升降序）会变。
  updateSortHeader(wrap.querySelector(".sort-header"));
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

// 盯着列表容器的宽度。窄了要收列，宽了要把列放回来。
//
// 用 ResizeObserver 而不是 window.resize——详情面板打开时主区会
// 变窄，但窗口尺寸没变，只有容器尺寸变了。
let _listObserver = null;

function observeListWidth(wrap) {
  if (typeof ResizeObserver === "undefined") return;
  if (!_listObserver) {
    _listObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const width = entry.contentRect.width;
        if (Math.abs(width - State.listWidth) < 1) continue;
        State.listWidth = width;
        if (State.view === "downloads") renderDownloads();
      }
    });
  }
  // 列表被清空再填上时会换一个 wrap 节点——要重新盯着新的那个，
  // 不然窄屏收列的逻辑就失联了。
  _listObserver.disconnect();
  _listObserver.observe(wrap);
  // 初始宽度取**内容盒**的宽度——容器查询（@container listarea）
  // 量的是内容盒，两边得用同一个口径，不然临界点会差一截。
  const pad = getComputedStyle(wrap);
  State.listWidth = wrap.clientWidth
    - (parseFloat(pad.paddingLeft) || 0)
    - (parseFloat(pad.paddingRight) || 0);
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
      el("span", { class: "banner-text", text: tf("expiredMoreWaiting", waiting.length - 1) }),
    ]));
  }
}

// ============ 详情抽屉 ============

// 分块进度可视化：一条横杠，一段一个分片，按完成度上色。
function renderChunkSegments(chunks) {
  const wrap = el("div", { class: "chunk-segments" });
  chunks.forEach((c) => {
    const size = Number(c.size || 0);
    const done = Number(c.bytes_done || 0);
    let cls = "chunk-seg";
    if (size > 0 && done >= size) cls += " done";
    else if (done > 0) cls += " partial";
    wrap.appendChild(el("span", { class: cls }));
  });
  return wrap;
}

// 速度曲线：最近 60 秒。
//
// **优先用后端给的 get_speed_history**；后端没实现时用前端自己
// 攒的采样（推送/轮询每来一次记一个点）。两条路的数据形状一样：
// [{t: 时间戳, v: 字节/秒}]。
function speedSeries(task) {
  const history = State.speedHistory[task.id];
  if (history && history.length) {
    return history.map((p) => ({ t: Number(p.t) || 0, v: Number(p.v ?? p.bytes_per_sec ?? p.speed_bytes_per_sec) || 0 }));
  }
  const samples = State.speedSamples[task.id] || [];
  // 任务停了之后采样点不会再被清理——读的时候顺手砍掉 60 秒前的。
  const cutoff = Date.now() - 60000;
  return samples.filter((p) => p.t >= cutoff);
}

// 记一个速度采样点。环形缓冲——只留最近 60 秒。
function recordSpeedSample(task) {
  if (!task || !task.id) return;
  const now = Date.now();
  let arr = State.speedSamples[task.id];
  if (!arr) { arr = []; State.speedSamples[task.id] = arr; }
  arr.push({ t: now, v: Number(task.speed_bytes_per_sec) || 0 });
  const cutoff = now - 60000;
  while (arr.length && arr[0].t < cutoff) arr.shift();
}

// 画速度曲线。内联 SVG，不用 canvas——数据点就几十个。
function renderSpeedCurve(task) {
  const points = speedSeries(task);
  const W = 300;
  const H = 72;
  if (points.length < 2) {
    return el("div", { class: "speed-chart-empty", text: t("detailSpeedCurveEmpty") });
  }

  const max = Math.max(1, ...points.map((p) => p.v));
  const t0 = points[0].t;
  const t1 = points[points.length - 1].t;
  const span = Math.max(1, t1 - t0);
  const xs = points.map((p) => ((p.t - t0) / span) * (W - 2) + 1);
  const ys = points.map((p) => H - 3 - (p.v / max) * (H - 8));

  const poly = points.map((_, i) => `${xs[i].toFixed(1)},${ys[i].toFixed(1)}`).join(" ");
  const area = `1,${H - 1} ${poly} ${xs[xs.length - 1].toFixed(1)},${H - 1}`;

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "speed-chart");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");

  const add = (tag, attrs) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    svg.appendChild(node);
    return node;
  };

  add("line", { class: "grid-line", x1: 0, y1: H - 1, x2: W, y2: H - 1 });
  add("polygon", { class: "area", points: area });
  add("polyline", { class: "curve", points: poly });

  const peak = document.createElementNS("http://www.w3.org/2000/svg", "text");
  peak.setAttribute("class", "axis-text");
  peak.setAttribute("x", "3");
  peak.setAttribute("y", "10");
  peak.textContent = fmtRate(max);
  svg.appendChild(peak);

  return svg;
}

// 日志摘要。
//
// **引擎还没提供按任务的日志接口**——契约里也没有那个方法。
// 这里用 get_task_error 的结果拼一行：有错显示错误，没有就显示
// 「暂无错误」。别去调一个不存在的后端接口。
function taskErrorSummary(task) {
  const err = State.taskErrors[task.id];
  if (err && err.message) {
    const category = err.category ? `[${err.category}] ` : "";
    return category + err.message;
  }
  if (task.error_message) return task.error_message;
  return t("detailNoError");
}

function selectTask(taskId) {
  State.selectedTaskId = State.selectedTaskId === taskId ? null : taskId;
  // 换任务时收起分片表——展开状态是按任务记的，别带过去
  State.expandedChunks = null;
  render();
  // 分片明细单独取——列表接口不带它（省内存 + 省序列化）
  if (State.selectedTaskId) loadTaskDetail(State.selectedTaskId);
}

// 取单个任务的详情：分片、错误、速度历史。
//
// 用契约里的 get_task_detail 一次拿全——分三次往返的话，详情面板
// 会一块一块地填出来，看着像卡了。后端还没实现这个接口时退回
// 旧的 get_task_chunks，至少分片表还在。
async function loadTaskDetail(taskId) {
  const backend = api();
  let detail = null;
  if (backend && typeof backend.get_task_detail === "function") {
    try {
      detail = await backend.get_task_detail(taskId);
    } catch (err) {
      detail = null;
    }
  }
  const task = State.tasks.find((x) => x.id === taskId);
  if (!task) return;
  if (detail) {
    if (detail.chunks && detail.chunks.length) task.chunks = detail.chunks;
    if (detail.error !== undefined) State.taskErrors[taskId] = detail.error || null;
    if (detail.speed_history && detail.speed_history.length) {
      State.speedHistory[taskId] = detail.speed_history;
    }
  } else if (backend && typeof backend.get_task_chunks === "function") {
    const chunks = await backend.get_task_chunks(taskId);
    if (chunks && chunks.length) task.chunks = chunks;
  }
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
  // 已下载 / 已上传。引擎给 bytes_done / uploaded_bytes。
  addMeta(t("detailDownloaded"), fmtSize(task.bytes_done));
  addMeta(t("detailUploaded"), fmtSize(task.uploaded_bytes));
  addMeta(t("detailSpeed"), fmtRate(task.speed_bytes_per_sec));
  // 连接数：当前活跃的连接数（引擎的 active_connections）。
  addMeta(t("detailConnections"), String(task.active_connections || 0));
  addMeta(t("detailETA"), task.total_bytes && task.speed_bytes_per_sec > 0
    ? fmtDuration((task.total_bytes - task.bytes_done) / task.speed_bytes_per_sec) : "—");
  if (task.speed_limit_bytes_per_sec > 0) {
    addMeta(t("detailPerTaskLimit"), fmtRate(task.speed_limit_bytes_per_sec));
  }
  if (task.error_message) {
    addMeta(t("detailError"), task.error_message);
  }
  // 日志摘要。引擎还没提供按任务的日志接口，这里用
  // get_task_error 的结果拼一行——有错就显示错误，没有就显示
  // 「暂无错误」。
  const errSummary = taskErrorSummary(task);
  addMeta(t("detailLogSummary"), errSummary);
  body.appendChild(metaList);

  // 速度曲线（最近 60 秒）
  body.appendChild(el("div", { class: "section-title" }, [
    icon("gauge", "icon-sm"),
    el("span", { text: t("detailSpeedCurve") }),
  ]));
  body.appendChild(renderSpeedCurve(task));

  // 分片
  if (task.chunks && task.chunks.length) {
    // 分片最多 128 个，全渲染 DOM 会偏大。默认显示前 64 个，
    // 底下给一个「展开全部分片」的按钮——想看全的时候能看全，
    // 不想看的时候不占地方。
    const CHUNK_VIEW_LIMIT = 64;
    const expanded = State.expandedChunks === task.id;
    const visible = expanded ? task.chunks : task.chunks.slice(0, CHUNK_VIEW_LIMIT);
    body.appendChild(el("div", { class: "section-title" }, [
      icon("layers", "icon-sm"),
      el("span", { text: `${t("detailShowChunks")} (${task.completed_chunks}/${task.chunk_count})` }),
    ]));
    // 分块进度可视化：一段一个分片。
    //
    // **超过 256 段就不画了**——几百个 1px 的小段既看不清，
    // 每次重绘还要重建几百个节点，纯浪费。给一行汇总就够了。
    const CHUNK_SEGMENT_LIMIT = 256;
    if (task.chunks.length <= CHUNK_SEGMENT_LIMIT) {
      body.appendChild(renderChunkSegments(task.chunks));
    } else {
      body.appendChild(el("div", { class: "chunk-summary", text: tf("detailChunkSummary", task.chunks.length) }));
    }
    const table = el("table", { class: "chunk-table" }, [
      el("thead", {}, [el("tr", {}, [
        el("th", { text: t("taskDetailChunkHeader") }),
        el("th", { text: t("taskDetailChunkBytes") }),
        el("th", { text: t("taskDetailChunkStatus") }),
        el("th", { text: t("taskDetailChunkElapsed") }),
      ])]),
    ]);
    const tbody = el("tbody");
    visible.forEach((c) => {
      const st = c.bytes_done >= c.size ? "done" : (c.bytes_done > 0 ? "downloading" : "pending");
      tbody.appendChild(el("tr", {}, [
        el("td", { class: "mono", text: `#${c.id}` }),
        el("td", { class: "mono", text: `${fmtSize(c.bytes_done)} / ${fmtSize(c.size)}` }),
        el("td", {}, [
          el("span", { class: "chunk-dot chunk-" + st }),
          el("span", { text: t("taskDetailChunk" + st.charAt(0).toUpperCase() + st.slice(1)) }),
        ]),
        el("td", { class: "mono cell-muted" + (c.idle_seconds > 15 ? " cell-stale" : ""),
                   text: c.idle_seconds != null ? fmtChunkElapsed(c.idle_seconds) : "—" }),
      ]));
    });
    table.appendChild(tbody);
    body.appendChild(table);

    // 展开 / 收起
    if (task.chunks.length > CHUNK_VIEW_LIMIT) {
      body.appendChild(el("button", {
        class: "btn btn-sm", style: "margin-top:10px",
        onclick: () => {
          State.expandedChunks = expanded ? null : task.id;
          renderDetail();
        },
      }, [
        icon("chevron", "icon-sm " + (expanded ? "icon-chevron-up" : "icon-chevron-down")),
        el("span", { text: expanded
          ? t("taskDetailChunksCollapse")
          : `${t("taskDetailChunksExpandMore")} (${task.chunks.length})` }),
      ]));
    }

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
    if (!res?.ok) { err.textContent = res?.error || t("commonFailed"); return; }
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

// 记下滚动位置，重画之后放回去。
//
// **为什么需要它**：改设置会重画整个设置页（后端推 settings_updated
// → render() → renderSettings()）。重画等于把滚动容器 `.list-wrap`
// 整个换掉，**新容器的 scrollTop 是 0**——用户改个设置就被弹回
// 页面顶部。任务被删掉（task_removed 推送）也会走同一条重画路径，
// 症状一样。
//
// 拆成两个独立函数是为了能测：它们只碰 `.list-wrap` 的 scrollTop，
// 不依赖整个设置页的 DOM。
function captureScrollTop(container) {
  return container?.querySelector?.(".list-wrap")?.scrollTop || 0;
}

function restoreScrollTop(container, top) {
  if (!container || !(top > 0)) return;
  container.scrollTop = top;
  // **下一帧再设一次。** 设置区里有站点规则表这种会自己撑高的块，
  // 首帧算出来的可滚动高度可能比最终值小，同步设的位置会被截短。
  requestAnimationFrame(() => { container.scrollTop = top; });
}

function renderSettings() {
  const view = document.getElementById("viewSettings");
  const prevScrollTop = captureScrollTop(view);
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
      // 主题。工具栏上也有一个快捷按钮，两边改的是同一个设置。
      toggleRow(t("settingsTheme"), t("settingsThemeHint"), "theme",
        [["system", t("themeSystem")], ["light", t("themeLight")], ["dark", t("themeDark")]],
        s.theme || "system"),
    ]),
  ]);
  wrap.appendChild(general);

  // 默认
  const threadsInput = el("input", { class: "input", type: "number", min: "1", max: "128", value: String(s.threads_default) });
  threadsInput.addEventListener("change", () => {
    saveSettings({ threads_default: parseInt(threadsInput.value, 10) || 32 });
  });
  // 带宽限制：输入框按 KB/s 显示，写回时换算成 bytes/s。
  //
  // 存储层（settings.bandwidth_bytes_per_sec）保持 bytes/s 语义——
  // 改存储单位会破坏已有用户配置，后端 set_speed_limit 的单位含义
  // 也会跟着变。单位转换只发生在这个边界上。
  const bandwidthKB = Math.round((s.bandwidth_bytes_per_sec || 0) / 1024);
  const bandwidthInput = el("input", { class: "input", type: "number", min: "0", value: String(bandwidthKB) });
  bandwidthInput.addEventListener("change", () => {
    const kb = parseInt(bandwidthInput.value, 10) || 0;
    saveSettings({ bandwidth_bytes_per_sec: kb * 1024 });
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
            onchange: (e) => saveSettings({ threads_default: parseInt(e.target.value, 10) }),
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
      // 同时活跃的任务数（契约 §2.4）。超出的任务排在队列里。
      el("div", { class: "field", style: "margin-top:14px" }, [
        el("label", { class: "field-label", text: t("settingsMaxConcurrent") }),
        el("div", { class: "field-row" }, [
          el("input", {
            class: "input", type: "number", min: "1", max: "20",
            value: String(s.max_concurrent_tasks || 5),
            onchange: (e) => saveSettings({ max_concurrent_tasks: parseInt(e.target.value, 10) || 5 }),
          }),
        ]),
        el("div", { class: "field-hint", text: t("settingsMaxConcurrentHint") }),
      ]),
      // 初始连接数 / 分片上限（契约 §2.5 的动态分段）
      el("div", { class: "field", style: "margin-top:14px" }, [
        el("label", { class: "field-label", text: t("settingsInitialConnections") }),
        el("div", { class: "field-row" }, [
          el("input", {
            class: "input", type: "number", min: "1", max: "32",
            value: String(s.initial_connections || 2),
            onchange: (e) => saveSettings({ initial_connections: parseInt(e.target.value, 10) || 2 }),
          }),
          el("input", {
            class: "input", type: "number", min: "1", max: "128",
            value: String(s.max_chunk_count || 32),
            onchange: (e) => saveSettings({ max_chunk_count: parseInt(e.target.value, 10) || 32 }),
          }),
        ]),
        el("div", { class: "field-hint", text: t("settingsChunkLimitsHint") }),
      ]),
    ]),
  ]));

  // 网络：全局代理（契约 §6 的 proxy_url / proxy_scheme）
  const proxyInput = el("input", {
    class: "input", type: "text", value: s.proxy_url || "",
    placeholder: "http://127.0.0.1:7890",
  });
  proxyInput.addEventListener("change", () => saveSettings({ proxy_url: proxyInput.value.trim() }));
  const proxyScheme = el("select", {
    class: "select",
    onchange: (e) => saveSettings({ proxy_scheme: e.target.value }),
  }, ["http", "https", "socks4", "socks5"].map((v) =>
    el("option", { value: v, text: v, selected: (s.proxy_scheme || "http") === v })));

  wrap.appendChild(el("div", { class: "setting-section" }, [
    el("div", { class: "setting-section-title", text: t("settingsSectionNetwork") }),
    el("div", { class: "card" }, [
      el("div", { class: "field" }, [
        el("label", { class: "field-label", text: t("settingsGlobalProxy") }),
        el("div", { class: "field-row" }, [proxyInput, proxyScheme]),
        el("div", { class: "field-hint", text: t("settingsGlobalProxyHint") }),
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
          el("span", { text: categoryLabel(key) }),
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
          onchange: (e) => saveSettings({ update_channel: e.target.value }),
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
  // 把滚动位置放回去（见函数开头）。
  restoreScrollTop(wrap, prevScrollTop);
}

function toggleRow(label, hint, key, options, value) {
  const row = el("div", { class: "setting-toggle" }, [
    el("div", { class: "setting-toggle-text" }, [
      el("div", { class: "setting-toggle-label", text: label }),
      hint ? el("div", { class: "setting-toggle-hint", text: hint }) : null,
    ]),
  ]);

  if (options) {
    // 选项可以是字符串，也可以是 [值, 显示文字] 的二元组。
    // 二元组是给「值和显示文字对不上」的场合用的——比如主题的值
    // 是 system / light / dark，显示的却是「跟随系统 / 浅色 / 深色」。
    const sel = el("select", {
      class: "select", style: "width:auto;min-width:120px",
      onchange: async (e) => {
        if (key === "locale") {
          const res = await api()?.set_language(e.target.value);
          if (res?.translations) { State.strings = res.translations; State.locale = e.target.value; applyStaticI18n(); }
        } else {
          // 主题要立刻生效——等一次往返用户会以为没点上。
          if (key === "theme") applyTheme(e.target.value);
          await saveSettings({ [key]: e.target.value });
        }
        render();
      },
    }, options.map((o) => {
      const val = Array.isArray(o) ? o[0] : o;
      let text;
      if (Array.isArray(o)) text = o[1];
      else if (o === "system") text = t("settingsLanguageSystem");
      else if (o === "zh-Hans") text = t("settingsLanguageZh");
      else if (o === "en") text = "English";
      else text = o;
      return el("option", { value: val, text, selected: val === value });
    }));
    row.appendChild(sel);
  } else {
    const input = el("input", { type: "checkbox", checked: !!value });
    input.addEventListener("change", async () => {
      if (key === "__openAtLogin") {
        await api()?.set_open_at_login(input.checked);
      } else {
        await saveSettings({ [key]: input.checked });
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
    if (!res?.ok) { err.textContent = res?.error || t("commonFailed"); return; }
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
    onchange: (e) => saveSettings({ logs_level: parseInt(e.target.value, 10) }),
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
    input.addEventListener("change", () => saveSettings({ [key]: parseInt(input.value, 10) || min }));
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
      onclick: async () => { const r = await api()?.open_log_in_console(); if (!r?.ok) toast(t("settingsLogsOpenConsoleFailed")); },
    }, [icon("external"), el("span", { text: t("settingsLogsOpenConsoleButton") })]),
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

// ============ 新建任务对话框（规格 §2.2） ============
//
// 结构在 index.html 里（静态），这里只管行为。
//
// **解析失败不是终态**——URL 探测失败时对话框不关，错误显示在
// URL 字段下面，用户改完 URL 可以点「重新解析」再来一次。
// 原来那种「填错就关窗」的做法，用户得重新填一遍所有字段。

const AddDialog = {
  probing: false,
  // 每次探测发一个号。旧探测的结果晚回来时号对不上，直接丢掉——
  // 否则用户改了 URL 之后会被上一个 URL 的结果覆盖。
  token: 0,
  // 用户手动改过保存目录就别再自动覆盖了。
  useCustomDir: false,
};

function openAddDialog() {
  const root = document.getElementById("addTaskRoot");
  if (!root) return;
  resetAddDialog();
  root.hidden = false;
  const url = document.getElementById("addUrl");
  if (url) setTimeout(() => url.focus(), 30);
}

function closeAddDialog() {
  const root = document.getElementById("addTaskRoot");
  if (root) root.hidden = true;
  AddDialog.token++;
}

function resetAddDialog() {
  AddDialog.probing = false;
  AddDialog.token++;
  AddDialog.useCustomDir = false;

  const url = document.getElementById("addUrl");
  if (url) url.value = "";
  const name = document.getElementById("addFilename");
  if (name) name.value = "";
  const proxy = document.getElementById("addProxy");
  if (proxy) proxy.value = "";
  const dir = document.getElementById("addSaveDir");
  if (dir) dir.value = "";
  const chunkMode = document.getElementById("addChunkMode");
  if (chunkMode) chunkMode.value = "auto";
  const chunkCount = document.getElementById("addChunkCount");
  if (chunkCount) { chunkCount.value = "8"; chunkCount.disabled = true; }
  const startWhen = document.getElementById("addStartWhen");
  if (startWhen) startWhen.value = "now";
  const at = document.getElementById("addScheduleAt");
  if (at) { at.value = ""; at.disabled = true; }

  const err = document.getElementById("addError");
  if (err) { err.textContent = ""; err.hidden = true; }
  const probeErr = document.getElementById("addProbeError");
  if (probeErr) { probeErr.textContent = ""; probeErr.hidden = true; }

  fillCategorySelect();
  syncChunkMode();
  setProbeState(null, "");
  syncDefaultDir();
}

// 分类下拉。分类名走 i18n。
function fillCategorySelect() {
  const sel = document.getElementById("addCategory");
  if (!sel) return;
  clear(sel);
  Object.keys(CATEGORY_META).forEach((cat) => {
    sel.appendChild(el("option", { value: cat, text: categoryLabel(cat) }));
  });
}

// 保存目录跟着分类走——除非用户自己改过。
async function syncDefaultDir() {
  if (AddDialog.useCustomDir) return;
  const sel = document.getElementById("addCategory");
  const dir = document.getElementById("addSaveDir");
  if (!sel || !dir) return;
  const res = await api()?.get_default_dir(sel.value);
  dir.value = res?.path || "";
}

// 探测状态：parsing / ok / error。
function setProbeState(kind, message) {
  const state = document.getElementById("addProbeState");
  if (!state) return;
  state.className = "probe-state" + (kind ? " " + kind : "");
  clear(state);
  if (kind === "parsing") state.appendChild(el("span", { class: "spin" }));
  if (message) state.appendChild(el("span", { text: message }));
  state.hidden = !kind;
}

function setProbeSize(text) {
  const size = document.getElementById("addProbeSize");
  if (size) size.textContent = text || "";
}

// 探测 URL。成功就补文件名和大小；失败显示行内错误，**不关窗**。
async function probeAddUrl() {
  const urlEl = document.getElementById("addUrl");
  const errorEl = document.getElementById("addProbeError");
  const reprobe = document.getElementById("addReprobe");
  if (!urlEl) return;

  const url = firstUrlLine(urlEl.value);
  if (!url) return;
  if (reprobe) reprobe.hidden = false;

  const backend = api();
  if (!backend || typeof backend.probe_url !== "function") {
    setProbeState("error", t("addSheetProbeUnavailable"));
    if (errorEl) { errorEl.textContent = t("addSheetProbeUnavailable"); errorEl.hidden = false; }
    return;
  }

  const token = ++AddDialog.token;
  AddDialog.probing = true;
  setProbeState("parsing", t("addSheetParsing"));
  setProbeSize("");
  if (errorEl) { errorEl.textContent = ""; errorEl.hidden = true; }

  let res = null;
  try {
    // 探测时带上用户填的 Referer——有些站点不带 Referer 直接探会被拒。
    const referer = addHeaderValue("Referer");
    res = referer ? await backend.probe_url(url, referer) : await backend.probe_url(url);
  } catch (err) {
    res = { ok: false, error: String(err && err.message ? err.message : err) };
  }
  // 探测期间用户又改了 URL —— 这次的结果作废。
  if (token !== AddDialog.token) return;
  AddDialog.probing = false;

  if (res && res.ok) {
    setProbeState("ok", t("addSheetProbeOk"));
    setProbeSize(res.size ? tf("addSheetProbeSize", fmtSize(res.size)) : "");
    if (res.filename) {
      const name = document.getElementById("addFilename");
      // 用户自己填了文件名就别覆盖。
      if (name && !name.value.trim()) name.value = res.filename;
    }
    return;
  }

  // 失败——留在原地，让用户改。
  const message = (res && res.error) || t("addSheetProbeFailed");
  setProbeState("error", t("addSheetProbeFailed"));
  if (errorEl) { errorEl.textContent = message; errorEl.hidden = false; }
  if (reprobe) reprobe.hidden = false;
}

// 找一行请求头的值（按名字，大小写不敏感）。
function addHeaderValue(name) {
  const wanted = String(name || "").toLowerCase();
  let found = "";
  document.querySelectorAll("#addHeaders .header-row").forEach((row) => {
    if (found) return;
    const key = (row.querySelector(".header-key")?.value || "").trim();
    if (key.toLowerCase() === wanted) {
      found = (row.querySelector(".header-value")?.value || "").trim();
    }
  });
  return found;
}

// URL 框里可能有好几行（批量粘贴）。一行一个地址。
function urlLines(text) {
  return String(text || "")
    .split(/[\r\n]+/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

function firstUrlLine(text) {
  const lines = urlLines(text);
  return lines.length ? lines[0] : "";
}

// 收集对话框里的选项，交给 create_task。
function collectAddOptions() {
  const value = (id) => (document.getElementById(id)?.value || "").trim();

  const headers = {};
  document.querySelectorAll("#addHeaders .header-row").forEach((row) => {
    const key = (row.querySelector(".header-key")?.value || "").trim();
    const val = (row.querySelector(".header-value")?.value || "").trim();
    if (key && val) headers[key] = val;
  });

  const chunkMode = value("addChunkMode");
  const chunkRaw = value("addChunkCount");
  let chunkCount = null;
  if (chunkMode === "manual") {
    const n = parseInt(chunkRaw, 10);
    if (n > 0) chunkCount = n;
  }

  const startWhen = value("addStartWhen") || "now";
  const options = {
    filename: value("addFilename") || null,
    save_dir: value("addSaveDir") || null,
    category: value("addCategory") || null,
    proxy: value("addProxy") || null,
    headers,
    chunk_count: chunkCount,
    start_when: startWhen,
  };

  if (startWhen === "scheduled") {
    const at = value("addScheduleAt");
    if (at) {
      const ts = new Date(at).getTime();
      if (!isNaN(ts)) options.start_at = ts / 1000;
    }
  }
  return options;
}

async function submitAddDialog() {
  const err = document.getElementById("addError");
  const urlEl = document.getElementById("addUrl");
  const urls = urlLines(urlEl ? urlEl.value : "");
  if (!urls.length) {
    if (err) { err.textContent = t("addSheetErrorBadURL"); err.hidden = false; }
    return;
  }
  const options = collectAddOptions();
  if (options.start_when === "scheduled" && !options.start_at) {
    if (err) { err.textContent = t("addSheetErrorNoTime"); err.hidden = false; }
    return;
  }

  let done = 0;
  let failed = 0;
  let lastError = "";
  for (const url of urls) {
    const res = await api()?.create_task(url, options);
    if (res && res.ok) done++;
    else { failed++; lastError = (res && res.error) || t("addSheetErrorBadURL"); }
  }

  if (failed) {
    // 一条都没成功就别关窗——用户得看见哪儿错了。
    if (!done) {
      if (err) { err.textContent = lastError; err.hidden = false; }
      return;
    }
    toast(`${done} ${t("addSheetCreated")}, ${failed} ${t("addSheetFailed")}`, "err");
  } else {
    toast(t("addSheetCreated"));
  }
  closeAddDialog();
  await refreshTasks();
}

// 分块数的「自动 / 手动」切换。
function syncChunkMode() {
  const mode = document.getElementById("addChunkMode")?.value;
  const count = document.getElementById("addChunkCount");
  if (count) count.disabled = mode !== "manual";
}

// 接上对话框里的所有交互。页面加载时调一次。
function wireAddDialog() {
  const root = document.getElementById("addTaskRoot");
  if (!root) return;

  document.getElementById("addTaskClose")?.addEventListener("click", closeAddDialog);
  document.getElementById("addTaskCancel")?.addEventListener("click", closeAddDialog);
  document.getElementById("addTaskBackdrop")?.addEventListener("click", closeAddDialog);
  document.getElementById("addTaskSubmit")?.addEventListener("click", submitAddDialog);

  const urlEl = document.getElementById("addUrl");
  if (urlEl) {
    // 粘贴了就探测——用户刚贴进来的地址最可能是想下的那个。
    urlEl.addEventListener("paste", () => setTimeout(() => probeAddUrl(), 0));
    urlEl.addEventListener("blur", () => probeAddUrl());
  }

  document.getElementById("addReprobe")?.addEventListener("click", probeAddUrl);
  document.getElementById("addChunkMode")?.addEventListener("change", syncChunkMode);

  document.getElementById("addCategory")?.addEventListener("change", syncDefaultDir);

  const dir = document.getElementById("addSaveDir");
  dir?.addEventListener("input", () => { AddDialog.useCustomDir = true; });

  document.getElementById("addChooseDir")?.addEventListener("click", async () => {
    const res = await api()?.choose_directory();
    if (res?.path) {
      dir.value = res.path;
      AddDialog.useCustomDir = true;
    }
  });

  document.getElementById("addStartWhen")?.addEventListener("change", (e) => {
    const at = document.getElementById("addScheduleAt");
    if (at) at.disabled = e.target.value !== "scheduled";
  });

  // 请求头：加一行 / 删一行。
  document.getElementById("addHeaderAdd")?.addEventListener("click", () => {
    const host = document.getElementById("addHeaders");
    if (host) host.appendChild(makeHeaderRow());
  });
  root.addEventListener("click", (e) => {
    const btn = e.target.closest?.(".header-remove");
    if (!btn) return;
    const rows = document.querySelectorAll("#addHeaders .header-row");
    // 至少留一行——全删了就没地方填了。
    if (rows.length <= 1) {
      const row = rows[0];
      if (row) {
        row.querySelector(".header-key").value = "";
        row.querySelector(".header-value").value = "";
      }
      return;
    }
    btn.closest(".header-row")?.remove();
  });
}

// 一个请求头行（键 + 值 + 删除按钮）。
function makeHeaderRow() {
  return el("div", { class: "header-row", "data-header-row": "1" }, [
    el("input", { class: "input header-key", type: "text", placeholder: "User-Agent" }),
    el("input", { class: "input header-value", type: "text", placeholder: t("addSheetHeaderValue") }),
    el("button", { class: "btn btn-ghost btn-icon header-remove", type: "button" }, [
      icon("x"),
    ]),
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
        el("th", { text: t("settingsLogsPreviewTime") }), el("th", { text: t("settingsLogsPreviewLevel") }),
        el("th", { text: t("settingsLogsPreviewCategory") }), el("th", { text: t("settingsLogsPreviewMessage") }),
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
        el("div", { class: "field-hint", text: t("rowLimitSpeedHint") }),
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
    if (!res) { err.textContent = t("commonFailed"); return; }
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
        toast(res?.error || t("commonFailed"), "err");
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
  // 点别处收起列选择器。
  const picker = document.getElementById("colPicker");
  const pickerBtn = document.querySelector(".col-picker-btn");
  if (picker && !picker.hidden
      && !picker.contains(e.target)
      && !(pickerBtn && pickerBtn.contains(e.target))) {
    picker.hidden = true;
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeContextMenu(); closeModal(); closeAddDialog(); }
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
  renderStatusBar();
  if (State.view === "downloads") renderDownloads();
  else if (State.view === "history") renderHistory();
  else if (State.view === "schedule") renderSchedule();
  else if (State.view === "settings") renderSettings();
  renderDetail();
}

// ============ 数据刷新 ============

// 存设置（契约 §3.1 的 save_settings）。
//
// **只改传进去的键**——前端知道自己改了哪几个，不用把整份设置
// 发回去。后端改完会把新的设置推过来（settings_updated），
// 这里先本地更新一份，界面不用等往返。
async function saveSettings(changes) {
  const res = await api()?.save_settings(changes);
  if (res && res.settings) State.settings = res.settings;
  else if (changes) Object.assign(State.settings, changes);
  return res;
}

async function refreshTasks() {
  const tasks = await api()?.get_tasks();
  if (!tasks) return;
  State.tasks = tasks;
  // 轮询是推送不可用时的兜底——顺手把速度采样记上，
  // 详情面板的曲线就不会因为没推送而空着。
  tasks.forEach((task) => recordSpeedSample(task));
  render();
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

// Python → JS 的**唯一**入口（契约 §3.2）。
//
// 后端只认这一个函数，kind 决定走哪条路：
//   task_updated / task_removed / global_speed_updated / notice / settings_updated
//
// **推送不可用时界面照样能用**——轮询（1.5s）还在，任务列表不会
// 停更。这个函数是「更快」，不是「唯一通道」。
window.sdmEvent = function sdmEvent(kind, payload) {
  try {
    handleSdmEvent(kind, payload || {});
  } catch (err) {
    // 推送处理炸了不能拖垮整个页面——记一下就算了。
    console.error("sdmEvent handler failed", kind, err);
  }
};

function handleSdmEvent(kind, payload) {
  if (kind === "task_updated") {
    applyTaskUpdate(payload);
    return;
  }
  if (kind === "task_removed") {
    const id = payload.id;
    if (!id) return;
    State.tasks = State.tasks.filter((x) => x.id !== id);
    if (State.selectedTaskId === id) State.selectedTaskId = null;
    // 采样和错误信息跟着任务一起走——留着就是纯占内存。
    delete State.speedSamples[id];
    delete State.speedHistory[id];
    delete State.taskErrors[id];
    render();
    return;
  }
  if (kind === "global_speed_updated") {
    State.globalSpeed = {
      bytes_per_sec: Number(payload.bytes_per_sec) || 0,
      limit_bytes_per_sec: Number(payload.limit_bytes_per_sec) || 0,
      active_count: Number(payload.active_count) || 0,
      received: true,
    };
    renderStatusBar();
    return;
  }
  if (kind === "notice") {
    const level = payload.level === "error" ? "err" : (payload.level === "warn" ? "info" : "ok");
    toast(payload.message || "", level);
    return;
  }
  if (kind === "settings_updated") {
    State.settings = payload || State.settings;
    applyTheme(State.settings.theme || "system");
    State.hiddenColumns = Array.isArray(State.settings.hidden_columns) ? State.settings.hidden_columns : [];
    render();
  }
}

// 应用一条任务更新。
//
// **只更新那一行**，不重画整个列表——推送每 0.7 秒来一次，
// 每次都重建侧栏和工具栏纯属浪费。侧栏的计数会变，所以单独刷
// 一下侧栏。
function applyTaskUpdate(task) {
  if (!task || !task.id) return;
  const idx = State.tasks.findIndex((x) => x.id === task.id);
  if (idx >= 0) State.tasks[idx] = { ...State.tasks[idx], ...task };
  else State.tasks.push(task);

  recordSpeedSample(State.tasks[idx >= 0 ? idx : State.tasks.length - 1]);

  const entry = _rowCache.get(task.id);
  if (entry) {
    updateRow(entry, State.tasks[idx >= 0 ? idx : State.tasks.length - 1]);
  } else {
    renderDownloads();
  }
  // 详情面板开着的话也要跟着更新——速度曲线靠它。
  if (State.selectedTaskId === task.id) renderDetail();
  renderSidebar();
  renderStatusBar();
}

function onBootstrap(state) {
  State.locale = state.language || "system";
  State.strings = state.translations || {};
  State.tasks = state.tasks || [];
  State.settings = state.settings || {};
  State.appInfo = state.app;
  State.showToolbarLabels = State.settings.toolbar_show_labels !== false;
  document.body.classList.toggle("hide-toolbar-labels", !State.showToolbarLabels);
  // 主题和列的显隐从设置里读。
  applyTheme(State.settings.theme || "system");
  State.hiddenColumns = Array.isArray(State.settings.hidden_columns) ? State.settings.hidden_columns : [];
  applyStaticI18n();
  wireAddDialog();
  // 侧栏脚注里的「设置」按钮原来没接事件——点了没反应。
  document.getElementById("btnSettings")?.addEventListener("click", () => setView("settings"));
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
    Object.keys(CATEGORY_META).map((c) => el("option", { value: c, text: categoryLabel(c), selected: c === candidate.category })));
  const dirInput = el("input", { class: "input", value: "" });
  const threadInput = el("input", { class: "input", type: "number", min: "1", max: "128", value: String(candidate.default_threads || 32) });

  // 用户手动改过目录就别再自动覆盖了。
  //
  // Swift 对照：App/MainWindow/ClipboardConfirmView.swift 的
  // useCustomDir 标志——用户手动选过目录，分类切换时就不再动它。
  let useCustomDir = false;

  // 分类切换时同步默认目录。
  const syncDefaultDir = async () => {
    if (useCustomDir) return;
    const r = await api()?.get_default_dir(catSel.value);
    dirInput.value = r?.path || "";
  };

  catSel.addEventListener("change", syncDefaultDir);
  // 手动改目录 → 标记为自定义
  dirInput.addEventListener("input", () => { useCustomDir = true; });

  // kill switch：勾上就关掉剪贴板监听。
  //
  // Swift 对照：ClipboardConfirmView.swift:136-141 绑的是
  // store.clipboardCapture。开关立即生效——下一轮轮询就停。
  //
  // **默认不勾**——勾上才表示「我不想再监听了」。现在已经关掉的
  // 情况下弹窗根本不会出现（监听器停了就不会有候选）。
  const disableToggle = el("input", { type: "checkbox" });

  const submit = async (skip) => {
    if (!skip) {
      // 目录不能是空的。空的话后端会按 URL 猜分类，跟用户选的
      // 分类可能对不上——那就落错目录了。
      let dir = dirInput.value.trim();
      if (!dir) {
        const r = await api()?.get_default_dir(catSel.value);
        dir = r?.path || "";
      }
      await api()?.add_url(candidate.url, null, catSel.value, dir || null, parseInt(threadInput.value, 10) || null);
    } else {
      // 「跳过」只是关掉弹窗，**不永久禁用剪贴板监听**。
      // Swift 版也是这个语义（ClipboardConfirmView 的 skip 按钮）。
      await api()?.dismiss_clipboard_candidate();
    }
    // kill switch 勾上了才改设置
    if (disableToggle.checked && State.settings.capture_clipboard !== false) {
      await saveSettings({ capture_clipboard: false });
      State.settings.capture_clipboard = false;
    }
    closeModal();
    await refreshTasks();
  };

  // 初始目录：按当前分类算
  syncDefaultDir();

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
      // kill switch：停止监听剪贴板。
      //
      // Swift 对照：ClipboardConfirmView.swift:136-141——弹窗里
      // 有个复选框，勾上就关掉剪贴板捕获。
      el("div", { class: "field" }, [
        el("label", { class: "check-row" }, [
          disableToggle,
          el("span", { text: t("clipboardDisableToggle") }),
        ]),
      ]),
    ]),
  ], [
    el("button", { class: "btn", onclick: () => submit(true) }, [el("span", { text: t("clipboardSkip") })]),
    el("button", { class: "btn btn-primary", onclick: () => submit(false) }, [el("span", { text: t("clipboardDownload") })]),
  ]);
}

function onScheduleFired() { refreshTasks(); }

// 首次启动时后端把 CLI 装好（软链到 PATH、注册 daemon）。
// 后端在后台线程里做，做完推过来。
function onCliInstall(result) {
  if (!result) return;
  const cli = result.cli || {};
  if (!cli.ok) {
    if (cli.reason === "conflict") {
      toast(t("cliInstallConflict") + " " + (cli.path || ""), "info");
    } else if (cli.reason && cli.reason !== "not_bundle") {
      toast(t("cliInstallFailed") + " " + cli.reason, "err");
    }
    return;
  }
  // 已经在 PATH 里就不用打扰用户了——这是正常状态，不是新闻。
  if (cli.in_path === false) {
    toast(t("cliInstallNoPath") + " " + (cli.path || ""), "info");
  }
}

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
  // 定时刷新任务状态。
  //
  // **推送是主通道，轮询只是兜底**（契约 §3.2）——所以间隔从
  // 700ms 放到 1500ms。窗口最小化/隐藏时不轮询——那纯是浪费。
  setInterval(() => {
    if (State.view !== "downloads") return;
    if (document.hidden) return;
    refreshTasks();
  }, 1500);
});
