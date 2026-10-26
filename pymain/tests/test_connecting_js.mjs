// 「连接中」的显示逻辑测试。
//
// 背景：任务开跑到收到第一个字节之间，速度确实是 0，但界面写
// 「0 KB/s」会让人以为卡死了——用户 2026-10-25 报的「下载一直
// 卡在 0kb」就是这段。这段必须显示「连接中」。
//
// app.js 是浏览器脚本，没法直接 import（它依赖 DOM）。这里把
// stateMetaFor / speedCellText 的源码抠出来在 Node 里跑——
// 测的是真实现，不是复制一份。
//
// 跑法：node tests/test_connecting_js.mjs

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

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

// 抠出一个 `function X(...) {...}`。
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

// 文案表：跟真实 i18n 一样，键不存在就返回键本身。
const strings = {
  stateConnecting: "连接中",
  stateDownloading: "下载中",
  stateQueued: "等待中",
  stateCompleted: "已完成",
};
const t = (key) => (strings[key] !== undefined ? strings[key] : key);

// fmtRate 也要抠——speedCellText 用它。
const fmtRateSrc = extractFunction("fmtRate");

const factory = new Function(
  "t",
  [
    extractConst("STATE_META"),
    extractConst("CONNECTING_META"),
    fmtRateSrc,
    extractFunction("stateMetaFor"),
    extractFunction("speedCellText"),
    "return { stateMetaFor, speedCellText, STATE_META, CONNECTING_META };",
  ].join("\n"),
);
const { stateMetaFor, speedCellText } = factory(t);

// -- 断言 ---------------------------------------------------------

// 1. 连接中：徽标和速度格都要说「连接中」。
{
  const task = { state: "downloading", connecting: true, speed_bytes_per_sec: 0 };
  assert.equal(stateMetaFor(task).key, "stateConnecting", "徽标没切到「连接中」");
  assert.equal(speedCellText(task), "连接中", "速度格还在显示 0 KB/s");
}

// 2. 收到字节之后：回到正常的下载中 + 真实速度。
{
  const task = { state: "downloading", connecting: false, speed_bytes_per_sec: 2048 };
  assert.equal(stateMetaFor(task).key, "stateDownloading");
  assert.equal(speedCellText(task), "2.0 KB/s");
}

// 3. 连接中的任务速度是 0，但显示的**不是** 0 KB/s——
//    这是整个改动的核心：0 和「还没开始」必须区分开。
{
  const task = { state: "downloading", connecting: true, speed_bytes_per_sec: 0 };
  const text = speedCellText(task);
  assert.ok(!/KB\/s|B\/s/.test(text), `速度格显示了速率：${text}`);
}

// 4. 别的状态不受影响。
{
  assert.equal(stateMetaFor({ state: "queued" }).key, "stateQueued");
  assert.equal(stateMetaFor({ state: "completed" }).key, "stateCompleted");
  assert.equal(speedCellText({ state: "queued", speed_bytes_per_sec: 0 }), "—");
  assert.equal(stateMetaFor({ state: "downloading", connecting: false }).key, "stateDownloading");
}

// 5. connecting 只在「下载中」时才有意义——别的状态不能被它带偏。
{
  assert.equal(stateMetaFor({ state: "paused", connecting: true }).key, "statePaused");
  assert.equal(stateMetaFor({ state: "failed", connecting: true }).key, "stateFailed");
}

console.log("5 个用例通过");
