# SDM v1.0.4 — IDM-级 UI/功能补完

> **目标**：把"能用 + 丑"升到"能 daily-drive + 不像玩具"。一次性做完 P0-P2 全部 12 项；中途不再问你"加不加"。每完成一个 P-level 在 commit 标题里写出来。

## 锁定决策（写代码前确认）

- v1.0.4 = git tag 之前的 commit。
- P0 (5 项) + P1 (4 项) + P2 (3 项) = 12 项。
- 中间不发 Pre-release，每个 P 完成后 commit 到 main + push 触发 CI（自己 push，已授权）。**仅 4 周开发期结束统一打 v1.0.4 Pre-release 一次**。
- 完成后 tag v1.0.4，pre-release；星空喵说"通过"才升 stable。

## 范围：P0 — 5 项 (week 1)

### P0-1. 多列 Table 视图
- 替换 `List { ForEach { TaskRow } }` 为 `Table` (macOS 12+) 7 列
- 列：☑ / 文件名 (含来源域名) / 大小 / 进度条 / 速度 / ETA / 状态 chip
- 列宽自适应 + 拖拽重排 + 列固定（macOS 14+）
- 排序：点击列头（速度/大小/ETA）

### P0-2. 拖 URL 到窗口直接入队
- `NSView` 上 `registerForDraggedTypes([.URL, .string])`
- 多个 URL 一次性入队
- 接受来自 Finder/Safari/Firefox 的拖拽

### P0-3. 剪贴板自动监听
- `NSPasteboard.general.changeCount` 轮询（2 Hz）
- 启发式：发现是 http/https URL + 队列里没同 URL + 用户没在文本框里 → 自动添加
- 设置里有"自动捕获 URL"开关，默认 ON
- 同 URL 5 分钟内不重复入队（去重）

### P0-4. Firefox 接管接通
- `Engine/DispatchServer.swift` 已经实现 `127.0.0.1:7955/add?url=...` 端点
- v1.0.4：在 `AppDelegate.applicationDidFinishLaunching` 启动 `DispatchServer`
- 接受请求 → 调 `engine.start(...)` → 200 + 任务 id
- 检查 `Looper/server` 状态：端口冲突就 warn 一行 + 不启动

### P0-5. 系统通知（下载完成/失败）
- `UNUserNotificationCenter` + bundle id 注册
- 完成时弹通知：标题"下载完成"，副标题文件名
- 失败时通知
- 通知点击 → `sdm://show` 打开主窗口 + 选中该行
- 设置里有"通知"开关

## 范围：P1 — 4 项 (week 2-3)

### P1-1. 自动分类文件夹
- 接管 `DownloadListStore.downloadDirectory()`：按 MIME + 扩展名分目录
  - Video → `~/Downloads/Video/` (mp4, mov, mkv, webm, avi, flv, m4v)
  - Music → `~/Downloads/Music/` (mp3, flac, wav, aac, ogg, m4a)
  - Pictures → `~/Downloads/Pictures/` (jpg, png, gif, webp, heic, svg)
  - Archives → `~/Downloads/Archives/` (zip, 7z, rar, tar, gz, bz2, xz, dmg, iso, pkg)
  - Documents → `~/Downloads/Documents/` (pdf, doc, docx, xls, xlsx, ppt, pptx, txt, md, epub)
  - Software → `~/Downloads/Software/` (Mach-O magic bytes 0xFEEDFACE/0xFEEDFACF/0xCEFAEDFE/0xCFFAEDFE, .app, .exe, .msi, .deb, .apk)
  - Other → `~/Downloads/Other/`
- ContentSniffer 已经识别 magic bytes；只要把分类结果存进 TaskSummary
- GUI 设置里可关掉分类、关掉就全扔 `~/Downloads/`
- 用 SF Symbol + chip 显示分类

### P1-2. 单条限速
- 右键 → "Limit speed…" → 输入 KB/s
- 存到 TaskSummary.speedLimitBytesPerSec
- Engine 启动 chunk 时按 per-task 限速（需要 SpeedLimiter 拆 per-task 队列）
- 显示当前限速在行末 chip

### P1-3. 真正的断点续传
- 现状：resume = 重启从 0（part 文件覆盖前被用也没用）
- v1.0.4：保存 `RangePlanner` 当前 chunk 状态到 `.sdm.json` sidecar
  - `ResumeState` 加 `chunks: [ChunkState]`（每 chunk 记录 `id/start/end/bytesDone/etag`）
  - engine 启动时：sidecar 存在 → 用 `Range: bytes=bytesDone-end` 续传每个 chunk（不是 `bytes=0-`）
  - server 端 ETag/Content-Length 必须一致；不一致就 warn + 重新整文件下
- 单条限速续传后立即生效

### P1-4. 完整右键菜单
- 每行右键：
  - 暂停/继续/取消/重试（同 P0-3）
  - 打开 / 在 Finder 中显示
  - 复制 URL
  - 打开来源页（`NSWorkspace.shared.open(sourceURL)` —— 任务存 originURL）
  - 限速... → P1-2
  - 计算 MD5 / SHA-256（完成后才能算，progress bar 旁边显示）
  - 移除（从队列删，文件保留）
  - 删除（队列删 + 文件删到废纸篓）

## 范围：P2 — 3 项 (week 4)

### P2-1. 下载历史
- 队列 + 完成后自动转到 `~/Library/Application Support/SuperDownloadManager/history.sqlite`
- 主窗口 顶部 Tab 切换：下载中 / 历史
- 历史视图：搜索 + 日期筛选 + 文件大小 + 来源域名
- 双击历史行：再次下载 / 打开文件 / 显示在 Finder
- 30 天自动清理

### P2-2. 计划任务
- 主窗口 + Add URL sheet 里加 "Schedule" 时间选择器（DatePicker）
- engine 启动一个定时器：到点 → enqueue
- 队列行显示 "⏰ 12:30" chip
- 多个 schedule 排队

### P2-3. 站点登录 / 自定义 header
- 设置 → 高级 → 站点规则：
  - hostname → { headers: {Referer, User-Agent, Cookie}, basicAuth: {user, pass} }
- ContentSniffer 启动下载时按 URL 匹配 host 注入 headers
- URLSession 自动带 Cookie jar（`HTTPCookieStorage.shared`）
- 凭据存 Keychain（`kSecClassGenericPassword`）
- GUI 编辑器在 Settings → 站点规则（v1.0.4 给基础 CRUD）

## 不在 v1.0.4 范围（v1.1+）

- Sparkle 2 自动更新（CI 拉不到 binary archive）
- Safari 接管（macOS 26 的 Managed Distribution 限制）
- 队列优先级
- 下载完成后自动运行 shell 命令
- iOS companion
- 视频站点嗅探（YouTube/Twitter）

## 验收标准

- 所有 12 项在主功能流里手动验过（不是单元测试）
- 50+ unit tests + 10+ integration tests
- 跑通 `xcodebuild test` 全绿
- 跑通 1 个真实下载 demo（httpbin.org / 100MB）从添加到完成 + 重新启动 app 后从断点恢复
- v1.0.4 Pre-release asset 上传成功

## 风险

| 风险 | 应对 |
|---|---|
| P1-3 续传跨 ETag 不一致 → 服务端给 200 而非 206 | 失败时回退到从 0 重下 + 提示用户 |
| P2-1 SQLite + Swift concurrency | 用 `sqlite3.h` C API + actor wrapper，不上 GRDB |
| P2-3 Keychain 调试期间忘了删 | 启动时检测 debug build 标志 + 清空 |
| Firefox 接管 127.0.0.1:7955 端口冲突 | 失败时静默不启 + 设置里可改端口 |
| 12 项全做完时间不够 | 先发 v1.0.4-pre1 (P0 5 项) → 再 v1.0.4-pre2 (+P1) → v1.0.4 (+P2) |

## 节奏

| Week | 完成项 | 提交到 main |
|---|---|---|
| 1 | P0-1, P0-2, P0-3, P0-4, P0-5 | 5 commits |
| 2-3 | P1-1, P1-2, P1-3, P1-4 | 4 commits |
| 4 | P2-1, P2-2, P2-3 | 3 commits |
| 4 末 | 全面测试 + tag v1.0.4 Pre-release | +1 commit (notes + tag) |
