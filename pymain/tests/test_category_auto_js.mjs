// 自动分类的前端逻辑测试。
//
// 背景：用户 2026-10-27 报「我加的是 .dmg 文件，应该分成『压缩包』
// 或者『程序』，但它还是保持在『视频』类别」。
//
// 根因在前端：分类下拉框的第一个选项是 video，`collectAddOptions`
// 把它当成分类发给后端，后端再覆盖掉引擎按 URL 猜出来的正确分类。
// 于是「自动」这个概念根本没传出去——用户没选，程序却替他选了视频。
//
// 修法：下拉框第一个选项是「自动」（auto），默认选中；自动时
// 后端传 null，由引擎按 URL 和服务器给的文件名判断。
//
// app.js 是浏览器脚本，没法直接 import（依赖 DOM）。这里把被测的
// 函数从源码里抠出来，在一个假的 DOM 上跑——测的是真实现。
//
// 跑法：node tests/test_category_auto_js.mjs

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
  return appJs.slice(start, end + 3);
}

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
    this._value = "";
    this._text = "";
    this.listeners = {};
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
  set innerHTML(v) {
    this._text = v;
  }
  get innerHTML() {
    return this._text;
  }
}

const nodes = {};

function resetDom() {
  for (const key of Object.keys(nodes)) delete nodes[key];
  nodes.addCategory = new FakeNode("select");
  nodes.addFilename = new FakeNode("input");
  nodes.addSaveDir = new FakeNode("input");
  nodes.addProxy = new FakeNode("input");
  nodes.addStartWhen = new FakeNode("select");
  nodes.addScheduleAt = new FakeNode("input");
  nodes.addUrl = new FakeNode("textarea");
  nodes.addProbeState = new FakeNode("div");
  nodes.addProbeSize = new FakeNode("span");
  nodes.addProbeError = new FakeNode("div");
  nodes.addReprobe = new FakeNode("button");
}

const documentStub = {
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: text }),
  getElementById: (id) => nodes[id] || null,
  querySelectorAll: () => [],
};

const strings = {
  categoryAuto: "自动",
  categoryVideo: "视频",
  categoryMusic: "音乐",
  categoryPictures: "图片",
  categoryArchives: "压缩包",
  categoryDocuments: "文档",
  categorySoftware: "程序",
  categoryOther: "其他",
};

const context = {
  document: documentStub,
  console,
  t: (key) => (strings[key] !== undefined ? strings[key] : key),
  api: () => null,
  tf: (key, arg) => `${key}:${arg}`,
  fmtSize: (n) => `${n} B`,
  AddDialog: { probing: false, token: 0, useCustomDir: false, categoryTouched: false },
};
vm.createContext(context);

const source = [
  extractConst("CATEGORY_META"),
  extractFunction("el"),
  extractFunction("clear"),
  extractFunction("categoryLabel"),
  extractFunction("urlLines"),
  extractFunction("firstUrlLine"),
  extractFunction("fillCategorySelect"),
  extractFunction("collectAddOptions"),
  extractFunction("setProbeState"),
  extractFunction("setProbeSize"),
  extractFunction("addHeaderValue"),
  extractFunction("syncDefaultDir"),
  extractFunction("probeAddUrl"),
].join("\n\n");
vm.runInContext(source, context);

// -- 用例 ----------------------------------------------------------

const cases = [];

function test(name, fn) {
  cases.push([name, fn]);
}

test("分类下拉第一个选项是「自动」，并且默认选中", () => {
  resetDom();
  context.fillCategorySelect();
  const sel = nodes.addCategory;
  const first = sel.children[0];
  assert.ok(first, "分类下拉是空的");
  assert.equal(first.getAttribute("value"), "auto", "第一个选项必须是「自动」");
  assert.equal(sel.value, "auto", "默认必须选中「自动」");
  // 「自动」之后才是各个具体分类。
  const values = sel.children.map((c) => c.getAttribute("value"));
  assert.equal(values.length, 8, "应该有 1 个自动 + 7 个分类");
  assert.ok(values.includes("video"));
  assert.ok(values.includes("software"));
});

test("分类是「自动」时不把分类发给后端", () => {
  resetDom();
  context.fillCategorySelect();
  const options = context.collectAddOptions();
  assert.equal(options.category, null, "「自动」必须传 null，让后端自己判");
});

test("用户选了具体分类就照他的选", () => {
  resetDom();
  context.fillCategorySelect();
  nodes.addCategory.value = "software";
  const options = context.collectAddOptions();
  assert.equal(options.category, "software");
});

test("创建任务不再带分片数", () => {
  resetDom();
  context.fillCategorySelect();
  const options = context.collectAddOptions();
  assert.ok(!("chunk_count" in options), "不该再传 chunk_count");
});

test("探测成功后按真实文件名自动切分类，保存目录跟着走", async () => {
  resetDom();
  context.AddDialog.categoryTouched = false;
  context.fillCategorySelect();
  nodes.addUrl.value = "http://example.com/download/12345";
  nodes.addSaveDir.value = "";

  const calls = [];
  context.api = () => ({
    probe_url: async () => ({
      ok: true,
      filename: "CleanShot X 5.0.2.dmg",
      size: 52475827,
      category: "software",
    }),
    get_default_dir: async (cat, url) => {
      calls.push([cat, url]);
      return { ok: true, path: "/Downloads/Software" };
    },
  });

  await context.probeAddUrl();

  assert.equal(nodes.addCategory.value, "software", "探测出 .dmg 应该自动切到「程序」");
  assert.equal(nodes.addSaveDir.value, "/Downloads/Software", "保存目录要跟着分类走");
  assert.deepEqual(calls[0], ["software", "http://example.com/download/12345"]);
});

test("用户手动改过分类，探测不再自动切", async () => {
  resetDom();
  context.AddDialog.categoryTouched = true;
  context.fillCategorySelect();
  nodes.addCategory.value = "video";
  nodes.addUrl.value = "http://example.com/download/12345";

  context.api = () => ({
    probe_url: async () => ({
      ok: true,
      filename: "CleanShot X 5.0.2.dmg",
      size: 1,
      category: "software",
    }),
    get_default_dir: async () => ({ ok: true, path: "/Downloads/Video" }),
  });

  await context.probeAddUrl();
  assert.equal(nodes.addCategory.value, "video", "用户选过的分类不该被改掉");
});

test("app.js 里没有手动分片数的痕迹", () => {
  assert.ok(!appJs.includes("addChunkCount"), "还有 addChunkCount");
  assert.ok(!appJs.includes("addChunkMode"), "还有 addChunkMode");
  assert.ok(!appJs.includes("syncChunkMode"), "还有 syncChunkMode");
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
