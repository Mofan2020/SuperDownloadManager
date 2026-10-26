// 设置页滚动位置保持的测试。
//
// 背景：改任何设置都会重画整个设置页（后端推 settings_updated
// → render() → renderSettings()）。重画把滚动容器 `.list-wrap`
// 整个换掉，新容器的 scrollTop 是 0 —— 用户改个设置就被弹回页面
// 顶部。
//
// app.js 是浏览器脚本，没法直接 import（它依赖 DOM）。这里把
// captureScrollTop / restoreScrollTop 的源码抠出来在 Node 里跑——
// 测的是真实现，不是复制一份。
//
// 跑法：node tests/test_settings_scroll_js.mjs

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const appJs = readFileSync(join(here, "..", "sdm", "gui", "assets", "app.js"), "utf-8");

// -- 把被测函数抠出来 ---------------------------------------------

function extractFunction(name) {
  const start = appJs.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `app.js 里找不到 ${name}`);
  let depth = 0;
  let i = appJs.indexOf("{", start);
  for (; i < appJs.length; i++) {
    if (appJs[i] === "{") depth++;
    else if (appJs[i] === "}") {
      depth--;
      if (depth === 0) {
        return appJs.slice(start, i + 1);
      }
    }
  }
  throw new Error(`${name} 的大括号没配对`);
}

const captureSrc = extractFunction("captureScrollTop");
const restoreSrc = extractFunction("restoreScrollTop");

// requestAnimationFrame 在 Node 里没有。记下调用次数就够了。
let rafCalls = 0;
const factory = new Function(
  "requestAnimationFrame",
  `${captureSrc}\n${restoreSrc}\nreturn { captureScrollTop, restoreScrollTop };`
);
const { captureScrollTop, restoreScrollTop } = factory(() => { rafCalls++; });

// -- 假的 DOM -----------------------------------------------------

// 一个只实现了这两个方法就够用的容器。
function fakeView(scrollTop) {
  return {
    querySelector(selector) {
      if (selector !== ".list-wrap") return null;
      return { scrollTop };
    },
  };
}

// -- 用例 ---------------------------------------------------------

let passed = 0;
function check(name, fn) {
  try {
    fn();
    passed++;
    console.log(`  ✓ ${name}`);
  } catch (err) {
    console.error(`  ✗ ${name}`);
    console.error(`    ${err.message}`);
    process.exitCode = 1;
  }
}

check("记下当前滚动位置", () => {
  assert.equal(captureScrollTop(fakeView(420)), 420);
});

check("没有滚动容器时记成 0", () => {
  assert.equal(captureScrollTop({ querySelector: () => null }), 0);
});

check("容器是 null 时记成 0", () => {
  assert.equal(captureScrollTop(null), 0);
});

check("重画后把滚动位置放回去", () => {
  const fresh = { scrollTop: 0 };
  restoreScrollTop(fresh, 420);
  assert.equal(fresh.scrollTop, 420, "同步就该设上");
});

check("下一帧再设一次（内容撑高后不截短）", () => {
  rafCalls = 0;
  const fresh = { scrollTop: 0 };
  restoreScrollTop(fresh, 420);
  assert.equal(rafCalls, 1, "应该排一次 requestAnimationFrame");
});

check("位置是 0 时不动它", () => {
  rafCalls = 0;
  const fresh = { scrollTop: 0 };
  restoreScrollTop(fresh, 0);
  assert.equal(fresh.scrollTop, 0);
  assert.equal(rafCalls, 0, "本来就在顶部，不用重设");
});

check("容器是 null 时不炸", () => {
  restoreScrollTop(null, 100);
});

// -- 端到端：重画一次，位置不丢 -----------------------------------

check("整轮重画后滚动位置不丢", () => {
  // 模拟：旧容器滚到 420 → 重画（换新容器）→ 位置放回新容器
  const oldView = fakeView(420);
  const saved = captureScrollTop(oldView);

  const newView = { scrollTop: 0 };  // 重画出来的新容器
  restoreScrollTop(newView, saved);

  assert.equal(newView.scrollTop, 420, "重画后位置丢了");
});

console.log(`\n${passed} 个用例通过`);
