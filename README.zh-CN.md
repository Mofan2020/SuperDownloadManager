# Super Download Manager (SDM)

> **[English README](./README.md)** | 中文

![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Windows-blue.svg)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

IDM 的开源替代品。多线程下载器，支持 macOS 与 Windows。

- 单个 Python 内核，Web UI，不带 Electron，不内置浏览器运行时
- **动态分段**：分片布局在下载过程中会长，不是开跑前一次性切死；从 2 条连接起步，
  有实打实的进度才逐级放开到上限，长分片跑到一半会被拆开另开连接继续
- 服务器对 Range 返回 200 而不是 206 时自动退化成单流，不会写坏文件
- 会话落盘（`<文件名>.sdm.session.json`），崩溃后按原布局接着下
- 真流式传输，字节到达即落盘
- 12 类结构化错误分类，每类都带中文说明和「能不能重试」
- 智能限速 + 时间段规则
- 代理（HTTP / SOCKS）、校验和（md5 / sha1 / sha256）、完成后行为（打开 / 跑脚本 / 休眠）
- 自动文件分类
- 浏览器接管走 **Native Messaging**，Cookie、UA、Referer 一起带过来
  （Chrome / Edge / Firefox 完整接管；Safari 嗅探+派单）
- 任务队列：同时活跃任务数有上限，可置顶置底
- 命令行 `sdm`，每个命令返回 JSON
- 后台常驻，开机自启
- 链接过期可续传（保留已下载分段）
- 液态玻璃界面，窗口自由缩放不裁切，浅色 / 深色主题
- 详情面板有分片可视化和最近 60 秒速度曲线
- macOS 菜单栏状态行 + Dock 进度 + AppleScript 词典
- Windows 托盘图标 + `superdownloadmanager://` URL Scheme

## 系统要求

- macOS 12+ / Windows 22H2+
- 从源码运行需要 Python 3.11+

## 安装

从 [Releases](https://github.com/Mofan2020/SuperDownloadManager/releases) 下载对应平台的压缩包，解压即可。

Release 分两种。标着 **Latest** 的是测过的版本，标着 **Pre-release** 的是更新的版本，
还在测，测完了会转成 Latest。

macOS：

```sh
ditto -x -k SuperDownloadManager-macOS.zip /Applications
open /Applications/SuperDownloadManager.app
```

Windows：解压后运行 `SuperDownloadManager.exe`。

命令行和后台 daemon 就在 app 里。第一次启动会把 `sdm` 加进 PATH，并注册一个
登录时启动的计划任务跑 daemon，不用再跑别的脚本。之后开一个新的终端窗口，
让它读到更新过的 PATH：

```sh
sdm ping
```

## 从源码运行

```sh
git clone https://github.com/Mofan2020/SuperDownloadManager.git
cd SuperDownloadManager/pymain
python -m pip install -r requirements.txt
python -m sdm.gui.main
```

## 命令行

`sdm` 命令跟后台 daemon 通信。app 第一次启动时就会把它装进 PATH：

```sh
sdm ping
```

```
sdm add <url> [--referer R] [--filename F] [--category C] [--threads N]
sdm list
sdm pause <id> | resume <id> | cancel <id>
sdm relink <id> <new_url>
sdm config speed <bytes/sec> | config threads <count>
sdm ping
```

每个命令返回一行 JSON。

## 浏览器扩展

详见 [`docs/extension-install.md`](docs/extension-install.md)。

扩展走 **Native Messaging** 把下载派给应用：app 自带一个小的 `sdmd-native-host`
可执行文件，首次启动会把宿主清单装进浏览器配置目录，用户不用做任何配置。
回退通道是本机回环 HTTP，强制校验配对令牌。

- **Chrome / Edge**：从 `Extensions/Chrome/` 或 `Extensions/Edge/` 加载未打包扩展；接管下载，页面的 Cookie 一起带过来
- **Firefox**：临时加载 `Extensions/Firefox/manifest.json`；接管下载
- **Safari**：手动允许未签名扩展；只做嗅探+派单（Apple 限制 Safari 不能拦截下载）

## AppleScript（macOS）

app 自带 AppleScript 词典，Script Editor 和任何能发 Apple 事件的脚本都能驱动它：

```applescript
tell application "Super Download Manager"
    set taskId to add download "https://example.com/big.zip"
    pause all downloads
    resume all downloads
    get download list
end tell
```

这些命令走的是跟 GUI、CLI 同一个 Task Service，不绕开引擎的状态机。

## 文件分类

下载完成后按以下规则落盘：

| 类别 | 路径 |
|---|---|
| 视频 | `~/Downloads/Video/` |
| 图片 | `~/Downloads/Pictures/` |
| 音频 | `~/Downloads/Audio/` |
| 归档 | `~/Downloads/Archives/` |
| 文档 | `~/Downloads/Documents/` |
| 软件 | `~/Downloads/Software/` |
| 其它 | `~/Downloads/` |

无扩展名的文件通过 magic byte 嗅探识别。

## 链接过期续传

下载过程中如服务端返回 403/410/451，SDM 标记任务为「等待新链接」：

1. GUI 顶部出现橙色 banner
2. 点「刷新链接」，粘贴新 URL
3. SDM 验证 ETag / Content-Length 一致 → 从已完成分段续传

CLI 路径：`sdm relink <task_id> <new_url>`

## 构建（开发者）

```sh
cd pymain
python -m pip install -r requirements.txt pyinstaller
python scripts/build.py          # macOS 出 .app，Windows 出 .exe
```

跑测试：

```sh
cd pymain
python -m pytest tests -q
python -m sdm.gui.main --self-test
```

## 仓库约定

- 自动 commit + 自动 push，推 tag 触发发版 workflow
- `docs/notes.md` 记录「没动的部分与原因」
- 对外文档简洁直接，技术细节留在 `docs/release.md` 和 `docs/notes.md`

## 许可

MIT，见 [LICENSE](./LICENSE)。
