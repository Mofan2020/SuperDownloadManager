# Super Download Manager (SDM)

> **[English README](./README.md)** | 中文

![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Windows%20%7C%20Linux-blue.svg)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

IDM 的开源跨平台替代品。多线程下载器，支持 macOS、Windows、Linux。

- 单个 Python 内核，Web UI，不带 Electron，不内置浏览器运行时
- 默认 32 段并行，上限 128
- 真流式传输，字节到达即落盘
- 智能限速 + 时间段规则
- 自动文件分类
- 浏览器接管（Firefox 完整接管；Safari 嗅探+派单）
- 命令行 `sdm`，每个命令返回 JSON
- 后台常驻，开机自启
- 链接过期可续传（保留已下载分段）
- 液态玻璃界面，窗口自由缩放不裁切

## 系统要求

- macOS 12+ / Windows 22H2+ / 较新的 Linux
- 从源码运行需要 Python 3.11+

## 安装

从 [Releases](https://github.com/Mofan2020/SuperDownloadManager/releases) 下载对应平台的压缩包，解压即可。

macOS：

```sh
ditto -x -k SuperDownloadManager-macOS.zip /Applications
open /Applications/SuperDownloadManager.app
```

Windows：解压后运行 `SuperDownloadManager.exe`。

命令行和后台 daemon 是单独的两个包，解压到同一个目录，然后跑一次安装脚本：

```powershell
.\install.ps1
sdm ping
```

脚本做两件事：把 `sdm` 命令加进 PATH（用 .cmd shim，Windows 没软链），注册一个登录时启动的计划任务跑 daemon。

## 从源码运行

```sh
git clone https://github.com/Mofan2020/SuperDownloadManager.git
cd SuperDownloadManager/pymain
python -m pip install -r requirements.txt
python -m sdm.gui.main
```

## 命令行

`sdm` 命令跟后台 daemon 通信。跑一次安装脚本，把它加进 PATH 并注册 daemon：

```sh
sh /Applications/SuperDownloadManager.app/Contents/Resources/install.sh
sdm ping
```

Windows 用 `install.ps1`（见上面的安装一节）。

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

- **Firefox**：临时加载 manifest.json；接管下载
- **Safari**：手动允许未签名扩展；只做嗅探+派单（Apple 限制 Safari 不能拦截下载）

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
