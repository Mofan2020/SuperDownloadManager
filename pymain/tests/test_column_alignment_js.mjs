// 表头与数据行的列对齐测试（无头 Chromium）。
//
// 背景：`.list-head` 原来用 flex 排——`.sort-header{flex:1}` +
// `.col-picker-btn{flex:none;width:26px}` + `gap:10px`，表头因此
// 比数据行**窄 36px**。表头和数据行共用同一套 grid 模板
// （`--row-cols`，name 列是 1fr），宽度一差，name 之后每一列
// 整体错开 36px：表头和行对不上，排序按钮点到的也不是它下面
// 那一列。
//
// 这里用真的浏览器量**像素位置**——纯 CSS 断言看不出「差 36px」
// 这种问题，量出来的 left 才是证据。
//
// 跑法：node tests/test_column_alignment_js.mjs

import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const assetsDir = join(here, "..", "sdm", "gui", "assets");
// 真实文案。表头标签的宽度要用真文案量——用 i18n 的 key
// （"listColumnEta" 这种）会宽出一大截，量出来的溢出是假的。
const strings = JSON.parse(
  readFileSync(join(here, "..", "sdm", "i18n", "zh-Hans.json"), "utf-8"),
);

// -- 找 playwright -------------------------------------------------
//
// 本机装在全局 node_modules 里（`npm i -g playwright`），
// 项目里没有 node_modules。找不到就跳过——CI 上没装浏览器，
// 不能因为这个把整个测试套挂掉。
async function loadPlaywright() {
  // 全局装的 playwright 是 CommonJS 包，用 require 加载（ESM 的
  // import() 不认目录路径）。
  const { createRequire } = await import("node:module");
  const require = createRequire(import.meta.url);
  const candidates = [
    "playwright",
    "/opt/homebrew/lib/node_modules/playwright",
    "/usr/local/lib/node_modules/playwright",
  ];
  for (const candidate of candidates) {
    try {
      const mod = require(candidate);
      if (mod && mod.chromium) return mod;
    } catch (err) {
      // 换下一个。
    }
  }
  return null;
}

const playwright = await loadPlaywright();
if (!playwright) {
  console.log("SKIP: 没找到 playwright，跳过列对齐测试");
  process.exit(0);
}

const browser = await playwright.chromium.launch();
let checked = 0;

const FAKE_TASK = {
  id: "t1",
  url: "https://example.com/big-file.zip",
  filename: "big-file.zip",
  state: "downloading",
  bytes_done: 1024,
  total_bytes: 10240,
  speed_bytes_per_sec: 2048,
  category: "archives",
  connecting: false,
};

async function render(page, hiddenColumns) {
  await page.evaluate(({ task, hidden, strings }) => {
    State.strings = strings;
    State.hiddenColumns = hidden;
    State.tasks = [task];
    renderDownloads();
  }, { task: FAKE_TASK, hidden: hiddenColumns, strings });
  // 等 ResizeObserver 把容器宽度写回 State（它会触发一次重画）。
  await page.waitForTimeout(250);
}

const MEASURE = () => {
  const head = document.querySelector(".sort-header");
  const row = document.querySelector(".row");
  if (!head || !row) return null;
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return { left: r.left, right: r.right, width: r.width };
  };
  // 格子**可见文字**的实际位置。
  //
  // 用 Range 量：它给的是文字本身的盒子，不含格子的内边距、
  // 不含 badge 这种「外壳」的 padding。所以表头的「状态」标签
  // 和行里的 badge 胶囊比的是文字对文字，不是胶囊对标签。
  const textBox = (el) => {
    const range = document.createRange();
    range.selectNodeContents(el);
    const r = range.getBoundingClientRect();
    range.detach?.();
    return { left: r.left, right: r.right, width: r.width };
  };
  // 格子**占的那一列**的左右边界。
  //
  // 表头格子 `.sh` 带 `margin:-2px -4px; padding:2px 4px`——盒子
  // 比轨道宽 8px，但内容区（border box 去掉 padding）正好等于轨道。
  // 数据行的格子没有外边距，border box 就是轨道。
  const trackBox = (el) => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const ml = parseFloat(cs.marginLeft) || 0;
    const mr = parseFloat(cs.marginRight) || 0;
    return { left: r.left - ml, right: r.right + mr };
  };
  return {
    head: rect(head),
    row: rect(row),
    headTracks: getComputedStyle(head).gridTemplateColumns,
    rowTracks: getComputedStyle(row).gridTemplateColumns,
    headGap: getComputedStyle(head).columnGap,
    rowGap: getComputedStyle(row).columnGap,
    // 表头每格：轨道边界 + 文字位置 + 实际对齐方向 + 有没有文字
    //
    // 「实际对齐方向」要看**生效后的样式**，不能看 class：列选择器
    // 关掉「操作」列时，最后一列会被 CSS 改成左对齐（给浮在右端
    // 的列选择器按钮腾地方），class 还是 .right。
    headCells: [...head.children].map((el) => ({
      ...trackBox(el),
      text: el.textContent.trim(),
      align:
        getComputedStyle(el).justifyContent === "flex-end" ? "right" : "left",
      label: textBox(el),
    })),
    // 数据行每格：轨道边界 + 文字位置 + 实际对齐方向
    //
    // `inset` = 这一格自己带左右内边距（状态那格的 badge 胶囊就
    // 带 9px padding）。带内边距的格子，文字天然比格子窄一截——
    // 那是胶囊自己的设计，文字对文字比没有意义，改成比轨道
    // （上面第 3 条已经比过了）。
    rowCells: [...row.children].map((el) => ({
      ...trackBox(el),
      align: getComputedStyle(el).textAlign === "right" ? "right" : "left",
      inset: (parseFloat(getComputedStyle(el).paddingLeft) || 0) > 0,
      label: textBox(el),
    })),
    picker: (() => {
      const btn = document.querySelector(".col-picker-btn");
      if (!btn) return null;
      const r = btn.getBoundingClientRect();
      return { left: r.left, right: r.right, top: r.top, bottom: r.bottom };
    })(),
  };
};

try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 900 } });
  await page.goto(`file://${join(assetsDir, "index.html")}`);

  // 两种列组合都测：默认全开，以及用户把「操作」列关掉。
  //
  // 「操作」列关掉时列选择器按钮会浮到表头最右端——那时候它压着
  // 的是「剩余时间」表头，所以那一格要给它让出位置（表头和数据行
  // 一起让）。这个组合必须单独测，不然改了 CSS 也不知道有没有弄坏。
  for (const hidden of [[], ["actions"]]) {
    const label = hidden.length ? "隐藏操作列" : "默认列";
    await render(page, hidden);
    const measured = await page.evaluate(MEASURE);
    assert.ok(measured, `${label}：列表没画出来（找不到 .sort-header / .row）`);
    assert.equal(
      measured.headCells.length,
      measured.rowCells.length,
      `${label}：表头 ${measured.headCells.length} 格、数据行 ${measured.rowCells.length} 格——列数都不一样`,
    );

    // 1. 两边的 grid 轨道必须一模一样。
    //    轨道不一样 = 列宽不一样 = 后面每一列都错位。
    assert.equal(
      measured.headTracks,
      measured.rowTracks,
      `${label}：表头轨道 ${measured.headTracks} ≠ 数据行轨道 ${measured.rowTracks}`,
    );
    assert.equal(
      measured.headGap,
      measured.rowGap,
      `${label}：表头间距 ${measured.headGap} ≠ 数据行间距 ${measured.rowGap}`,
    );
    checked += 1;

    // 2. 表头的宽度必须跟数据行一样。
    //    这是错位的根源：宽度不同 → 1fr 的 name 列把后面的列全推偏。
    const widthDiff = Math.abs(measured.head.width - measured.row.width);
    assert.ok(
      widthDiff <= 1,
      `${label}：表头宽 ${measured.head.width.toFixed(1)}px，数据行宽 ${measured.row.width.toFixed(1)}px，差 ${widthDiff.toFixed(1)}px`,
    );
    checked += 1;

    // 3. 每一列的**轨道边界**必须对得上（容差 1px）。
    //    这一条就是「表头和数据行是同一张表」的定义。
    for (let i = 0; i < measured.headCells.length; i++) {
      const h = measured.headCells[i];
      const r = measured.rowCells[i];
      const leftDiff = Math.abs(h.left - r.left);
      const rightDiff = Math.abs(h.right - r.right);
      assert.ok(
        leftDiff <= 1 && rightDiff <= 1,
        `${label}：第 ${i + 1} 列的轨道对不齐——表头 [${h.left.toFixed(1)}, ${h.right.toFixed(1)}]，数据行 [${r.left.toFixed(1)}, ${r.right.toFixed(1)}]`,
      );
    }
    checked += 1;

    // 4. 表头标签和数据必须共用同一个边缘。
    //    两边都右对齐就比右边缘，都左对齐就比左边缘；
    //    对齐方向不一样（理论上不该出现）就比左边缘。
    for (let i = 0; i < measured.headCells.length; i++) {
      const h = measured.headCells[i];
      const r = measured.rowCells[i];
      if (!h.text) continue; // 表头这一格没标签（「操作」列），没得比
      if (r.inset) continue; // 胶囊格子：文字本来就内缩，比轨道就够了
      const useRight = h.align === "right" && r.align === "right";
      const diff = useRight
        ? Math.abs(h.label.right - r.label.right)
        : Math.abs(h.label.left - r.label.left);
      const what = useRight ? "右边缘" : "左边缘";
      assert.ok(
        diff <= 1,
        `${label}：第 ${i + 1} 列的表头标签和数据没对齐——标签${what} ${(useRight ? h.label.right : h.label.left).toFixed(1)}，数据${what} ${(useRight ? r.label.right : r.label.left).toFixed(1)}，差 ${diff.toFixed(1)}px`,
      );
    }
    checked += 1;

    // 4. 列选择器按钮不能跑出表头，也不能压住最后一列的文字。
    assert.ok(measured.picker, `${label}：找不到列选择器按钮`);
    assert.ok(
      measured.picker.right <= measured.head.right + 1,
      `${label}：列选择器按钮跑出表头右边界（按钮 ${measured.picker.right.toFixed(1)}，表头 ${measured.head.right.toFixed(1)}）`,
    );
    assert.ok(
      measured.picker.left >= measured.head.left,
      `${label}：列选择器按钮跑到表头左边去了`,
    );
    // 按钮不能盖住任何一列表头的**文字**。
    // 「操作」列表头本来就是空的（按钮正好压在上面），空的跳过。
    for (let i = 0; i < measured.headCells.length; i++) {
      const h = measured.headCells[i];
      if (!h.text) continue;
      const overlap =
        Math.min(h.label.right, measured.picker.right) -
        Math.max(h.label.left, measured.picker.left);
      assert.ok(
        overlap <= 1,
        `${label}：列选择器按钮压住了第 ${i + 1} 列的表头文字「${h.text}」（重叠 ${overlap.toFixed(1)}px）`,
      );
    }
    checked += 1;
  }
} finally {
  await browser.close();
}

console.log(`${checked} 个用例通过`);
