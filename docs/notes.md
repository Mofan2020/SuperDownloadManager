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

## UserNotifications：没有 bundle identity 会 abort 进程

Swift 版用 `UNUserNotificationCenter` 发通知，Python 版一开始想用
`osascript` 顶一下。差别不只是「点通知能不能回主窗口」——`UN` 那条路
有个会**直接杀掉进程**的坑。

`UNUserNotificationCenter.currentNotificationCenter()` 要求进程有 bundle
identity。从源码跑 Python 时没有，`NSInternalInconsistencyException`
（`bundleProxyForCurrentProcess is nil`）——**ObjC 异常，不是 Python
异常**，`try/except Exception` 接不住，进程直接 abort（exit 134）。

所以判定必须发生在碰框架之前：

```python
def _bundle_identifier() -> str | None:
    from Foundation import NSBundle
    return NSBundle.mainBundle().bundleIdentifier() or None
```

拿不到就走 osascript 兜底。兜底路径只能弹横幅，**点通知回不到主窗口**
——那是 UN 独有的能力。

打包成 .app 就有 bundle identity（`CFBundleIdentifier` 在 Info.plist
里），所以两条路都留着：开发时走 osascript，发版走 UN。

### 打包时得显式声明 hidden import

`requirements.txt` 里加：

```
pyobjc-framework-UserNotifications>=12.0; sys_platform == "darwin"
```

环境标记必须有——Windows 上没这个框架。

**光装上还不够**：`notifications.py` 里是 `import UserNotifications` 包在
`try/except ImportError` 里，PyInstaller 的静态分析会跳过它，不显式
声明的话打包出来的 .app 里没有这个模块，通知会静默退回 osascript。
`build.py` 里加：

```python
MACOS_GUI_EXTRA_IMPORTS = ["UserNotifications"]
```

### ObjC 类名不能重复

PyObjC 把 Python 类按**类名**注册进 Objective-C 运行时。同名类第二次
定义会抛：

```
_NotificationsDelegate is overriding existing Objective-C class
```

`install_delegate()` 被调两次就炸（热重载、或者测试里每个 case 都调一
次）。解法是类名带自增序号：

```python
_delegate_seq += 1
cls_name = f"_SDMNotificationsDelegate{_delegate_seq}"
cls = type(cls_name, (NSObject,), {...})
```

另外 delegate 实例必须**强引用**——局部变量被回收后系统回调就找不到
对象了，表现为「点了通知什么反应都没有」。

### macOS 的 os.log 不存在

Python 3.14 才加 `os.log()`。3.11 上没有。想往统一日志里写，用
`Foundation.NSLog`（实测可用）。

### 通知 identifier 要带任务 id

系统按 identifier 去重。Swift 版是 `complete-<task.id.uuidString>`，
Python 版照抄——同一个任务重复通知会替换掉旧的，不同任务各弹各的。

## URL scheme 怎么接住：NSAppleEventManager

Swift 版实现了 `handleURLEvent` 但从没在 Info.plist 注册 `CFBundleURLTypes`，
系统根本不会送 URL 过来——那个入口是死的（台账 R-13）。Python 版把
注册补上了（`build.py` 往 Info.plist 写 `CFBundleURLTypes`），比 Swift
好。但**注册只是让系统知道「这个 app 收这个 scheme」，还得有东西接住
送来的 URL**。

pywebview 没有 `url_request` 事件（`window.py` 的事件清单里没有），
`window.get_current_url()` 永远返回 index.html。macOS 后端也没实现
`application:openURLs:`。

**解法：`NSAppleEventManager`。** URL scheme 的系统级入口本来就是
Apple Event——`kInternetEventClass` / `kAEGetURL`。注册一个 handler
就能接住，**不碰 pywebview 的任何东西**（它的 NSApplication 实例自己
持有，不去动它的委托）。

三个坑：

1. `setEventHandler:andSelector:forEventClass:andEventID:` 的
   eventClass / eventID 要的是**整数**不是字符串。传 `'GURL'` 报
   `ValueError: depythonifying 'unsigned int', got 'str'`。四字符码
   自己转：`(ord(c[0]) << 24) | (ord(c[1]) << 16) | ...`。
2. ObjC 的 `handleURL:withReplyEvent:` 有两个参数，PyObjC 的 Python
   方法就要写 `self, event, reply` 三个——**self 对应 ObjC 的第一个
   参数**。少一个抛 `BadPrototypeError: expects 2 arguments, ... has
   1 positional arguments`。
3. handler 必须保持**强引用**——局部变量被回收后系统回调找不到对象，
   表现为「双击链接唤起 app 但什么反应都没有」。

取 URL 的路径：`event.paramDescriptorForKeyword_(keyDirectObject)`
拿到描述符，再 `.data()` 取字节串。`keyDirectObject` 是 `'----'`，
四字符码 0x2d2d2d2d。

## referer 要一路传到请求头

Swift 版读了 referer 就扔了（`Daemon/IPC/Dispatcher.swift` 里
`referer` 参数收下但没用，`App/SDMApp.swift` 那套也一样），
Python 版不能照抄。

链路是三段，**每段都要传**，少一段就白搭：

```
dispatcher 读到 referer
  → StartSpec.referer
    → DownloadFlow(headers={"Referer": ...})     ← 分片请求
    → downloader.probe(url, extra_headers={...}) ← 探测请求
```

**探测请求最容易漏。** 探测是 `Range: bytes=0-0` 的 GET，
有些站点探测阶段就 403——不带头的话任务连启动都做不到，
用户看到的是「下载失败」，但真实原因是 Referer 缺失。

`DownloadFlow` 内部还有一次探测（`download_flow.py` 的 `_run`
里），那处也得传 `extra_headers=self.headers`——引擎层探过一次
不代表 flow 不用再探一次。

优先级：**站点规则 > 任务级 referer**。规则是用户为该主机显式
配置的全局设置，任务级 referer 只是一次性补充。

## 测试别往用户目录里写

`SDMLogger` 默认写到 `~/Library/Logs/SuperDownloadManager/sdm.log`，
`HistoryStore` 默认写到 `~/Library/Application Support/SuperDownloadManager/history.sqlite`。
测试跑一次就往那儿追加东西——用户的日志里混着测试的记录，
下载历史里也是，sqlite 还会越涨越大。

**方案一（错的那版）**：在 `tests/conftest.py` 里用 autouse fixture
monkeypatch `paths.log_dir`。当时能挡住大部分，但**漏了**。

漏的原因有两条，都是实测出来的：

**一、logger 是全局单例 + 后台写盘线程。** 日志 entry 先进队列，
写盘线程随后才落盘。fixture 一结束（monkeypatch 回滚），写盘线程
再处理队列里剩下的 entry 时，路径已经变回用户真实目录——那几条
日志就写进用户的日志文件了。实测每次全量测试漏 140 字节左右。

**二、`HistoryStore` 在 `__init__` 里就把路径取走了**
（`path or paths.history_db_path()`）。单例在测试早期构造，
后面再 patch 也不生效。

**方案二（现在这版）**：给 `sdm/paths.py` 加三个环境变量覆盖，
`conftest.py` 在**模块加载时**设好，全程不撤销。

```python
# sdm/paths.py
def config_dir() -> Path:
    override = os.environ.get("SDM_CONFIG_DIR")
    if override:
        return Path(override)
    ...

def log_dir() -> Path:      # SDM_LOG_DIR
def download_dir() -> Path: # SDM_DOWNLOAD_DIR
```

```python
# tests/conftest.py —— 模块级，不是 fixture
_SANDBOX = Path(tempfile.mkdtemp(prefix="sdm-pytest-"))
os.environ["SDM_CONFIG_DIR"] = str(_SANDBOX / "config")
os.environ["SDM_LOG_DIR"] = str(_SANDBOX / "logs")
os.environ["SDM_DOWNLOAD_DIR"] = str(_SANDBOX / "downloads")
```

**环境变量而不是 monkeypatch**：环境变量是进程级的，一改就是整个
pytest 进程的事，不随 fixture 生命周期回滚。后台线程什么时候处理
队列都无所谓。

顺带说，这两个环境变量对产品也有用——便携模式可以把配置和数据库
放在 U 盘里。

**守卫机制**：`tests/test_isolation_guard.py` 断言四个路径都在沙箱里，
外加「环境变量优先级高于平台默认」和「没有环境变量时走平台默认」。
机制哪天被拆了它就红。负向验证做过——把 `SDM_CONFIG_DIR` 的设置
去掉、或者把 `paths.py` 里的覆盖逻辑删掉，守卫都抓到了。

同样的道理适用于**任何会删文件**的功能——比如日志的每日清理
（F119）。测试里那类定时器不能对着真目录跑：清理间隔要可注入，
测试用临时目录。

验证方法：跑一遍全量测试，比对用户目录里文件的大小和 mtime 有没有
变。这条检查现在是每次全量测试都跑的一部分。

## Dock 图标：关窗后藏起来，唤回时露出来

Swift 版 `App/SDMApp.swift:444-541` 的 `updateDockVisibility`：
关窗后切 `.accessory`（不占 Dock，只在菜单栏待着），任一窗口变 key
立刻切回 `.regular`。

Python 侧对应 `sdm/gui/macos_status.py` 的 `set_dock_visible()`。

**AppKit 的策略常量得实测，别凭记忆。** 实测
`AppKit.NSApplicationActivationPolicyRegular = 0`、
`Accessory = 1`。注意非 GUI 进程里 `activationPolicy()` 返回 **2**
（Prohibited）——那是「没有激活策略」，不是 Accessory。

**0.15 秒宽限**：Swift 版关窗后延迟 0.15s 再切策略，避免 cmd-W
之后马上点托盘时 Dock 图标闪一下。Python 版用 `threading.Timer`。

**sheet 和 nonactivating panel 不算「主窗口在前」**：选目录的
NSOpenPanel 关闭时不该触发 Dock 策略切换。判据是
`win.sheetParent() is not None` 和 `styleMask & NSNonactivatingPanelWindowMask`。
后者这个常量 PyObjC 没暴露（实测 `AppKit.NSNonactivatingPanelWindowMask`
是 N/A），值是 1 << 7 = 128，写死并注明出处。

**pywebview 没有「窗口变 key」事件**，所以两条唤回路径分开处理：

| 路径 | 怎么接 |
|---|---|
| 菜单栏「打开主窗口」 | `StatusBarController` 的 `openMainWindow` 回调 → `on_show` 参数 |
| Dock 图标点击 | pywebview 无对应事件；`events.shown` 只在首次显示 set 一次，`events.restored` 是取消最小化/退全屏，都覆盖不到 |

`events.shown` / `events.restored` 也挂了 `_restore_dock`——覆盖
「从最小化恢复」和「首次显示」，但**不能**指望它们覆盖
「关窗后从 Dock 唤回」。

## 分类名和扩展名是两回事

`bridge.add_url()` 收一个 `category` 参数。Swift 版
`App/MainWindow/DownloadListStore.swift:195` 是
`category ?? DownloadCategory.from(url: url)`——直接拿分类枚举用。

Python 版原来写成 `category_for_extension(category)`，把分类名
当扩展名查。前端传的 `"video"` 查不到 → 回退到按 URL 猜 →
落进 `archives` 目录。**用户选了「视频」，文件进了「归档」。**

现在 `sdm/gui/bridge.py` 的 `_resolve_category()` 两种都认：
先按分类名（`DownloadCategory(value)`），认不出来再按扩展名查
（`category_for_extension`）。

## 自检要真的能失败

`--self-test` 是 CI 和打包流程的守门人。给它加检查项时，光加一项
不够——**要证明它失败时退出码非零**，否则它就是个摆设。

`tests/test_self_test.py` 里有两条：

- 把 `_resolve_category` 换成恒返回 None 的假货 → 自检必须退出非零
- 往英文文案表塞一个中文表没有的 key → 「文案键一致」必须报 FAIL

第二条是为了区分「检查恒真」和「检查真的在查」。中英 key 集合本来
就一致，正常数据下这两种实现结果一样——只有造出不一致的数据
才能区分。

**顺带一个坑**：`I18n.catalog()` 返回的是「en 打底 + 当前语言覆盖」
的合并表，key 集合恒等。拿它检查中英一致性永远发现不了缺键。
要直接读 `sdm.i18n.CATALOGS`。


## 关窗口后 app 常驻：pywebview 拦得住


Swift 版关闭窗口后 app 还活着（SwiftUI App + 状态栏图标），能从菜单
栏唤回。

**早先这里写着「pywebview 做不到」，是错的。** 查过源码之后结论反
过来。三个事实：

| 位置 | 事实 |
|---|---|
| `webview/window.py:163` | `self.events.closing = Event(self, True)` —— **should_lock=True** |
| `webview/event.py:53-63` | should_lock 为真时 `execute()` **同步调用** handler，返回值收进 `return_values`；`set()` 返回 `len(false_values) != 0` |
| `webview/platforms/cocoa.py:1315-1317` | `should_cancel = window.events.closing.set()`，为真就 `return Foundation.NO`（取消关闭） |

所以 closing handler **返回 False 就能取消关闭**。

两个坑：

1. handler 的返回值必须是 `False` 本身。返回 `None` 不算取消——`set()`
   只找 `is False` 的值。
2. pywebview 用 `inspect.signature` 决定怎么调 handler：零参数就零参数
   调，带 `window` 这个名字的参数就传 window。写零参数最稳。

窗口被 hide 之后 `webview.start()` 不返回，main 不结束，进程就活着。
菜单栏的 quitApp 走 `window.destroy()`，那条路要能真正退出——用一个
「正在退出」标志位区分，别被关闭拦截挡住。

设置页那个「关闭窗口时隐藏 Dock 图标」开关（Swift 版的
`sdm.hideDockWhenWindowClosed`）跟着这条一起接。

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

## v2.1.0 规格补齐：这一轮做了什么取舍

这一版按用户给的 DeepSeek 规格做了整轮补齐。规格 §7 自己把功能分成两层：
**引擎和错误处理是「真正壁垒」**，**UI 和浏览器接管是「体验放大器」**，
**macOS 深度集成里只有菜单栏和 URL Scheme 低风险高回报**。所以这一轮的
优先级不是按功能点数量排的，是按「错了会毁掉用户已有数据」排的。

### 动态分段：拆在途分片 + 写入上限截断

规格 §1.2 要求「分片不是开始时一次性切死的，是下载过程中根据实际情况动态调整的」。
落地方案有两条路：

1. **字节交接**——旧连接写到的位置交给新分片，新分片从那里接手。
2. **拆点截断**——在旧分片的中点切开，旧连接写到上限就停、丢弃超出部分，
   新连接从拆点重新拉。

选了 2。原因是 1 有竞态：交接点和「新连接实际从哪个字节开始读」这两件事
各自独立发生，交接记账和请求起点对不上的时候，文件中间会有一段错位。
2 的代价是每个拆点最多浪费一个 64 KB 块，换来的是「不可能写出错位的文件」。
**下载器的第一原则是文件不能坏，速度可以慢。**

拆点取「已写位置到分片末尾」的中点，最小分片 1 MB（`min_chunk_size`）——
再小就碎片化了，连接数上限才 32，拆太碎等于自伤。

### `default_chunks` 的语义变了

以前 `default_chunks` 是「开始时切几片」，现在它是「目标分片数」，
实际起点是 `initial_chunks=2`，之后连接数按 ramp 往上走，上限就是
`default_chunks`。这样既满足规格的「不是一次性切死」，又保住了既有测试里
sidecar `chunks == 2` 的约定，也保住了 README 承诺的「默认 32 段、上限 128」。

**规格没覆盖的引擎参数保持项目既有值**，不为了对齐规格文字而改已发布的产品承诺。

### 连接数 ramp 间隔 0.1 秒

用户把线程数调到 32，不应该等 8 秒才开满。保守只体现在最初 100 ms。
ramp 需要「最近确实有进度」才放开下一级——服务器慢的时候盲目开连接只会
让每个连接都超时。

### 名字冲突默认「保留两者」

`foo.zip` 已存在 → 下成 `foo (1).zip`。覆盖策略走 `send2trash`，失败再改名兜底。
**绝不静默删用户文件。** 这是不可逆损失，跟省一个文件名不在一个量级。

### 队列严格 FIFO

`start_when="now"` 不插队。插队会让排在前面用户的任务被推后。
`"scheduled"` 的任务不进队列，等计划触发。

### 保留「链接失效」语义，但错误类别按规格归到 auth

403 / 410 / 451 → 任务转「等待新链接」（既有语义，UI 有橙色 banner 和
「刷新链接」按钮）。错误类别按规格 §5.1 归到 `auth`。两套分类各管一件事：
状态管 UI 流程，类别管「能不能重试 / 该告诉用户什么」。

### 浏览器传输以 Native Messaging 为主通道

规格 §3 明文要求。回环 HTTP 保留作回退，但**强制校验 pair token**，
且非 http(s) 协议直接拒绝。

**`/pair` 不在 HTTP 端口开放**：那个端口带 `Access-Control-Allow-Origin: *`，
开放 `/pair` 等于把 token 贴给任意网页。Safari 和没装宿主的 Firefox
走扩展选项页手填。

### 引擎侧的速度曲线

`get_speed_history` 放在引擎而不是 GUI。GUI 之前是自己轮询攒曲线的，
daemon 模式下没人攒——引擎必须是数据源。窗口按**时间**剪（60 秒）而不是
按条数剪：按条数剪会让慢速任务的曲线只剩几个点，看不出速度变化。

### 菜单栏结构变了，旧测试跟着改

契约 §5.1 把菜单栏钉成：状态行 → 显示主窗口 → 暂停全部 → 继续全部 →
剪贴板监控 → 退出。状态行放第一项、不可点，是「此刻在干什么」的一行摘要。

旧的 `tests/test_macos_status.py` 断言的是 Swift 版的六项菜单，
跟新契约不符，**改的是测试不是菜单**。菜单是用户能看见的东西，
测试是内部约定——对外契约变了才动测试。

## v2.1.0 暂不做

| # | 项 | 原因 |
|---|---|---|
| 1 | Safari 自动拦截下载 | Apple 平台限制，Safari 不允许扩展拦下载；只做右键菜单 + 工具栏按钮的手动派单 |
| 2 | App Intents 原生集成 | 规格里列为可延后；先让 AppleScript 词典把「脚本能驱动」这件事做通 |
| 3 | BT / 磁力 | 沿用 v1.0 的判断：tracker 协议 + DHT + 跨网 NAT 是半年工作量，跟「普通文件下载」定位冲突 |
| 4 | 公网分享 / 云同步 | 下载链接是私密行为，本地 SQLite 足够 |
| 5 | SOCKS 代理内置支持 | `httpx-socks` 不是标准依赖，需要用户自己装；不装时明确报错而不是静默走直连 |

## v2.1.0 已知的妥协

| # | 项 | 妥协 vs 影响 |
|---|---|---|
| 1 | **未经大规模手测** | 按仓库约定发 **Pre-release**。这一轮改动面很大（引擎 + UI + 浏览器 + 系统集成四条线同时动），手测责任在用户 |
| 2 | **Windows 产物只能由 CI 构建** | 本机是 macOS，`build.py --windows` 直接跳过；Windows 上的行为靠 CI 的构建 + 单测覆盖，真机手测没有 |
| 3 | **macOS 仍是 ad-hoc 签名** | Developer ID 签名仍是未来目标。Gatekeeper 首次打开需要用户自己放行 |
| 4 | **SOCKS 代理要额外装包** | `pip install httpx-socks`。不装时引擎明确报 RuntimeError 并说明装什么，不会静默失败 |
| 5 | **动态分段没有做过公网大文件实测** | 引擎侧有本地 Range 服务器的端到端测试，但公网 + 大文件 + 多连接的真实带宽表现还没验证过 |

## v2.1.0 集成期挖出来的三个真 bug

这三条都是「代码看起来对，但整条链路是死的」的类型。它们的共同点：
**每一环单独测都通过，合起来不工作。**

### URL Scheme 收到原始串没人解析

`url_scheme.install()` 把 Apple Event 里取到的**原始串**
（`superdownloadmanager://add?url=http://example.com/a.zip`）直接
交给回调，而 `main.py` 的回调把它当成「目标地址」入队。

结果：引擎去下载 `superdownloadmanager://add?url=...` 这个字符串。
**快捷指令那条路（规格 §4.2 的路径 A）整个是死的**——而它恰恰是
规格里推荐的、不依赖 App Intents 的方案。

`parse_scheme_url()` 写得挺好、测试也全过，只是**没人调它**。
修法是把解析塞进派发层（`make_dispatcher()`），而不是靠调用方记得调。

### Windows 的 URL Scheme 会被 argparse 拒掉

注册表里 `shell\open\command` 是 `"<exe>" "%1"`，双击链接就是拿链接
当命令行参数启动应用。`parse_args()` 原来没有位置参数，argparse 遇到
未知的位置参数**直接报错退出（退出码 2）**——应用连窗口都开不出来。

另外 `register_url_scheme()` 写好了但**没有任何地方调它**，注册表
项从来没被创建过。

### 原生消息的帧长度前缀

Native Messaging 的帧是「4 字节小端长度 + JSON」，长度**不包含**
那 4 个字节本身。文档里手写的 `\x0e` 是按 14 算的，但
`{"op":"ping"}` 只有 13 字节——宿主读到「帧被截断」。

解码器严格拒收是对的（截断的帧说明流坏了），错的是文档里的示例。
现在文档改成让 python 自己算长度，不再手写前缀。

> 顺带一提：`{"op":"ping"}` 的回包是 `{"ok":true,"pong":true}`，
> 文档原来写的是 `{"ok":true,"result":{...,"task_id":...}}`——那是
> `add` 的回包形状，一并改了。

## v2.1.0 打包上的一个坑：macOS 的 Resources 目录名

PyInstaller 打的 .app 里 `Contents/Resources/` 下已经有 `sdm` 和 `sdmd`
两个软链（指向 CLI 和 daemon），所以辅助产物的目录名要避开这两个。

原生宿主的目录名一度在两处不一致：`build.py` 写的是 `sdm-native-host/`，
而 `docs/extension-install.md` 和 `Extensions/native-messaging-host/`
的模板写的是 `native-host/`。**统一成 `native-host/`**——
文档是给人看的，模板是给人手抄的，它们指向的路径必须是真实存在的那一个。

## 发版流程：tag 必须在代码定稿之后打

`.github/workflows/release-python.yml` 监听 `v*` tag，tag 一推上去
就开始构建 macOS + Windows 并发布 Pre-release。**推 tag 之前代码必须
完全定稿**——tag 指向的 commit 就是这次发布的全部内容。

### 推 tag 之后又改了代码怎么办

能改，但要按这个顺序，顺序错了会出两份 release：

1. 先提交改动、推分支
2. **取消已经在跑的 workflow run**（`POST /repos/{owner}/{repo}/actions/runs/{id}/cancel`）
   —— 不取消的话，旧 run 跑完会拿旧 commit 发一份 release
3. 删远端 tag（`git push origin :refs/tags/v2.1.0`）
4. 删本地 tag、重新 `git tag -a` 指向新 commit、再推

第 2 步是关键。tag 删掉**不会**自动取消已经排队的 workflow——GitHub
那边照跑不误，跑完还会把 release 补出来（`softprops/action-gh-release`
遇到不存在的 tag 会自己建一个）。

### 本地先跑一遍打包

`python scripts/build.py --mac` 要在**干净 venv** 里跑。开发机上
anaconda 之类的环境里常常同时装着 PyQt5 和 PySide6，PyInstaller 会
直接报错：

```
ERROR: Aborting build process due to attempt to collect multiple Qt bindings packages
```

这不是代码的问题，是环境的问题——`webview.platforms.qt` 在 hidden
imports 里（Linux 从源码构建要用），它会把环境里的 Qt 绑定一起收进来。
CI 上是干净的 3.11 环境，没这个问题。

### 打包冒烟会跑冻结包的自检

`build.py` 打完包会跑一次 `.app` 里的 `--self-test`。所以自检里
import 一次新模块，就等于验证了「这个模块真的进包了」——比看
PyInstaller 的分析日志可靠。新加模块时记得往自检里加一条。


## 自检项要分平台：Windows CI 抓到的

打包冒烟跑的是**打包后**的可执行文件的 `--self-test`，所以自检里加一条
检查项，等于给「这个资源真的进包了」加一道闸。

但**闸门本身也会写错**。v2.1.0 加了一条「AppleScript 词典在不在」的
检查，macOS 上对（词典在 `Contents/Resources/`），Windows 上必红——
词典是 macOS 专属的东西（Apple Events + Script Editor），Windows 包里
没有它、也不需要它。红掉的不是代码，是检查项写错了地方。

两条经验：

- **自检项要认平台。** macOS 专属资源只在 macOS 上查。
- **检查项要拆成能注入的函数。** `is_macos` 单独传进来而不是在函数里
  读 `sys.platform`——源码跑的时候词典就在模块旁边，查不查都过，
  模拟不出「Windows 冻结包里没有词典」那个情形。两个分支各写一条测试，
  并且**验证过测试确实能红**（把修复回退，测试必须失败）。


## v2.1.1：三个用户报上来的问题

### `launchctl load` 会当场把 job 跑起来

开启「开机自启」时用户报「立刻多了一个 SDM 进程」。原因在 `launchctl load`
的语义：它不只是「注册」这个 LaunchAgent，**当场就把 job 跑起来**。

自启的意思是「下次登录时启动」，不是「现在启动」，所以这一步根本不该做。
而且它也不必要——launchd 每次登录都会扫 `~/Library/LaunchAgents/`，把里面的
plist 装进 launchd 域，`RunAtLoad` 为真就启动。写完 plist 就完事了。

禁用路径保留 `launchctl unload`：卸载已经注册的 job 跟「注册」是两回事，
不装不等于卸载。

### 进度合并窗口到期时要送最新的值

`ProgressChannel.report` 在 50ms 合并窗口到期时送的是 `_pending`（上一个
窗口攒的旧值），把**这一次**的上报值直接抹掉。下载末尾那次上报正好撞上
窗口到期时，最后一段进度就永远少报一截。

症状是 2 MB 的文件最终报成 2031616，正好少一个 64 KB 块——文件本身是
对的（2097152），所以是**进度上报**少了一块，不是文件损坏。

这类 bug 难定位是因为它**概率性**：看最后一个数据块落在窗口的哪一侧。
端到端那条测试跑 60 次挂 2 次。真正的锁要下在机制上——用假时钟把窗口
边界钉死，写确定性的单元测试（`tests/test_progress_channel.py`），
端到端那条只当兜底。

同一处还有第二个 bug：`ChunkProgress` 从 0 起算，续传时第一次上报比盘上
已有的字节数还小，进度条会倒退一次（引擎的 `summary.bytes_done` 直接被
覆盖），紧接着算出来的速度也是个巨大的假值。汇总器要有 `base` 基数。

### 磁力链接：立刻失败并说人话，不重试

用户报「解析链接一直在说所有重试都失败，但别的下载器能下」。磁力链接
（`magnet:` 开头）httpx 根本解析不了，异常被当成网络错误重试 5 次，最后
报「所有重试都失败」——跟真实原因完全对不上号。

**BT / 磁力本身不做**：要支持磁力得整个塞一个 BT 客户端进来（tracker
协议 + DHT + peer 连接 + piece 校验），那是另一个量级的工程。所以这次
解决的是「不支持的地址要立刻失败并说清楚」，不是「支持磁力」。

推论出一条更一般的规则：**不可重试的失败要从重试循环里摘出去。**
404（文件不存在）、416（服务器不认这个 Range）这类重试多少次结果都一样，
硬重试只是让用户多等 7.5 秒，还在日志里刷一串「第 N 次失败」——看起来
就像「所有重试都失败」，其实一次都不会成功。

### 错误分类要只有一份

「新建任务对话框」和「任务列表」对同一个失败给出过两套分类：对话框按
状态码归类，下载路径一律按 `network` 算。磁力链接在任务列表里显示成
「网络连接中断，将自动重试」还标着可重试——用户点了重试还是同样的结果。

分类逻辑收进 `ProbeError.category` 自己，两个调用方共用。

### 设置页重画要保留滚动位置

改任何设置都会重画整个设置页（后端推 `settings_updated` → `render()`），
而重画把滚动容器整个换掉，新容器的 `scrollTop` 是 0。

选「保存 / 恢复滚动位置」而不是「不重画设置页」：后者要动整个设置页的
渲染策略，改动面大得多。放回时同步设一次再用 `requestAnimationFrame`
设一次——站点规则表这种会自己撑高的块，首帧算出来的可滚动高度可能比
最终值小，同步设的位置会被截短。


## 版本号散在几个文件里，改一处不算改完

`pymain/sdm/version.py` 的 `__version__` 和 `pymain/pyproject.toml` 的
`version` 要一致，`tests/test_app_icon.py::test_version_is_consistent`
就是守这条的。

v2.1.1 发版时踩了：改了 `version.py` 忘了 `pyproject.toml`，而且
**改完版本号之后没有再跑一次全量测试**——之前的全量是在改版本号
之前跑的。CI 两个平台的测试作业全红，release 没发出去。

推论出一条流程规则：

- **「跑测试」必须发生在最后一次改动之后。** 流程上写「代码全部定稿
  → 跑测试 → 打 tag → 推 tag」，实际执行时最容易违反的就是这一条：
  改版本号、写发布说明这类"不影响代码"的改动，往往发生在跑完测试
  之后。
- 改版本号之后，最少要跑一遍**带版本一致性检查的那条测试**，不要靠
  "我只改了版本号，能有什么影响"的判断。

## v2.1.2：0 KB/s 与列表布局

### 现象

用户报：一个任务跑了 3 分半，界面一直 0 KB/s，没有报错也没有进度，
最后被取消；顺带说「部分 UI 也有错位」。

用户日志里什么都没有——`settings.json` 的 `logs_level: 3`（WARNING）
把 INFO/NOTICE 全滤掉了。复现要先把日志级别调下来。

### 「卡在 0 KB/s」是三件事叠在一起

用用户的真实链接（`dl.macked.site` 的 SharePoint 直链，服务器建连
要 2.5 秒）复现出的时间线：

```
probe #1  → 206   耗时 2.53s
probe #2  → 206   又 2.59s
chunk 0 GET      又 2.59s 才拿到首字节
```

三条根因：

1. **探测探了两次**：引擎层探一次拿真实文件名和总长度（`download_engine.
   _run_task`），下载流程层又探一次（`download_flow._run_once`）——代码
   注释自己都写着「下面的正式流程会再探一次」。两个 RTT 的纯等待。
   修法：引擎探到的结果直接交给流程层，流程层不再探第二次；探测失败
   也把错误传下去，由流程层统一分类（403 → 等待新链接，404 → 文件
   不存在），不再「探一次失败、再探一次拿错误」。
2. **探测用的超时是分片的看门狗**：`HTTPDownloader.probe` 用的是共享
   客户端的 read 超时（60 秒空闲看门狗）。那个超时是给分片用的（慢而
   健康的连接不能被误杀），给探测用是灾难：服务器不回响应头时一次
   探测就静默 60 秒。修法：探测用自己的短超时（15 秒），写在**请求**
   上而不是改客户端。
3. **重试预算只统计退避的 sleep**：`probe_retry_budget=8.0` 名义上是
   「探测阶段的总时间预算」，但 `elapsed` 只累加 `asyncio.sleep` 的
   时间。服务器不响应时预算永远花不出去，5 次重试 = 5 分钟静默。
   修法：请求本身耗掉的时间也计入预算。

推论出一条更一般的规则：**「预算」必须约束的是用户等的时间，不是
我们自己的记账时间。** 凡是名义上叫「时间预算」的东西，要算的是
用户能感知的那段。

### 「还没开始」和「卡死了」必须分开显示

界面在收到第一个字节之前，速度格恒显示「0 KB/s」。用户看到的是
「下得慢成 0」，而不是「还没开始下」。

修法不是改文案糊过去，是三件事一起做：去掉重复探测（省一个 RTT）、
给探测独立短超时并让预算真正约束总耗时（最坏二十几秒就明确失败）、
连接阶段显示「连接中」。

引擎快照上加了 `connecting` 字段：任务开跑时置 True，收到第一个
字节（进度流里 `total > 0`）时置 False，任务进入任何终态时置 False。
前端徽标和速度格都跟着它走。

### CSS 自定义属性是在「声明它的那个元素」上做替换的

界面错位那条查出来比想象的大：下载列表的 grid 模板**从来没生效过**。

表头和数据行用的是同一套 grid 模板 `--row-cols`（JS 写在列表容器
`.list-wrap` 的行内样式上），值是「`var(--w-name) var(--w-category) …`」，
而 `--w-*` 只定义在 `.row` / `.sort-header` 上。

CSS 自定义属性的 `var()` 是在**声明它的那个元素**上做替换的。写在
`.list-wrap` 上时，替换发生在 `.list-wrap` 上，那里根本没有
`--w-name` → 整个 `--row-cols` 变成「无效值」→ `grid-template-columns`
退回 `none` → **所有列竖着叠起来**（Chromium 和 WebKit 都实测：一行
281px 高，八个格子竖排）。

修法：模板写在**用它的元素**上（表头和每一行各自一份）。这样替换
用的是元素自己的 `--w-*`，容器查询（窄屏收窄列）也跟着生效。

### 表头和数据行不该由 flex 抢宽度

`.list-head` 原来用 flex 排：`.sort-header{flex:1}` +
`.col-picker-btn{flex:none;width:26px}` + `gap:10px` —— 表头因此比
数据行**窄 36px**。两边共用同一个 grid 模板（名字列是 `1fr`），宽度
一差，名字之后每一列都整体错开 36px：表头和行对不上，排序按钮点到的
也不是它下面那一列。

修法：列选择器按钮改绝对定位，浮在表头右端，表头拿回满宽。按钮压在
「操作」列表头上（那一格本来就是空的）。「操作」列被用户关掉时，
最后一列改成左对齐，给按钮腾地方。

顺带修掉两处小的：数字列（大小/速度/剩余时间）行里是左对齐的、表头
是右对齐的，标签和数字永远对不上；排序箭头没选中时是透明的但照样占
宽度 + 一个 3px 的 gap，右对齐的表头文字会整体左移 3px。

### UI 类问题要用无头浏览器量像素

纯 CSS 断言看不出「整张表塌了」「差 36px」这种问题。列对齐用真的
浏览器量：注入假任务 → `renderDownloads()` → 比较 `.sort-header > *`
和 `.row > *` 的 `getBoundingClientRect()`。

两条纪律：

- **量「用户看到的文字位置」，不是盒子位置**。表头格子 `.sh` 带
  `margin:-2px -4px`（给 hover 背景留地方），盒子比轨道宽 8px，
  但文字仍在轨道起点上——量盒子会量到那个负边距。
- **表头和数据行要对齐的是同一个东西**：grid 轨道一致 + 文字边缘
  一致。状态那列行里是 badge 胶囊（自带 9px 内边距），文字天然内缩，
  比轨道就够了，不能比文字。

### 验证方式

- 引擎侧：`tests/test_connect_phase.py`——探测预算把请求耗时算进去
  （回退修复后测试会红）、流程层不重复探测、探测超时要给一句人话、
  连接态的生命周期
- 前端：`tests/test_connecting_js.mjs`（连接中不能显示 0 KB/s）、
  `tests/test_column_alignment_js.mjs`（无头 Chromium 量像素，两种列
  组合都测；缺 playwright 就跳过，CI 上没有浏览器）
- 每条新测试都验过「有牙齿」：临时回退修复，确认测试变红
