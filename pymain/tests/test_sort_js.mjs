// F076 列排序的逻辑测试。
//
// app.js 是浏览器脚本，没法直接 import（它依赖 DOM）。这里把
// sortTasks / toggleSort 的源码抠出来在 Node 里跑——测的是真
// 实现，不是复制一份。
//
// 跑法：node tests/test_sort_js.mjs

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
  // 从 function 关键字开始，扫到配对的收尾大括号
  let depth = 0;
  let i = appJs.indexOf("{", start);
  const bodyStart = i;
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

// State 只需要 sortKey / sortAsc 两个字段。
const stateSrc = extractFunction("sortTasks");
const toggleSrc = extractFunction("toggleSort");

const State = { sortKey: null, sortAsc: true };
// toggleSort 里会调 renderDownloads 重绘——这里 stub 掉，只测排序状态。
const factory = new Function(
  "State",
  "renderDownloads",
  `${stateSrc}\n${toggleSrc}\nreturn { sortTasks, toggleSort };`
);
const { sortTasks, toggleSort } = factory(State, () => {});

// -- 测试数据 -----------------------------------------------------

const tasks = () => [
  { id: "1", filename: "banana.zip", state: "completed", total_bytes: 300, speed_bytes_per_sec: 50 },
  { id: "2", filename: "apple.zip", state: "downloading", total_bytes: 100, speed_bytes_per_sec: 900 },
  { id: "3", filename: "cherry.zip", state: "failed", total_bytes: null, speed_bytes_per_sec: 0 },
  { id: "4", filename: "date.zip", state: "queued", total_bytes: 200, speed_bytes_per_sec: 300 },
];

const ids = (list) => list.map((t) => t.id);

// -- 用例 ---------------------------------------------------------

let passed = 0;
function check(name, fn) {
  try {
    fn();
    passed++;
    console.log(`  ✓ ${name}`);
  } catch (err) {
    console.log(`  ✗ ${name}\n    ${err.message}`);
    process.exitCode = 1;
  }
}

console.log("F076 列排序");

check("按文件名升序（apple banana cherry date）", () => {
  assert.deepEqual(ids(sortTasks(tasks(), "filename", true)), ["2", "1", "3", "4"]);
});

check("按文件名降序", () => {
  assert.deepEqual(ids(sortTasks(tasks(), "filename", false)), ["4", "3", "1", "2"]);
});

check("按大小升序", () => {
  assert.deepEqual(ids(sortTasks(tasks(), "size", true)), ["2", "4", "1", "3"]);
});

check("按大小降序", () => {
  assert.deepEqual(ids(sortTasks(tasks(), "size", false)), ["1", "4", "2", "3"]);
});

check("未知大小沉底（升序）", () => {
  const sorted = sortTasks(tasks(), "size", true);
  assert.equal(sorted[sorted.length - 1].id, "3", "未知大小的应该在最后");
});

check("未知大小沉底（降序）", () => {
  const sorted = sortTasks(tasks(), "size", false);
  assert.equal(sorted[sorted.length - 1].id, "3", "未知大小的应该在最后");
});

check("按速度升序", () => {
  assert.deepEqual(ids(sortTasks(tasks(), "speed", true)), ["3", "1", "4", "2"]);
});

check("按速度降序", () => {
  assert.deepEqual(ids(sortTasks(tasks(), "speed", false)), ["2", "4", "1", "3"]);
});

check("按状态：下载中排最前，已完成排最后", () => {
  // stateOrder: downloading 0, queued 1, failed 4, completed 5
  const sorted = sortTasks(tasks(), "state", true);
  assert.deepEqual(ids(sorted), ["2", "4", "3", "1"]);
});

check("排序不改动原数组", () => {
  const original = tasks();
  const copy = original.map((t) => t.id);
  sortTasks(original, "filename", true);
  assert.deepEqual(original.map((t) => t.id), copy, "原数组被改了");
});

check("点同一个表头换升降序", () => {
  State.sortKey = null;
  State.sortAsc = true;
  toggleSort("filename");
  assert.equal(State.sortKey, "filename");
  assert.equal(State.sortAsc, true, "第一次点应该是升序");
  toggleSort("filename");
  assert.equal(State.sortAsc, false, "再点一次应该变降序");
  toggleSort("filename");
  assert.equal(State.sortAsc, true, "第三次点回升序");
});

check("点不同表头重置为升序", () => {
  State.sortKey = "filename";
  State.sortAsc = false;
  toggleSort("size");
  assert.equal(State.sortKey, "size");
  assert.equal(State.sortAsc, true, "换表头应该回升序");
});

check("未知大小沉底（unknown 在数组中间）", () => {
  const list = [
    { id: "u", filename: "u.zip", state: "queued", total_bytes: null },
    { id: "s", filename: "s.zip", state: "queued", total_bytes: 50 },
    { id: "m", filename: "m.zip", state: "queued", total_bytes: 500 },
  ];
  const asc = ids(sortTasks(list, "size", true));
  assert.deepEqual(asc, ["s", "m", "u"], "未知大小必须沉底");
  const desc = ids(sortTasks(list, "size", false));
  assert.deepEqual(desc, ["m", "s", "u"], "未知大小必须沉底（降序）");
});

check("文件名排序区分大小写（localeCompare 语义）", () => {
  // localeCompare 在多数 locale 下把 "apple" 排在 "Banana" 前面
  // （小写优先于大写，locale 规则），裸 ">" 比较则是 "Banana" 在前。
  // 两种排序结果不同——这个用例能区分两者。
  const list = [
    { id: "u", filename: "apple", state: "queued", total_bytes: 1 },
    { id: "l", filename: "Banana", state: "queued", total_bytes: 1 },
  ];
  const sorted = ids(sortTasks(list, "filename", true));
  assert.deepEqual(sorted, ["u", "l"], "localeCompare 语义：apple 在 Banana 前");
});

check("文件名排序用本地化规则（中文）", () => {
  const list = [
    { id: "a", filename: "阿尔法", state: "queued", total_bytes: 1 },
    { id: "b", filename: "beta", state: "queued", total_bytes: 1 },
    { id: "c", filename: "伽马", state: "queued", total_bytes: 1 },
  ];
  const asc = ids(sortTasks(list, "filename", true));
  const desc = ids(sortTasks(list, "filename", false));

  // **不硬编码 collation 结果**——不同 ICU 环境下 localeCompare
  // 的排序可能不同（macOS Node 带完整 ICU，容器里可能退化成码点序）。
  // 这里只断言：元素不丢、升降序互为逆序。
  assert.equal(asc.length, 3);
  assert.equal(desc.length, 3);
  assert.deepEqual([...asc].sort(), ["a", "b", "c"], "有元素丢了");
  assert.deepEqual([...desc].sort(), ["a", "b", "c"], "有元素丢了");
  assert.notDeepEqual(asc, desc, "升降序结果不该一样");
});

console.log(`\n${passed} 个用例通过`);
