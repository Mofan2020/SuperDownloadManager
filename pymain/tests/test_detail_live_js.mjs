// 详情面板的分片明细必须实时更新。
//
// 背景：用户 2026-10-29 报的
// 「详情页分段显示不实时更新，首次点开详情页时不会实时更新，
//  必须关掉详情页再重新点开才正常更新」。
//
// 根因：列表轮询每 1.5 秒把 State.tasks 整个换掉并调 render()，
// 而分片明细只在「打开详情」时取一次（loadTaskDetail）——面板
// 一直在重画一张过期的分片表。关掉重开会重新取数，所以「正常」。
//
// app.js 是浏览器脚本，没法直接 import（它依赖 DOM）。这里把
// refreshTasks / fetchTaskDetail 的源码抠出来在 Node 里跑——
// 测的是真实现，不是复制一份。
//
// 跑法：node tests/test_detail_live_js.mjs

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const appJs = readFileSync(join(here, "..", "sdm", "gui", "assets", "app.js"), "utf-8");

// -- 把被测代码抠出来 ---------------------------------------------

// 抠出一个 `function X(...) {...}`（`async function` 也认）。
function extractFunction(name) {
  const start = appJs.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `app.js 里找不到 function ${name}`);
  let depth = 0;
  let i = appJs.indexOf("{", start);
  for (; i < appJs.length; i++) {
    if (appJs[i] === "{") depth++;
    else if (appJs[i] === "}") {
      depth--;
      if (depth === 0) {
        const src = appJs.slice(start, i + 1);
        // 前面如果有 `async `，把它带上——抠掉 async 的话函数体里
        // 的 `await` 就是语法错误。
        const before = appJs.slice(Math.max(0, start - 6), start);
        return before.endsWith("async ") ? "async " + src : src;
      }
    }
  }
  throw new Error(`${name} 的大括号没配对`);
}

const FN_SRC = [
  extractFunction("fetchTaskDetail"),
  extractFunction("refreshTasks"),
].join("\n");

// -- 测试脚手架 ---------------------------------------------------

/**
 * 造一套假的 State / api，把 app.js 的真函数装进来跑。
 *
 * `getTaskDetail` 可以传自定义实现——用来模拟「下载推进了，
 * 后端给的新分片明细变了」。
 */
function makeEnv({ selectedTaskId, chunks, detailCalls, getTaskDetail }) {
  const State = {
    selectedTaskId,
    tasks: [{ id: "t1", filename: "a.bin" }],
    taskChunks: {},
    taskErrors: {},
    speedHistory: {},
    speedSamples: {},
  };
  const api = () => ({
    get_tasks: async () => State.tasks,
    get_task_detail: async () => {
      detailCalls.count += 1;
      if (getTaskDetail) return getTaskDetail();
      return { chunks, error: null, speed_history: [] };
    },
  });
  const factory = new Function(
    "State",
    "api",
    "render",
    "recordSpeedSample",
    `${FN_SRC}\nreturn { refreshTasks, fetchTaskDetail };`,
  );
  const { refreshTasks, fetchTaskDetail } = factory(
    State,
    api,
    () => {},
    () => {},
  );
  return { State, refreshTasks, fetchTaskDetail };
}

let passed = 0;
function ok(name) {
  passed++;
  console.log(`  ✓ ${name}`);
}

// -- 用例 ---------------------------------------------------------

// 1. 详情面板开着的时候，轮询必须顺带刷新分片明细。
{
  const detailCalls = { count: 0 };
  const chunks = [{ id: 0, size: 100, bytes_done: 50 }];
  const { State, refreshTasks } = makeEnv({
    selectedTaskId: "t1",
    chunks,
    detailCalls,
  });
  await refreshTasks();
  assert.equal(detailCalls.count, 1, "轮询没有去取详情");
  assert.deepEqual(
    State.taskChunks["t1"],
    chunks,
    "分片明细没写进 State.taskChunks",
  );
  ok("轮询会刷新打开着的详情页的分片明细");
}

// 2. 没有打开详情的时候，不要多打后端。
{
  const detailCalls = { count: 0 };
  const { refreshTasks } = makeEnv({
    selectedTaskId: null,
    chunks: [],
    detailCalls,
  });
  await refreshTasks();
  assert.equal(detailCalls.count, 0, "没打开详情却去取详情了");
  ok("没打开详情时不请求详情接口");
}

// 3. 轮询拿到的必须是**新**的分片明细（不能是上一次打开时的）。
{
  const detailCalls = { count: 0 };
  let current = [{ id: 0, size: 100, bytes_done: 10 }];
  const { State, refreshTasks } = makeEnv({
    selectedTaskId: "t1",
    chunks: current,
    detailCalls,
    getTaskDetail: () => ({
      chunks: current,
      error: null,
      speed_history: [],
    }),
  });
  await refreshTasks();
  assert.equal(State.taskChunks["t1"][0].bytes_done, 10);

  // 下载推进了，后端给的新明细不一样了
  current = [{ id: 0, size: 100, bytes_done: 90 }];
  await refreshTasks();
  assert.equal(
    State.taskChunks["t1"][0].bytes_done,
    90,
    "分片明细还是打开详情时的那份旧数据",
  );
  ok("轮询会把旧的分片明细换成新的");
}

// 4. 打开详情的那条路也必须还能用。
{
  const detailCalls = { count: 0 };
  const chunks = [{ id: 3, size: 42, bytes_done: 7 }];
  const { State, fetchTaskDetail } = makeEnv({
    selectedTaskId: "t1",
    chunks,
    detailCalls,
  });
  await fetchTaskDetail("t1");
  assert.deepEqual(State.taskChunks["t1"], chunks);
  ok("打开详情时照样能取到分片明细");
}

console.log(`\n${passed} 个用例通过`);
