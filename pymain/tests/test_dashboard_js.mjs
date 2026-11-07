// 首页（仪表盘）的行为测试。
//
// 背景：仪表盘从「一个统计页」变成了应用的首页——启动就停在它，
// 侧栏里它排第一，而且首页要能直接把链接丢进去开始下载。
//
// 这里测的是三件容易坏的事：
//   1. 默认视图是不是 dashboard（不是 downloads）。
//   2. 首页的「快速添加」提交时是不是真的调 create_task(url, {})。
//   3. 统计卡是不是能点，点了之后 State.view / State.filter 变没变。
//
// app.js 是浏览器脚本，没法直接 import（它依赖 DOM）。这里把被测的
// 代码从源码里抠出来，在一个假的 DOM 上跑——测的是真实现。
//
// 跑法：node tests/test_dashboard_js.mjs

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const appJs = readFileSync(join(here, "..", "sdm", "gui", "assets", "app.js"), "utf-8");

// -- 把被测代码抠出来 ---------------------------------------------

// 抠出一个 `const X = {...};` 形式的常量。
function extractConst(name) {
  const start = appJs.indexOf(`const ${name} = {`);
  assert.ok(start >= 0, `app.js 里找不到 const ${name}`);
  const end = appJs.indexOf("\n};", start);
  assert.ok(end > start, `${name} 的大括号没配对`);
  return appJs.slice(start, end + 3);
}

// 抠出一个模块级的 `let x = ...;` 声明。
// 仪表盘缓存了两块 DOM（快速添加区、空态块），它们是模块级变量——
// 不抠进来，被测函数里���引用就是 ReferenceError。
function extractLet(name) {
  const start = appJs.indexOf(`let ${name} =`);
  assert.ok(start >= 0, `app.js 里找不到 let ${name}`);
  const end = appJs.indexOf(";", start);
  assert.ok(end > start, `${name} 的声明没写完`);
  return appJs.slice(start, end + 1);
}

// 抠出一个 `function X(...) {...}`。
function extractFunction(name) {
  const start = appJs.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `app.js 里找不到 ${name}`);
  // **async 要一起抠出来。** 只抠 `function xxx(` 的话，
  // `async function` 的 async 会被丢掉——里面的 await 就成了
  // 语法错误（"Unexpected identifier"）。
  const before = appJs.slice(Math.max(0, start - 6), start);
  const head = before.endsWith("async ") ? "async " : "";
  let depth = 0;
  let i = appJs.indexOf("{", start);
  for (; i < appJs.length; i++) {
    if (appJs[i] === "{") depth++;
    else if (appJs[i] === "}") {
      depth--;
      if (depth === 0) return appJs.slice(start - head.length, i + 1);
    }
  }
  throw new Error(`${name} 的大括号没配对`);
}

// -- 假的 DOM ------------------------------------------------------

class FakeNode {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this._value = "";
    this._text = "";
    this.listeners = {};
    this.parentNode = null;
    this.hidden = false;
  }
  get firstChild() {
    return this.children.length ? this.children[0] : null;
  }
  appendChild(child) {
    // **真实 DOM 里 appendChild 是「移动」，不是「复制」。**
    // 同一个节点挂到新父亲下，旧父亲里就没它了。
    //
    // 这条语义很重要——快速添加区在「有数据」和「空态」之间
    // 换来换去，用的就是同一个 DOM 节点。假 DOM 不模拟这个的话，
    // 节点会同时挂在两个父亲下，测试就测不出真实行为。
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    this.children = this.children.filter((c) => c !== child);
    child.parentNode = null;
    return child;
  }
  insertBefore(node, ref) {
    node.parentNode = this;
    const idx = ref ? this.children.indexOf(ref) : -1;
    if (idx < 0) this.children.push(node);
    else this.children.splice(idx, 0, node);
    return node;
  }
  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }
  setAttribute(k, v) {
    this.attributes[k] = v;
    if (k === "value") this._value = v;
    if (k === "class") this.className = v;
  }
  getAttribute(k) {
    return this.attributes[k];
  }
  addEventListener(name, fn) {
    (this.listeners[name] = this.listeners[name] || []).push(fn);
  }
  set textContent(v) {
    this._text = v;
    this.children = [];
  }
  get textContent() {
    return this._text;
  }
  set innerHTML(v) {
    this._text = v;
  }
  get innerHTML() {
    return this._text;
  }
  set className(v) {
    this.attributes.class = v;
  }
  get className() {
    return this.attributes.class || "";
  }
  set value(v) {
    this._value = v;
  }
  get value() {
    return this._value;
  }
  focus() {}
  scrollIntoView() {}
  contains(node) {
    if (node === this) return true;
    return this.children.some((c) => c.contains(node));
  }
  querySelector() {
    return null;
  }
  querySelectorAll() {
    return [];
  }
}

const nodes = {};

function node(id, tag) {
  if (!nodes[id]) nodes[id] = new FakeNode(tag || "div");
  return nodes[id];
}

// 复刻 index.html 里的结构：仪表盘的视图容器是 viewDashboard，
// renderDashboard 往它里面塞内容。统计卡、分类行、最近完成
// 都得在这个容器底下才找得到。
node("viewDashboard");

const documentStub = {
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (ns, tag) => new FakeNode(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: text }),
  getElementById: (id) => node(id),
  querySelectorAll: () => [],
};

// -- 假的后端 ------------------------------------------------------

const apiCalls = [];
const toasts = [];

const fakeApi = {
  create_task: async (url, options) => {
    apiCalls.push(["create_task", url, options]);
    return { ok: true, task_id: "t1" };
  },
  get_tasks: async () => [],
  get_history: async () => historyRows,
};

// 历史数据。**goHistory 必须先把它拉回来再切视图**——
// `setView("history")` 里的 refreshHistory 是异步的、没等它。
let historyRows = [];

// vm 里的 State 还没建好，先用这个壳子占位。跑完 vm 之后
// 把真正的 State 塞进来。
const stateHolder = { state: null };

const context = {
  console,
  document: documentStub,
  // 假 api 对象注入：被测代码只认 `api()?.xxx` 这一个入口。
  api: () => fakeApi,
  toast: (message, kind) => toasts.push([message, kind]),
  // 这些是 app.js 里跟仪表盘无关的刷新函数，测试里只需要它们别炸。
  render: () => {},
  refreshTasks: async () => {},
  refreshDashboard: async () => {},
  // **refreshHistory 要真的更新 State.history。** 它是 async 的，
  // goHistory 得等它返回再切视图——不等的话渲染时用的是上一次的
  // 数据（第一次进历史页就是空的），高亮就丢了。
  refreshHistory: async () => {
    if (stateHolder.state) {
      stateHolder.state.history = await fakeApi.get_history(500);
    }
  },
  refreshSchedules: async () => {},
  refreshBrowserCapture: async () => {},
};

// -- 把要测的那部分装进 vm ----------------------------------------

const source = [
  extractConst("State"),
  extractConst("CATEGORY_META"),
  extractLet("_dashQuick"),
  extractLet("_dashEmpty"),
  extractFunction("t"),
  extractFunction("tf"),
  extractFunction("fmtSize"),
  extractFunction("fmtRate"),
  extractFunction("fmtDate"),
  extractFunction("el"),
  extractFunction("icon"),
  extractFunction("clear"),
  extractFunction("categoryLabel"),
  extractFunction("setView"),
  extractFunction("captureScrollTop"),
  extractFunction("restoreScrollTop"),
  extractFunction("quickAddRegion"),
  extractFunction("submitQuickAdd"),
  extractFunction("goDownloads"),
  extractFunction("goHistory"),
  extractFunction("dashStatTile"),
  extractFunction("renderDashboard"),
  "globalThis.__app = { State, quickAddRegion, submitQuickAdd, renderDashboard };",
].join("\n\n");
vm.createContext(context);
vm.runInContext(source, context);

const app = context.__app;
const State = app.State;
stateHolder.state = State;

// -- 工具 ----------------------------------------------------------

// 在假 DOM 里按 dataset 找节点。
function findByDataset(root, key, value) {
  let found = null;
  const walk = (n) => {
    if (found) return;
    if (n.dataset && n.dataset[key] === value) {
      found = n;
      return;
    }
    n.children.forEach(walk);
  };
  walk(root);
  return found;
}

// 触发节点上的第一个 click 监听器。
async function click(node) {
  const handlers = node.listeners.click || [];
  assert.ok(handlers.length, "这个节点没有 click 监听器");
  for (const fn of handlers) await fn({ preventDefault() {}, stopPropagation() {} });
}

function dashboardContent() {
  return documentStub.getElementById("viewDashboard");
}

const SAMPLE_DASHBOARD = {
  ok: true,
  active: { count: 2, speed: 1024 },
  today: { downloads: 1, bytes: 2048 },
  totals: {
    downloads: 5,
    bytes: 10240,
    failed: 1,
    by_category: [{ id: "video", count: 2, bytes: 4096 }],
  },
  recent: [{ id: "r1", filename: "a.mp4", category: "video", bytes: 4096, state: "completed", finished_at: 1700000000 }],
};

// -- 用例 ----------------------------------------------------------

const cases = [];

function test(name, fn) {
  cases.push([name, fn]);
}

test("默认视图是仪表盘（首页），不是下载列表", () => {
  assert.equal(State.view, "dashboard", "State.view 初值必须是 dashboard");
  // 源码里也不该再有把视图拽回 downloads 的地方。
  assert.ok(!/State\.view\s*=\s*"downloads"/.test(appJs), "还有地方把视图设回 downloads");
});

test("首页有快速添加区：URL 输入框 + 添加按钮", () => {
  const region = app.quickAddRegion();
  assert.ok(region && region.input, "快速添加区没有 URL 输入框");
  assert.ok(region.node, "快速添加区没有 DOM");
  const buttons = [];
  const walk = (n) => {
    if (n.tagName === "button") buttons.push(n);
    n.children.forEach(walk);
  };
  walk(region.node);
  assert.ok(buttons.length >= 1, "快速添加区没有添加按钮");
});

test("快速添加回车提交：调的是 create_task(url, {})", async () => {
  const region = app.quickAddRegion();
  region.input.value = "  https://example.com/a.zip  ";
  const keydown = region.input.listeners.keydown || [];
  assert.ok(keydown.length, "输入框没接回车");
  await keydown[0]({ key: "Enter", preventDefault() {} });

  assert.equal(apiCalls.length, 1, `create_task 调了 ${apiCalls.length} 次`);
  // **不能直接 deepEqual 整个数组。** 被测代码在 vm 里跑，
  // 它造的 `{}` 的原型是 vm 那个 realm 的 Object.prototype，
  // 跟这里（测试 realm）的不是同一个——strict 版的 deepEqual
  // 会因为原型不同而判不等，尽管结构一模一样。
  const call = apiCalls[0];
  assert.equal(call[0], "create_task");
  assert.equal(call[1], "https://example.com/a.zip");
  assert.deepEqual(Object.keys(call[2]), [], "options 应该是空对象");
  assert.equal(region.input.value, "", "提交成功后输入框要清空");
  assert.ok(toasts.length, "提交成功要有 toast 提示");
});

test("快速添加失败：后端返回的 error 要显示出来", async () => {
  apiCalls.length = 0;
  const region = app.quickAddRegion();
  region.input.value = "https://example.com/bad";
  const original = fakeApi.create_task;
  fakeApi.create_task = async () => ({ ok: false, task_id: null, error: "后端说不行" });
  try {
    await app.submitQuickAdd();
  } finally {
    fakeApi.create_task = original;
  }
  assert.equal(region.error.textContent, "后端说不行", "后端的错误没显示出来");
  assert.equal(region.error.hidden, false, "错误行应该显示");
});

test("统计卡能点：点「正在下载」切到下载列表并筛选下载中", async () => {
  State.dashboard = SAMPLE_DASHBOARD;
  app.renderDashboard();

  const tile = findByDataset(dashboardContent(), "dashStat", "active");
  assert.ok(tile, "找不到「正在下载」那张统计卡");
  await click(tile);

  assert.equal(State.view, "downloads", "点了统计卡没切到下载列表");
  assert.equal(State.filter, "downloading", "没筛选出「下载中」");
  assert.equal(State.categoryFilter, null, "不该带着分类筛选");
});

test("统计卡能点：点「今日完成」切到历史", async () => {
  State.dashboard = SAMPLE_DASHBOARD;
  app.renderDashboard();

  const tile = findByDataset(dashboardContent(), "dashStat", "todayCount");
  assert.ok(tile, "找不到「今日完成」那张统计卡");
  await click(tile);

  assert.equal(State.view, "history", "点了「今日完成」没切到历史");
});

test("分类条形图能点：切到下载列表并按该分类筛选", async () => {
  State.dashboard = SAMPLE_DASHBOARD;
  app.renderDashboard();

  const row = findByDataset(dashboardContent(), "dashCategory", "video");
  assert.ok(row, "找不到分类条形图那一行");
  await click(row);

  assert.equal(State.view, "downloads", "点分类行没切到下载列表");
  assert.equal(State.categoryFilter, "video", "没按分类筛选");
});

test("最近完成的条目能点：跳到历史并记住要高亮哪一条", async () => {
  State.dashboard = SAMPLE_DASHBOARD;
  app.renderDashboard();

  const item = findByDataset(dashboardContent(), "dashRecent", "r1");
  assert.ok(item, "找不到「最近完成」里的条目");
  await click(item);

  assert.equal(State.view, "history", "点最近完成没跳到历史");
  assert.equal(State.historyHighlightId, "r1", "没记住要高亮哪一条");
});

test("点最近完成：切视图之前先把历史拉回来", async () => {
  /** 历史数据要先到，高亮才有意义。
   *
   * `setView("history")` 里的 `refreshHistory()` 是异步的、没等它——
   * `render()` 会先跑，用的是上一次的 `State.history`。用户第一次
   * 进历史页的话，那份数据是空的，刚点的那一条根本不在里面，
   * 高亮就丢了。
   *
   * 所以 `goHistory` 必须先 `await refreshHistory()` 再切视图。
   */
  historyRows = [{ id: "r1", filename: "a.mp4", category: "video", bytes: 4096, state: "completed", finished_at: 1700000000 }];
  State.history = [];
  State.dashboard = SAMPLE_DASHBOARD;
  app.renderDashboard();

  const item = findByDataset(dashboardContent(), "dashRecent", "r1");
  await click(item);

  assert.equal(State.history.length, 1, "切视图之前历史没拉回来");
  assert.equal(State.history[0].id, "r1");
  assert.equal(State.historyHighlightId, "r1", "高亮请求被丢了");
  historyRows = [];
});

test("仪表盘重画不弄丢快速添加框里已经打好的链接", () => {
  const region = app.quickAddRegion();
  region.input.value = "https://example.com/half-typed";
  State.dashboard = SAMPLE_DASHBOARD;
  app.renderDashboard();
  assert.equal(region.input.value, "https://example.com/half-typed", "重画把用户打的字弄丢了");
  region.input.value = "";
});

test("没有数据时的空态里也能直接添加任务", () => {
  State.dashboard = null;
  app.renderDashboard();
  const empty = dashboardContent().children[0];
  assert.ok(empty, "空态没渲染出来");
  assert.ok(/empty/.test(empty.className), "空态块的类名不对");
  // 快速添加区就在空态块里面（不是空态外面另有一份）。
  const region = app.quickAddRegion();
  assert.equal(region.node.parentNode, empty, "快速添加区没被放进空态里");
});

// -- 跑 -----------------------------------------------------------

let passed = 0;
for (const [name, fn] of cases) {
  try {
    await fn();
    passed++;
    console.log(`  ✓ ${name}`);
  } catch (err) {
    console.error(`  ✗ ${name}`);
    console.error(err && err.stack ? err.stack : err);
    process.exitCode = 1;
  }
}
console.log(`${passed} 个用例通过`);
if (passed !== cases.length) process.exitCode = 1;
