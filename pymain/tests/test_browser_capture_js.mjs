// 浏览器接管板块（设置页）的前端逻辑测试。
//
// 背景：这个板块是用户唯一能看见「浏览器接管」的地方——
// 扩展在哪、装没装、配对 token 是多少，全靠它。它拼的是
// 后端 get_browser_capture_info() 返回的结构，字段名对不上
// 就会静默显示空面板（用户以为功能坏了）。
//
// app.js 是浏览器脚本，没法直接 import（依赖 DOM）。这里把被测的
// 函数从源码里抠出来，在一个假的 DOM 上跑——测的是真实现。
//
// 跑法：node tests/test_browser_capture_js.mjs

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const appJs = readFileSync(join(here, "..", "sdm", "gui", "assets", "app.js"), "utf-8");

// -- 把被测代码抠出来 ---------------------------------------------

function extractConst(name) {
  const start = appJs.indexOf(`const ${name} = {`);
  assert.ok(start >= 0, `app.js 里找不到 const ${name}`);
  const end = appJs.indexOf("\n};", start);
  assert.ok(end > start, `${name} 的大括号没配对`);
  // **const 要改成 var。** vm 里跑的顶层 `const` 只进脚本的词法
  // 作用域，不会挂到 context 上——`context.State` 会是 undefined，
  // 用例里连设置状态都做不到。`var` 才会成为全局对象的属性。
  return appJs.slice(start, end + 3).replace(/^const /, "var ");
}

function extractFunction(name) {
  const start = appJs.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `app.js 里找不到 ${name}`);
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
    this._value = "";
    this._text = "";
    this.listeners = {};
    this.selected = false;
  }
  get firstChild() {
    return this.children.length ? this.children[0] : null;
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    this.children = this.children.filter((c) => c !== child);
    return child;
  }
  setAttribute(k, v) {
    this.attributes[k] = v;
    if (k === "value") this._value = v;
  }
  getAttribute(k) {
    return this.attributes[k];
  }
  addEventListener(name, fn) {
    (this.listeners[name] = this.listeners[name] || []).push(fn);
  }
  select() {
    this.selected = true;
  }
  set textContent(v) {
    this._text = v;
    this.children = [];
  }
  get textContent() {
    return this._text;
  }
  set value(v) {
    this._value = v;
  }
  get value() {
    return this._value;
  }
  set className(v) {
    this.attributes.class = v;
  }
  get className() {
    return this.attributes.class || "";
  }
}

const strings = {
  settingsBrowserCapture: "浏览器接管",
  browserCaptureExplanation: "装上扩展后浏览器下载交给本程序。",
  browserCaptureLoading: "正在读取浏览器接管状态…",
  browserCaptureUnavailable: "读取失败。",
  browserCaptureToken: "配对 token",
  browserCaptureTokenHint: "Safari 需要手动粘贴。",
  browserCaptureCopy: "复制",
  browserCaptureCopied: "token 已复制",
  browserCaptureCopyFailed: "复制失败。",
  browserCaptureReset: "重新生成",
  browserCaptureResetDone: "已生成新的 token。",
  browserCaptureOpenFolder: "打开扩展目录",
  browserCaptureOpenPage: "打开扩展管理页",
  browserCaptureInstalled: "已安装",
  browserCaptureNotInstalled: "未安装",
  browserCaptureAuto: "自动接管下载",
  browserCaptureManualOnly: "只能手动派单",
  browserCaptureHostReady: "宿主已就位。",
  browserCaptureHostMissing: "宿主未安装。",
  browserCaptureSafariSteps: "Safari 安装步骤：设置 → 高级 → 开发者模式。",
  browserCaptureTemporaryNote: "临时扩展 24 小时后失效。",
  browserCaptureReloadHint: "扩展更新后要点「重新加载」。",
};

const documentStub = {
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (_ns, tag) => new FakeNode(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: text }),
  getElementById: () => null,
  querySelectorAll: () => [],
};

const toasts = [];
const apiCalls = [];
let apiStub = null;

const context = {
  document: documentStub,
  console,
  navigator: {
    clipboard: {
      writeText: async (text) => {
        context.__copied = text;
      },
    },
  },
  t: (key) => (strings[key] !== undefined ? strings[key] : key),
  toast: (message, kind) => { toasts.push([message, kind]); },
  api: () => apiStub,
  requestAnimationFrame: (fn) => fn(),
  // app.js 里的 render() 会重画整个界面——测试里不需要，
  // 但被测代码会调它，得给个空壳。
  render: () => {},
};
vm.createContext(context);

const source = [
  extractConst("State"),
  extractFunction("el"),
  extractFunction("icon"),
  extractFunction("clear"),
  extractFunction("renderBrowserCaptureSection"),
  extractFunction("refreshBrowserCapture"),
].join("\n\n");
vm.runInContext(source, context);

// -- 工具 ----------------------------------------------------------

function walk(node, out = []) {
  out.push(node);
  for (const child of node.children || []) {
    if (child && child.children) walk(child, out);
  }
  return out;
}

function texts(node) {
  return walk(node)
    .map((n) => n._text || "")
    .filter(Boolean)
    .join(" | ");
}

function findButton(node, label) {
  // 按钮上的文字在子 span 里，**按钮自己的 textContent 是空的**——
  // 得看整棵子树。
  return walk(node).find(
    (n) => n.tagName === "button" && texts(n).includes(label),
  );
}

function findButtons(node, label) {
  return walk(node).filter(
    (n) => n.tagName === "button" && texts(n).includes(label),
  );
}

function browserEntry(id, extra = {}) {
  return {
    id,
    label: id === "chrome" ? "Chrome" : id === "firefox" ? "Firefox" : "Safari",
    extensions_url:
      id === "safari" ? "" : id === "chrome" ? "chrome://extensions/" : "about:debugging",
    needs_manual_pairing: id === "safari",
    auto_capture: id !== "safari",
    extension_dir: `/tmp/extensions/${id}`,
    available: true,
    installed: false,
    native_host: id !== "safari",
    ...extra,
  };
}

function renderWith(info) {
  context.State.browserCapture = info;
  const node = context.renderBrowserCaptureSection();
  return node;
}

const fullInfo = {
  ok: true,
  token: "TOKEN-123",
  extension_root: "/tmp/extensions",
  browsers: [browserEntry("chrome"), browserEntry("firefox"), browserEntry("safari")],
  host_browsers: ["chrome", "firefox"],
};

// -- 用例 ----------------------------------------------------------

const cases = [];
function test(name, fn) {
  cases.push([name, fn]);
}

test("数据没到时显示占位提示，不是一片空白", () => {
  const node = renderWith(null);
  assert.ok(texts(node).includes("正在读取"), "应该有占位文案");
});

test("后端报错时显示失败提示", () => {
  const node = renderWith({ ok: false, error: "炸了", browsers: [] });
  assert.ok(texts(node).includes("读取失败"), "应该显示失败文案");
});

test("板块标题和说明都渲染出来", () => {
  const node = renderWith(fullInfo);
  const all = texts(node);
  assert.ok(all.includes("浏览器接管"), "缺少板块标题");
  assert.ok(all.includes("装上扩展后浏览器下载交给本程序。"), "缺少说明");
});

test("配对 token 显示在后端给的值上", () => {
  const node = renderWith(fullInfo);
  const input = walk(node).find((n) => n.tagName === "input" && n.getAttribute("readonly") !== undefined);
  assert.ok(input, "找不到只读的 token 输入框");
  assert.equal(input.value, "TOKEN-123");
});

test("三个浏览器都列出来了", () => {
  const node = renderWith(fullInfo);
  const all = texts(node);
  for (const label of ["Chrome", "Firefox", "Safari"]) {
    assert.ok(all.includes(label), `少了 ${label}`);
  }
});

test("装没装如实显示", () => {
  const node = renderWith({
    ...fullInfo,
    browsers: [browserEntry("chrome", { installed: true }), browserEntry("firefox")],
  });
  const all = texts(node);
  assert.ok(all.includes("已安装"), "Chrome 装了却没显示「已安装」");
  assert.ok(all.includes("未安装"), "Firefox 没装却没显示「未安装」");
});

test("Safari 写明只能手动派单", () => {
  const node = renderWith(fullInfo);
  assert.ok(texts(node).includes("只能手动派单"), "Safari 的限制要写出来");
});

test("Safari 行给出安装步骤和临时扩展的警告", () => {
  const node = renderWith(fullInfo);
  const all = texts(node);
  assert.ok(all.includes("Safari 安装步骤"), "缺少 Safari 安装步骤");
  assert.ok(all.includes("24 小时"), "缺少临时扩展的时效说明");
});

test("Safari 不给「打开扩展管理页」按钮", () => {
  const node = renderWith(fullInfo);
  // Chrome 和 Firefox 有，Safari 没有——所以只有两个，不该有第三个。
  assert.equal(findButtons(node, "打开扩展管理页").length, 2, "Safari 不该有扩展管理页按钮");
  assert.equal(findButtons(node, "打开扩展目录").length, 3, "三个浏览器都该能打开扩展目录");
});

test("复制按钮把 token 写进剪贴板并提示", async () => {
  context.__copied = null;
  toasts.length = 0;
  const node = renderWith(fullInfo);
  const btn = findButton(node, "复制");
  assert.ok(btn, "找不到复制按钮");
  await btn.listeners.click[0]();
  assert.equal(context.__copied, "TOKEN-123");
  assert.ok(toasts.some(([msg]) => msg === "token 已复制"), "复制后要提示");
});

test("重新生成按钮调后端并刷新状态", async () => {
  toasts.length = 0;
  apiCalls.length = 0;
  apiStub = {
    reset_pair_token: async () => {
      apiCalls.push(["reset_pair_token"]);
      return { ok: true, token: "NEW" };
    },
    get_browser_capture_info: async () => fullInfo,
  };
  const node = renderWith(fullInfo);
  const btn = findButton(node, "重新生成");
  assert.ok(btn, "找不到重新生成按钮");
  await btn.listeners.click[0]();
  assert.ok(apiCalls.some(([name]) => name === "reset_pair_token"), "没调后端重置");
  assert.ok(toasts.some(([msg]) => msg === "已生成新的 token。"), "重置后要提示");
});

test("打开扩展目录按钮带上浏览器 id", async () => {
  apiCalls.length = 0;
  apiStub = {
    open_browser_extension_folder: async (id) => {
      apiCalls.push(["open_browser_extension_folder", id]);
      return { ok: true };
    },
  };
  const node = renderWith(fullInfo);
  const btn = findButton(node, "打开扩展目录");
  await btn.listeners.click[0]();
  const call = apiCalls.find(([name]) => name === "open_browser_extension_folder");
  assert.ok(call, "没调打开目录");
  assert.ok(call[1], "得告诉后端是哪个浏览器");
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
