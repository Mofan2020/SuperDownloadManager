# 没动的与原因 / Deliberately Omitted

按星空喵硬偏好："项目文档是给所有维护者看的，包括人类，不要把只对 AI 助手有意义的指令写进仓库文档"。本文件只记录**项目级决策与未做事项 + 原因**，给任何人类/AI 维护者看。

## v1.0.0 状态

仓库里的 v1.0.0 已经由 GitHub Actions 发布为 **Pre-release**：

> https://github.com/Mofan2020/SuperDownloadManager/releases/tag/v1.0.0

asset 是 `SuperDownloadManager.app.zip`（ad-hoc 签名、解压拖 `/Applications` 即可）。按星空喵硬性偏好 **未经手测只发 Pre-release** ——星空喵下载手测通过后，agent 把 `gh release edit v1.0.0 --prerelease=false`。

## v1.0 暂不做

| # | 项 | 原因 |
|---|---|---|
| 1 | Live2D 模型接管 | Apple cubism SDK 法律约束；非工程能力 |
| 2 | 3D 模型预览 | macOS 原生 3D 能力够但渲染工作量大；P2 留口子 |
| 3 | BT / Magnet 下载 | 与本项目"普通文件下载"定位冲突；上 BT 意味着补 tracker 协议 + DHT + 跨网 NAT，半年工作量 |
| 4 | 自动解包 zip / 自动挂载 dmg | 安全顾虑 — 自动执行任意归档内容会把用户机器信任半径拉大；v2 视用户呼声决定 |
| 5 | 公网 URL 上传 / 分享 | 隐私问题 — SDM 持有下载链接 = 私密行为，外泄不可接受 |
| 6 | 多账号云同步 | 同上，下载历史属私密；本地 SQLite 足够 |

## v1.0 已知的妥协

| # | 项 | 妥协 vs 影响 |
|---|---|---|
| 1 | **Safari 浏览器接管能力有限** | macOS 15+ Safari Web Extension 强制"7 天重签"是平台设计，非工程问题；我们交付嗅探+悬停按钮接管入口。**Safari 用户点按钮手动派单** |
| 2 | **ad-hoc 签名本地跑** | Developer ID 是 v2/v3 目标；v1.0 走 ad-hoc + 用户自信任，DMG 安装需用户手动 "Open Anyway"。等星空喵购入开发者证书再升 |
| 3 | **动态分段策略激进版** | 初版合并阈值 0.3、分裂阈值 1.5，可通过 GUI 切保守；实测数据后迭代 |
| 4 | **无 CLI 的 `sdmd` 子进程实际启动** | P0 只声明源码层 stub，`@main` 入口在 P4 真正的 `sdmd` target 上写 |
| 5 | **GUI 截图实测** | TCC 限制（Screen Recording / Accessibility）下，agent session 无权限截图；GUI 验证落到星空喵手测。CLI 端到端测试 agent 可跑 |
| 6 | ~~Little Snitch + ad-hoc 签名应用的出站 0 字节~~ → 收回（误判） | 2026-10-19 agent 第一次排查时，把"URLSession 卡 60 s timeout"归因到 Little Snitch 默认拦截 ad-hoc 应用出站；星空喵现场确认 Little Snitch 模式 Silent Allow + SDM 实际请求未被拦截、规则无异样。收回这条，留作未来参考：URLSession 卡死现象 ≠ Little Snitch。**真因见 #7** |

| 7 | **`release-assets.githubusercontent.com` Range 请求在 SDM 里 0 KB/s，curl 直连同 URL 30 s 拿到 1 MB** | 实测（2026-10-19）：`curl -r 0-2097151` 拉用户给的 UCopy.exe URL，30 秒拿到 1,017,794 / 2,097,152 字节（33 KB/s 真实带宽上限，Content-Range = `bytes 0-2097151/13091795`、Content-Length = 2097152）。同一 URL 在 macOS 上 `URLSession.data(for: req)`（专属 session + IgnoreSSLAuthChallengeDelegate bypass）卡 60 s 后 timeout `kCFStreamErrorCodeKey=-2102`。端到端测试 (`Tests/EngineTests/DownloadFlowEndToEndTests.swift`) 只覆盖本地 Python range server，不覆盖公网 HTTPS + Fastly + 大文件 + chunk 并发，所以这条问题在 CI 单测里**完全没人守门**。**v1.1.9 修复路径（已合 main，2026-10-19）**：(a) `DownloadEngine.runFlow/runResume` 的临时 `HTTPDownloader()` 实例化 → `self.downloader`，让 probe 走与 chunk worker 相同的 IgnoreSSL bypass；(b) `HTTPDownloader.downloadChunk` 加 4 个时间点诊断日志（`chunkStartWall`、`headersReceivedWall`、`bodyReceivedWall`、`body received bytes`），用户切到 Console.app 看 `subsystem=com.skyc8266.superdownloadmanager` 即可区分"chunk 没发出去/发了没收头/收头没收 body"；(c) 新增 `Engine/SDMLogger.swift` 集中日志系统（分级 + 文件落盘 + 自动清理）+ Settings → Logs 页（级别切换 / 持久化开关 / 保留天数 / 轮转 KB / 行数上限 / 预览 / 在 Console.app 中打开 / 在访达中显示）；(d) TaskDetailView 加每 chunk 实时 elapsed 列（基于 partial 文件 mtime，> 15 s 不动变红）。**下一步如果还有 0 KB/s**，按 Console.app 输出继续定位：`headers 一直不来`→ 网络层；`headers 来了但 body 卡死`→ `URLSession.data(for:)` 在 HTTP/2 上换 `bytes` 流式；`body 来了但 bytes=0`→ 服务器拒绝 Range |

## 借鉴 / 算法事实

| # | 项 | 来源 | 声明位置 |
|---|---|---|---|
| - | (P1 起) Range 合并 / 分裂策略 | 行业通用算法，IDM/aria2/FreeDownloadManager 均有类似实现 | 文件顶部 + commit + 此文档 |

## 参考

- [macos-native-app-development skill](../../../../../.hermes/skills/software-development/macos-native-app-development/SKILL.md) — 所有 macOS 工程的硬规矩底本
- [`sdm-v1.0.md`](./sdm-v1.0.md) — v1.0 主线计划

## v2.0.0 状态

Python 重写版。Swift 实现保留在仓库根目录的 `Engine/` `App/` `CLI/` `Daemon/` `Extensions/`，作为语义对照。Python 版在 `pymain/`，包名 `sdm`。

`docs/functional-inventory.md` 记录了 Swift 版的 191 个功能点与 35 项重构风险，是功能完整性的验收基准。`docs/ui-spec.md` 是 UI 规范。

## v2.0 暂不做

| # | 项 | 原因 |
|---|---|---|
| 1 | 菜单栏常驻模式 | Swift 版用 `MenuBarExtra` 做常驻入口。pywebview 没有等价 API，要在 macOS 上做需要原生辅助进程。窗口 + 剪贴板监听 + 扩展派单覆盖了主要入口 |
| 2 | 应用内自动更新 | Swift 版用 Sparkle，Python 版没有等价物。发版走 GitHub Releases |
| 3 | DMG 封装 | 当前分发是 zip。`ditto` 解压拖进 `/Applications` 够用 |
| 4 | 传输中的动态 split/merge | 只在传输**前**重规划。传输中切分要重开在途工作单元并重算 `bytesDone`，数据损坏风险高。Swift 版的 `RangePlanner.rearrange()` 从来没有调用方，属于同源问题 |

## v2.0 已知的妥协

| # | 项 | 妥协 vs 影响 |
|---|---|---|
| 1 | **Windows 只在 CI runner 上编译过** | 打包脚本的 Windows 分支（`--add-data` 用 `;` 分隔、多目标 onefile、`--console`）按平台差异逐条核对过，但没有在 Windows 真机上跑过打包产物。真机验证留给 Windows 用户 |
| 2 | **分片布局在传输开始后固定** | 传输前会按文件大小和期望片大小重规划一次。传输中不再调整 |
| 3 | **详情页分片表最多显示 64 行** | 分片数上限 128，全渲染 DOM 太大。超出部分用一行汇总 |
| 4 | **站点规则密码存系统钥匙串** | JSON 里不存密码。钥匙串不可用时规则退化为无认证 |
| 5 | **扩展派单端口 7955 未做鉴权** | 只监听 loopback。跟 Swift 版一致 |

## v2.0 修掉的 Swift 版缺陷

按 `docs/functional-inventory.md` 的 R 编号：

| R | 缺陷 | 修法 |
|---|---|---|
| R-32 | 服务器返回 206 但 `Content-Range` 与请求不符时，整个文件写进分片。文件大小看着差不多但内容全乱 | 收到 206 时断言 `Content-Range` 起止与请求一致，不一致立刻报错且不写盘 |
| R-10 | 取消任务后残留 `.sdm.partial-*` 文件 | 引擎层 `cancel()` 删分片。`pause()` 保留——resume 靠它续传 |
| R-03 | 进度聚合器接受负值上报，进度条会倒退 | `ChunkProgress.report()` 忽略负值 |
| R-01 | 探测请求遇到 503 直接判任务失败，不重试 | 探测加重试与时间预算 |
| R-01 | 重试用指数退避，服务器持续 503 时要等 25 秒 | 探测与分片各加时间预算，连接类错误用短退避 |
| R-09 | 续传补完 `.part` 后不落成目标文件 | 补完用 `os.replace` 落成最终文件 |
| — | `HistoryStore.open()` 在锁内调 `_prune_old_entries()`，后者拿同一把非重入锁。`insert()` 必死锁 | 清理挪到锁外 |
| — | 分片数超过并发上限时 `active` 列表被截断，截掉的分片永远不下载，文件缺一截 | 不截断。分片数固定，信号量控制同时开几个连接 |
| — | 文件名没清洗，Windows 非法字符导致下载跑完、进度 100%、最后落盘时炸 | `sdm/engine/filenames.py` 统一清洗 |
| — | 落盘用 `Path.rename`，Windows 上目标已存在时抛 `FileExistsError` | 改用 `os.replace` |
