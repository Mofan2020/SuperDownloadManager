# Super Download Manager (SDM)

> 自研多线程动态分段下载器，对标 IDM 的 macOS 原生实现。

> **状态：P0（工程骨架）** — 仓库可 `xcodegen generate && xcodebuild build` 通过；下载引擎、CLI、扩展等进入 P1-P8。

## 它是什么

一个 macOS 原生下载管理器：

- **全自研**，无 aria2 / 第三方下载核心
- 默认 32 段动态并行，**硬上限 128 段**
- 智能限速（令牌桶，时间段规则可配）
- 自动文件分类（Video / Pictures / Audio / Archives / Documents / Software / Other，按 MIME + 扩展名 + magic byte 三键）
- 浏览器接管（Firefox 全功能；Safari 嗅探+悬停按钮，受 macOS 15+ 设计限制）
- macOS 26 GUI（Swift 5.9 语言模式，Xcode 27 SDK 编译）
- 头less `sdmd` 守护进程 + `sdmdm` CLI（自动加入 `~/.local/bin/`）
- 链接过期后可"刷新续传"（保留已完成分段，校验 ETag/Content-Length 一致性）
- LaunchAgent 开机自启 + 后台常驻
- Sparkle 2 自动更新（GitHub Releases + edDSA 签名）

## 当前阶段

完整路线图见 [`.hermes/plans/sdm-v1.0.md`](.hermes/plans/sdm-v1.0.md)。

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | XcodeGen 工程骨架 + 构建闭环（3 target，0 warning） | ✅ |
| P1 | 下载引擎核心 | ⏳ |
| P2 | GUI 主窗口 + 设置 | ⏳ |
| P3 | 文件分类 + 过期链接续传 | ⏳ |
| P4 | sdmd daemon + Unix Socket IPC + LaunchAgent + CLI | ⏳ |
| P5 | Firefox/Safari 浏览器扩展 | ⏳ |
| P6 | 智能限速规则系统 | ⏳ |
| P7 | Sparkle 自动更新 + 自绘图标 + DMG | ⏳ |
| P8 | 文档 / License / 发版 | ⏳ |

## 构建

需要 macOS 26+，Xcode 27，`brew install xcodegen`。

```sh
xcodegen generate
xcodebuild -project SuperDownloadManager.xcodeproj \
           -scheme SuperDownloadManager \
           -configuration Debug \
           -derivedDataPath build build
open build/Build/Products/Debug/SuperDownloadManager.app
```

## 仓库约定

- 自动 commit，**不 push**（星空喵偏好，等明确指令）
- `docs/notes.md` 记录"没动的与原因"——所有 v1.0 暂不做的事都在那里说明
- 公开文本（README / Release）简洁直接，技术细节进 `docs/release.md`（开发者向）
- 借用第三方算法时，**算法可鉴，源码不抄**；声明三处（文件顶部 / commit message / docs/notes.md）

## 许可证

MIT。详见 [LICENSE](./LICENSE)。
