# v1.0.6 — 现实修复 + Safari 扩展

上一轮星空喵手测反馈 6 项 + Safari 必做一并解决。

## 1. 全局限速真生效 (P0)

v1.0.x: SpeedLimiter 写死 rateBytesPerSec 一次，Settings 改 KB/s 不会重设桶
  → engine.setSpeedLimit() 是 no-op, UI 显示变化但实际不生效

v1.0.6:
- `SpeedLimiter` 改 `var rateBytesPerSec` + 加 `setRate(_:)` (真的重置桶)
- `DownloadEngine.setSpeedLimit(_:)` 调 `limiter.setRate(bytesPerSec)`
- `DownloadListStore.applyPersistedBandwidth()` 启动时把 AppStorage 推给 engine
- `observeBandwidth()` 监听 `UserDefaults.didChangeNotification` 实时同步
  (Settings 改 KB/s 立即生效)

## 2. per-task 限速 (P1)

- `TaskSummary.speedLimitBytesPerSec: Double = 0`
- `DownloadListStore.setSpeedLimit(id:)` NSAlert sheet + NSTextField + KB/s
  (0 = 用全局)
- `Engine/SpeedLimiter.rateBytesPerSec` per-task 优先, 全局是上限
- `DownloadEngine` 加 `setTaskSpeedLimit(id:bytesPerSec:)` + `taskLimiters[UUID] = SpeedLimiter`
- `runFlow` 优先用 task limiter 替换全局 limiter
- row Status 列: 有 per-task limit 显示 speedometer icon

## 3. 详细任务面板 (P0)

`App/MainWindow/TaskDetailView.swift` (273 行) sheet 视图, 双击行 / 右键 "Show details" 触发:

- 原始 URL / destination / ETag / Last-Modified / bytesDone / totalBytes
- 速度 (实时) / ETA
- error 历史
- **chunk 状态**: Show chunks toggle → 32 个 chunk 列表
  (id/bytesDone/totalBytes/speedBytesPerSec/state + 进度条 `[████░░] 80%`)
- "Copy as JSON" 一键复制全部元数据 (贴 bug report 方便)

## 4. 真断点续传 + Range fallback (P1)

- `ResumeFlow` 探测服务器是否真支持 Range
- 服务器返回 200 (而非 206) → 自动回退到 `HTTPDownloader` 普通下载
  从 0 重下整个文件 (不是从 N 单 stream)
- 续传完成后 `.part → destination` rename 走 `FileAssembler.finalize()`

## 5. 浏览器集成 (P2)

### Firefox
- `Extensions/Firefox/manifest.json` MV3
- 加 icons 16/32/48/64/128
- permissions: downloads / downloads.open / activeTab / storage / scripting / nativeMessaging
- host_permissions: <all_urls>
- background scripts + content_scripts (sniffer.js + hover-button.css)
- `browser_specific_settings.gecko.id="sdm-firefox@mofan2020.dev"`
- 安装: about:debugging → Load Temporary Add-on → manifest.json

### Safari (未签名, 必须)
- `Extensions/Safari/manifest.json` MV3 + icons + service_worker
- permissions: activeTab / scripting / storage / nativeMessaging
- 装: Safari → Settings → Advanced → Show features for web developers
       → Develop → Allow Unsigned Extensions
       → Develop → Load Unpacked Extension → Extensions/Safari/
- macOS 每次重启会重新询问是否允许扩展 (这是 Safari unsigned 设计, 不是 bug)
- Safari WebExtension API 没有 `browser.downloads.onCreated`, 不能接管所有下载
  → 只能 hover button 嗅探 media/file, 全自动接管还是要用 Firefox

## 6. 临时文件清理 (P0)

`App/SDMApp.swift applicationDidFinishLaunching` 调 `cleanStalePartFiles(maxAgeHours:24)`:

- 扫 `defaultDownloadDir` + 各 category 子目录 + `~/.partials/`
- `.part` 字节为 0 或 mtime > 24h → 删除
- 同时清孤儿 `*.sdm.json`
- 取消下载 (`cancel(id:)`) → 立即 unlink `.part` + sidecar (不是只标 cancelled)

## 7. 真文件名 (P0)

`Engine/DownloadEngine.runFlow`:

- 第 1 步 probe (1-byte Range) → 拿到 `Content-Disposition` filename
- user 没设 `filenameOverride` → rename `TaskSummary.destination`
  到 `dir.appendingPathComponent(real)`
- v1.0.6 已 plumb, 真实 server 返回需手测

## 8. 速度 / 进度监控重写 (P0)

v1.0.x: 200ms 磁盘轮询 attributesOfItem(.size)

v1.0.6: `ProgressChannel` (`AsyncStream<Int64>`, 50ms coalesce, `.bufferingNewest(16)`)

- `DownloadFlow` 每 256KB 写完 → `report(bytes)` 后台 yield
- `DownloadEngine.runFlow` 收 stream → `queue.update { $0.bytesDone = bytes }`
- 速度: EMA(α=0.4) + 200ms `deltaBytes/0.2` 兜底
- `smoothedSpeed: [UUID: Double]` 缓存

## 9. Settings UI 重写 (P0)

`App/MainWindow/SettingsView.swift`:

- `NavigationSplitView` 左 6 个 `SettingsSection` (general/defaults/categories/siteRules/startup/about)
- 显式 `.environment(\.locale, locale).environment(\.strings, strings)`
- 双语字典化: 每 key 显式 `S.settingsBandwidth` (不再 %lld)
- 语言切换"立即生效": `Strings(locale: LocaleTag(rawValue:newVal))` 重建
- 类别规则 + 站点规则: `NavigationLink` 跳转 editor
- 修 SwiftUI `Section` 与自定义 `enum Section` 名字冲突 → `SettingsSection`

## 验证

- [x] build Debug 0 error
- [x] build Release 0 error (修 main-actor isolation)
- [x] tests 50/50 pass
- [x] git tag v1.0.6 + push
- [x] CI Release 绿
- [x] gh release edit v1.0.6 --prerelease + 写客观 release notes

## 没做

- **Move to Trash**: 不做。星空喵明确要彻底移除不留恢复路径。现状: `FileManager.removeItem` 直接删除 (没 NSWorkspace/Trash API), v1.0.5 launch 清理 + v1.0.6 cancel unlink + 下载完成 rename 全是 rm 路径
- Sparkle: UpdateService.swift 还是 stub (CI 拉不到 SPM binary archive)
- macOS 27 sparkle 2.6.4: 需要等 CI 支持 Apple Silicon + SDK 27 才能升
- Range fallback e2e test (要真实 server 才能跑)
- chunk status UI e2e test (要真实 Range 进程才能跑)
