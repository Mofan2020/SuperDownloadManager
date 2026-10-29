# SuperDownloadManager — Python 重构版 UI 设计规范

> 目标实现栈：Python + pywebview（macOS = WKWebView，Windows = WebView2）+ HTML/CSS/JS 前端。
> 本文档是前端实现的唯一依据：所有尺寸、颜色、断点、状态文案均已给出可直接落地的数值。
> 本规范同时是前端实现的唯一依据：所有尺寸、颜色、断点、状态文案均已给出
> 可直接落地的数值。语义（状态名、分类、按钮文案）沿用项目早期的 macOS 原生版，
> 载体换成 DOM。

---

## 目录

0. [全局约定](#0-全局约定)
1. [设计令牌](#1-设计令牌)
2. [布局骨架](#2-布局骨架)
3. [响应式规则](#3-响应式规则)
4. [组件规格](#4-组件规格)
5. [状态映射](#5-状态映射)
6. [交互细则](#6-交互细则)
7. [无障碍与键盘](#7-无障碍与键盘)
8. [实现最容易踩的三个布局点](#8-实现最容易踩的三个布局点)

---

## 0. 全局约定

### 0.1 技术前提

| 项 | 决策 | 理由 |
|---|---|---|
| 窗口框架 | **保留系统原生标题栏**（pywebview `create_window(..., frameless=False)`） | macOS 的红绿灯、窗口拖拽、双击标题栏缩放、全屏全部原生可靠；自绘标题栏需要自行实现 traffic lights 与 resize handle，收益低、风险高 |
| 沉浸式模式（备选） | 若后续需要内容延伸到标题栏下方，改用 `frameless=True` + 标题栏 `position: fixed` + 拖拽区，见 §2.2.2 | 预留但不默认启用 |
| 默认窗口尺寸 | `width=1180, height=760` | 落在 L 断点，全列可见 |
| 最小窗口尺寸 | `min_size=(640, 440)` | 见 §3.8 |
| 设置窗口 | 独立 pywebview 窗口，`width=720, height=560, min_size=(560, 440)` | 与主窗口解耦，避免主窗口最小尺寸被设置页绑架 |
| 背景色 | `background_color="#EEF1F6"` | 与亮色模式 `--bg-app` 一致，避免 WebView 白闪 |
| 缩放 | `text-size-adjust: 100%`，禁止用户缩放影响布局 | WKWebView / WebView2 默认不缩放，但显式声明避免移动端 UA 误判 |

### 0.2 断点命名

| 名称 | 窗口宽度区间 | 用途 |
|---|---|---|
| `XS` | 640 – 879 px | 侧栏收成图标轨，列表高度压缩 |
| `S` | 880 – 1039 px | 侧栏窄版，列表 6 列 |
| `M` | 1040 – 1179 px | 侧栏中宽，列表 7 列 |
| `L` | 1180 – 1439 px | 完整 9 列 |
| `XL` | ≥ 1440 px | 同 L，列表内容最大宽 1400 px 居中 |

断点按 **窗口外框宽度**（`window.innerWidth`，含系统标题栏与左右边框）计算，不用容器宽度 —— 否则侧栏折叠会改变断点判定，产生抖动。

CSS 侧统一用 `@media (max-width: 879px)` … `@media (min-width: 1440px)` 实现（用 `min-width` 向上覆盖，避免 `max-width` 优先级打架）。

### 0.3 图标规范

- **不使用 emoji**，一律使用 Feather 风格线性 SVG。
- 通用参数：`viewBox="0 0 24 24"`，`width/height` 由 CSS 控制，`fill="none"`，`stroke="currentColor"`，`stroke-width="2"`，`stroke-linecap="round"`，`stroke-linejoin="round"`。
- 尺寸档位：`sm = 14px`（徽章内、行内小标记）、`md = 16px`（按钮、菜单）、`lg = 20px`（工具栏主按钮）、`xl = 28px`（空状态、详情头）。
- 图标颜色继承 `currentColor`，跟随文字色；**禁止**给图标单独写死颜色（唯一例外：限速标记恒为 `--warning`，分类图标恒为分类色）。
- 实现方式：单个 `<svg>` sprite（`<symbol id="i-xxx">`）内联在 `index.html` 顶部，图标引用 `<svg class="icon"><use href="#i-download"/></svg>`。
- 完整图标清单与 Feather 原始 path 见 §4.23。

> **本文档所有 ASCII 示意图中的符号（`o` `#` `-` `=` `^` `$` `>` `@` `v` `x` `!` `‖` `◎` `▓░`）都只是占位符**，实际渲染一律替换为对应的 Feather SVG。规范正文不使用 emoji 作为界面图标。

### 0.4 文案与语言

- 双语：English / 简体中文，跟随系统。语言切换 **不重启**（前端持有 `locale` 状态，所有文案走 `t(key)`）。
- 文案 key 沿用项目早期的语义命名，落成 `locale.py` 字典：`{"en": {...}, "zh-Hans": {...}}`。
- 数字、字节、时间统一用 `Intl.NumberFormat` / 自写格式化：
  - 字节：`1 KB = 1024 B`，`MB`、`GB` 保留 1 位小数，`< 1024` 显示 `512 B`。
  - 速度：`B/s`、`KB/s`、`MB/s`（1 位小数），恒定宽度 → 用 `font-variant-numeric: tabular-nums`。
  - 剩余时间：`45s`、`3m 20s`、`1h 05m`。
- 文案里不得出现 `…` 之外的省略风格混用：截断用 CSS `text-overflow: ellipsis`，菜单项固定写 `…`。

### 0.5 全局反馈

所有非阻塞操作（复制链接、复制 JSON、限速已应用、已移除）通过右上角 **Toast** 反馈，停留 2.4 s。

```
Toast 尺寸：min-width 220px，max-width 380px，padding 10px 14px
圆角 10px，背景 var(--glass-bg-strong)，backdrop-filter blur(18px) saturate(160%)
阴影 var(--shadow-3)，左上角 16px 图标（check-circle / alert-triangle / info）
最多同时 3 条，堆叠间距 8px，从顶部右侧 24px 处滑入 160ms
```

---

## 1. 设计令牌

### 1.1 颜色 — 亮色（默认）

```css
:root {
  color-scheme: light;

  /* —— 应用背景（三段渐变 + 两团模糊色斑，制造玻璃折射的内容）—— */
  --bg-app:
    radial-gradient(1200px 600px at 12% -10%, rgba(59,130,246,0.16), transparent 60%),
    radial-gradient(900px 520px at 92% 8%, rgba(168,85,247,0.14), transparent 62%),
    linear-gradient(160deg, #F4F6FB 0%, #E9EEF7 48%, #E2E9F4 100%);
  --bg-app-solid: #EEF1F6;          /* pywebview background_color 用的实色 */

  /* —— 玻璃层 —— */
  --glass-bg:        rgba(255,255,255,0.55);
  --glass-bg-strong: rgba(255,255,255,0.78);
  --glass-bg-faint:  rgba(255,255,255,0.32);
  --glass-blur: 24px;
  --glass-sat: 180%;

  /* —— 描边 —— */
  --border-subtle: rgba(15,23,42,0.08);
  --border-default: rgba(15,23,42,0.12);
  --border-strong: rgba(15,23,42,0.20);
  --border-glass: rgba(255,255,255,0.65);   /* 玻璃卡内侧高光 */

  /* —— 文本 —— */
  --text-primary:   #0F172A;
  --text-secondary: #475569;
  --text-tertiary:  #8A97AC;
  --text-disabled:  #B6C0CE;
  --text-on-accent: #FFFFFF;

  /* —— 交互色 —— */
  --accent:        #2563EB;
  --accent-hover:  #1D4ED8;
  --accent-active: #1E40AF;
  --accent-soft:   rgba(37,99,235,0.12);
  --accent-softer: rgba(37,99,235,0.07);
  --focus-ring:    rgba(37,99,235,0.45);

  --danger:        #DC2626;
  --danger-hover:  #B91C1C;
  --danger-soft:   rgba(220,38,38,0.12);
  --warning:       #D97706;
  --warning-soft:  rgba(217,119,6,0.14);
  --success:       #16A34A;
  --success-soft:  rgba(22,163,74,0.13);
  --neutral:       #64748B;
  --neutral-soft:  rgba(100,116,139,0.12);

  /* —— 输入控件 —— */
  --input-bg: rgba(255,255,255,0.72);
  --input-bg-disabled: rgba(148,163,184,0.18);
  --track-bg: rgba(15,23,42,0.10);

  /* —— 阴影 —— */
  --shadow-1: 0 1px 2px rgba(15,23,42,0.06);
  --shadow-2: 0 4px 16px rgba(15,23,42,0.08);
  --shadow-3: 0 12px 40px rgba(15,23,42,0.14);
  --shadow-pop: 0 16px 48px rgba(15,23,42,0.22);
  --shadow-inset-top: inset 0 1px 0 rgba(255,255,255,0.60);
}
```

### 1.2 颜色 — 暗色

```css
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;

    --bg-app:
      radial-gradient(1200px 600px at 12% -10%, rgba(37,99,235,0.22), transparent 60%),
      radial-gradient(900px 520px at 92% 8%, rgba(147,51,234,0.18), transparent 62%),
      linear-gradient(160deg, #0C1222 0%, #111A2E 48%, #0A101E 100%);
    --bg-app-solid: #0C1222;

    --glass-bg:        rgba(24,32,52,0.55);
    --glass-bg-strong: rgba(20,27,45,0.82);
    --glass-bg-faint:  rgba(24,32,52,0.32);
    --glass-blur: 24px;
    --glass-sat: 170%;

    --border-subtle: rgba(255,255,255,0.07);
    --border-default: rgba(255,255,255,0.12);
    --border-strong: rgba(255,255,255,0.20);
    --border-glass: rgba(255,255,255,0.10);

    --text-primary:   #E9EEF8;
    --text-secondary: #A3B0C6;
    --text-tertiary:  #6E7D96;
    --text-disabled:  #4A5568;
    --text-on-accent: #FFFFFF;

    --accent:        #5B8DEF;
    --accent-hover:  #7BA4F5;
    --accent-active: #4A7BE0;
    --accent-soft:   rgba(91,141,239,0.18);
    --accent-softer: rgba(91,141,239,0.10);
    --focus-ring:    rgba(123,164,245,0.55);

    --danger:        #F87171;
    --danger-hover:  #FCA5A5;
    --danger-soft:   rgba(248,113,113,0.16);
    --warning:       #FBBF24;
    --warning-soft:  rgba(251,191,36,0.16);
    --success:       #34D399;
    --success-soft:  rgba(52,211,153,0.15);
    --neutral:       #94A3B8;
    --neutral-soft:  rgba(148,163,184,0.15);

    --input-bg: rgba(255,255,255,0.06);
    --input-bg-disabled: rgba(255,255,255,0.04);
    --track-bg: rgba(255,255,255,0.12);

    --shadow-1: 0 1px 2px rgba(0,0,0,0.40);
    --shadow-2: 0 4px 16px rgba(0,0,0,0.45);
    --shadow-3: 0 12px 40px rgba(0,0,0,0.55);
    --shadow-pop: 0 16px 48px rgba(0,0,0,0.65);
    --shadow-inset-top: inset 0 1px 0 rgba(255,255,255,0.06);
  }
}

/* 手动主题覆盖（设置项 theme: system | light | dark），优先级高于系统 */
:root[data-theme="light"] { /* 同 §1.1 */ }
:root[data-theme="dark"]  { /* 同 §1.2 */ }
```

主题判定优先级：设置项 `theme` 显式值 > 系统外观。系统外观通过 `matchMedia('(prefers-color-scheme: dark)')` 监听；pywebview 在 macOS 上 WKWebView 的媒体查询跟随系统外观，若实测不跟随，则由 Python 侧读 `NSApp.effectiveAppearance` 后经 `window.evaluate_js` 调用 `window.__sdmSetTheme('dark')` 兜底。

### 1.3 语义色映射表

**下载状态色**（徽章文字/图标用 100% 实色，徽章底用 12–18% 透明度）

| 状态 key | 亮色 | 暗色 | 变量 |
|---|---|---|---|
| `queued` 排队中 | `#64748B` | `#94A3B8` | `--neutral` |
| `downloading` 下载中 | `#2563EB` | `#5B8DEF` | `--accent` |
| `paused` 已暂停 | `#D97706` | `#FBBF24` | `--warning` |
| `completed` 已完成 | `#16A34A` | `#34D399` | `--success` |
| `failed` 失败 | `#DC2626` | `#F87171` | `--danger` |
| `cancelled` 已取消 | `#64748B` | `#94A3B8` | `--neutral` |
| `awaiting_new_link` 等待新链接 | `#EA580C` | `#FB923C` | `--warning-strong`（新增变量：`#EA580C` / `#FB923C`） |

**分类色**（按分类色板的十六进制值）

| 分类 | 图标 | 亮色 | 暗色 |
|---|---|---|---|
| Video 视频 | `video` | `#DB2777` | `#F472B6` |
| Music 音乐 | `music` | `#9333EA` | `#C084FC` |
| Pictures 图片 | `image` | `#0D9488` | `#2DD4BF` |
| Archives 压缩包 | `archive` | `#EA580C` | `#FB923C` |
| Documents 文档 | `file-text` | `#2563EB` | `#60A5FA` |
| Software 软件 | `package` | `#4F46E5` | `#818CF8` |
| Other 其他 | `help-circle` | `#64748B` | `#94A3B8` |

**日志级别色**

| 级别 | 颜色 | 变量 |
|---|---|---|
| DEBUG | `--text-tertiary` | `#8A97AC` / `#6E7D96` |
| INFO | `--text-primary` | `#0F172A` / `#E9EEF8` |
| NOTICE | `#0284C7` | `#0284C7` / `#38BDF8` |
| WARNING | `--warning` | `#D97706` / `#FBBF24` |
| ERROR | `--danger` | `#DC2626` / `#F87171` |

### 1.4 圆角

```css
--radius-xs:   4px;   /* 进度条、圆点 */
--radius-sm:   6px;   /* 芯片 chip、小图标底 */
--radius-md:   8px;   /* 按钮、输入框、行内操作 */
--radius-lg:  12px;   /* 卡片、横幅、弹层内容块 */
--radius-xl:  16px;   /* 弹层 dialog、抽屉 */
--radius-2xl: 20px;   /* 大卡片（空状态容器） */
--radius-pill: 999px; /* 徽章、开关、工具栏胶囊 */
```

### 1.5 间距

4px 基准栅格，只允许以下值：

```
--space-1:  4px
--space-2:  8px
--space-3: 12px
--space-4: 16px
--space-5: 20px
--space-6: 24px
--space-8: 32px
--space-10: 40px
--space-12: 48px
```

常用组合：
- 卡片内边距 `20px`（大）/ `16px`（中）/ `12px`（密集列表项）
- 表单「标签 → 控件」间距 `8px`；不同字段之间 `16px`
- 设置分区之间 `24px`；分区标题与首项之间 `12px`
- 弹层内边距 `24px`；弹层头部与内容 `16px`；内容与底部按钮区 `20px`

### 1.6 字号与行高

```css
--font-sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue",
              "PingFang SC", "Microsoft YaHei", "Noto Sans SC", sans-serif;
--font-mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas,
              "Liberation Mono", monospace;
```

| token | 字号 / 行高 | 字重 | 用途 |
|---|---|---|---|
| `--text-hero` | 22px / 28px | 600 | 空状态标题、弹层大标题 |
| `--text-title` | 17px / 24px | 600 | 页面标题、设置分区标题 |
| `--text-subtitle` | 15px / 21px | 600 | 列表主文本（文件名） |
| `--text-body` | 13px / 19px | 400 | 正文、表单标签 |
| `--text-label` | 12px / 16px | 500 | 次级标签、列头 |
| `--text-caption` | 11px / 15px | 400 | 副文本、主机名、计数 |
| `--text-micro` | 10px / 14px | 600 | 徽章文字（字距 0.02em，大写仅英文） |
| `--text-mono` | 12px / 17px | 400 | URL、路径、JSON、分片范围 |

所有数字列（大小、速度、ETA、百分比、计数）加 `font-variant-numeric: tabular-nums;`。

### 1.7 阴影

| token | 值 | 用途 |
|---|---|---|
| `--shadow-1` | `0 1px 2px rgba(15,23,42,.06)` | 徽章、行内操作按钮底 |
| `--shadow-2` | `0 4px 16px rgba(15,23,42,.08)` | 卡片、工具栏胶囊 |
| `--shadow-3` | `0 12px 40px rgba(15,23,42,.14)` | Toast、右键菜单、抽屉 |
| `--shadow-pop` | `0 16px 48px rgba(15,23,42,.22)` | 模态弹层 |
| `--shadow-inset-top` | `inset 0 1px 0 rgba(255,255,255,.60)` | 玻璃卡顶部高光 |

玻璃卡额外描边：`border: 1px solid var(--border-glass)`（亮色是白色高光，暗色是白色低透明），暗色模式另加 `box-shadow: var(--shadow-2), var(--shadow-inset-top)`。

### 1.8 玻璃效果（backdrop-filter）与降级

**标准玻璃容器**

```css
.glass {
  background: var(--glass-bg);
  -webkit-backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-sat));
          backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-sat));
  border: 1px solid var(--border-glass);
  box-shadow: var(--shadow-2), var(--shadow-inset-top);
}
```

| 使用位置 | `--glass-bg` | blur | sat |
|---|---|---|---|
| 侧边栏 | `--glass-bg` | 24px | 180% |
| 工具栏胶囊 | `--glass-bg-strong` | 24px | 180% |
| 表格头（sticky） | `--glass-bg-strong` | 18px | 160% |
| Toast / 右键菜单 | `--glass-bg-strong` | 18px | 160% |
| 弹层 dialog | `--glass-bg-strong` | 30px | 190% |
| 抽屉 | `--glass-bg-strong` | 30px | 190% |
| 设置分区卡片 | `--glass-bg-faint` | 16px | 140% |

**降级方案（三级）**

1. **完全支持**（WKWebView / WebView2 ≥ Chromium 76）：使用上面的 `backdrop-filter`。
2. **不支持 `backdrop-filter`**：用 `@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px)))` 把背景不透明度提到 **0.92 / 0.96（strong）**、**0.88（faint）**，并保留同色系 1px 描边与阴影，视觉退化为「实色卡片」。

```css
@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {
  :root {
    --glass-bg:        rgba(252,253,255,0.92);
    --glass-bg-strong: rgba(252,253,255,0.96);
    --glass-bg-faint:  rgba(252,253,255,0.88);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --glass-bg:        rgba(16,22,38,0.93);
      --glass-bg-strong: rgba(14,19,33,0.96);
      --glass-bg-faint:  rgba(16,22,38,0.90);
    }
  }
}
```

3. **用户强制关闭毛玻璃**（设置项 `appearance.glass = auto | on | off`）：`off` 时加 `html.no-glass`，该类下 `--glass-*` 全部改为实色（亮色 `#FFFFFF` / 暗色 `#141B2D`），并把 `--glass-blur` 置 0：

```css
html.no-glass { --glass-bg:#FFFFFF; --glass-bg-strong:#FFFFFF; --glass-bg-faint:#F6F8FC; --glass-blur:0px; --glass-sat:100%; }
@media (prefers-color-scheme: dark) { html.no-glass { --glass-bg:#141B2D; --glass-bg-strong:#111827; --glass-bg-faint:#161E30; } }
```

**降级时的额外约束**：`backdrop-filter` 与 `filter` / `transform` / `overflow:hidden` 的祖先组合会产生「包含块」与「backdrop 根」异常 —— 详见 §8。

### 1.9 层级（z-index）

| 层 | z-index | 内容 |
|---|---|---|
| 0 | 0 | 应用背景渐变 |
| 10 | 10 | 侧边栏、工具栏、列表（常规流） |
| 20 | 20 | 吸顶表头、过期链接横幅（sticky） |
| 30 | 30 | 右键菜单 / 下拉菜单 |
| 40 | 40 | 详情抽屉 + 遮罩 |
| 50 | 50 | 模态弹层 + 遮罩 |
| 60 | 60 | Toast |
| 70 | 70 | 拖拽提示层（drag overlay） |

### 1.10 动效

```css
--ease-out: cubic-bezier(0.22, 0.61, 0.36, 1);
--ease-in-out: cubic-bezier(0.4, 0, 0.2, 1);
--dur-fast: 120ms;    /* hover / 颜色 */
--dur-base: 180ms;    /* 展开 / 抽屉 */
--dur-slow: 280ms;    /* 弹层出现 */
```

`@media (prefers-reduced-motion: reduce)` 下：所有 `transition-duration` 与 `animation-duration` 设为 `0.01ms`，进度条的 indeterminate 动画停止，改为静态 30% 宽的斜纹块。

---

## 2. 布局骨架

### 2.1 主窗口总栅格

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ← 系统标题栏（原生，pywebview frameless=False）                                 │
├──────────────────────────────────────────────────────────────────────────────┤
│ .app-shell  (grid)                                                            │
│ ┌────────────────┬─────────────────────────────────────────────────────────┐ │
│ │ .sidebar       │ .main                                                    │ │
│ │ 232px (L)     │ ┌───────────────────────────────────────────────────────┐ │ │
│ │ 216px (M)     │ │ .toolbar  h=52px  (glass 胶囊, 圆角 pill)             │ │ │
│ │ 196px (S)     │ │  [+ 添加 URL]        │  [‖ 全部暂停][> 全部恢复][x 清空]│ │ │
│ │ 56px  (XS)    │ ├───────────────────────────────────────────────────────┤ │ │
│ │                │ │ .banner (过期链接, 可选, h=48px, sticky)             │ │ │
│ │  ┌──────────┐  │ ├───────────────────────────────────────────────────────┤ │ │
│ │  │ 顶部 44px│  │ │ .list-head (sticky top, h=34px)                     │ │ │
│ │  │ 应用名    │  │ │  o  文件名         状态   进度   大小   速度  ETA  ◎  操作│ │ │
│ │  ├──────────┤  │ ├───────────────────────────────────────────────────────┤ │ │
│ │  │ 活动      │  │ │ .row  h=44px                                        │ │ │
│ │  │  全部下载 3│  │ │  o  movie.mkv        o下载中 ▓▓▓▓░ 12.4/1.2G 3.1MB/s 2m │ │ │
│ │  │  下载中  2│  │ │ o  archive.zip       o等待中 ░░░░░  —       —    —    │ │ │
│ │  │  排队中  1│  │ │ o  song.mp3          o已完成 ▓▓▓▓▓ 8.2/8.2M  —      —    │ │ │
│ │  │  已暂停  1│  │ │ ...                                                    │ │ │
│ │  │  已完成  9│  │ │                                                       │ │ │
│ │  │  失败    0│  │ │                                                       │ │ │
│ │  │ 按分类    │  │ │                                                       │ │ │
│ │  │  视频    4│  │ │                                                       │ │ │
│ │  │  音乐    2│  │ │                                                       │ │ │
│ │  │ 其他      │  │ │                                                       │ │ │
│ │  │  历史     │  │ │                                                       │ │ │
│ │  │  计划任务 │  │ │                                                       │ │ │
│ │  └──────────┘  │ └───────────────────────────────────────────────────────┘ │ │
│ └────────────────┴─────────────────────────────────────────────────────────┘ │
│                              ↑ 滚动容器 = .main-body                          │
│                                                                               │
│  .drawer（详情，position:fixed 右侧滑出，宽 440px）  ← 覆盖在 .main 之上         │
│  ┌──────────────────────────────────────────┐                                │
│  │ o movie.mkv                  [o下载中]  │                                │
│  │ https://cdn.x.com/movie.mkv              │                                │
│  │ ──────────────────────────────────────── │                                │
│  │ 保存路径   /Users/me/Downloads/Video/…   │                                │
│  │ 大小       1.2 GB / 1.2 GB               │                                │
│  │ 速度       3.1 MB/s    ETA 2m 10s        │                                │
│  │ 单任务限速 512 KB/s                      │                                │
│  │ 错误       —                             │                                │
│  │ [暂停][取消][打开][限速…][SHA-256][复制JSON]│                                │
│  │ > 分片 (32)                    [复制 JSON]│                                │
│  └──────────────────────────────────────────┘                                │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 应用外壳

```html
<div class="app-shell">
  <aside class="sidebar">…</aside>
  <main class="main">
    <div class="toolbar">…</div>
    <div class="banner-slot">…</div>   <!-- 可选 -->
    <div class="main-body">…</div>     <!-- 纵向滚动容器 -->
  </main>
</div>
<div class="drawer-scrim" hidden></div>
<aside class="drawer" hidden>…</aside>
<div class="toast-stack"></div>
```

```css
html, body { height: 100%; margin: 0; overflow: hidden; }
.app-shell {
  display: grid;
  grid-template-columns: var(--sidebar-w) minmax(0, 1fr);  /* minmax(0,1fr) 关键：防止内容撑破 */
  height: 100vh;
  background: var(--bg-app);
  background-attachment: fixed;
}
.sidebar { grid-column: 1; overflow-y: auto; overflow-x: hidden; }
.main    { grid-column: 2; display: flex; flex-direction: column; min-width: 0; overflow: hidden; }
.main-body { flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden; }
```

关键：`grid-template-columns` 的第二列必须写 `minmax(0, 1fr)`，否则长文件名会把列撑出视口，页面出现横向滚动条。

侧栏宽度变量：

```css
:root { --sidebar-w: 232px; }
@media (max-width: 1179px) { :root { --sidebar-w: 216px; } }
@media (max-width: 1039px) { :root { --sidebar-w: 196px; } }
@media (max-width: 879px)  { :root { --sidebar-w: 56px; } }
```

#### 2.2.1 侧边栏内部结构

```
┌────────────────────┐
│ o  Super Download  │  ← .sidebar-head  h=44px, padding 0 12px
│    Manager         │     折叠态(XS)只显示 28px 应用图标
├────────────────────┤
│ 活动               │  ← .sidebar-section-title  h=26px, 11px 大写 tertiary
│  - 全部下载      3 │  ← .sidebar-item  h=30px  [i-list]
│  # 下载中        2 │                          [i-download]
│  - 排队中        1 │                          [i-clock]
│  # 已暂停        1 │                          [i-pause]
│  - 已完成        9 │                          [i-check-circle]
│  - 失败          0 │                          [i-alert-octagon]
│ 按分类             │
│  > 视频          4 │  ← 图标使用分类色  [i-video]
│  ^ 音乐          2 │                          [i-music]
│  = 图片          0 │                          [i-image]
│  = 压缩包        1 │                          [i-archive]
│  = 文档          3 │                          [i-file-text]
│  = 软件          0 │                          [i-package]
│  ? 其他          0 │                          [i-help-circle]
│ 其他               │
│  ↻ 历史            │                          [i-archive]
│  @ 计划任务        │                          [i-alarm]
├────────────────────┤
│ $ 设置    ⟨ 折叠⟩  │  ← .sidebar-foot h=40px，贴底  [i-settings]
└────────────────────┘
```

> 上图中的 `o` `#` `-` `=` `^` `$` `>` `@` `v` `x` `!` 只是 **ASCII 占位符**，实际渲染时全部替换为 §4.23 的 Feather SVG 图标（方括号内即对应的 `id`）。**规范内任何位置都不使用 emoji 字符作为界面图标** —— 占位符仅用于本 ASCII 示意图。

尺寸：
- `.sidebar-item`：`height: 30px; padding: 0 10px; border-radius: 8px; margin: 0 8px; gap: 10px;`
- 图标 16px，颜色 `var(--text-secondary)`；选中时图标与文字都变 `--accent`。
- 计数徽章：`min-width: 20px; height: 18px; padding: 0 6px; border-radius: 999px; background: var(--neutral-soft); color: var(--text-secondary); font: var(--text-micro);` 计数为 0 时不渲染徽章，但失败项为 0 时改为淡红点 `2px` 圆点以示警示。
- 选中态：`background: var(--accent-soft); color: var(--accent); font-weight: 500;` + 左侧 3px 圆角条 `::before { content:''; position:absolute; left:-8px; top:6px; bottom:6px; width:3px; border-radius:0 3px 3px 0; background:var(--accent); }`（`position: relative` 于 item）。
- 悬停态：`background: rgba(15,23,42,0.05)`（暗色 `rgba(255,255,255,0.06)`）。
- 分区标题：11px / 600 / 字距 0.04em / `--text-tertiary` / 大写（仅英文；中文不转大写）/ padding `8px 18px 4px`。三个分组标题固定为 **活动 / 按分类 / 其他**。
- 侧栏顶部 44px 与底 40px 用 `position: sticky` 固定，中间列表滚动。

#### 2.2.2 侧栏折叠（用户手动）

- 折叠开关在 `.sidebar-foot` 右侧，图标 `chevron-left` / `chevron-right`。
- 折叠后宽度固定 56px（不是 0），item 变为 40×40 居中图标按钮，`title` 属性提供 tooltip（tooltip 用自绘 div，延迟 400ms 显示）。
- 折叠状态持久化到 `sdm.ui.sidebarCollapsed`，与响应式断点无关：**断点折叠是自动的，手动折叠是持久的**，两者叠加时以 56px 为准。
- 断点恢复到 ≥880px 时，如果用户没有手动折叠过，则自动展开；手动折叠过的用户保持折叠。

#### 2.2.3 备选：沉浸式标题栏（`frameless=True`）

如果启用，需要：

```css
body.frameless { padding-top: 38px; }             /* 让出标题栏高度 */
.titlebar {
  position: fixed; inset: 0 0 auto 0; height: 38px; z-index: 40;
  -webkit-app-region: drag;
  display: flex; align-items: center;
}
.titlebar .no-drag, .titlebar button, .titlebar input { -webkit-app-region: no-drag; }
/* macOS: 左侧留出交通灯位置 */
body[data-platform="macos"] .titlebar { padding-left: 78px; }
/* Windows: 右侧自绘最小化/最大化/关闭 46x32 */
```

注意：`-webkit-app-region: drag` 会被子元素继承，**所有可点击子元素必须显式 `no-drag`**，否则按钮点不动。这是 pywebview 自绘标题栏最常见的 bug。

### 2.3 主区工具栏

```
┌────────────────────────────────────────────────────────────────────┐
│  o 添加 URL            │   ‖ 全部暂停   > 全部恢复   x 清空已完成 │  h=52
└────────────────────────────────────────────────────────────────────┘
```

- 容器：`.toolbar { height: 52px; padding: 0 12px; display:flex; align-items:center; gap:8px; }`，内部再套一层 `.toolbar-capsule { display:flex; align-items:center; gap:6px; padding:6px; border-radius: var(--radius-pill); background: var(--glass-bg-strong); backdrop-filter: blur(24px) saturate(180%); border:1px solid var(--border-glass); box-shadow: var(--shadow-2); }`
- 左组：`添加 URL`（主按钮，`--accent` 底、白字、圆角 pill、padding `0 14px`）。
- 右组：`全部暂停` / `全部恢复` / `清空已完成`（ghost 按钮，图标+文字）。
- 两组之间用 `flex: 1` 撑开（不画分隔线；如需视觉分组，加 1px `--border-subtle` 竖线，间距 8px）。
- **标签模式**：设置项 `toolbar.labelMode` = `icons`（默认）| `icons-text`。
  - `icons`：按钮 `32×32`，只有 16px 图标。
  - `icons-text`：按钮 `height: 30px; padding: 0 12px;`，16px 图标 + 6px 间距 + 13px/500 文字。
  - 切换时按钮宽度会变 —— 用 `transition: width var(--dur-base)` 会抖动，**不要做宽度动画**，直接切换。
- 禁用态：`全部暂停` 在没有 downloading/queued 任务时禁用；`全部恢复` 无 paused 时禁用；`清空已完成` 无 completed 时禁用。禁用 = `opacity:.4; cursor:not-allowed;` 且**不响应 hover**。
- 窄窗口（<880px）：文字标签隐藏（自动降级为 `icons`），且「全部暂停/全部恢复/清空已完成」三个按钮收进右侧 `more-horizontal` 溢出菜单。

### 2.4 下载列表（主区）

```
.list-head  (position: sticky; top: 0; z-index: 20; h=34px; backdrop-filter blur(18px))
┌──────┬──────────────────────┬────────┬───────────────┬──────────┬─────────┬──────┬──────┬────────┐
│  o   │ 文件名               │ 状态   │ 进度          │ 已下/总量│ 速度    │ ETA  │ ◎   │ 操作   │
├──────┼──────────────────────┼────────┼───────────────┼──────────┼─────────┼──────┼──────┼────────┤
│  o   │ movie.mkv            │o 下载中│ ▓▓▓▓▓░░ 52%   │ 634MB/1.2GB│ 3.1MB/s│ 2m10s│ ◎   │ ‖ ×   │
│      │ cdn.example.com      │        │               │          │         │      │      │        │
└──────┴──────────────────────┴────────┴───────────────┴──────────┴─────────┴──────┴──────┴────────┘
```

**列定义（9 列）与断点可见性**

所有宽度均为**该断点的固定值**（不是最小值）；只有文件名列是 `minmax()` 弹性列。

| # | 列 | class | L/M 宽 | S 宽 | XS 宽 | 可见断点 |
|---|---|---|---|---|---|---|
| 1 | 状态图标 | `.col-icon` | 24px | 24px | 24px | 全部 |
| 2 | 文件名 + 主机 | `.col-name` | `minmax(180px, 1fr)` | `minmax(150px, 1fr)` | `minmax(120px, 1fr)` | 全部 |
| 3 | 状态徽章 | `.col-badge` | 104px | 96px | — | S 以上 |
| 4 | 进度条 + % | `.col-progress` | 132px | 120px | 100px | 全部 |
| 5 | 已下 / 总量 | `.col-size` | 108px | — | — | M 以上 |
| 6 | 速度 | `.col-speed` | 80px | 76px | — | S 以上 |
| 7 | 剩余时间 | `.col-eta` | 64px | — | — | L 以上 |
| 8 | 每任务限速标记 | `.col-limit` | 24px | — | — | L 以上 |
| 9 | 操作按钮组 | `.col-actions` | 92px | 76px | 36px | 全部 |

> 操作列宽度与按钮数对应：92px = 3 个 28px 按钮 + 2×4px 间距；76px = 2 个按钮；36px = 1 个按钮（其余进「…」菜单，见 §3.4）。

**列模板（必须与可见列数严格一致）**

```css
/* 基准（L / XL）—— 9 列：图标 名 徽章 进度 大小 速度 ETA 限速 操作 */
:root { --dl-cols: 24px minmax(180px,1fr) 104px 132px 108px 80px 64px 24px 92px; }

/* M —— 7 列（隐藏 7 ETA、8 限速） */
@media (max-width: 1179px) {
  :root { --dl-cols: 24px minmax(160px,1fr) 104px 132px 108px 80px 92px; }
}
/* S —— 6 列（隐藏 5 大小、7 ETA、8 限速） */
@media (max-width: 1039px) {
  :root { --dl-cols: 24px minmax(150px,1fr) 96px 120px 76px 76px; }
}
/* XS —— 4 列（隐藏 3 徽章、5 大小、6 速度、7 ETA、8 限速） */
@media (max-width: 879px) {
  :root { --dl-cols: 24px minmax(120px,1fr) 100px 36px; }
}
```

> 断点同时改变 `column-gap`（12px→10px→8px）与左右 `padding`（14px→12px→10px），见下方规则。上表的固定轨道宽已按各自断点的 gap 与 padding 核算过，任何断点下文件名列的实际宽度都 ≥ 它的 `minmax()` 下限（核算见 §3.2）。

```css
.list-head, .list-row {
  display: grid;
  grid-template-columns: var(--dl-cols);
  align-items: center;
  column-gap: 12px;
  padding: 0 14px;
}
.list-head { height: 34px; font: var(--text-label); color: var(--text-secondary);
             border-bottom: 1px solid var(--border-subtle); position: sticky; top: 0; z-index: 20; }
.list-row { min-height: 44px; padding-top: 4px; padding-bottom: 4px; border-bottom: 1px solid var(--border-subtle);
            position: relative; }   /* 选中条的定位上下文，必须有 */
.list-row:hover { background: rgba(15,23,42,0.035); }
.list-row[aria-selected="true"] { background: var(--accent-softer); }
.list-row[aria-selected="true"]::before {
  content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--accent);
}

/* 断点同时收紧间距，为固定列腾出空间 */
@media (max-width: 1039px) { .list-head, .list-row { column-gap: 10px; padding: 0 12px; } }
@media (max-width: 879px)  { .list-head, .list-row { column-gap: 8px;  padding: 0 10px; } }
```

**隐藏列的实现**：给每列单元格加 class，断点处用 `display:none` 从 grid 流中移除。**模板轨道数必须等于可见单元格数**，否则最后一项会落到隐式轨道里导致错位。

```css
@media (max-width: 1179px) { .col-eta, .col-limit { display: none; } }   /* 9 → 7 列 */
@media (max-width: 1039px) { .col-size { display: none; } }              /* 7 → 6 列 */
@media (max-width: 879px)  { .col-badge, .col-speed { display: none; } } /* 6 → 4 列 */
```

> 三条规则按「从宽到窄」叠加生效：≤1179 隐藏 2 列（9→7），≤1039 再隐藏 1 列（7→6），≤879 再隐藏 2 列（6→4）。**每一步的 `--dl-cols` 覆盖值都写在 §2.4 的模板块里，二者必须成对修改。**

**行内元素细节**

- 文件名列：两层竖排。第一行文件名 `--text-subtitle`，`overflow:hidden; text-overflow:ellipsis; white-space:nowrap;`（中段截断用 `direction: rtl` 技巧不推荐，直接尾截断即可）。第二行主机名 `--text-caption` `--text-tertiary`，同样省略。父容器 `min-width: 0`。
- 进度列：`flex` 竖排 = 进度条（宽 100%，高 6px）+ 百分比（`--text-caption`，右对齐，`tabular-nums`）。总大小未知时显示 `—`。
- 已下/总量：`634 MB / 1.2 GB`，两段之间 ` / ` 分隔，`--text-caption`。
- 速度：仅 downloading 状态用 `--text-primary`，其余 `--text-tertiary`；`0 B/s` 时显示 `—`。
- ETA：无 ETA 时 `—`，`--text-tertiary`。
- 限速标记：`gauge` 图标 14px，颜色恒为 `--warning`，`title="限速 512 KB/s"`。
- 操作列：见 §5 各状态可用操作；按钮 `28×28`，图标 14px，间距 4px。

**列表容器**

```css
.list { max-width: 1400px; margin: 0 auto; }  /* XL 下居中，>=1440 才生效 */
```

### 2.5 详情面板（右侧滑出抽屉）

**决策：右侧滑出抽屉（overlay drawer），不是底部面板。** 理由：底部面板会吃掉列表可视高度，而下载列表的主信息（进度、速度）本身就靠纵向空间；右侧抽屉与 macOS 侧边栏习惯一致，且不改变主区宽度 → **列宽不会因为打开详情而重排抖动**。

**抽屉里的数据必须跟着列表轮询一起刷新。** 列表每 1.5 秒轮询一次，
每次轮询都会重画整个界面（包括抽屉）。所以抽屉显示的分片明细必须
**在轮询时顺带取一次**——不能只在「打开抽屉时」取一次，否则抽屉会一直
重画一张过期的分片表。用户看到的现象是「首次点开不实时更新，关掉重开
才更新」。

**重画要保住滚动位置。** 轮询每 1.5 秒重画一次抽屉，不保住滚动位置的
话，用户正看着分片表，滚动条就被弹回顶部——数据实时了，眼睛却跟不上。

**「已重新开始」不是错误，是解释。** 服务器上的文件变了 → 旧进度作废
→ 从头再下一次。这时任务状态是**已完成**，但用户明明看到进度从 90%
掉回 0%，界面得解释一句。做法：任务带一个 `stale_restarted` 标志，
抽屉里显示一行「已重新开始 / 服务器上的文件已变化，无法续传，已重新
开始下载」，**按当前语言写**（不是把引擎的中文原文抖出来），并且
**不要**挂在「错误」那一栏下面——它不是错误。


```
X=0                                            窗口右边缘
┌──────────────────────────────────────────────┬──────────┐
│                                              │  .drawer │
│              .main（不变宽）                  │  440px   │
│                                              │  玻璃卡   │
│                                              │  固定     │
└──────────────────────────────────────────────┴──────────┘
                    .drawer-scrim 覆盖整个窗口 rgba(15,23,42,.28)
```

```css
.drawer {
  position: fixed; top: 0; right: 0; bottom: 0;
  width: var(--drawer-w);
  z-index: 40;
  transform: translateX(100%);
  transition: transform var(--dur-base) var(--ease-out);
  overflow-y: auto;
  background: var(--glass-bg-strong);
  -webkit-backdrop-filter: blur(30px) saturate(190%);
          backdrop-filter: blur(30px) saturate(190%);
  border-left: 1px solid var(--border-glass);
  box-shadow: var(--shadow-3);
}
.drawer[data-open="true"] { transform: translateX(0); }
.drawer-scrim {
  position: fixed; inset: 0; z-index: 39;
  background: rgba(15,23,42,0.28);
  opacity: 0; pointer-events: none;
  transition: opacity var(--dur-base) var(--ease-out);
}
.drawer-scrim[data-open="true"] { opacity: 1; pointer-events: auto; }
```

抽屉宽度是**按断点写死的变量**，不是 `clamp()` —— 因为分片表等子组件要按抽屉宽度的确定值做减列决策，用 `clamp()` 会让「减列阈值」和「实际宽度」脱钩：

```css
:root                { --drawer-w: 440px; }                    /* L / XL */
@media (max-width: 1179px) { :root { --drawer-w: 400px; } }     /* M */
@media (max-width: 1039px) { :root { --drawer-w: 360px; } }     /* S */
@media (max-width: 879px)  { :root { --drawer-w: calc(100vw - 32px); } }  /* XS */
```

> 抽屉内容区 = `--drawer-w` − 左右 padding 40px：L 400px、M 360px、S 320px、XS ≥568px。
> 分片表的减列阈值（§4.9）正是按这四个值核算的 —— 改抽屉宽度必须回头复核 §4.9 的减列规则。

- 用 `transform` 动画，**不要**动画 `right`/`width`（会触发主区重排）。
- 抽屉是 `position: fixed`，必须挂在 `body` 下（或最近的非 transform 祖先下）—— 见 §8。
- 打开时把焦点移到抽屉标题（`tabindex="-1"`），关闭时把焦点还给触发它的行。
- XS 断点：抽屉宽度 `calc(100vw - 32px)`，遮罩不透明度提高到 `0.4`。

**抽屉内容结构**

```
┌──────────────────────────────────────────────┐
│ o  movie.mkv                       [o 下载中] │  ← .drawer-head  padding 20px
│     https://cdn.example.com/movie.mkv        │     标题 15/600，URL 12 mono 省略
├──────────────────────────────────────────────┤
│ 保存路径     /Users/me/Downloads/Video/mv.mkv │  ← .meta-grid
│ 大小         634 MB / 1.2 GB                  │     label 列 96px --text-secondary
│ 速度         3.1 MB/s                         │     value 列 1fr --text-body mono
│ 剩余时间     2m 10s                           │
│ 单任务限速   512 KB/s                         │
│ 错误         HTTP 403 Forbidden               │  ← 失败时出现，--danger，最多 3 行
├──────────────────────────────────────────────┤
│ [‖ 暂停][× 取消][> 打开][◎ 限速…][# SHA-256]│  ← .drawer-actions  wrap
├──────────────────────────────────────────────┤
│ > 分片（32）                        [复制 JSON]│  ← 可折叠 section
│ ┌────┬─────────────┬────────┬──────┬──────┐  │
│ │ ID │ 字节范围    │ 进度   │ 耗时 │ 状态 │  │
│ ├────┼─────────────┼────────┼──────┼──────┤  │
│ │ 00 │ 0 B–2.0 MB  │ ▓▓▓▓ 100%│ 1.2s │ 完成 │  │
│ │ 01 │ 2.0–4.0 MB │ ▓▓▓░ 78% │ 0.8s │ 下载 │  │
│ └────┴─────────────┴────────┴──────┴──────┘  │
└──────────────────────────────────────────────┘
```

- 元信息网格：`display: grid; grid-template-columns: 96px minmax(0,1fr); gap: 8px 16px;`
- 抽屉底部固定一个「复制 JSON」按钮，复制整条任务的 JSON（沿用项目早期字段：`id / filename / url / state / bytesDone / totalBytes / speedBytesPerSec / speedLimitBytesPerSec / destination / error`）。

### 2.6 历史视图

```
┌───────────────────────────────────────────────────────────────┐
│ / [ 筛选…                          ]        128 条记录       │  ← .view-bar h=48
├───────────────────────────────────────────────────────────────┤
│ 文件 / URL                大小      状态      完成时间   操作 │
├───────────────────────────────────────────────────────────────┤
│ movie.mkv              1.2 GB    o已完成  10-24 18:20  v > x│
│ cdn.example.com/…                                              │
└───────────────────────────────────────────────────────────────┘
```

- 视图切换时，**保留主区的工具栏胶囊**（复用同一个 `.toolbar` 位置，但按钮换成历史视图自己的：「清空全部」），避免布局跳动。
- 搜索框：`flex: 1; max-width: 360px; height: 30px;` 左侧 14px 放大镜图标，右侧计数。
- 列模板（同样遵守「轨道数 = 可见单元格数」）：

```css
/* 基准（M / L / XL）—— 5 列：文件+URL 大小 状态 完成时间 操作 */
:root { --hl-cols: minmax(180px,1fr) 100px 100px 150px 104px; }
/* S —— 4 列（隐藏完成时间，并入文件列副行） */
@media (max-width: 1039px) {
  :root { --hl-cols: minmax(150px,1fr) 100px 100px 104px; }
}
/* XS —— 3 列（隐藏大小） */
@media (max-width: 879px) {
  :root { --hl-cols: minmax(120px,1fr) 100px 104px; }
}
.history-head, .history-row {
  display: grid; grid-template-columns: var(--hl-cols);
  align-items: center; column-gap: 10px; padding: 0 14px;
}
.history-row { min-height: 44px; padding-top: 4px; padding-bottom: 4px;
               border-bottom: 1px solid var(--border-subtle); }
@media (max-width: 1039px) { .history-head, .history-row { column-gap: 8px; padding: 0 10px; } }
@media (max-width: 1039px) { .col-h-date { display: none; } }   /* 5 → 4 列 */
@media (max-width: 879px)  { .col-h-size { display: none; } }  /* 4 → 3 列 */
```

> 核算：M 断点（1040 − 216 − 28 = 796）固定列 454 + gap 40 = 494，文件名得 302px（≥180 OK）；S（880 − 196 − 24 = 660）固定 354 + gap 30 = 384，文件名得 276px（≥150 OK）；XS（640 − 56 − 20 = 564）固定 204 + gap 16 = 220，文件名得 344px（≥120 OK）。
- 空状态：见 §4.11。
- 底部固定操作条：`清空全部`（danger ghost 按钮）+ 二次确认弹层。

### 2.7 计划任务视图

```
┌───────────────────────────────────────────────────────────────┐
│ [https://example.com/f.zip] [= 10-26 14:30 ▾] [备注…] [添加] │  ← .view-bar h=48
├───────────────────────────────────────────────────────────────┤
│ @  https://example.com/f.zip            2026-10-26 14:30  [x]│
│     备注文字                                                │
└───────────────────────────────────────────────────────────────┘
```

- 新增行：`display:flex; gap:8px; padding: 10px 14px;`，URL 输入 `flex:1; min-width:0;`，日期时间选择器 `width: 190px;`，备注输入 `width: 160px;`（<1040 隐藏备注输入，改为行内展开）。
- 列表项：`height: 52px`，左侧 16px `alarm` 图标（`--accent`），中间 URL（省略）+ 备注（caption），右侧时间 + 删除。
- 空状态：`alarm` 插图 + 「暂无计划任务」。

### 2.8 设置窗口（独立窗口）

```
┌─────────────────────────────────────────────────────────────┐
│ o 通用    │  行为                                          │
│ # 默认    │  ┌───────────────────────────────────────────┐  │
│ o 分类    │  │ 语言        [跟随系统 ▾]                 │  │
│ o 站点规则│  │ 启用自动分类                    [ o─── ]   │  │
│ o 启动    │  │ 启用剪贴板捕获                [ ───o ]   │  │
│ o 日志    │  │ 下载完成时系统通知              [ o─── ]   │  │
│ o 关于    │  │ 关闭窗口时隐藏 Dock 图标        [ ───o ]   │  │
│           │  │ 工具栏显示文字标签              [ o─── ]   │  │
│           │  │ 忽略 SSL 证书验证             [ ───o ]   │  │
│           │  │                                  （提示行） │  │
│           │  └───────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
   ↑ rail 160px              ↑ detail 1fr
```

- 设置窗口 `720×560`，`min_size=(560, 440)`。
- 左 rail 固定 160px：图标 16px + 文字 13px，行高 32px，选中态同侧栏。
- 右 detail：`padding: 24px; overflow-y: auto;` 内容最大宽 520px（左对齐，不居中）。
- 7 个分区：**通用 / 默认 / 分类 / 站点规则 / 启动 / 日志 / 关于**。
- 分区内部结构：标题（`--text-title` 17/600）+ 提示行（`--text-caption` `--text-secondary`，`margin-bottom: 16px`）+ 分组卡片（`.glass` 玻璃卡，`border-radius: 12px; padding: 16px;`，组内每行 `min-height: 32px; display:flex; align-items:center; gap:12px;`）。
- 每行结构：`label`（flex: 1，最小 160px）+ 控件（固定宽度，见 §4）。
- 控件宽度：开关 38×22；下拉 `min-width: 160px; max-width: 240px`；数字输入 88px（右对齐）；滑块 + 输入组合 `flex:1 + 72px`。

### 2.9 站点规则设置（设置 → 站点规则）

```
新增规则
┌───────────────────────────────────────────────────────────────┐
│ 主机名*        [ example.com                          ]      │
│ Referer        [ https://example.com/                ]      │
│ User-Agent     [ Mozilla/5.0 …                        ]      │
│ Cookie         [ a=1; b=2                             ]      │
│ Basic Auth     [ user     ] [ password / ]                    │
│                [ 添加规则 ]  [ 清空全部 ]                      │
│ ! 主机名不能为空                                                │
└───────────────────────────────────────────────────────────────┘

已有规则
┌───────────────────────────────────────────────────────────────┐
│ example.com                              [ o─── 启用 ] [x]    │
│   Referer      https://example.com/                          │
│   User-Agent   Mozilla/5.0 …                                 │
│   Cookie       a=1; b=2                                      │
│   Basic Auth   user / ••••••••                                │
└───────────────────────────────────────────────────────────────┘
```

- 「新增规则」与「已有规则」是两张独立玻璃卡，间距 20px。
- 已有规则的每一项是「规则卡」：头行（主机名 14/600 + 启用开关 + 删除按钮），下方是 2 列网格的字段摘要（label 72px / value 1fr），value 超出时省略 + `title` 全量。
- **密码字段**：永远不渲染明文；保存时写入系统钥匙串（macOS Keychain / Windows Credential Manager），列表里只显示用户名与 `••••••••`。输入框 `type="password"`，右侧 14px `eye` / `eye-off` 切换按钮。规则卡上的「修改密码」入口复用同一弹层。
- 启用开关放在卡片头行右侧，`role="switch"`，`aria-label="启用 example.com 规则"`。

### 2.10 分类规则设置（设置 → 分类）

```
┌───────────────────────────────────────────────────────────────┐
│ 启用自动分类                                          [ o─── ] │
│ 启用后，下载文件会落到 ~/Downloads/<分类>/ 目录下。               │
└───────────────────────────────────────────────────────────────┘
┌───────────────────────────────────────────────────────────────┐
│ > 视频     mp4 × mov × mkv × webm × avi × flv × m4v × wmv × ts × │
│             [ + 添加扩展名 ]                                  │
│ ^ 音乐     mp3 × flac × wav × aac × ogg × m4a × opus ×          │
│ = 图片     jpg × jpeg × png × gif × webp × heic × svg × …       │
│ = 压缩包   zip × 7z × rar × tar × gz × bz2 × xz × iso × tgz ×    │
│ = 文档     pdf × doc × docx × xls × xlsx × ppt × pptx × …      │
│ = 软件     exe × msi × deb × rpm × apk × app × dmg × pkg ×      │
│ ? 其他     （其它一切）                                        │
└───────────────────────────────────────────────────────────────┘
```

- 每个分类一行：图标（分类色，16px）+ 名称（固定 88px）+ 扩展名芯片流（`flex-wrap: wrap; gap: 6px;`）+ 「+ 添加扩展名」按钮。
- 芯片 `.chip`：高 22px，`padding: 0 6px 0 8px`，圆角 6px，底 `--neutral-soft`，12px 文字，右侧 12px `×` 按钮。
- 芯片流在窄宽度（<720 的设置 detail 区）下换行，分类名与芯片流改为上下两行（`flex-wrap: wrap` + 名称 `flex-basis: 100%`）。
- 「其他」分类的扩展名固定为 `…`（表示「其余全部」），不可编辑、不可删除。

### 2.11 日志预览弹层

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 日志预览                                          3,214 行             │  h=56
├──────────────────────────────────────────────────────────────────────────┤
│ 路径  ~/Library/Logs/SuperDownloadManager/sdm.log            [复制路径]  │  h=32
│ 筛选  [ / 过滤关键字…                                            ]  │  h=40
├──────────────────────────────────────────────────────────────────────────┤
│ 时间            级别    分类              消息                            │
│ 18:20:31.402   INFO    engine            chunk 03 headers received        │
│ 18:20:31.556   WARN    download          chunk 03 stalled > 15s           │
│ …                                                                    │
├──────────────────────────────────────────────────────────────────────────┤
│                                        [复制全部]  [关闭]                │  h=56
└──────────────────────────────────────────────────────────────────────────┘
   宽 900 / 高 560，max-height: calc(100vh - 80px)
```

- 表格列宽：时间 100px（mono）、级别 64px、分类 140px、消息 `minmax(200px, 1fr)`。
- 行高 22px，`--text-mono` 12px。级别列用语义色。
- 新日志在前（倒序）。超过 5000 行时只渲染最新 2000 行（虚拟滚动由实现自行选择；本规范只要求滚动容器 `overflow-y: auto` 且表头 sticky）。
- 筛选框对「消息」和「分类」做大小写不敏感子串匹配（项目既有行为）。
- 「复制全部」复制**筛选后**的行，格式 `时间戳 [级别] [分类] 消息`，每行一条。

### 2.12 过期链接横幅

```
┌──────────────────────────────────────────────────────────────────────────┐
│ !  链接已失效：服务器返回 403，movie.mkv 需要新的下载地址。                 │  h=48
│                                    [ 输入新链接…        ] [ 刷新链接 ]    │
└──────────────────────────────────────────────────────────────────────────┘
```

- 位置：`.main` 内，工具栏下方、列表上方，`position: sticky; top: 0; z-index: 20;`（跟随列表滚动吸顶）。
- 样式：`background: var(--warning-soft); border: 1px solid rgba(217,119,6,.30); border-radius: 12px;` 文字 `--text-primary`，图标 `--warning`。
- 默认只显示文案 + 「刷新链接」按钮；点击「刷新链接」后**在横幅内就地展开**一个 URL 输入框（不弹模态，避免打断上下文），输入框 `flex:1; height: 30px;`，旁边「刷新链接」变为「应用」。
- 多个任务同时过期时：横幅只显示第一条 + 「还有 N 个链接失效」；点击展开一个列表，每条一个任务名，点击定位到该任务行。
- 横幅可手动关闭（×），关闭后任务仍保持 `awaiting_new_link` 状态，行内徽章与行右键菜单的「刷新链接」入口仍在。

### 2.13 剪贴板候选确认弹层

```
┌──────────────────────────────────────────────────────────────┐
│ =  从剪贴板下载？                                          │
│     检测到一个可能的下载链接，确认是否添加。                    │
├──────────────────────────────────────────────────────────────┤
│ URL                                                          │
│ ┌──────────────────────────────────────────────────────────┐│
│ │ https://example.com/files/report.pdf                    ││  ← mono 12px，最多 3 行
│ └──────────────────────────────────────────────────────────┘││
│ 分类        [ 图片 ▾ ]                                       │
│ 保存目录    [ ~/Downloads/Pictures                    ][选择] │
│ 每任务最大线程数  [ 32 ]  （1–128，默认取全局设置）             │
│ □ 停止监听剪贴板                                              │
├──────────────────────────────────────────────────────────────┤
│                                    [ 跳过 ]  [ 下载 ]        │
└──────────────────────────────────────────────────────────────┘
   宽 520
```

- 弹层宽度 520；`max-height: calc(100vh - 80px); overflow-y: auto;`。
- 分类下拉改变时，若用户未手动选过目录，保存目录自动切到 `~/Downloads/<分类>/`；用户手动选过则不再自动改（沿用项目既有的 `useCustomDir` 逻辑）。
- 「停止监听剪贴板」勾选后即写入 `sdm.capture.clipboard=false` 并关闭弹层（等同设置里的开关）。
- 「跳过」：URL 记入 `lastSeenURLs`，5 分钟内不再提示。
- 按钮顺序：左「跳过」（secondary）、右「下载」（primary，`Enter` 为默认按钮）。

### 2.14 添加 URL 弹层

```
┌──────────────────────────────────────────────────────────────┐
│ 添加下载                                                    │
│ 输入 URL，SDM 将立即开始多线程下载。                           │
├──────────────────────────────────────────────────────────────┤
│ URL                                                          │
│ [ https://example.com/file.zip                            ] │
│ 另存为（可选）                                                │
│ [ 留空使用服务器的文件名                                     ] │
│ ! 请输入有效的 http(s) URL。                                  │  ← 仅在错误时出现
├──────────────────────────────────────────────────────────────┤
│                                    [ 取消 ]  [ 添加 ]        │
└──────────────────────────────────────────────────────────────┘
   宽 460
```

- URL 输入自动聚焦（`autofocus`），`Enter` 提交。
- 校验：必须能 `new URL()` 解析且 `protocol` 为 `http:` / `https:`。错误时输入框边框变 `--danger`、下方出现 12px `--danger` 错误行（`role="alert"`），焦点留在输入框。
- 「添加」按钮在 URL 为空时禁用（`disabled`）。

### 2.15 空状态视图

```
┌──────────────────────────────────────────┐
│                                          │
│              o  (56px 线框插图)          │
│                                          │
│           还没有下载                      │  22px/600
│     点击 + 或按 CmdN 添加 URL。              │  13px secondary
│     也可以直接把 URL 拖进这个窗口。         │  13px secondary
│                                          │
│      设置 → 语言 可切换到 中文             │  11px tertiary
│                                          │
└──────────────────────────────────────────┘
```

- 垂直居中，最大宽 420px，文字居中。
- 插图：56px 的线性 SVG（`download-cloud` 风格），颜色 `--text-tertiary`，`stroke-width: 1.5`。
- 各视图的插图不同：下载列表 `download-cloud`、历史 `rotate-ccw`、计划任务 `alarm`、站点规则 `globe`、日志 `file-text`。

---

## 3. 响应式规则

### 3.1 断点总表

| 断点 | 窗口宽 | 侧栏 | 下载列表列 | 工具栏 | 抽屉 | 历史列表 | 设置窗口 |
|---|---|---|---|---|---|---|---|
| **XS** | 640–879 | 56px 图标轨 | 4 列：图标 / 文件名 / 进度 / 操作 | 只显示图标；批量按钮进「更多」菜单 | 宽度 `calc(100vw - 32px)` | 3 列：文件+URL / 状态 / 操作 | 左 rail 保留 160px |
| **S** | 880–1039 | 196px | 6 列：+徽章 +速度 | 只显示图标（文字自动隐藏） | 360px | 4 列：+完成时间 | 同上 |
| **M** | 1040–1179 | 216px | 7 列：+大小 | 图标+文字 | 400px | 5 列 | 同上 |
| **L** | 1180–1439 | 232px | 9 列（全） | 图标+文字 | 440px | 5 列 | 同上 |
| **XL** | ≥1440 | 232px | 9 列，列表最大宽 1400 居中 | 图标+文字 | 440px | 同上 | 同上 |

### 3.2 列折叠与降级的具体依据

列的可见性由「**主区可用宽度**」决定：

```
主区可用宽 = 窗口宽 − 侧栏宽 − 列表左右 padding × 2
文件名实际宽 = 主区可用宽 − 固定列宽合计 − column-gap × (列数 − 1)
```

下表是逐断点的核算（列数 = 可见单元格数，与 `--dl-cols` 的轨道数一一对应）：

| 断点 | 窗口宽 | 侧栏 | padding | gap | 主区可用宽 | 固定列合计 | gap 合计 | 文件名实际宽 | 下限 | 余量 |
|---|---|---|---|---|---|---|---|---|---|---|
| L（9 列） | 1180 | 232 | 14×2 | 12×8 | 920 | 628 | 96 | **196px** | 180 | +16 |
| M（7 列） | 1040 | 216 | 12×2 | 10×6 | 796 | 540 | 60 | **196px** | 160 | +36 |
| S（6 列） | 880 | 196 | 12×2 | 10×5 | 660 | 392 | 50 | **218px** | 150 | +68 |
| XS（4 列） | 640 | 56 | 10×2 | 8×3 | 564 | 160 | 24 | **380px** | 120 | +260 |

结论：**每个断点下文件名实际宽都 ≥ 其 `minmax()` 下限**，剩余空间全部由 `1fr` 吃掉给文件名，其余固定列永远不会被挤变形。因为文件名轨道是 `minmax(Npx, 1fr)`，即使窗口比断点下限更窄（最小 640px），它也只会停在 Npx 而不是把其它列压扁。

> 核算口诀：**固定列合计 + gap + padding 是「不可协商」的开销，文件名是唯一的弹性项**。调整任何固定列宽度时，必须回到这张表复核余量仍 ≥ 0。

### 3.3 行高与密度

| 断点 | 行高 | 单元格 padding | 文件名副行（主机名） | 进度条高度 |
|---|---|---|---|---|
| XS | 36px | 0 10px | 隐藏 | 4px |
| S | 40px | 0 12px | 显示 | 5px |
| M / L / XL | 44px | 0 14px | 显示 | 6px |

- 行高用 `min-height` + `padding: 4px 0` 实现，不写死 `height` —— 文件名两行时行可以自然长高（不会截断）。
- 表格头高度恒定 34px。

### 3.4 按钮在窄窗口下的降级顺序

1. **第 1 步**：隐藏按钮文字（保留图标），`title` 提供 tooltip。
2. **第 2 步**：把次要按钮收进「更多」菜单（`more-horizontal`）。收纳顺序：清空已完成 → 全部恢复 → 全部暂停。`添加 URL` **永不收纳**（它是主操作，且在左侧独立组）。
3. **第 3 步**：若仍然放不下（S 断点），侧栏收成图标轨，让出 140–176px 给主区。
4. 行内操作按钮在 XS 下最多保留 1 个（主操作：暂停/继续/打开），其余进「…」，点击弹出垂直菜单。

### 3.5 详情抽屉的降级

- 宽度由 `--drawer-w` 按断点写死：L/XL `440px`、M `400px`、S `360px`、XS `calc(100vw - 32px)`（见 §2.5）。
- XS 下遮罩不透明度 `0.4`（更明确地表明被遮挡的区域不可交互）。
- 抽屉内部永远单列（不做两列），元信息网格的 label 列固定 88px（XS）/ 96px（M 以上），value 列 `minmax(0,1fr)` 并允许换行。
- 分片表按抽屉宽度减列：M 及更窄隐藏「字节范围」列（规则与核算见 §4.9）。

### 3.6 设置窗口的降级

- 设置窗口最小 560px 宽：左 rail 160px + detail 400px，恰好放得下。
- detail 区 < 440px 宽时（等价于窗口 600px），「标签 → 控件」的横向行改为纵向堆叠（`flex-direction: column; align-items: flex-start;` 控件 `width: 100%`）。
- 「分类」分区的扩展名芯片流始终 `flex-wrap: wrap`。

### 3.7 最小可用窗口尺寸

| 项 | 值 | 说明 |
|---|---|---|
| 主窗口最小 | **640 × 440** | pywebview `create_window(..., min_size=(640, 440))` |
| 主窗口默认 | 1180 × 760 | 落在 L 断点 |
| 设置窗口最小 | 560 × 440 | `min_size=(560, 440)` |
| 设置窗口默认 | 720 × 560 | |
| 内容最小高度 | 260px | 低于此值列表区不再压缩（会出现自身滚动） |

在 640×440 的窗口里必须仍然可用：侧栏图标轨（6 个状态项 + 分类图标 + 历史/计划）、工具栏 4 个图标、列表 4 列、抽屉全宽覆盖、弹层可滚动。

### 3.8 任何宽度下都不允许出现的坏情况（验收清单）

| # | 坏情况 | 防止手段 |
|---|---|---|
| 1 | 出现横向滚动条 | `html,body{overflow:hidden}`；`grid-template-columns` 第二列用 `minmax(0,1fr)`；所有 flex 子项加 `min-width:0`；`.main-body{overflow-x:hidden}` |
| 2 | 文字被截断成半个字 | 所有截断必须用 `text-overflow: ellipsis` + `white-space: nowrap`（或 `-webkit-line-clamp`），禁止 `overflow:hidden` 无 ellipsis |
| 3 | 控件重叠 | 工具栏用 `flex-wrap: nowrap` + 溢出菜单；弹层内容用 flex column + `gap`；行内用 grid 的固定轨道 |
| 4 | 按钮被挤没 | 断点隐藏文字 → 收纳进「更多」菜单；主操作永不隐藏 |
| 5 | 表格列错位 | 隐藏列时模板轨道数必须同步减少；表头与行必须共用同一个 `var(--dl-cols)` |
| 6 | 内容溢出到卡片外 | 卡片 `overflow: hidden`；长文本容器 `min-width: 0` + `overflow-wrap: anywhere` |
| 7 | 弹层在矮窗口里超出屏幕 | `.dialog{ max-height: calc(100vh - 80px); overflow-y: auto; }` |
| 8 | 抽屉遮住整个界面却无法关闭 | 遮罩可点击关闭；`Esc` 可关闭；抽屉右上角有 × |
| 9 | 滚动时表头被内容盖住 | 表头 `position: sticky; top: 0; z-index: 20`，并有半透明玻璃底 |
| 10 | 玻璃效果导致文字不可读 | 玻璃层之上必须有实色文字；降级方案见 §1.8 |
| 11 | 焦点被裁掉（focus ring 看不见） | 焦点元素不得放在 `overflow: hidden` 容器边缘；用 `outline-offset: 2px` + `outline`（不用 `box-shadow` 做焦点环，否则会被裁） |
| 12 | 列表为空时出现 0px 高的空状态 | 空状态容器 `min-height: 240px`，父容器 `flex: 1` |

---

## 4. 组件规格

### 4.1 按钮

| 类型 | 尺寸 | 圆角 | 字体 | 图标 | 用途 |
|---|---|---|---|---|---|
| `.btn--primary` | `height: 30px; padding: 0 14px` | pill | 13px/500 | 16px | 添加 URL、下载、确认 |
| `.btn--secondary` | `height: 30px; padding: 0 14px` | 8px | 13px/500 | 16px | 取消、跳过、复制 |
| `.btn--ghost` | `height: 30px; padding: 0 10px` | 8px | 13px/500 | 16px | 工具栏批量按钮、列表行操作 |
| `.btn--icon` | `32×32`（工具栏）/ `28×28`（行内） | 8px | — | 16px / 14px | 纯图标操作 |
| `.btn--danger` | 同 secondary | 8px | 13px/500 | 16px | 删除文件、清空全部 |

**状态**

| 状态 | 样式 |
|---|---|
| 默认 primary | `background: var(--accent); color: var(--text-on-accent); box-shadow: var(--shadow-1)` |
| 悬停 primary | `background: var(--accent-hover)` |
| 按下 primary | `background: var(--accent-active); transform: translateY(0.5px)` |
| 默认 secondary | `background: var(--glass-bg); border: 1px solid var(--border-default); color: var(--text-primary)` |
| 悬停 secondary | `background: var(--glass-bg-strong); border-color: var(--border-strong)` |
| 默认 ghost | `background: transparent; color: var(--text-secondary)` |
| 悬停 ghost | `background: rgba(15,23,42,.06); color: var(--text-primary)`（暗色 `rgba(255,255,255,.08)`） |
| 禁用 | `opacity: .4; cursor: not-allowed; pointer-events: none;` |
| 加载 | 左侧 14px 旋转圆环（`spinner`），文字保留，`pointer-events: none` |
| 焦点 | `outline: 2px solid var(--focus-ring); outline-offset: 2px;`（`:focus-visible`） |

**文案**（沿用 L10n）
- `添加 URL…` / `全部暂停` / `全部恢复` / `清空已完成` / `清空全部`
- `添加` / `取消` / `下载` / `跳过` / `关闭` / `复制全部` / `刷新链接` / `计算校验和` / `限制速度…` / `复制全部为 JSON` / `显示分段` / `隐藏分段`

### 4.2 输入框

```css
.input {
  height: 32px;
  padding: 0 10px;
  border-radius: 8px;
  border: 1px solid var(--border-default);
  background: var(--input-bg);
  color: var(--text-primary);
  font: var(--text-body);
  transition: border-color var(--dur-fast), box-shadow var(--dur-fast);
}
.input::placeholder { color: var(--text-tertiary); }
.input:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}
.input[aria-invalid="true"] { border-color: var(--danger); }
.input:disabled { background: var(--input-bg-disabled); color: var(--text-disabled); cursor: not-allowed; }
```

- 数字输入：`.input--num { width: 88px; text-align: right; font-variant-numeric: tabular-nums; }`，自带 `type="number"` 的步进箭头在 macOS 上显示为原生外观 → 用 `appearance: none` 关掉，改用 `input[type=number]::-webkit-inner-spin-button { display:none; }`。
- 搜索框：`.input--search { height: 30px; padding-left: 30px; }`，左侧 14px 图标绝对定位。
- 路径框（只读展示）：`.input--path { font-family: var(--font-mono); font-size: var(--text-mono); background: var(--track-bg); border-style: dashed; }`

### 4.3 下拉（select）

- 高度 32px，`padding: 0 28px 0 10px`，圆角 8px，`border: 1px solid var(--border-default)`，`background: var(--input-bg)`。
- 右侧 14px `chevron-down` 图标绝对定位，`color: var(--text-tertiary)`；`appearance: none`。
- 下拉面板（原生 `<select>` 由系统绘制；自绘下拉时）：圆角 10px、`--glass-bg-strong`、blur 18px、`--shadow-3`、选项行高 30px、选中项 `--accent-soft`。
- 宽度：`min-width: 160px; max-width: 240px;`（分类下拉在剪贴板弹层里 `width: 100%`）。

### 4.4 开关（switch）

```css
.switch {
  position: relative; width: 38px; height: 22px; flex: 0 0 auto;
  border-radius: 999px; background: var(--track-bg);
  transition: background var(--dur-fast) var(--ease-out);
}
.switch::after {
  content: ""; position: absolute; top: 2px; left: 2px;
  width: 18px; height: 18px; border-radius: 50%;
  background: #fff; box-shadow: var(--shadow-1);
  transition: transform var(--dur-base) var(--ease-out);
}
.switch[aria-checked="true"] { background: var(--accent); }
.switch[aria-checked="true"]::after { transform: translateX(16px); }
```

- 选中态圆点带 `box-shadow: 0 0 0 1px rgba(0,0,0,.06)`。
- `role="switch"` + `aria-checked`。
- 禁用：`opacity:.4; pointer-events:none`。

### 4.5 滑块（slider）

- 轨道：高 4px，圆角 2px，背景 `linear-gradient(90deg, var(--accent) var(--fill,50%), var(--track-bg) var(--fill,50%))`。
- 滑块：16px 白色圆，`border: 1px solid rgba(15,23,42,.12)`，`box-shadow: var(--shadow-1)`；聚焦时 `outline: 2px solid var(--focus-ring); outline-offset: 2px`。
- 容器：`height: 18px; display:flex; align-items:center;` 左侧滑块 `flex:1`，右侧数值输入 72px（`--text-mono`，右对齐），中间间距 12px。
- 刻度：默认线程数滑块范围 1–128，步进 1，右端显示上限 128。

### 4.6 进度条

```css
.progress {
  position: relative; width: 100%; height: 6px;
  border-radius: 999px; background: var(--track-bg); overflow: hidden;
}
.progress__fill {
  position: absolute; inset: 0 auto 0 0;
  border-radius: 999px; background: var(--accent);
  transition: width 200ms linear;
}
```

| 用途 | 高度 | 圆角 |
|---|---|---|
| 列表行 | 6px | pill |
| 抽屉/详情 | 8px | pill |
| 分片表 | 4px | 2px |

- 无总量时（`totalBytes` 未知）：显示不确定态 —— 30% 宽的滑块循环动画：

```css
.progress--indeterminate .progress__fill {
  width: 30%; animation: sdm-indeterminate 1.4s var(--ease-in-out) infinite;
}
@keyframes sdm-indeterminate {
  0%   { transform: translateX(-100%); }
  100% { transform: translateX(333%); }
}
```

- 颜色由状态决定（见 §5），用 `background: var(--state-color)`。

### 4.7 状态徽章

```css
.badge {
  display: inline-flex; align-items: center; gap: 4px;
  height: 20px; padding: 0 8px;
  border-radius: 999px;
  font: var(--text-micro);
  background: color-mix(in srgb, var(--state-color) 16%, transparent);
  color: var(--state-color);
  border: 1px solid color-mix(in srgb, var(--state-color) 28%, transparent);
  white-space: nowrap;
}
```

- 徽章内可带 12px 图标（状态图标）。
- 徽章文字用 `--text-micro`（10px/600）；列表列宽 108px，徽章最大宽 100px，超出省略。
- `--state-color` 由行上的 `data-state` 属性驱动：

```css
.row[data-state="downloading"] { --state-color: var(--accent); }
.row[data-state="queued"]       { --state-color: var(--neutral); }
.row[data-state="paused"]       { --state-color: var(--warning); }
.row[data-state="completed"]    { --state-color: var(--success); }
.row[data-state="failed"]       { --state-color: var(--danger); }
.row[data-state="cancelled"]    { --state-color: var(--neutral); }
.row[data-state="awaiting_new_link"] { --state-color: var(--warning-strong); }
```

### 4.8 下载列表行

- 结构：`.list-row`（grid） → 9 个 `.col-*` 单元格。
- 状态图标列：18px 图标，颜色 `var(--state-color)`。
- 文件名列：两层（文件名 / 主机名）。整体 `min-width: 0`。
- 进度列：进度条 + 百分比。
- 操作列：`display: flex; gap: 4px; justify-content: flex-end;`
- 行悬停时操作按钮从 `opacity: .55` 变为 `1`（不是从 0 出现，避免抖动）。

### 4.9 分片状态表

列模板（轨道数 = 可见单元格数，与 §2.4 同一条纪律）：

```css
/* 抽屉内基准 —— 5 列：ID 字节范围 进度 耗时 状态 */
:root { --chunk-cols: 36px minmax(72px,1fr) 92px 48px 60px; }
.chunk-head, .chunk-row {
  display: grid; grid-template-columns: var(--chunk-cols);
  align-items: center; column-gap: 8px; padding: 0 8px;
}
```

| 列 | 宽度 | 内容 |
|---|---|---|
| 分片 ID | 36px | `00`…`31`，`--font-mono`，`--text-caption`，右对齐 |
| 字节范围 | `minmax(72px, 1fr)` | `0 B – 2.0 MB`，`--font-mono`，`--text-caption`，溢出省略 |
| 进度 | 92px | 4px 进度条 + 右侧 36px 百分比（`tabular-nums`） |
| 耗时 | 48px | `1.2s` / `45s` / `3m` / `—`，右对齐，`--font-mono` |
| 状态 | 60px | 12px 图标 + 文字（等待 / 下载中 / 完成 / 失败） |

> **固定开销 = 36 + 92 + 48 + 60 = 236px，gap 4×8 = 32px，合计 268px。**
> 抽屉内容区宽度 = 抽屉宽 − 左右 padding 40px：
> L 抽屉 440 → 内容 400 → 字节范围得 132px OK；
> M 抽屉 400 → 内容 360 → 字节范围得 92px OK（≥72 下限）；
> S 抽屉 360 → 内容 320 → 字节范围只剩 52px（< 72 下限 → 必须减列）。
>
> **因此 S 断点隐藏「字节范围」列**，字节范围改由进度条的 `title` 提示：

```css
/* ≤1039px（S 及更窄）：隐藏「字节范围」列 */
@media (max-width: 1039px) {
  :root { --chunk-cols: 36px 92px 48px 60px; }
  .col-c-range { display: none; }
}
```

隐藏后固定列 236 + gap 3×8 = 260px，在 S 抽屉的 320px 内容区里剩 60px 余量；XS 抽屉全宽（≥608px）充裕。

- 行高 28px，`--text-caption`。
- 状态色：等待 `--neutral`、下载中 `--accent`、完成 `--success`、失败 `--danger`。
- **耗时 > 15s 且状态不是「完成」时**，耗时数字变 `--danger`（沿用项目既有的停滞检测语义）。
- 表头 sticky `top: 0`，玻璃底。
- 容器：`max-height: 260px; overflow-y: auto;`（抽屉内）。
- 抽屉底部固定一个「复制 JSON」按钮，复制整条任务的 JSON（沿用项目早期字段：`id / filename / url / state / bytesDone / totalBytes / speedBytesPerSec / speedLimitBytesPerSec / destination / error`）。

### 4.10 日志表

```css
:root { --log-cols: 100px 64px 140px minmax(200px,1fr); }
.log-head, .log-row {
  display: grid; grid-template-columns: var(--log-cols);
  align-items: baseline; column-gap: 10px; padding: 0 16px;
}
/* 弹层窄于 720px：隐藏「分类」列 */
@media (max-width: 719px) {
  :root { --log-cols: 100px 64px minmax(200px,1fr); }
  .col-log-cat { display: none; }
}
```

| 列 | 宽度 | 样式 |
|---|---|---|
| 时间 | 100px | `--font-mono` 12px，`--text-secondary` |
| 级别 | 64px | `--font-mono` 12px，语义色 |
| 分类 | 140px | `--font-mono` 12px，`--text-tertiary` |
| 消息 | `minmax(200px, 1fr)` | `--font-mono` 12px，`--text-primary`，`white-space: pre-wrap; word-break: break-word;` |

> 核算：日志弹层宽 `min(900px, 100vw − 48px)`，内边距 24×2 → 内容区 = 弹层宽 − 48。
> 900px 弹层 → 内容 852 − 固定 304 − gap 30 = **消息列 518px** OK；
> 640px 窗口 → 弹层 592 → 内容 544 − 固定 304 − gap 30 = 210px OK（≥200）；
> 弹层窄于 720px 时隐藏「分类」列，消息列获得额外 150px。

- 行高 22px，行间无竖线，只有 1px `--border-subtle` 底边（透明度 50%）。
- 时间格式 `HH:mm:ss.SSS`。

### 4.11 空状态

- 容器：`display:flex; flex-direction:column; align-items:center; justify-content:center; gap: 12px; min-height: 240px; padding: 40px 24px;`
- 插图：56px，线框 SVG，`stroke-width: 1.5`，`color: var(--text-tertiary)`。
- 标题：22px/600 `--text-primary`。
- 说明：13px `--text-secondary`，`max-width: 420px`，`text-align: center`。
- 提示行：11px `--text-tertiary`。

### 4.12 弹层（modal）

```css
.modal-scrim {
  position: fixed; inset: 0; z-index: 50;
  background: rgba(15,23,42,0.35);
  -webkit-backdrop-filter: blur(6px);
          backdrop-filter: blur(6px);
  display: flex; align-items: center; justify-content: center;
  padding: 24px;
}
.dialog {
  width: var(--dialog-w, 460px);
  max-width: 100%;
  max-height: calc(100vh - 80px);
  overflow-y: auto;
  border-radius: var(--radius-xl);
  background: var(--glass-bg-strong);
  -webkit-backdrop-filter: blur(30px) saturate(190%);
          backdrop-filter: blur(30px) saturate(190%);
  border: 1px solid var(--border-glass);
  box-shadow: var(--shadow-pop);
  padding: 24px;
  animation: sdm-dialog-in var(--dur-slow) var(--ease-out);
}
@keyframes sdm-dialog-in {
  from { opacity: 0; transform: translateY(12px) scale(0.98); }
  to   { opacity: 1; transform: none; }
}
```

- 宽度：`添加 URL` 460px、`剪贴板确认` 520px、`日志预览` 900px、`站点规则删除确认` 420px。
- 底部按钮区：`margin-top: 20px; display:flex; gap: 8px; justify-content: flex-end;`
- 焦点陷阱：打开时聚焦第一个可聚焦元素（URL 输入框）；`Tab` 在弹层内循环；`Esc` 关闭；关闭后焦点回到触发元素。
- `role="dialog" aria-modal="true" aria-labelledby="<title-id>"`。

### 4.13 横幅（banner）

```css
.banner {
  display: flex; align-items: center; gap: 10px;
  min-height: 48px; padding: 8px 12px;
  border-radius: 12px;
  background: var(--warning-soft);
  border: 1px solid rgba(217,119,6,.30);
}
```

- 图标 18px `--warning`；文字 13px；右侧按钮。
- 展开输入框时：横幅高度从 48px 变为 56px，用 `min-height` + 换行实现，不做高度动画。

### 4.14 侧边栏项

见 §2.2.1。

### 4.15 工具栏

见 §2.3。

### 4.16 右键菜单 / 下拉菜单

```css
.menu {
  position: fixed; z-index: 30; min-width: 200px;
  padding: 6px; border-radius: 10px;
  background: var(--glass-bg-strong);
  -webkit-backdrop-filter: blur(18px) saturate(160%);
          backdrop-filter: blur(18px) saturate(160%);
  border: 1px solid var(--border-glass);
  box-shadow: var(--shadow-3);
}
.menu__item {
  display: flex; align-items: center; gap: 10px;
  height: 28px; padding: 0 10px; border-radius: 6px;
  font: var(--text-body); color: var(--text-primary);
}
.menu__item:hover { background: var(--accent-soft); color: var(--accent); }
.menu__item--danger { color: var(--danger); }
.menu__item--danger:hover { background: var(--danger-soft); color: var(--danger); }
.menu__sep { height: 1px; margin: 5px 8px; background: var(--border-subtle); }
```

- 打开逻辑：右键时 `event.preventDefault()`，在鼠标位置弹出；若右侧剩余空间 < 200px 则向左翻转；若下方剩余空间 < 菜单高度则向上翻转。菜单打开后点击外部或 `Esc` 关闭。
- 菜单项右侧可带快捷键提示（12px `--text-tertiary`，`margin-left: auto`）。
- **行右键菜单项**（顺序固定）：

```
查看详情
─────────
打开                 （仅 completed）
在访达中显示          （仅 completed）
暂停                 （downloading / queued）
取消                 （downloading / queued / paused）
继续 / 重试           （paused / failed / cancelled / awaiting_new_link）
─────────
复制链接
打开来源页
─────────
限制速度…
计算校验和            （仅 completed）
─────────
从列表移除
删除文件
```

### 4.17 Toast

见 §0.5。

### 4.18 日期时间选择器

原生 `<input type="datetime-local">`，高度 32px，样式与 `.input` 一致；`::-webkit-calendar-picker-indicator { opacity: .55; }`。
值以本地时间读写，提交给后端时转 ISO-8601 带时区。

### 4.19 复选框

18×18，圆角 5px，`border: 1px solid var(--border-strong)`；选中时 `--accent` 底 + 12px 白色对勾 SVG。
用于剪贴板弹层的「停止监听剪贴板」。

### 4.20 加载态

- 骨架行：`height` 与真实行一致，`background: linear-gradient(90deg, var(--track-bg) 25%, rgba(15,23,42,.04) 37%, var(--track-bg) 63%)`，`background-size: 400% 100%`，`animation: sdm-shimmer 1.4s ease infinite`。
- 旋转圆环：16px，`border: 2px solid var(--track-bg); border-top-color: var(--accent); border-radius:50%; animation: sdm-spin .7s linear infinite;`

### 4.21 分段控件（语言、更新通道）

高度 28px，`background: var(--track-bg)`，`border-radius: 8px`，`padding: 2px`；选中项 `background: var(--glass-bg-strong)`、`border-radius: 6px`、`box-shadow: var(--shadow-1)`；项内文字 12px/500，选中色 `--accent`。

### 4.22 表格通用

- 表头：`position: sticky; top: 0; z-index: 20;` `background: var(--glass-bg-strong)` + `backdrop-filter: blur(18px)`；文字 `--text-label` 12px/500 `--text-secondary`；可排序列右侧 12px `chevron-up`/`chevron-down`（`aria-sort`）。
- 斑马纹：**不用**。行分隔用 1px `--border-subtle` 底边。
- 选中行：`background: var(--accent-softer)` + 左侧 3px `--accent` 条。

### 4.23 图标清单（Feather 原始 path）

统一：`viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"`。

| id | 用途 | path / 元素 |
|---|---|---|
| `i-download` | 下载中 / 添加 | `<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>` |
| `i-pause` | 暂停 | `<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>` |
| `i-play` | 继续 / 恢复 | `<polygon points="5 3 19 12 5 21 5 3"/>` |
| `i-check-circle` | 已完成 | `<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>` |
| `i-alert-octagon` | 失败 | `<polygon points="7.86 2 16.14 2 22 7.86 22 16.14 16.14 22 7.86 22 2 16.14 2 7.86 7.86"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>` |
| `i-minus-circle` | 已取消 | `<circle cx="12" cy="12" r="10"/><line x1="8" y1="12" x2="16" y2="12"/>` |
| `i-clock` | 排队中 / 计划任务 | `<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>` |
| `i-link` | 等待新链接 | `<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>` |
| `i-plus` | 添加 | `<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>` |
| `i-trash` | 清空 / 删除 | `<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>` |
| `i-folder` | 在访达中显示 | `<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>` |
| `i-external-link` | 打开来源页 | `<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>` |
| `i-copy` | 复制链接 / 复制 JSON | `<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>` |
| `i-refresh-cw` | 重试 / 刷新链接 | `<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>` |
| `i-gauge` | 每任务限速 | `<path d="M12 14l4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>` |
| `i-shield` | 计算校验和 | `<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>` |
| `i-search` | 搜索 | `<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>` |
| `i-more-horizontal` | 更多 | `<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>` |
| `i-chevron-down` | 下拉 | `<polyline points="6 9 12 15 18 9"/>` |
| `i-chevron-left` | 折叠侧栏 | `<polyline points="15 18 9 12 15 6"/>` |
| `i-chevron-right` | 展开侧栏 | `<polyline points="9 18 15 12 9 6"/>` |
| `i-x` | 关闭 / 取消 | `<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>` |
| `i-list` | 全部下载 | `<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>` |
| `i-layers` | 按分类 | `<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>` |
| `i-archive` | 历史 | `<polyline points="21 8 21 21 3 21 3 8"/><rect x="1" y="3" width="22" height="5"/><line x1="10" y1="12" x2="14" y2="12"/>` |
| `i-alarm` | 计划任务 | `<circle cx="12" cy="13" r="8"/><polyline points="12 9 12 13 15 15"/><path d="M5 3 2 6"/><path d="M22 6 19 3"/>` |
| `i-video` | 视频分类 | `<polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2"/>` |
| `i-music` | 音乐分类 | `<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>` |
| `i-image` | 图片分类 | `<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>` |
| `i-file-text` | 文档分类 / 日志 | `<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>` |
| `i-package` | 软件分类 | `<line x1="16.5" y1="9.4" x2="7.5" y2="4.21"/><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>` |
| `i-help-circle` | 其他分类 | `<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/>` |
| `i-settings` | 设置 | `<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>` |
| `i-globe` | 站点规则 | `<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>` |
| `i-power` | 启动 | `<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/>` |
| `i-info` | 关于 | `<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>` |
| `i-clipboard` | 剪贴板候选 | `<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/>` |
| `i-alert-triangle` | 横幅警告 | `<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>` |
| `i-lock` | 密码 | `<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>` |
| `i-eye` / `i-eye-off` | 密码显隐 | `<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>` / `<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>` |
| `i-file-minus` | 删除文件 | `<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="9" y1="15" x2="15" y2="15"/>` |
| `i-corner-up-left` | 从列表移除 | `<polyline points="9 14 4 9 9 4"/><path d="M20 20v-7a4 4 0 0 0-4-4H4"/>` |
| `i-check-square` | 完成 / 勾选 | `<polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>` |
| `i-activity` | 速度 | `<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>` |
| `i-hash`（用 `i-check-square` 替代） | — | — |

---

## 5. 状态映射

### 5.1 下载状态 → 颜色 / 徽章 / 进度条 / 可用操作

| 状态 key | 徽章文案（EN / 中文） | 图标 | 颜色 | 进度条 | 速度列 | ETA 列 | 行内可用操作 | 右键菜单 |
|---|---|---|---|---|---|---|---|---|
| `queued` 排队中 | `Queued` / `等待中` | `clock` | `--neutral` | 空轨道（0%），填充 `--neutral` 30% 透明 | `—` | `—` | 暂停、取消 | 查看详情 / 暂停 / 取消 / 复制链接 / 打开来源页 / 限速 / 移除 / 删除文件 |
| `downloading` 下载中 | `Downloading` / `下载中` | `download` | `--accent` | `--accent` 填充，200ms 线性过渡 | 主文本色，`3.1 MB/s` | `2m 10s` | 暂停、取消 | 查看详情 / 暂停 / 取消 / 复制链接 / 打开来源页 / 限速 / 移除 / 删除文件 |
| `paused` 已暂停 | `Paused` / `已暂停` | `pause` | `--warning` | 保留最后进度，填充改为 `color-mix(in srgb, var(--warning) 45%, transparent)` + 45° 斜纹 | `--text-tertiary` | `—` | 继续、换链接、取消 | 查看详情 / 继续 / 换链接 / 复制链接 / 打开来源页 / 限速 / 移除 / 删除文件 |
| `completed` 已完成 | `Completed` / `已完成` | `check-circle` | `--success` | 100% `--success` 填充 | `—` | `—` | 打开、在访达中显示 | 查看详情 / 打开 / 在访达中显示 / 复制链接 / 打开来源页 / 计算校验和 / 移除 / 删除文件 |
| `failed` 失败 | `Failed` / `失败` | `alert-octagon` | `--danger` | 保留进度但填充改 `--danger`（表达「停在失败点」） | `—` | `—` | 重试、换链接、从列表移除 | 查看详情 / 重试 / 换链接 / 复制链接 / 打开来源页 / 限速 / 移除 / 删除文件 |
| `cancelled` 已取消 | `Cancelled` / `已取消` | `minus-circle` | `--neutral` | 空轨道 | `—` | `—` | 重试、从列表移除 | 查看详情 / 重试 / 复制链接 / 打开来源页 / 移除 / 删除文件 |
| `awaiting_new_link` 等待新链接 | `Awaiting link` / `等待新链接` | `link` | `--warning-strong` | 保留进度，填充 `--warning-strong` 45% | `—` | `—` | 刷新链接、复制链接 | 查看详情 / 刷新链接 / 换链接 / 复制链接 / 移除 |

补充规则：
- 行首状态图标使用与徽章同色；`failed` 行额外在文件名下方显示一行 11px `--danger` 错误摘要（最多 1 行，截断，完整错误在详情抽屉）。
- `completed` 行的进度条在 100% 后不再动画；`downloading` 行的进度条每 200ms 更新一次。
- 状态切换必须有 150ms 的颜色过渡（`transition: background var(--dur-fast), color var(--dur-fast)`）。
- `failed` 行不做整行红底，只把图标、徽章、进度条变红，避免大量失败任务时整屏刺眼。

### 5.2 分片状态

| 状态 | 图标 | 颜色 | 徽章文案 |
|---|---|---|---|
| 等待 pending | `circle`（空心） | `--neutral` | `等待` / Pending |
| 下载中 active | `download` | `--accent` | `下载中` / Active |
| 完成 done | `check-circle` | `--success` | `完成` / Done |
| 失败 failed | `alert-octagon` | `--danger` | `失败` / Failed |

### 5.3 日志级别

见 §1.3 日志级别色表；短标签 `DEBUG / INFO / NOTICE / WARN / ERROR`。

### 5.4 分类

见 §1.3 分类色表；分类图标固定颜色，不随状态变化。

---

## 6. 交互细则

### 6.1 拖拽 URL 入队

- 拖拽源：浏览器地址栏、其它应用的文本、`.webloc` / 链接。
- `dragover` 时（`e.dataTransfer.types` 含 `text/uri-list` 或 `text/plain`）：
  - 整个 `.main` 出现拖拽提示层：`.drag-overlay { position: fixed; inset: 12px; z-index: 70; border: 2px dashed var(--accent); border-radius: 16px; background: var(--accent-softer); display:flex; align-items:center; justify-content:center; font: var(--text-title); color: var(--accent); }`，文案「松开以添加下载」。
- `drop` 时解析顺序：`text/uri-list` → `text/plain`。每行一个 URL，逐个校验 `http:`/`https:`。
- 合法 URL：立即入队（`store.add(url)`），并 Toast「已添加 N 个下载」。
- 非法内容：Toast 错误「不是有效的 http(s) 链接」，不弹窗打断。
- 拖入的是**本地文件路径**（`file://`）→ 不接受，Toast「只支持 http(s) 链接」。
- 拖拽提示层在 `dragleave`（`relatedTarget` 为 null 时）或 `drop` 后移除。

### 6.2 剪贴板确认

- 捕获时机：主窗口获得焦点后每 1 秒轮询一次剪贴板（Python 侧 `NSPasteboard` / Windows clipboard API，比 JS `navigator.clipboard.read()` 更可靠，因为 WebView 不一定有权限）。
- 判定：`FileURLHeuristic.isLikelyFileURL(url)` —— 路径扩展名能映射到非 `other` 分类才算候选。
- 去重：同一 URL 5 分钟内只提示一次（`lastSeenURLs`）。
- 队列：同时只弹一个；用户处理完一个后，若队列里还有下一个，等 400ms 再弹。
- 「跳过」→ 记入 `lastSeenURLs`，不入队。
- 「下载」→ `addWithConfig(url, category, saveDirectory, maxThreads)`，然后关闭弹层。
- 「停止监听剪贴板」→ 写 `sdm.capture.clipboard=false` + 关闭弹层 + Toast「已停止监听剪贴板」。
- 若设置里 `sdm.capture.clipboard=false`，不捕获、不弹窗。

### 6.3 过期链接重链

1. 引擎检测到 403/410/451 → 任务转 `awaiting_new_link`，**保留已下载的字节**。
2. GUI 收到事件 → 顶部出现过期链接横幅（§2.12），该任务所在行也显示 `等待新链接` 徽章。
3. 用户在横幅输入新 URL 点「应用」，或在行右键菜单点「刷新链接」→ 打开同一个输入弹层（带该任务的文件名作为标题）。
4. 后端用 `ExpiredLinkHandler.evaluateRelink(candidate)` 校验：

| 判定 | 前端表现 |
|---|---|
| `safeToResume` | 直接续传；Toast「已恢复下载」；横幅消失 |
| `unsafeButProceed(reason)` | 二次确认弹层：「新链接与原文件不完全一致（原因），仍要继续？」→ 继续 / 取消 |
| `unsafeReject(reason)` | 错误弹层：「新链接校验失败（原因）」，只给「取消」；横幅保留 |

5. 「刷新链接」失败时不改变任务状态，横幅保留。

### 6.4 行右键菜单

- 触发：`contextmenu` 事件，`preventDefault()`，自绘菜单（不用系统原生菜单，pywebview 下原生菜单不可控）。
- 右键的行同时被选中（若尚未选中）。
- 菜单项按 §5.1 的「右键菜单」列动态生成；不适用的项**不渲染**（不置灰）。
- 菜单项较多时超过窗口高度 → 菜单自身 `max-height: calc(100vh - 24px); overflow-y: auto;`。
- 点击菜单项后菜单立即关闭并执行动作；`删除文件` 与 `清空全部` 需要二次确认弹层。

### 6.5 设置项即时生效时机

| 设置项 | 键 | 生效时机 |
|---|---|---|
| 语言 | `sdm.locale` | **立即**：前端重渲染所有文案（不重启） |
| 主题 | `sdm.theme` | 立即：切换 `<html data-theme>` |
| 毛玻璃开关 | `sdm.appearance.glass` | 立即：切换 `html.no-glass` |
| 启用自动分类 | `sdm.classify.enabled` | 立即：影响之后新建任务的落盘目录；已在队列的任务不受影响 |
| 剪贴板捕获 | `sdm.capture.clipboard` | 立即：关闭后停止轮询；正在显示的候选弹层保留 |
| 完成通知 | `sdm.notify.onComplete` | 立即：影响下一次完成事件 |
| 关闭窗口时隐藏 Dock 图标 | `sdm.hideDockWhenWindowClosed` | 立即调用平台 API 切换激活策略（macOS `NSApp.setActivationPolicy`，Windows 改窗口创建参数需重启 → **Windows 上标记为「重启后生效」**） |
| 工具栏显示文字标签 | `sdm.toolbar.showLabels` | 立即：工具栏按钮重排 |
| 忽略 SSL 验证 | `sdm.network.ignoreSSL` | 立即：推给引擎（`engine.setIgnoreSSL`），影响后续请求；已在途的请求不中断 |
| 默认线程数 | `sdm.threads.default` | 立即：影响之后新建的任务；已存在的任务保持原线程数 |
| 全局带宽限制 | `sdm.bandwidth.bytesPerSec` | 立即：引擎限速器热更新，**正在下载的任务立即受影响** |
| 分类规则（扩展名） | `sdm.categories.*` | 立即：影响之后新建任务的分类 |
| 站点规则 | `sdm.siteRules` | 立即：影响之后发起的请求；已开始的请求不中断 |
| 开机自启 | `sdm.openAtLogin` | 立即调用平台 API（macOS `SMAppService` / Windows 注册表 Run 键） |
| 更新通道 | `sdm.updateChannel` | 立即：影响下一次检查更新 |
| 日志级别 | `sdm.logs.level` | 立即：推给中央 logger |
| 日志持久化 | `sdm.logs.persistence` | 立即 |
| 日志保留天数 / 轮转上限 / 行数上限 | `sdm.logs.retentionDays` / `maxBytesKB` / `maxLines` | 立即（清理在下一次写入时执行） |
| 工具栏标签显隐 | 同上 | 立即 |
| 侧栏折叠 | `sdm.ui.sidebarCollapsed` | 立即 + 持久化 |

所有设置写入走 `userdefaults.json`（或 Python 端 JSON 配置），前端通过 `window.pywebview.api` 读写；写成功后由后端广播事件，前端订阅刷新。

### 6.6 选中与详情联动

- 单击行 → 选中（不自动开抽屉，避免误触）。
- 双击行 / `Enter` / 右键「查看详情」→ 打开右侧抽屉。
- 抽屉打开时该行保持选中态；若该任务从列表消失（被移除），抽屉自动关闭并 Toast「任务已不存在」。
- 抽屉内的进度/速度/ETA 与列表同一数据源，同步刷新。

### 6.7 排序与搜索

- 列头点击排序，循环：升序 → 降序 → 取消排序。`aria-sort="ascending|descending|none"`。
- 默认排序：`state` 升序（下载中优先、排队中其次、其余按加入时间倒序）。
- 列表为空时（被筛选器过滤掉）显示空状态，文案区分「还没有下载」和「该分类下没有任务」。

---

## 7. 无障碍与键盘

### 7.1 焦点顺序

```
1. 跳过导航链接（.skip-link，仅键盘 Tab 时可见）
2. 侧栏：折叠按钮 → 各分组标题（不可聚焦）→ 各侧栏项
3. 主区：工具栏按钮（从左到右）
4. 列表：表头（可排序列）→ 每一行（行本身是 gridcell 容器，单个 tabindex）
5. 抽屉内容（打开时）
6. 弹层内容（打开时）
7. Toast（不参与 Tab 顺序，用 `aria-live` 播报）
```

- 抽屉与弹层打开时，`Tab` 被限制在容器内（focus trap）；`Esc` 关闭。
- 关闭抽屉/弹层后焦点回到触发元素（行 / 按钮）。
- 焦点环统一：`outline: 2px solid var(--focus-ring); outline-offset: 2px;`，用 `:focus-visible` 触发（鼠标点击不显示）。

### 7.2 快捷键

| 快捷键 | 动作 | 作用域 |
|---|---|---|
| `⌘N` / `Ctrl+N` | 添加 URL 弹层 | 全局 |
| `⌘⇧A` | 全部暂停 | 主窗口（下载列表视图） |
| `⌘⇧R` | 全部恢复 | 主窗口 |
| `⌘⇧C` | 清空已完成 | 主窗口 |
| `⌘F` / `Ctrl+F` | 聚焦搜索框（历史视图） | 视图 |
| `⌘,` | 打开设置窗口 | 全局 |
| `⌘0` | 聚焦主窗口 | 全局 |
| `⌘W` | 关闭窗口 | 全局（系统） |
| `Esc` | 关闭抽屉 / 弹层 / 菜单 | 局部 |
| `↑` `↓` | 在行之间移动选择 | 列表 |
| `Home` / `End` | 跳到首行 / 末行 | 列表 |
| `Space` | 暂停 / 恢复选中任务（`downloading/queued`→暂停；`paused`→继续） | 列表 |
| `Enter` | 打开选中行的详情抽屉 | 列表 |
| `Delete` / `Backspace` | 取消选中任务（需确认） | 列表 |
| `⌘C` | 复制选中任务的 URL | 列表 |
| `⌘⇧C` | 复制任务 JSON | 详情抽屉内 |
| `⌘⇧J` | 在 Console / 日志文件中打开 | 设置 → 日志 |
| `Tab` / `Shift+Tab` | 前后切换焦点 | 全局 |

- macOS 用 `⌘`，Windows 用 `Ctrl`。快捷键在 `keydown` 里按 `event.metaKey || event.ctrlKey` 统一判断。
- `⌘F` 必须 `preventDefault()`（否则触发 WebView 内置查找）。
- `Delete` 取消任务需要确认弹层：「取消这个下载？已下载的部分会被丢弃。」→ 取消 / 确认取消。

### 7.3 对比度要求

| 内容 | 最低对比度（亮色 / 暗色） |
|---|---|
| 正文与背景 | 4.5:1 |
| 标题、徽章文字、按钮文字 | 4.5:1 |
| 图标、边框、输入框描边（非文字） | 3:1 |
| 禁用文字 | 不强制，但不得与背景对比度高于 4.5:1（避免误读为可用） |
| 焦点指示器与相邻色 | 3:1 |

- 具体校验：亮色 `--text-primary #0F172A` on `--glass-bg`（≈#F3F5FA）≈ 15.8:1 OK；`--text-secondary #475569` ≈ 7.2:1 OK；`--text-tertiary #8A97AC` ≈ 3.4:1（仅用于 11px 以下的非关键信息，**不得**用于需要阅读的正文，若要用于正文需加深到 `#64748B` ≈ 4.8:1）。
- 暗色 `--text-primary #E9EEF8` on ≈#141B2D ≈ 13.6:1 OK；`--text-tertiary #6E7D96` ≈ 4.0:1（同上限制）。
- 状态色在徽章上：徽章底是 16% 透明度的主色，文字是 100% 主色 —— 例如 `--success #16A34A` on `rgba(22,163,74,.16)` 覆白底 ≈ 4.0:1，**徽章文字加粗到 600 且 10px 时按 WCAG 大文本不达标**，因此徽章文字必须使用 ≥ 4.5:1 的颜色：`--success` 在亮色下改用 `#15803D`（≈5.2:1），`--danger` 用 `#B91C1C`（≈6.1:1），`--warning` 用 `#B45309`（≈4.9:1），`--accent` 用 `#1D4ED8`（≈6.3:1）。**徽章专用变量**：

```css
:root {
  --badge-queued: #475569;
  --badge-downloading: #1D4ED8;
  --badge-paused: #B45309;
  --badge-completed: #15803D;
  --badge-failed: #B91C1C;
  --badge-cancelled: #475569;
  --badge-awaiting: #C2410C;
}
@media (prefers-color-scheme: dark) {
  :root {
    --badge-queued: #CBD5E1;
    --badge-downloading: #93B4FB;
    --badge-paused: #FCD34D;
    --badge-completed: #6EE7B7;
    --badge-failed: #FCA5A5;
    --badge-cancelled: #CBD5E1;
    --badge-awaiting: #FDBA74;
  }
}
```

- 暗色模式下徽章底仍用主色 16% 透明度，但底色叠加在深色玻璃上时实际更暗 → 对比度更高，无需额外处理。

### 7.4 ARIA

| 区域 | 语义 |
|---|---|
| 侧栏 | `<nav aria-label="下载分类">`，每项 `role="button"` + `aria-current="page"`（当前项）+ `aria-label="下载中，2 项"` |
| 下载列表 | `role="grid"`，行 `role="row" aria-selected`，单元格 `role="gridcell"`，`aria-rowcount`，表头 `role="columnheader" aria-sort` |
| 状态徽章 | `<span class="badge">` 内文字即状态；行上 `aria-label="movie.mkv，下载中，52%"` |
| 开关 | `role="switch" aria-checked` |
| 滑块 | `role="slider" aria-valuemin aria-valuemax aria-valuenow aria-valuetext` |
| 进度条 | `role="progressbar" aria-valuenow aria-valuemin aria-valuemax aria-valuetext="52%"` |
| 抽屉 | `role="complementary" aria-label="任务详情"`，标题 `tabindex="-1"` |
| 弹层 | `role="dialog" aria-modal="true" aria-labelledby` |
| 右键菜单 | `role="menu"`，项 `role="menuitem"`，`aria-haspopup="menu"` |
| Toast | 容器 `aria-live="polite" aria-atomic="false"` |
| 错误信息 | `role="alert"` |
| 剪贴板候选 | `role="dialog" aria-modal="true"`，焦点落在 URL 只读文本上 |

- 状态变化（任务完成/失败）通过 `aria-live="polite"` 区域播报：「movie.mkv 下载完成」。
- 侧栏计数变化的文本包含在 `aria-label` 中（视觉上只是数字）。
- 纯装饰图标 `aria-hidden="true"`；有语义的图标按钮必须有 `aria-label`。

### 7.5 其它

- 所有动效遵守 `prefers-reduced-motion`。
- 颜色不是唯一信息载体：状态同时用图标 + 文字 + 颜色三重编码。
- 正文最小字号 11px（仅限辅助信息），正文正文不低于 12px。
- 支持系统缩放（浏览器缩放 / OS 缩放）到 200% 时布局不破：正文用 `px` 而非 `vw` 排版，容器用 `min-width: 0`。

---

## 8. 实现最容易踩的三个布局点

**① 网格列的「模板轨道数必须等于可见单元格数」，且宽度核算必须算上 gap 与 padding。**
下载列表用 `grid-template-columns: var(--dl-cols)`，断点处把隐藏列设成 `display:none`。如果只在 `display:none` 之后忘了同步减少模板轨道，第 9 个单元格会掉进隐式轨道（宽度变成 `auto`），表现为「操作按钮跑到最右边还多出一段空白」。修复方式：每个断点整体重写 `--dl-cols`，轨道数 = 该断点可见列数（9/7/6/4），表头和行共用同一个变量。
更要命的是**算列宽时漏掉 `column-gap` 和左右 padding**：9 列固定轨道若定成 720px，在 1180px 窗口下主区可用宽只有 920px，减去 8 个 10px gap 和 28px padding 后固定列已占 808px，**文件名被压到 112px，低于它 200px 的下限 → 整行溢出出现横向滚动条**。正确做法是把「固定列合计 + gap×(列数−1) + padding×2」当作不可协商开销，文件名作为唯一 `1fr` 弹性列，核算表见 §3.2。
同理，grid 的第二列必须写 `minmax(0, 1fr)` 而不是 `1fr` —— 否则长文件名的 flex 子项默认 `min-width:auto` 会把整列撑破。
另一个连带坑：`position: absolute` 的选中条（`::before`）需要 `.list-row` 本身是 `position: relative`，否则它会相对更外层的定位祖先定位，左边缘对齐全错。

**② `backdrop-filter` 会被祖先的 `transform` / `filter` / `overflow:hidden` 破坏。**
毛玻璃要求「祖先不能有 transform」。如果抽屉、Toast、弹层被放在某个带 `transform: translateX(...)` 的父容器里，或者主区用了 `overflow:hidden`，会出现两种故障：(a) `position: fixed` 的抽屉被包含块改成那个 transform 祖先，位置错乱、遮罩裁切；(b) `backdrop-filter` 取不到背景，玻璃变成纯色。修复：抽屉、遮罩、Toast、弹层全部作为 `body` 的直接子元素挂载（`position: fixed` + `z-index` 分层，见 §1.9），主区用 `overflow: hidden` 时不要把 fixed 元素放进去；玻璃容器自身不要加 `transform`（需要位移动画时用 `opacity` 或把 transform 移到内层子元素上）。

**③ 焦点环与滚动容器裁剪、以及「打开详情不改变主区宽度」。**
两个常见翻车点：(a) 焦点环用 `box-shadow` 实现时会被父级 `overflow:hidden` 裁掉，只剩半圈 —— 必须用 `outline` + `outline-offset`，且键盘可达的滚动容器本身要留 2px 余量；(b) 详情面板如果做成「主区 `margin-right` 变大」的挤压式，抽屉打开的瞬间列宽重排 → 文件名换行 → 行高跳动 → 列表滚动位置跳变。规范因此规定详情是 overlay 抽屉（`transform: translateX()` 动画，主区宽度完全不变），遮罩 `pointer-events` 控制点击穿透，并在 XS 断点把抽屉宽度改成 `calc(100vw - 32px)` 保持同样的「不挤压」语义。

---

## 附：实现检查清单

- [ ] 640×440 窗口下：侧栏变图标轨、列表 4 列、抽屉全宽、弹层可滚动、无横向滚动条
- [ ] 1180×760 窗口下：列表 9 列全显示，行高 44px
- [ ] 1440px 以上：列表内容最大宽 1400px 居中
- [ ] 关闭 `backdrop-filter`（DevTools 模拟）后所有卡片仍可读、无透明文字
- [ ] 深色模式下所有徽章文字对比度 ≥ 4.5:1
- [ ] 键盘 Tab 一圈能走完所有可交互元素，无焦点陷阱（除弹层/抽屉内）
- [ ] 拖入非法 URL 有错误提示，不静默失败
- [ ] 剪贴板弹层可跳过、可关闭监听、5 分钟内不重复提示
- [ ] 过期链接横幅的三种判定结果都有对应的前端表现
- [ ] 所有「设置项生效时机」表中的项都实测过立即生效
