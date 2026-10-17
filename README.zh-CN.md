# Super Download Manager (SDM)

> **[English README](./README.md)** | 中文

![Platform](https://img.shields.io/badge/Platform-macOS%2026%2B-blue.svg)
![Swift](https://img.shields.io/badge/Swift-5.9-orange.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

自研 macOS 多线程下载器。

- 全自研 Swift 实现
- 默认 32 段动态并行，上限 128
- 智能限速 + 时间段规则
- 自动文件分类
- 浏览器接管（Firefox 完整接管；Safari 嗅探+派单，受 macOS 限制）
- 命令行 `sdmdm`（自动加入 `~/.local/bin`）
- 后台常驻，开机自启
- 链接过期可续传（保留已下载分段）

## 系统要求

- macOS 26.0+
- Apple Silicon 或 Intel Mac

## 安装

下载最新 `SuperDownloadManager.dmg`，双击，把 `SuperDownloadManager.app` 拖进 `/Applications`。

打开一次应用以触发 LaunchAgent 注册：

```sh
open /Applications/SuperDownloadManager.app
# 首次启动自动调用 install.sh 注册 daemon
```

或手动：

```sh
sh /Applications/SuperDownloadManager.app/Contents/Resources/install.sh
```

完成后：

```sh
sdmdm ping
```

应返回 `{"ok":true, "result":{"pong":true}}`。

## 命令行

```
sdmdm add <url> [--referer R] [--filename F] [--category C] [--threads N]
sdmdm list
sdmdm pause <id> | resume <id> | cancel <id>
sdmdm relink <id> <new_url>
sdmdm config speed <bytes/sec> | config threads <count>
sdmdm ping
```

每个命令返回一行 JSON。AI agent 可直接调用。

## 浏览器扩展

详见 [`docs/extension-install.md`](docs/extension-install.md)。

- **Firefox**：临时加载 manifest.json；接管下载
- **Safari**：手动允许未签名扩展；只做嗅探+派单（Apple 限制 Safari 不能拦截下载）

## 文件分类

下载完成后按以下规则落盘（用户可在 Settings 修改）：

| 类别 | 路径 |
|---|---|
| 视频 | `~/Downloads/Video/` |
| 图片 | `~/Downloads/Pictures/` |
| 音频 | `~/Downloads/Audio/` |
| 归档 (zip/dmg/7z/...) | `~/Downloads/Archives/` |
| 文档 (PDF/DOCX/...) | `~/Downloads/Documents/` |
| 可执行 | `~/Downloads/Software/` |
| 其它 | `~/Downloads/` |

无扩展名的 `.dmg` / `.zip` 通过 magic byte 嗅探识别。

## 链接过期续传

下载过程中如服务端返回 403/410/451，SDM 标记任务为「等待新链接」：

1. GUI 顶部出现橙色 banner
2. 点 "刷新链接"，粘贴新 URL
3. SDM 验证 ETag / Content-Length 一致 → 从已完成分段续传

CLI 路径：`sdmdm relink <task_id> <new_url>`

## 自动更新

应用内"检查更新…"调起 Sparkle。更新发布于 GitHub Releases，全部经 edDSA 签名验证。

## 构建（开发者）

需要 macOS 26+，Xcode 27，`brew install xcodegen`。

```sh
xcodegen generate
xcodebuild -project SuperDownloadManager.xcodeproj \
           -scheme SuperDownloadManager \
           -configuration Debug \
           -derivedDataPath build build
open build/Build/Products/Debug/SuperDownloadManager.app
```

跑测试：

```sh
xcodebuild test -scheme SDMSelfTest -destination 'platform=macOS' -derivedDataPath build
```

打包 Release：

```sh
xcodebuild -configuration Release -derivedDataPath build_release build
bash scripts/make-dmg.sh
```

## 仓库约定

- 自动 commit + 自动 push（CI 编译 + tag 触发 release）
- `docs/notes.md` 记录 "没动的与原因"
- 公开文本简洁直接；技术细节进 `docs/release.md` 与 `docs/notes.md`
- 借用第三方算法时：**算法可鉴，源码不抄**；声明三处

## 许可证

MIT。详见 [LICENSE](./LICENSE)。