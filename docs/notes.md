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

## v2.0.5 分片计数一直是 0/0

详情面板那行「已完成分片 / 总片数」读的是 `TaskSummary.chunk_count` 和
`completed_chunks`。这两个字段有定义、有默认值、有序列化，但**从来没有
任何一处给它们赋值**——`update_chunk_states` 只写 `self._chunk_states`，
那是给详情面板单独取明细用的另一份数据。

所以列表和详情面板显示的永远是 `0/0`，跟实际分片数无关。

改法：`update_chunk_states` 里同步写摘要的两个字段。判定「分片完成」
用 `bytes_done >= size`（`Chunk.complete` 是同一个判据）。

顺带修了两个相关的：

- **`_notify_chunk_progress()` 的调用顺序**。worker 里原本先通知进度、
  再把 `c.bytes_done` 顶到 `chunk.end`。顺序反了的话最后完成的那个
  分片在面板里永远显示「未完成」。改成先更新 `bytes_done` 再通知。
- **任务结束后 `_chunk_states` 被 pop 掉**。Swift 版的详情面板对已完成
  任务也展示分片（虽然是合成的假数据）。现在保留最终状态，同时按
  `CHUNK_STATE_RETENTION = 20` 限制保留的任务数——每个任务几十到上百个
  dict，无上限会占内存。`remove()` 时仍然彻底清掉。

## unix socket 的路径长度上限

macOS 的 unix socket 路径上限 104 字节，Linux 108。超了 socket 根本建
不出来，daemon 启动直接报 `AF_UNIX path too long`。

正常的 macOS 路径是
`~/Library/Application Support/SuperDownloadManager/sdmd.sock`，七十
几字节没事。但用户名长、或者 HOME 指向深层目录时就超了。

`paths.socket_path()` 现在检查字节数，超过 100 就退回
`/tmp/sdm-<uid>.sock`——短，且带 uid 不会跟别的用户撞，socket 权限
0600 只有本人能连。daemon 和 CLI 都走这个函数，算出来必然一致。

## 图标进了 CI 的校验项

PyInstaller 找不到 `--icon` 时会静默用它自带的
`bootloader/images/icon-windowed.icns`，不报错也不警告。打包脚本传错
路径、写错文件名、或者 tag 上还没有图标文件，产物照样出得来，只是
图标是默认的。

所以校验不能靠「打包命令里有没有 `--icon`」，得查产物：

- macOS：`Info.plist` 的 `CFBundleIconFile` 要等于 `AppIcon.icns`，
  `Contents/Resources/` 下要有这个文件，且内容不能跟 PyInstaller 的
  默认图标字节相同。zip 解出来的那份也要再查一遍——打包过程可能把
  Resources 里的文件弄丢，只查 dist 目录不够。
- Windows：exe 的 `.rsrc` 段必须存在且不能太小（ICO 打不进资源段时
  段几乎不存在）。

另外 `pymain/tests/test_app_icon.py` 在本地也查同一套：icns 头是
`icns`、长度字段自洽、含多个尺寸块；ico 的 reserved/type/count 正确、
含 256x256；`build.py` 拼出来的 GUI 参数里有 `--icon` 且路径存在，
CLI 参数里没有。

## v2.0.3 应用图标

打包时没传 `--icon`，PyInstaller 就用它自己那个默认图标。用户看到的
是访达和 Dock 里一把扳手。

图标资源本来就在仓库里（Swift 版留下的）：

- `Resources/AppIcon.icns` —— macOS 用，509 KB，ic12 格式
- `App/Assets.xcassets/AppIcon.appiconset/` —— 全套 PNG

Windows 那边没有 .ico，用 `pymain/resources/AppIcon.ico`——从
appiconset 的 PNG 手写 ICO 容器（Vista 以后支持 PNG 压缩的 ICO：
6 字节头 + 16 字节目录项 + PNG 数据）。7 个尺寸，16 到 256。

两个都复制到 `pymain/resources/`，让 Python 子项目自包含。

**PyInstaller 的 `--icon` 平台相关**：macOS 收 .icns，Windows 收 .ico，
传错格式直接报错退出。

## PyObjC 踩的坑

macOS 菜单栏图标（NSStatusItem）用 PyObjC 实现，几个不查文档就会卡
住的地方：

| 坑 | 现象 | 解法 |
|---|---|---|
| `variableLength` 拿不到 | Swift 里是 `NSStatusItem.variableLength` 类属性，ObjC 里是宏 `#define variableLength -1`，PyObjC 不暴露 | 用常量 `NSVariableStatusItemLength`（-1） |
| 类里的内部方法被当成 selector | `_run` 不是 ObjC 方法名，PyObjC 照样转换，签名对不上就 `BadPrototypeError` | 内部辅助函数放到类外面 |
| selector 字符串怎么变 SEL | `objc.selectorFromString` / `objc.namedSelector` 都不对（前者不存在，后者返回 Python 函数不能脱 Python 化） | `Foundation.NSSelectorFromString("addURL:")` |
| 方法名到 selector 的映射 | Python 的 `addURL_` 对应 ObjC 的 `addURL:` | 用 `respondsToSelector_` 验证，不报错但点了没反应 |

NSStatusItem 必须在主线程创建——GUI 的 `on_loaded` 回调正好在主线程。

## 关窗口后 app 常驻：pywebview 做不到

Swift 版关闭窗口后 app 还活着（SwiftUI App + 状态栏图标），能从菜单
栏唤回。Python 版做不到，两个原因：

1. **closing 事件不可靠**。pywebview 的 `Event.set()` 在非锁模式下把
   handler 扔到另一个线程跑，然后**立刻**检查返回值——几乎必然读到
   空的 `return_values`，所以「返回 False 取消关闭」基本不生效。
2. **窗口关掉 `webview.start()` 就返回**，main 结束，进程退出。

菜单栏图标本身能用（打开主窗口、加 URL、Finder、设置、关于、退出），
但「关窗口后常驻」没做。设置页那个「关闭窗口时隐藏 Dock 图标」开关
（Swift 版的 `sdm.hideDockWhenWindowClosed`）在 Python 版是死的——字段
在、UI 在、功能没接。

## v2.0.2 分发：为什么只有一个包

早先 macOS 发两个包（`SuperDownloadManager-macOS.zip` 和 `sdm-macOS.zip`），
Windows 发三个（GUI / CLI / daemon）。理由是「用户可能只想拿命令行工具，
不装整个 app」。

这个理由站不住：CLI 需要的 daemon、站点规则、计划任务、历史都在 app 里，
只装 CLI 等于装了个残废的。而且用户还得手动跑 `install.sh` 才能用。

现在的分发：

| 平台 | 包 | CLI 和 daemon 在哪 |
|---|---|---|
| macOS | `SuperDownloadManager-macOS.zip` | `.app/Contents/Resources/sdm-cli/` 和 `sdmd-daemon/` |
| Windows | `SuperDownloadManager-Windows.zip` | GUI exe 内的 zip，首次运行时解到 `%LOCALAPPDATA%` |

GUI 首次启动时在后台线程里做安装（`sdm/gui/bootstrap.py`）：

- macOS：软链 `sdm` 到 `/usr/local/bin`（装不进去退回 `~/.local/bin`），
  写并加载 daemon 的 LaunchAgent
- Windows：解出 CLI/daemon，写 `.cmd` shim，用 `winreg` 改用户 PATH，
  `schtasks` 注册登录计划任务

几个刻意的取舍：

- **不改用户的 shell 配置文件**。往 `~/.zshrc` 里塞 PATH 是侵入式行为。
  装不到 PATH 上的目录时，界面提示用户，不擅自改配置。
- **PATH 里已有同名命令不动**。可能是用户自己装的别的工具，覆盖掉会让人
  怀疑人生。报告冲突，让用户自己决定。
- **daemon 已加载时不 kickstart**。kickstart 会把正在传的文件打断。
  只有 plist 内容变了才重启。
- **Windows 用 winreg 而不是 setx**。setx 有 1024 字符截断，老机器的 PATH
  经常超。

`install.sh` 和 `install.ps1` 仍然保留在仓库里——用户想重装或换安装位置
时用得上，只是不再随 release 分发。

## v2.0 暂不做

|| # | 项 | 原因 |
|---|---|---|
| 1 | 菜单栏常驻模式 | **v2.0.3 部分实现**：用 PyObjC 做了 NSStatusItem，菜单项和快捷键跟 Swift 版一致。**但「关窗口后 app 继续活着」没做**——pywebview 的 closing 事件有竞态不可靠，窗口一关 `webview.start()` 就返回，进程退出。细节见上面「关窗口后 app 常驻：pywebview 做不到」 |
| 2 | 应用内自动更新 | Swift 版用 Sparkle，Python 版没有等价物。发版走 GitHub Releases。**Sparkle 需要 Developer ID 签名 + appcast 服务器**，用户的 ad-hoc 签名方案本来就跑不了 |
| 3 | DMG 封装 | **已实现**（本地 `build.py --mac` 生成 `~/Downloads/Archives/SuperDownloadManager-<version>-<arch>.dmg`，优先用 create-dmg，回退到 hdiutil） |
| 4 | 传输中的动态 split/merge | 只在传输**前**重规划。传输中切分要重开在途工作单元并重算 `bytesDone`，数据损坏风险高。Swift 版的 `RangePlanner.rearrange()` 从来没有调用方，属于同源问题。**Python 版的传输前重规划比 Swift 版更完整** |

## v2.0 已知的妥协

|| # | 项 | 妥协 vs 影响 |
|---|---|---|
| 1 | **Windows 只在 CI runner 上编译过** | 打包脚本的 Windows 分支按平台差异逐条核对过，但没有在 Windows 真机上跑过打包产物。真机验证留给 Windows 用户 |
| 2 | **分片布局在传输开始后固定** | 传输前会按文件大小和期望片大小重规划一次。传输中不再调整 |
| 3 | **详情页分片表默认只列 64 个** | 分片数上限 128，全渲染 DOM 偏大。**v2.0.4 起底下有「展开全部分片」按钮，想看全能看全** |
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
| 2 | **CI 的发布 workflow 与 CI workflow 分开** | `release-python.yml` 负责打包发版，`ci-python.yml` 负责测试。Swift 版的 `release.yml` 已去掉 push 触发，只留手动——两个 workflow 抢同一个 tag 会互相覆盖产物 |

## v2.0.0 CI 踩过的坑

| # | 坑 | 现象 | 修法 |
|---|---|---|---|
| 1 | `requirements.txt` 只有运行时依赖 | CI 上 `pytest` 报 `No module named pytest` | workflow 里显式装 pytest |
| 2 | 两个发布 workflow 抢同一个 tag | Swift 版和 Python 版同时跑，Python 版的产物被覆盖 | Swift 版 `release.yml` 去掉 push 触发 |
| 3 | PyInstaller 6 的 `--add-data` 把单文件打成目录 | `install.sh` 变成 `install.sh/install.sh`，脚本没法执行 | 打包完用 Python 直接复制到 `.app/Contents/Resources/` |
| 4 | `PlistBuddy` 的 `Add` 不带类型前缀 | `Add :CFBundleVersion 1` 报 `Unrecognized Type: 1`，键根本没加上 | 改用 `plutil -insert/-replace`，类型明确 |
| 5 | 签名失败没人知道 | `codesign` 的 stderr 被 `DEVNULL` 吞了，返回值也没检查，坏签名直接发出去 | 每步检查返回码，失败就中止打包 |
| 6 | onedir 目录放进 .app 会让签名失败 | `codesign` 递归 `Contents/` 下的目录树，把含 Mach-O 的目录当 nested bundle，一路递归撞到 `_internal/click-8.4.2.dist-info` 这种非 bundle 目录，报 `bundle format unrecognized, invalid, or unsuitable` | CLI/daemon 放 `Contents/Resources/` 下，签名不递归那里 |
| 7 | Windows 上取消后分片删不掉 | `unlink` 抛 `PermissionError` 被 `except OSError: pass` 吞了，分片留在下载目录 | 先 `await` 任务真正结束、关掉文件句柄，再删；删失败重试 |
| 8 | ruff 默认规则集随版本漂 | 0.17 默认开了 YTT/ASYNC/UP 等，同一份代码换个版本就红 | 加 `pyproject.toml` 写死规则集 |

## ruff 抓出来的真 bug

lint 不只是格式问题。这批里有三个是真 bug，都因为没跑到那条路径而一直藏着：

| # | 位置 | 现象 | 影响 |
|---|---|---|---|
| 1 | `sdm/engine/resume_state.py` | `read()` 写成 `@staticmethod` 却用了 `cls` | 过期链接换新链接续传一调就 `NameError`，整条 relink 路径不可用 |
| 2 | `sdm/gui/bridge.py` | `copy_to_clipboard()` 的 else 分支多了一次 `import subprocess` | 局部变量把模块名遮住，剪贴板复制一调就 `NameError` |
| 3 | `sdm/engine/download_flow.py` | 分片 worker 里 `result` 赋值后没用 | 无功能影响，但说明当时写的时候没走一遍 |

第 1、2 条都补了回归测试（`tests/test_regression_bugs.py`）——不是为了覆盖率，是为了这种「改回去就红」。

## Windows 特有行为怎么在 macOS 上测

macOS 上这些路径根本不会触发：POSIX 允许删掉正在被写的文件，Windows 不行。所以本机跑测试永远是绿的，CI 上才红。

`tests/test_windows_semantics.py` 的做法是把 Windows 的失败模式模拟出来——让 `Path.unlink` 前 N 次抛 `PermissionError`（Windows 上文件被别的句柄打开时的真实行为），然后验证重试逻辑、句柄释放、清理不抛异常。这样本机改代码也能守住 Windows 那条路径。

## macOS 签名：onedir 目录不能进 .app

这个坑花了不少时间，值得完整记下来。

### 现象

把 PyInstaller onedir 产物（`sdm/`、`sdmd/`，各有 `_internal/` 子目录）放进 .app 后签名必失败：

```
codesign --force --sign - SuperDownloadManager.app
→ bundle format unrecognized, invalid, or unsuitable
  In subcomponent: .../Contents/Helpers/sdmd/_internal/click-8.4.2.dist-info
```

换 `Contents/MacOS/`、`Contents/Frameworks/`、`Contents/Helpers/` 都一样。CI 上 `codesign --verify --deep --strict` 直接红。

### 根因

codesign 签一个 bundle 时会递归 `Contents/` 下的目录树。判定规则是「目录里直接有 Mach-O 就当 nested bundle 处理」。ondir 的 `_internal/` 里正好有 `python3.11/lib-dynload/*.so`，于是 `_internal` 被当成 nested bundle，codesign 要求它是合法 bundle（得有 `Contents/Info.plist`），没有就报错。

报错的子目录名会变（`click-8.4.2.dist-info`、`python3.11`、`.internal`），因为每次递归到的最深可疑点不一样。**这个变化会误导人往「删掉那个目录」的方向想**——删掉 `.dist-info` 后报错只是换了个对象。

试过但不管用的路子：

| 尝试 | 结果 |
|---|---|
| `codesign --force --deep --sign -` | 一样失败，--deep 只是让递归更彻底 |
| 目录改名（`_internal` → `.internal`） | 隐藏目录也躲不掉 |
| 挪到 `Contents/Frameworks/` | 一样，Frameworks 下同样被递归 |
| 挪到 `Contents/Helpers/` | 一样 |
| 给 `_internal` 加 `Contents/Info.plist` | 变成合法 bundle 了，但报 `unsealed contents present in the bundle root`（bundle 根下的内容必须都在 `Contents/` 里） |
| 删掉 `.dist-info` | 只是让报错对象换人 |

### 解法

`Contents/Resources/` 是 codesign 当**资源**处理的位置——不递归、不签里面的代码。CLI/daemon 放那儿，签名能过，程序照常能跑。

两个细节：

1. **目录名要避开 `sdm` / `sdmd`**。PyInstaller 给 GUI 打的 .app 里已经有 `Contents/Resources/sdm` 软链（指向 `../Resources/sdm`）。同名覆盖会变成自指软链，`codesign --verify --deep` 报 `No such file or directory`，运行时 `Too many levels of symbolic links`。所以改成 `sdm-cli` / `sdmd-daemon`。
2. **签名命令不带 `--deep`**。签完 .app 立刻 `codesign --verify --deep --strict` 验证。

### 为什么不用 onefile

onefile 是单个 Mach-O 文件，放哪儿都不会触发递归，签名天然没问题。但 onefile 每次启动要把整个包解到临时目录再重签名：daemon 建 socket 要 10.9 秒，CLI 要 5.9 秒。onedir 分别是 0.18 秒和 0.06 秒。

daemon 常驻，慢一点无所谓；CLI 是用户每次手敲的，5.9 秒不能接受。所以 CLI/daemon 坚持 onedir。

### 验证

```bash
codesign --verify --deep --strict SuperDownloadManager.app   # exit 0
Contents/Resources/sdm-cli/sdm ping                          # 0.06s
Contents/Resources/sdmd-daemon/sdmd                           # 0.18s 出 socket
```

## 取消语义：CancelledError 不能转成 FlowError

`DownloadFlow.run()` 原来把 `asyncio.CancelledError` 捕获后转成 `FlowError(kind="cancelled")`。看起来无害，实际是错的：

```
flow.run() 抛 FlowError("cancelled")
  → 引擎的 except FlowError 分支
  → summary.state = FAILED
```

**取消/暂停被报成了失败。** Swift 版就有这个毛病（暂停后任务显示失败），Python 版重构时又带进来了。

引擎里其实有正确的分支：

```python
except asyncio.CancelledError:
    if live.cancelled: summary.state = TaskState.CANCELLED.value
    elif live.paused:  summary.state = TaskState.PAUSED.value
```

但它永远走不到——`flow.run()` 已经把 CancelledError 吃掉了。

**修法**：`flow.run()` 里 CancelledError 原样往上抛。它在 Python 3.8+ 继承自 BaseException，不转的话 asyncio 才知道这是「请求取消」而不是「出错」。

引擎的 `except FlowError` 里另加了一条 `kind == "cancelled"` 的判断作为双保险。

R-08 原来只有一条测试，测的是 flow 层抛 FlowError——它锁死了错误行为。拆成两条：一条测 flow 层抛什么，一条测引擎层状态是什么（后者才是用户看到的）。

## CI 上 macOS runner 的 abort（exit 134）

pytest 退出时报：

```
Fatal Python error: _enter_buffered_busy: could not acquire lock for
<_io.BufferedWriter name='<stderr>'> at interpreter shutdown, possibly
due to daemon threads
```

**报的不是真正的失败原因**——真正的失败被这个 abort 盖住了。

原因：客户端取消下载会直接关连接，服务器的线程读 socket 抛 `ConnectionResetError`。socketserver 默认把这个 traceback 打到 stderr。pytest 退出那一刻打，stderr 的锁已经销毁 → 整个进程 abort。

修法是 `QuietErrorMixin`：`handle_error` 里把 `ConnectionResetError` / `BrokenPipeError` / `ConnectionAbortedError` 静默掉。这些是正常现象，不是错误。

**教训**：测试基础设施的 stderr 噪音会在收尾时变成致命的 abort。凡是有后台线程往 stderr 写的地方，都得考虑解释器关闭那一刻。

## monkeypatch 替换类方法时的一个坑

`monkeypatch.setattr(Path, "unlink", fake)` 时，fake 必须是**函数**，不能是对象。

Path.unlink 原本是方法。替换成普通对象时，Python 不会绑定 self——`path.unlink()` 以零参数调用它。于是所有删除请求都被当成「目标文件」的请求，测试变成假绿（看起来在验证锁竞争，实际上验证的是别的东西）。

写成类并实现 `__get__` 也不行：`__get__` 返回绑定方法后，调用时同样不传 path。正确做法是闭包函数：

```python
def make(self):
    def fake_unlink(path=None, *args, **kwargs):
        ...
    return fake_unlink
```

**这个坑差点让我得出错误结论**——看到「分片残留」以为是引擎的问题，实际是 fake 自己的问题。引擎那侧确实也有真 bug（live 被 pop 之后拿不到 flow），但如果 fake 是错的，测试结果就说明不了任何事。

## Windows 的安装方式

macOS 有 .app 包，install.sh 挂在 `Contents/Resources/` 里跟着走。Windows 没有 .app
这种东西——产物就是一个 exe，装 CLI 和注册 daemon 得另外想办法。

对等实现：

| | macOS | Windows |
|---|---|---|
| CLI 上 PATH | 软链 `~/.local/bin/sdm` | `.cmd` shim 放进 `%LOCALAPPDATA%\SuperDownloadManager\bin`，再把该目录加进用户 PATH |
| daemon 常驻 | launchd LaunchAgent | 计划任务（登录时启动） |
| 卸载 | `uninstall.sh` | `uninstall.ps1` |

**为什么 Windows 用 .cmd shim 而不是软链**：Windows 的软链要开开发者模式或者管理员权限，
普通用户装个下载器不该被要求改系统设置。shim 是个两行的批处理，指向真正的 exe。

**计划任务的注册**用 `Register-ScheduledTask`，重复运行前先 `Unregister-ScheduledTask`
——脚本要幂等，跑两次不能报错。

CI 里加了一步用 PowerShell 解析器检查脚本语法：

```powershell
[System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$errors)
```

脚本写错了要等用户手里才炸，那太晚。这步在 Windows runner 上跑，语法错误当场就红。
