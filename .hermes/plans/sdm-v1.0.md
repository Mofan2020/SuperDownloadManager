# SDM v1.0 — 全自研下载器

仓库：`Mofan2020/SuperDownloadManager`（本地先开仓，`git init`，commit 不 push 等用户明确）
许可证：MIT
部署目标：macOS 26.0（Apple Silicon only macOS 26+ 也行，Swift 5.9 语言模式）
语言：Swift 5.9（用 Xcode 27 SDK 编译，以 `-swift-version 5` 模式）
目录：`~/Documents/MyProjects/SuperDownloadManager/`

## 已锁定决策

| # | 项 | 决策 |
|---|---|---|
| 1 | 命名 | `SuperDownloadManager`（缩写 **SDM**，**M 在最后不是 D**） |
| 2 | 架构 | 全自研 Swift，不用 aria2 |
| 3 | 仓库 | `Mofan2020/SuperDownloadManager` MIT |
| 4 | macOS 部署目标 | 26.0 |
| 5 | 默认线程 | 32，上限 128 |
| 6 | 默认段大小 | 2MB（启动段数 = min(32, Content-Length / 2MB)） |
| 7 | 动态分段策略 | **激进版**，启动后 10 秒即可触发首次重组；单段每 5 秒采样一次；合并阈值 < 0.3，分裂阈值 > 1.5 |
| 8 | 下载分类 | Video / Pictures / Audio / Archives(dmg/zip/tar/7z/rar/iso/pkg + 无扩展名 magic byte 命中 Mach-O/ZIP 的) / Documents / Other |
| 9 | 智能限速 | 令牌桶算法，粒度 100ms；最小 100 KB/s，0 = 不限速；规则（时间段/进程优先级）要可扩展 |
| 10 | 浏览器接管 | Firefox: `browser.downloads.onDeterminingFilename` 钩子 + 本地 HTTP `127.0.0.1:7955/add` 派单；Safari: 嗅探+悬停按钮 + 派单（**接管能力以 Firefox 为主**，Safari 用户走手动按钮派单） |
| 11 | 嗅探 | 扩展 content script 注入，监听 `mouseover` + `contextmenu`，识别 `<a href>` / `<video src>` / `<audio src>` / `Content-Disposition` 嗅探 |
| 12 | 后台常驻 | `LSUIElement=false`（保留 Dock 入口），`MenuBarExtra` + `WindowGroup(id:)` + `openWindow(id:)`；菜单栏常驻 |
| 13 | 开机自启 | LaunchAgent plist（`~/Library/LaunchAgents/`），`launchctl bootstrap` 加载 |
| 14 | CLI | `~/.local/bin/sdmdm` 软链；CLI ↔ daemon 走 Unix Socket；子命令 add/list/pause/resume/cancel/config/sniff/**relink** |
| 15 | 自动更新 | **Sparkle 2**（MIT，SwiftPM）；appcast 自托管在 GitHub Releases；GUI "检查更新" + 后台 24h 自动 |
| 16 | 图标 | 自画 SVG → .icns（菜单栏用 monochrome，App 彩色） |
| 17 | 过期链接续传 | 见 P3 子任务表 |

## 进程模型

```
┌─────────────────────┐                    ┌─────────────────────┐
│  SDM.app (GUI)       │  Unix Socket       │  sdmd (headless)     │
│  SwiftUI MenuBar+Win│◄──── IPC ──────────►│  DownloadEngine     │
│  LaunchAgent 启    │   ~/sdmd.sock      │  默认 32 / 128 上限  │
└─────────────────────┘                    └─────────────────────┘
         │                                         ▲
         │                              ┌──────────┴────────┐
    ┌────┴─────┐    派单 API             │  sdmdm CLI         │
    │  Safari  │ 127.0.0.1:7955/add     │  ~/.local/bin      │
    │  Firefox │ ──────────────────────►└────────────────────┘
    │ 扩展     │
    └──────────┘
```

**关键点**：`sdmd` 是核心 daemon；GUI 是 daemon 的控制台；AI 调用 CLI = 跟 daemon 通讯，**与 GUI 互不冲突；用户关掉 GUI 也不会中断下载**。

## 模块结构

```
SuperDownloadManager/
├── project.yml                       # XcodeGen 工程定义
├── Package.swift                     # 不可用（macOS app 不能纯 SPM）；依赖通过 project.yml
├── App/                              # GUI（SwiftUI menu bar）
│   ├── SDMApp.swift
│   ├── StatusMenuController.swift
│   ├── MainWindow/                   # 任务列表/详情/设置
│   ├── UpdateService.swift           # Sparkle 集成
│   └── ...
├── Engine/                           # 下载引擎（核心）
│   ├── DownloadEngine.swift           # 任务调度器
│   ├── HTTPDownloader.swift            # 单段下载
│   ├── RangeManager.swift              # 动态分段
│   ├── ConnectionPool.swift            # 连接复用
│   ├── SpeedLimiter.swift              # 智能限速（令牌桶）
│   ├── FileAssembler.swift             # 分段合并 + 原子 rename
│   ├── ResumeState.swift               # .sdm sidecar 读写 + content_signature
│   ├── ContentSniffer.swift            # 嗅探 + MIME + magic byte
│   ├── ExpiredLinkHandler.swift        # P3 子任务
│   └── TaskQueue.swift                 # 并发上限控制
├── Daemon/                            # headless sdmd
│   ├── sdmdmain.swift                 # LaunchAgent 注册
│   ├── IPC/                           # Unix Socket 协议
│   │   ├── SocketServer.swift
│   │   ├── Protocol.swift
│   │   └── Dispatcher.swift
│   └── DispatchPolicy.swift           # 派单与限速策略
├── CLI/                               # sdmdm 命令行（独立 target，二进制 1MB 以内）
│   └── main.swift
├── Extensions/
│   ├── Firefox/                       # WebExtension MV2
│   │   ├── manifest.json
│   │   ├── background.js
│   │   ├── sniffer.js                 # content script
│   │   ├── hover-button.css
│   │   └── icons/
│   └── Safari/                        # Safari Web Extension
│       ├── manifest.json
│       ├── background.js
│       ├── sniffer.js
│       ├── hover-button.css
│       └── icons/
├── Launcher/                          # 安装/卸载/LaunchAgent
│   ├── install.sh                     # 首次启动调起
│   ├── autostart.sh
│   └── templates/
│       └── com.skyc8266.superdownloadmanager.daemon.plist
├── Resources/                         # 静态资源
│   ├── AppIcon.iconmodel/             # 设计源（SVG）
│   ├── Assets.xcassets                # 编译后 asset catalog
│   └── Localizable.strings
├── Tests/
│   ├── EngineTests/                   # 单元测试（download logic / RangeManager / SpeedLimiter）
│   ├── CLIIntegrationTests/
│   └── ExpiredLinkTests/              # P3 子任务
├── scripts/
│   ├── build-app.sh                   # xcodebuild + codesign
│   ├── make-dmg.sh                    # DMG 打包（用户偏好的分类位置）
│   └── make-icon.sh                   # SVG → .icns
├── docs/
│   ├── README.md
│   ├── LICENSE                        # MIT
│   ├── notes.md                       # 没动的与原因
│   ├── release.md                     # 发版说明（开发者向）
│   └── extension-install.md           # 浏览器扩展安装指南
└── .gitignore
```

## 阶段划分

### P0 — 工程骨架（这一步）

**目标**：仓库可编译、可 `xcodebuild` 出 .app、可 0 warning 通过。

任务：
1. `git init`，`main` 分支，commit
2. `project.yml` (XcodeGen) 写好，3 个 target：`SuperDownloadManager`（app）、`sdmd`（embedded CLI / 复用 app code）、`SuperDownloadManagerTests`
3. `Info.plist`：`LSUIElement=false`，`LSApplicationCategoryType=public.app-category.utilities`，`CFBundleLocalizations=[en, zh-Hans]`
4. `.entitlements`：`com.apple.security.network.client`、`com.apple.security.app-sandbox=false`（自研 CLI 派单 + 本地 socket 不需要 sandbox；如 sandbox 必开，留口子给 `network.client` + `network.server`）
5. `Assets.xcassets` 占位（先放 SF Symbol，**P7 再换自画 SVG → .icns**）
6. `SDMApp.swift` 一个 `MenuBarExtra` + `WindowGroup(id: "main")`，主窗口用 `ContentUnavailableView` 占位
7. `sdmdmain.swift` 占位（`print("sdmd stub")`，P1-P3 替换为正式 daemon）
8. **构建闭环**：`xcodegen generate && xcodebuild -scheme SuperDownloadManager -configuration Debug build` → BUILD SUCCEEDED，0 warning
9. 资源文件 (`Resources/install-helper.sh`, `templates/*.plist`) 必须显式 `buildPhase: resources`，否则不进 .app
10. `scripts/build-app.sh`：未来 P4 的 release 构建；P0 先空跑通

**验证项**：
- [ ] `xcodebuild ... build` BUILD SUCCEEDED
- [ ] `xcodebuild ... build` 0 warning（`-Werror` 严格）
- [ ] `find build/Build/Products/Debug/SuperDownloadManager.app` 内含 `Contents/MacOS/SuperDownloadManager`、`Contents/Info.plist`、`Contents/Resources/com.skyc8266.superdownloadmanager.daemon.plist`
- [ ] `codesign -dv SuperDownloadManager.app` 显示 `identifier=com.skyc8266.superdownloadmanager.app`、`signature=adhoc`
- [ ] `git log -1 --stat` 单 commit

**commit**：`P0: XcodeGen 工程骨架，3 个 target，Debug 构建 0 warning`

### P1 — 下载引擎基础（核心，技术风险最高）

**目标**：`Engine/` 下核心模块全部实现，能跑通单文件单段 → 多段 → 重组 → 限速 → 续传的全链路。

任务：
1. `ContentSniffer`：HEAD 探测（Accept-Ranges / Content-Length / ETag / Last-Modified）
2. `HTTPDownloader`：`URLSession` async，`Range` header，错误分类（403/410/5xx/超时），backoff 策略
3. `RangeManager`：段表数据结构 + 动态调整算法
   - 合并：(当前段/最快段) < 0.3 持续 5 秒 → 与邻居合并
   - 分裂：(最快段/当前段) > 1.5 且当前段 ≥ 4MB → 中点切两半
   - 重连：100ms → 200ms → 400ms → 800ms → 取消
4. `ConnectionPool`：URLSession 复用 + HTTPMaximumConnectionsPerHost 软上限
5. `SpeedLimiter`：令牌桶，100ms 粒度，0 = 关闭
6. `FileAssembler`：每段独立 `FileHandle`，按 offset seek 写；`.part` 临时文件；全部完成原子 rename
7. `ResumeState`：`.sdm` sidecar JSON `{url, headers, content_signature, segments[], saved_at}`
8. `TaskQueue`：并发上限调度（同 task 内并行段数 = 32，多 task 间按 fair-share）
9. `DownloadEngine`：把上面串成可调度的引擎

**测试**：
- `EngineTests/HTTPDownloaderTests`：mock 一个本地 HTTP server 返回可控 Content-Length / Content-Range / 403
- `EngineTests/RangeManagerTests`：段合并 / 分裂触发条件
- `EngineTests/SpeedLimiterTests`：令牌桶精度 ±5%
- `EngineTests/FileAssemblerTests`：原子 rename 行为，崩溃恢复

**验证项**：
- [ ] `swift test` 全过，coverage ≥ 70%
- [ ] 本地 HTTP server 测试：200MB 文件，32 段，全程高速
- [ ] `xcodebuild -configuration Release build` 0 warning

**commit**：`P1: 下载引擎核心 — Range/SpeedLimiter/FileAssembler/ResumeState`

### P2 — GUI 与文件分类

**目标**：主窗口可看下载任务列表、详情、文件分类规则编辑；菜单栏图标功能可用。

任务：
1. `MainWindow/DownloadListView`：task 列表，分组（下载中/已完成/失败/等待新链接）
2. `MainWindow/TaskDetailView`：分段时间线、当前段数、平均速度、ETA
3. `MainWindow/SettingsView`：默认线程、限速规则、分类规则表、MIME/扩展名映射、启动自启开关
4. `MainWindow/CategoryRulesEditor`：可视化分类规则编辑
5. `MainWindow/ExpiredLinkBanner`："链接已过期，请粘贴新链接" 非阻塞 banner（P3 暴露给 P2 用）
6. `ContentSniffer` 接入 GUI：默认分类规则表
7. `Resources/Localizable.strings` (en + zh-Hans 基础)

**验证项**：
- [ ] `ContentUnavailableView` 占位全部替换为真 UI
- [ ] `CGWindowListCopyWindowInfo` 验证窗口存在（不依赖截图）
- [ ] 开启 GUI 验证文件落到分类目录（P1 已跑，P2 重点测分类规则）

**commit**：`P2: GUI 主窗口 / 任务列表 / 设置 / 分类规则`

### P3 — 文件分类 + 过期链接续传子任务

**目标**：分类准确（含 magic byte 嗅探无扩展名文件）；续传链路完整。

任务：
1. `ContentSniffer` magic byte 嗅探：
   - `0x7F 45 4C 46` → Mach-O → `Software/`
   - `50 4B 03 04` → ZIP → `Archives/`
   - `25 50 44 46` → PDF → `Documents/`
   - 等等，覆盖率 ≥ 95% 常见格式
2. `ExpiredLinkHandler`：
   - 触发：收到 403/410/451，标记任务 `awaiting_new_link`
   - 续传前置 HEAD：校验 ETag / Content-Length，提示用户差异
   - 支持 Range 才续传；不支持则提示"必须重下"
   - GUI `ExpiredLinkBanner` 接管
3. `ResumeState.content_signature`：写 sidecar 时一并写
4. `CLI: sdmdm relink <id> <new_url>`：续传链路 CLI 全通

**测试**：
- `EngineTests/ContentSnifferTests`：每种 magic byte 一个 fixture
- `Tests/ExpiredLinkTests`：
   1. 403 + ETag 不变 → 续传成功
   2. 403 + ETag 变 + 长度一致 → 用户二次确认
   3. 新链接不支持 Range → 退化为重下
   4. 新链接文件实际不同 → 阻断
   5. CLI `relink` 全流程

**验证项**：
- [ ] 所有 ExpiredLinkTests 5 个场景全过
- [ ] ContentSnifferTests ≥ 10 种 magic byte 覆盖

**commit**：`P3: 文件分类 + 过期链接续传`

### P4 — 守护进程与 IPC

**目标**：`sdmd` 真正独立跑，CLI `sdmdm` 与 GUI 都能调。

任务：
1. `Daemon/IPC/SocketServer`：Unix Domain Socket，路径 `~/Library/Application Support/SuperDownloadManager/sdmd.sock`
2. `Daemon/IPC/Protocol`：JSONL 协议（每行一个 JSON-RPC 风格请求/响应）
3. `Daemon/IPC/Dispatcher`：方法路由 → DownloadEngine
4. `Daemon/sdmdmain`：LaunchAgent 安装逻辑；接到 socket 后转发到 DownloadEngine
5. 现有 CLI target 接入：实装 add/list/pause/resume/cancel/config/sniff/relink
6. `Launcher/install.sh`：首次启动时调起（用户主动），写 LaunchAgent plist → `launchctl bootstrap gui/$(id -u)`
7. `Launcher/templates/com.skyc8266.superdownloadmanager.daemon.plist`：`KeepAlive=true` + `RunAtLoad=true`，环境变量 `SDM_CONFIG=~/Library/Preferences/com.skyc8266.superdownloadmanager.app.plist`

**验证项**：
- [ ] `~/.local/bin/sdmdm add <url>` 入队成功，CLI 返回 task_id
- [ ] `~/.local/bin/sdmdm list` JSON 输出
- [ ] LaunchAgent plist `plutil -lint` 通过
- [ ] GUI 可连 socket 看到 CLI 加入的任务
- [ ] 关掉 GUI 后 CLI 仍能加任务

**commit**：`P4: sdmd daemon / Unix Socket IPC / LaunchAgent / CLI`

### P5 — 浏览器扩展（Firefox + Safari）

**目标**：Firefox 接管无 bug；Safari 嗅探+按钮可用。

任务：
1. `Extensions/Firefox/`：
   - `manifest.json` MV2
   - `background.js`：`browser.downloads.onDeterminingFilename` 拦截 → `fetch('http://127.0.0.1:7955/add?url=...')` → 取消原下载
   - `sniffer.js` content script：`mouseover` 监听所有 `<a href>` / `<video src>` / `<audio src>`，注入半透明按钮
2. `Extensions/Safari/`：
   - `manifest.json` MV3 (Safari 兼容)
   - 同上 sniff 逻辑；下载触发同样 fetch
   - 注意 Safari Web Extension 也用同一 fetch 接口
3. `Engine/DispatchServer`：`127.0.0.1:7955/add?url=...&referer=...&filename=...` 端点（loopback only）
4. 用户安装指引（`docs/extension-install.md`）：Firefox 直接拖 .xpi，Safari 手动允许未签名

**测试**：
- **Firefox 必测**：从测试网页触发下载 → SDM 接管（必须有真实测试）
- Safari 嗅探按钮注入 DOM 渲染（用 puppeteer / Playwright 替代品测 headless）

**验证项**：
- [ ] Firefox 接管成功，原始下载被取消
- [ ] 嗅探按钮在 hover 时注入，点击后 fetch 成功
- [ ] 鼠标右键 contextmenu 也注入按钮（备选入口）

**commit**：`P5: Firefox/Safari 浏览器扩展 + 派单 API`

### P6 — 智能限速与策略

**目标**：用户可配置限速规则（时间段/最大带宽），按进程/任务优先级。

任务：
1. `SpeedLimiter`：扩展到规则系统 `[[from, to, max_speed], ...]`
2. GUI 设置页：可视化规则编辑
3. CLI `sdmdm config rule add`：CLI 也能配
4. Process priority：Mac 系统级 `setpriority`（Nice 值范围 0-20，**prio 是 Int32 不是 Int**——见 skill pitfall）
5. **测试**：模拟限速下能否让下载速度稳定在目标值的 ±5%

**验证项**：
- [ ] SpeedLimiter ±5% 精度测试
- [ ] Nice 值写入真实生效（但不要在实际生产进程上跑——见 test plan）

**commit**：`P6: 智能限速规则系统`

### P7 — 自动更新 + 图标 + DMG

**目标**：能 sparkly 自更新；自绘图标上线；DMG 打包分类归档。

任务：
1. **Sparkle 集成**：
   - SwiftPM 依赖：`https://github.com/sparkle-project/Sparkle`
   - `UpdateService.swift`：wrap SPUStandardUpdaterController
   - appcast.xml 自托管路径：GitHub Release API 自动生成
   - edDSA 签名
2. **自画图标**：
   - SVG 设计（下行箭头 + 加速弧线）
   - `scripts/make-icon.sh`：SVG → .icns（全部尺寸 + monochrome）
   - `vision_analyze` 验证 1024px 和 32px 两种尺寸的可读性
3. **DMG 打包**：
   - `scripts/make-dmg.sh`：appcast + DMG → 分类到 `~/Downloads/Archives/`（用户偏好）
   - 同时拷贝 Sparkle 公钥到 bundle

**验证项**：
- [ ] Sparkle 集成：手动触发检查更新（用 mock appcast）
- [ ] 图标 1024px / 32px / 16px / mono 4 种视觉验证通过
- [ ] DMG 文件落到 `~/Downloads/Archives/`，`file *.dmg` 显示 "Apple DMG"

**commit**：`P7: Sparkle 自动更新 + 自绘图标 + DMG 打包`

### P8 — 文档 / License / 测试 / 发版

**目标**：仓库开源就绪，发 v1.0.0 Pre-release。

任务：
1. `LICENSE` (MIT)
2. `README.md`（过 humanizer-zh，简洁直接）：功能 + 安装 + 浏览器扩展安装 + CLI 用法 + 截图
3. `docs/release.md`（开发者向，详细）
4. `docs/notes.md`：所有"没动的与原因"——例如：为什么不做 Live2D / 3D 模型 / 公网分发
5. **Pre-ship 走查**：每个新按钮/CLI flag 跑一次"用户按下 → handler → CLI → 副作用"链路，找出 silent no-op
6. `check-docs.py`（沿用 DeepSleep 模板）：自动校验文档与代码同步
7. 跑 `swift test` 全过 + `xcodebuild Release 0 warning` + `plutil -lint` 全过
8. `swift run` 整个 release 构建脚本（build → sign → DMG → edDSA sign → 上传 appcast）
9. **第一次发版流程**：
   - `git tag v1.0.0` → push → 等 CI 通过
   - `gh release edit v1.0.0 --prerelease`
   - 报告星空喵，**等他手测通过** 才 `gh release edit v1.0.0 --prerelease=false`
   - 在此之前不升正式版（沿用 NCM v1.0.1 教训）

**验证项**：
- [ ] `check-docs.py` 全绿
- [ ] `swift test` 全过
- [ ] Release 构建：DMG + appcast + signature 三件套齐
- [ ] GitHub Pre-release 创建成功

**commit**：`P8: 文档 / License / 测试 / 发版发布`

## "过期链接续传"P3 子任务技术细节

| 难点 | 处理 |
|---|---|
| 新链接是否同一文件 | 不能只校验 Content-Length，需校验 ETag / Last-Modified；Content-Length 相同必要，ETag 相同充分 |
| 新链接是否支持 Range | 先 HEAD，没有 `Accept-Ranges: bytes` 或 HEAD 返回 200（不是 206）→ 退化为"必须重下"，提示用户 |
| 新链接 302/301 | URLSession 正常跟随即可 |
| sidecar hash 对应 | sidecar 写 `content_signature = {length, etag, last_modified}` |
| 过期检测提前 | 收到 403 时不仅标记当前段，把任务 `expiry_eta` 推算给用户（参考 `Retry-After` header） |
| 日志可审计 | 写明 trace：原链接 / 新链接 / ETag diff / 丢失进度 |
| 批量续传 | 多个失败任务批量走 `awaiting_new_link`；GUI 列表视图支持批量填基链接 |

## 阶段串行规则

- **P0 → P1**：P1 依赖 P0 的工程骨架 + 构建闭环
- **P1 → P2**：P2 的 GUI 需要 P1 的引擎能跑（单元测试通过）
- **P2 → P3**：P3 复用 P2 的 banner UI
- **P3 → P4**：P4 daemon 把 P1-P3 的引擎接到 CLI / GUI
- **P4 → P5**：P5 浏览器派单 API 由 P4 daemon 提供
- **P5 → P6**：P6 限速规则系统是 P4 daemon 之上的策略
- **P6 → P7**：P7 不影响核心逻辑，可与 P6 异步推进
- **P7 → P8**：发版必须 P7 自更新机制可用（第一次发版就有）

## 关键风险与预案

| # | 风险 | 预案 |
|---|---|---|
| 1 | 动态分段策略 v1.0 激进版可能抖动 | 写明策略参数（合并/分裂阈值）GUI 可调；用户可切保守 |
| 2 | Safari 扩展 macOS 15+ 7 天重签烦扰 | 用户在文档明确告知，非工程问题 |
| 3 | 128 线程请求可能被服务端 rate-limit | RangeManager 检测 429 主动 backoff，不盲干 |
| 4 | 大文件 (>10GB) 多段写入 I/O 瓶颈 | FileAssembler 按段连续写；用 `ftruncate` 预分配 |
| 5 | 自研 Range 服务器兼容 | 单元测试覆盖：CDN / 静态服务器 / 重定向 / 不支持 Range 四种场景 |
| 6 | Sparkle 自更新与 macOS 权限 (Sparkle 2 需 Developer ID) | ad-hoc 签名下 Sparkle 也能用，但需要用户在 GUI 里点 "Always Allow"；文档说明 |
| 7 | 浏览器扩展派单端口被防火墙拦 | 用 `127.0.0.1` 本地起 socket，不对外暴露；扩展失败有 fallback（提示用户手动复制 URL） |

## 不做的事

- Live2D 模型接管（非工程能力）
- 3D 模型预览（暂留口子）
- 公网 URL 上传/分享（破坏性隐私问题）
- BT / Magnet 下载（与本项目定位冲突）
- 多文件包解包（zip 自动解包类特性，P2 之后讨论）

## 借用第三方算法

任何下载调度算法 / 速率采样算法 / Range 合并算法的实现，**借鉴时必须**：
- 在文件顶部声明"借鉴声明"（算法来源、license、事实复用、源码未抄）
- 在 commit message 写明
- 在 `docs/notes.md` 记录

见 macos-native-app-development skill 的 `references/borrowing-third-party-algorithms.md`。
