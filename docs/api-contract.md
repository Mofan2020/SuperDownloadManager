# 跨模块接口契约（v2.1）

这份文件是**并行开发的唯一真源**。引擎层、UI 层、浏览器接管层、系统集成层
各自独立开发，靠这份契约对齐。任何一方改接口，先改这里。

对应规格：DeepSeek 整理的《下载管理器规格文档》§1 引擎 / §2 UI / §3 浏览器接管
/ §4 系统集成 / §5 错误处理。

---

## 1. 错误分类（`sdm/engine/errors.py`）

规格 §5.1 要求 12 个类别。**枚举值就是规格里的字符串**，前端和 CLI 直接用。

```python
class ErrorCategory(enum.StrEnum):
    NETWORK       = "network"          # DNS 失败 / TCP 复位 / 不可达
    TIMEOUT       = "timeout"          # 请求或读取超时
    SERVER_ERROR  = "server_error"     # 5xx
    RATE_LIMITED  = "rate_limited"     # 429
    CLIENT_ERROR  = "client_error"     # 4xx（429 除外）
    AUTH          = "auth"             # 401 / 403
    NOT_FOUND     = "not_found"        # 404
    RANGE_ERROR   = "range_error"      # 416
    DISK          = "disk"             # 磁盘满 / 写入失败 / 权限拒绝
    STALE_SESSION = "stale_session"    # 恢复时 ETag/Last-Modified/大小变了
    NAME_CONFLICT = "name_conflict"    # 目标路径被占用
    UNKNOWN       = "unknown"
```

```python
@dataclass
class TaskError:
    category: ErrorCategory
    message: str          # 用户可见的中文/英文短句
    detail: str = ""      # 技术细节（日志、复制错误详情用）
    retryable: bool = False
    status: int | None = None
    def to_dict(self) -> dict: ...
```

**重试语义（规格 §5.2）**

| 类别 | retryable | 行为 |
|---|---|---|
| `network` / `timeout` / `server_error` | 是 | 逐分片指数退避重试，base 500ms、上限 30s、抖动 ~200ms、每分片最多 5 次 |
| `rate_limited` | 是 | 同上，但退避时长至少为 `Retry-After`；同时把全局并发降一档 |
| `client_error` / `auth` / `not_found` | 否 | 立即失败该分片，**不消耗**其它分片的重试预算 |
| `range_error` | 否 | 触发回退：单流重下 |
| `disk` | 否 | 失败并提示用户检查空间与权限，用户修好后可手动重试 |
| `stale_session` | 否 | 会话跟服务器对不上：**流程层自动清掉会话和分片、从头再下一次**，并在界面上写明「已重新开始下载」。只有连重下都失败时才会真的落到这个类别 |
| `name_conflict` | 否 | 需要用户决策：保留两者 / 覆盖 |
| `unknown` | 视情况 | 默认不可重试 |

**HTTP 状态码 → 类别**：`401/403` → `auth`；`404` → `not_found`；`416` →
`range_error`；`429` → `rate_limited`；`5xx` → `server_error`；其它 `4xx` →
`client_error`。

> 项目已有的「链接失效」语义（403/410/451 → 任务进 `awaiting_new_link`）
> **保留**。它是本产品的差异化功能：用户可以粘贴新链接从已下分片续传。
> 错误类别仍按上表归类（403 → `auth`，404 → `not_found`），但任务状态走
> `awaiting_new_link` 而不是直接 `failed`。

---

## 2. 引擎 API（`sdm/engine/download_engine.py`）

### 2.1 `StartSpec` 新增字段

```python
@dataclass
class StartSpec:
    url: str
    destination_directory: Path
    filename_override: str | None = None
    max_threads_override: int | None = None
    referer: str | None = None
    headers: dict[str, str] = field(default_factory=dict)   # 任务级自定义头
    proxy: str | None = None                                 # 任务级代理覆盖
    chunk_count_override: int | None = None                  # 分块数（None = 自动）
    category: str | None = None                              # 用户显式选的分类（None = 自动）
    start_when: str = "now"                                 # now | queue | scheduled
    checksum_algorithm: str | None = None                   # md5 | sha1 | sha256 | None
    on_complete: str = "none"                               # none|open_file|open_folder|run_script|sleep
    on_complete_script: str = ""                            # run_script 用的脚本路径
```

### 2.2 `TaskSummary` 新增字段

```python
    error_category: str | None = None       # ErrorCategory 的 value
    error_retryable: bool = False
    active_connections: int = 0             # 当前活跃连接数
    uploaded_bytes: int = 0
    checksum_algorithm: str | None = None
    checksum_value: str | None = None
    server_supports_ranges: bool = True     # False = 服务器不支持分块
    queued_position: int = 0                # 队列中的位置（0 = 下一个就跑）
    stale_restarted: bool = False           # 这次是「重新开始」过的（见下）
```

`to_dict()` 必须包含以上全部字段（前端和 CLI 依赖它们）。

**`stale_restarted` 的含义**：服务器上的文件变了 → 旧进度作废 →
流程层清掉会话和分片、**从头再下一次**。这个标志让界面能解释
「进度为什么归零」——用户明明看到进度从 90% 掉回 0%。
界面按**当前语言**显示这句解释，不直接用引擎里的中文原文。

### 2.3 新增方法

```python
async def probe_url(self, url: str, referer: str | None = None) -> dict
    """探测 URL，给新建任务对话框用（规格 §2.2 的自动解析）。

    返回: {"ok": bool, "filename": str|None, "size": int|None,
           "final_url": str|None, "accepts_ranges": bool,
           "category": str|None,
           "error": str|None, "error_category": str|None}
    探测失败**不是终态**：返回 ok=False，调用方让用户改完重试。

    `category` 是按**服务器给的文件名**（Content-Disposition）判出来的
    分类（`video` / `music` / `pictures` / `archives` / `documents` /
    `software` / `other`）。前端据此自动切换分类下拉——用户没手动
    选过时，.dmg 会自动变成「程序」。
    """

async def set_global_proxy(self, proxy: str | None) -> None
async def set_task_proxy(self, task_id: str, proxy: str | None) -> None
async def set_task_checksum(self, task_id: str, algorithm: str | None) -> None
async def set_task_on_complete(self, task_id: str, action: str, script: str = "") -> None
async def move_task(self, task_id: str, position: str) -> None   # "top" | "bottom"
async def retry_task(self, task_id: str) -> None                  # 从持久化进度恢复
def get_task_error(self, task_id: str) -> dict | None
    # {"category": str, "message": str, "detail": str, "retryable": bool, "status": int|None}
```

### 2.4 队列语义（规格 §1.8）

- 同时活跃任务数上限：`settings.max_concurrent_tasks`，默认 **5**。
- 队列顺序 FIFO；`move_task` 支持置顶 / 置底。
- 超过上限的任务停在 `queued`，有槽位时自动开跑。
- 暂停 / 取消 / 失败释放槽位。

### 2.5 动态分段（规格 §1.2）

- 初始分片数保守（`initial_connections`，默认 2），**不**一次性切死。
- 空闲连接到来时，对**当前最大的未完成分片**对半拆分，新连接从拆分点继续。
- 分片数上限 `max_chunks`（默认 32，可配到 512），**但强制不低于
  「连接数 × 4」**——分片数被卡到跟连接数一样大时，空闲连接没活干，
  下载末尾会长出很长的尾巴。
- **目标分片数 = 连接数，不是分片数的硬上限。** 分片数必须能超过
  连接数，多出来的分片就是给空闲连接准备的活干。
- 最小可拆大小**随剩余量自适应**：按「剩余总量 / 目标分片数」算出
  平均每片该多大，夹在硬地板（64KB）和 `min_chunk_size` 之间。
  剩得越少，切得越细。
- worker 拆不出新分片时**不退出**，改成等新分片（20ms 一轮）——早退
  会让那条连接永久消失。
- 「找活干」**不占连接名额**；名额只在真正传输时才占。
- 分片布局落盘到会话文件，崩溃/暂停后能按原布局恢复。

### 2.6 会话文件（规格 §1.3）

路径：`~/Library/Application Support/SuperDownloadManager/downloads/<key>.sdm.session.json`
（Windows 是 `%LOCALAPPDATA%\SuperDownloadManager\downloads\`，
Linux 是 `~/.local/share/SuperDownloadManager/downloads/`）。

**辅助文件一律不住在下载目录里。** `key` 是目标文件名主干加上目标
绝对路径的短哈希——同名文件在不同目录是不同文件，sidecar 用来比对
etag / content_length 判断「还是不是同一个文件」，共用一个会导致把
新内容续写进旧文件的空洞里。

升级前的会话文件（目标文件旁边）会在读取时自动搬过来。

```json
{
  "version": 1,
  "url": "...",
  "etag": "...",
  "last_modified": "...",
  "content_length": 12345,
  "chunks": [{"id": 0, "start": 0, "end": 100, "bytes_done": 40}, ...],
  "saved_at": 1730000000.0
}
```

- 原子写（临时文件 + `os.replace`）。
- 恢复时按 **ETag → Last-Modified → Content-Length** 的顺序校验资源是否变化；
  变了就是「这个会话永久不可用了」——**流程层自己清掉会话和分片，
  在同一个协程里从头再下一次**（不新建后台任务），并置 `stale_restarted`，
  界面据此提示「服务器上的文件已变化，已重新开始下载」。
  **不抛给用户**：抛出去等于用户点一次重试失败一次，永远恢复不了。
- **弱 ETag（`W/"..."`）不参与判断**——RFC 7232：弱校验器只保证语义等价，
  不保证字节一致，而且明确禁止用于 Range 请求。很多 CDN 每次请求都换弱
  ETag，拿它判「资源变了」会把用户的每一次续传都变成「从头重下」。
- **强 ETag 变了也不直接判死**：长度一样时先用**字节比对**确认（拿盘上
  已下载的那段字节去服务器取同样的偏移比一比）。内容一样 = 服务器在轮换
  ETag，照样能续。
- 下载完成 / 取消后删除会话文件。

### 2.7 换链接（`relink_task` / `engine.relink`）

链接失效（403/410/451）时任务进入 `awaiting_new_link`，用户拿到新链接后
调用 `relink_task(task_id, new_url)`。

返回：

```json
{"decision": "safeToResume" | "unsafeButProceed" | "unsafeReject",
 "reason": "...", "verifiedSameFile": true}
```

| 判定 | 含义 | 行为 |
|---|---|---|
| `safeToResume` | 确认是同一个文件 | **续传**：把会话文件里的 URL 改写成新链接，同步新链接的 ETag / Last-Modified，已下载的字节一个都不重下 |
| `unsafeButProceed` | 没法确认是同一个文件（没下过一个字节、无法取样、或新链接不支持 Range） | 清掉会话和分片，**从头开始**，界面上说清楚进度没了 |
| `unsafeReject` | 确认是**另一个文件**（长度不同，或字节比对不一致） | 拒绝，不动任何已下载的字节 |

**判定「是不是同一个文件」用字节比对，不用 ETag。**

- 长度只能说明「大小一样」——1MB 的安装包和 1MB 的壁纸长度相同。
- ETag 靠不住——很多 CDN 每次请求都给不同的弱 ETag，同一个文件两次请求
  ETag 不同是常态，拿它判「换了文件」会把正常的换链误杀成「下不了」。
- 字节比对是内容级的：同样的偏移取同样的字节，逐字节相同就说明两个链接
  指向同一份内容。

取样方式：拿盘上已下载分片的**头部若干字节**（最多 3 段、每段 4KB），
对新链接发同样的 `Range` 请求取回来比。取样请求带 `X-SDM-Verify: 1` 头，
用来把「校验」和「下载」区分开。

**「续传」成立的前提是会话文件里的 URL 被改写成新链接。** 不改的话，
流程层恢复时会检查「会话的 URL 跟本次下载的 URL 一致吗」，不一致就判定
「同名文件重新下载」，把整个会话丢掉、从零重下。

> `unsafeButProceed` 里的「没法确认」不是失败——任务继续下，只是从零开始。
> 拒绝（`unsafeReject`）只给「确认是另一个文件」的情况，避免把别的内容
> 写进用户以为的那个文件里。

---

## 3. 桥接层 API（`sdm/gui/bridge.py`）

### 3.1 JS → Python（规格 §2.4 要求的 9 个方法，名字必须一字不差）

| 方法 | 参数 | 返回 |
|---|---|---|
| `create_task(url, options)` | `options` 是 dict，可含 `filename` / `save_dir` / `category` / `proxy` / `headers` / `chunk_count` / `start_when` / `checksum` / `on_complete` / `on_complete_script` / `referer`。**`category` 传 null / 空 / `"auto"` = 自动**（引擎按 URL 和服务器给的文件名判断）；传具体分类 id 才表示用户选定了。`chunk_count` 引擎仍然支持，但 GUI 不再传——分片由引擎动态规划 | `{"ok": bool, "task_id": str|None, "error": str|None}` |
| `pause_task(task_id)` | | `{"ok": bool, "error": str|None}` |
| `resume_task(task_id)` | | 同上 |
| `remove_task(task_id, delete_file)` | `delete_file` 决定**已完成的成品**是否删除 | 同上 |
| `get_tasks(filter)` | `filter` 是 dict，可含 `state` / `category`；`None` = 全部 | 任务数组 |
| `get_task_detail(task_id)` | | `{"task": {...}, "chunks": [...], "error": {...}|null, "speed_history": [...]}` |
| `set_global_speed_limit(kbps)` | 整数或 `None`（不限速） | `{"ok": bool}` |
| `get_settings()` | | 设置对象 |
| `save_settings(obj)` | 设置对象（只改传入的键） | `{"ok": bool}` |

**已有的旧名字保留为别名**，别删（前端和 CLI 还在用）：
`add_url` / `update_settings` / `get_task` / `get_task_chunks` / `cancel_task` /
`retry_task` / `clear_completed` / `pause_all` / `resume_all` /
`set_task_speed_limit` / `relink_task` / `set_language` / `choose_directory` /
`get_clipboard_candidate` / `set_clipboard_candidate`。

新增（供 §2.2 / §2.3 / §1.8 用）：

| 方法 | 说明 |
|---|---|
| `probe_url(url)` | 探测，返回 §2.3 的字典 |
| `set_global_proxy(proxy)` | `None` / `""` = 清除 |
| `set_task_proxy(task_id, proxy)` | |
| `set_task_options(task_id, options)` | 改任务的校验算法 / 完成后行为 / 代理 |
| `move_task(task_id, position)` | `top` / `bottom` |
| `retry_task(task_id)` | 从持久化进度恢复 |
| `get_task_error(task_id)` | |
| `get_speed_history(task_id)` | 最近 60 秒的速度采样点 |
| `get_status_summary()` | 全局摘要：`{"speed": 字节/秒, "active_count": int, "limit": 字节/秒, "progress": 0~1}`。**给菜单栏状态行和 Dock 进度用**，不走 `get_tasks()`——那个方法给每个任务建一个字典，1 秒一次、几百个任务时是白烧 CPU（规格 §6.4） |

### 3.2 Python → JS 推送

**统一走一个入口**，前端只注册一个函数：

```js
window.sdmEvent(kind, payload)
```

| kind | payload |
|---|---|
| `task_updated` | 任务 dict（同 `get_tasks()` 里的元素） |
| `task_removed` | `{"id": "..."}` |
| `global_speed_updated` | `{"bytes_per_sec": 123.0, "limit_bytes_per_sec": 0, "active_count": 2}` |
| `notice` | `{"level": "info"\|"warn"\|"error", "message": "..."}` |
| `settings_updated` | 设置对象 |

轮询保留但降频到 1500ms，推送是主通道。

---

## 4. 浏览器接管（`sdm/ipc/native_host.py` + `Extensions/`）

### 4.1 传输

规格 §3.1 要求 **Native Messaging**。主通道是 Chrome 的
**4 字节小端长度前缀 + JSON** 帧格式，走 stdio。

- 应用 ID：`com.skyc8266.superdownloadmanager`
- 主机清单：`Extensions/native-messaging-host/com.skyc8266.superdownloadmanager.json`
- 安装位置：
  - macOS Chrome：`~/Library/Application Support/Google/Chrome/NativeMessagingHosts/`
  - macOS Chromium：`~/Library/Application Support/Chromium/NativeMessagingHosts/`
  - macOS Edge：`~/Library/Application Support/Microsoft Edge/NativeMessagingHosts/`
  - macOS Firefox：`~/Library/Application Support/Mozilla/NativeMessagingHosts/`
  - Windows Chrome：`%LOCALAPPDATA%\Google\Chrome\User Data\NativeMessagingHosts\`
  - Windows Edge：`%LOCALAPPDATA%\Microsoft\Edge\User Data\NativeMessagingHosts\`
  - Windows Firefox：`%APPDATA%\Mozilla\NativeMessagingHosts\`
- 清单里的 `path` 指向安装目录下的启动脚本（macOS `.command` 风格的 sh 脚本，
  Windows `.cmd`），脚本转调打包好的 `sdmd-native-host` 可执行文件。

**HTTP 通道保留**（`127.0.0.1:7955`），但必须：带 Pair Token、
拒绝 `blob:` / `data:`、支持 POST 体、把 referer / cookie / UA 传下去。

### 4.2 Pair Token（规格 §3.1.2）

- 首次配对时本地生成 32 字节随机 token，存 `settings.browser_pair_token`。
- 扩展第一次连接时发送 `{"op": "pair"}`，应用把 token 返回给扩展，
  扩展存进 `browser.storage.local`。
- 之后每条消息带 `{"op": "add", "token": "<token>", ...}`。
- 应用侧不匹配就拒绝，返回 `{"ok": false, "error": "unauthorized"}`。

### 4.3 消息格式

```json
{"op": "pair"}
{"op": "add", "token": "...", "url": "https://...", "filename": "a.zip",
 "referer": "https://page", "cookie": "...", "user_agent": "...",
 "method": "GET", "post_data": null}
```

`op` 取值：`pair` / `add` / `ping`。

响应：

```json
{"ok": true, "task_id": "..."}
{"ok": false, "error": "...", "error_category": "..."}
```

### 4.4 扩展行为（规格 §3.1）

1. **下载开始前取消浏览器原生下载**：`chrome.downloads.onDeterminingFilename`
   里 `return {cancel: true}`，同时用 `chrome.webRequest` 监听请求拿
   Cookie / UA / POST 体。
2. **逃生舱**：按住 `Alt`（macOS `Option`）点击时不接管。
3. **不支持的协议**：`blob:` / `data:` 直接放行给浏览器，**不接管**，
   也不能取消浏览器下载（否则用户会丢文件）。
4. 必须支持的浏览器：**Chrome / Edge / Firefox**（Chromium 系用
   `background.service_worker`，Firefox 用 `background.scripts`）。
5. **Safari**：Web Extension，手动「发送到下载器」（右键菜单 + 工具栏按钮），
   不做自动拦截。UI 里必须标注**试验性**，首次启用时说明「可能不适用于
   所有网站，需要在 Safari 设置里手动启用扩展」。

### 4.4.1 Chrome / Edge 的扩展 ID 必须固定

宿主清单里的 `allowed_origins` 要跟扩展的 origin **精确一致**，否则
Chrome 直接拒绝拉起宿主。而「加载已解压的扩展程序」装出来的扩展，
ID 默认是按**目录路径**算的哈希——换个机器、换个目录就变了。

所以 Chrome / Edge 的 manifest 里写死一把公钥（`key` 字段），
扩展 ID 由公钥算出来，恒定：

```
扩展 ID = SHA256(公钥 DER 字节) 的前 32 个十六进制位，每个位 0-f 映射成 a-p
```

当前值 `bangcjehjdodcpfekmnkfmbeffcclpih`，定义在
`sdm/ipc/native_host.py:CHROMIUM_EXTENSION_ID`。**换公钥 = 所有装好的
宿主清单全部失效**，有一条测试盯着两边是否一致。

Firefox 不需要这一步：它的扩展 ID 是 manifest 里
`browser_specific_settings.gecko.id` 自己定的，宿主清单用
`allowed_extensions` 授权。

### 4.5 用户界面：设置页的「浏览器接管」板块

**扩展不能只躺在源码仓库里。** 用户得能拿到扩展文件、知道装没装、
知道 Safari 为什么要手动粘 token。设置页新增板块：

| 元素 | 数据来源 | 说明 |
|---|---|---|
| 配对 token | `browser_pair_token` | 可复制、可**重新生成**（重新生成 = 旧 token 立刻作废） |
| 扩展目录 | 打包版 `Resources/extensions/<浏览器>`；源码版仓库根 `Extensions/` | 可在访达/资源管理器里打开 |
| 装没装 | 扫各浏览器的扩展目录读 manifest 名字；Safari 走 `pluginkit` | 按 manifest 的**名字**认，不按扩展 ID |
| 安装入口 | 浏览器自己的扩展管理页 | Safari 没有这一项 |

桥接层接口（`GuiBridge`）：

- `get_browser_capture_info()` → `{token, extension_root, browsers[], host_browsers[]}`
- `reset_pair_token()` → 新 token（旧 token 立刻作废）
- `open_browser_extension_folder(browser_id)` → 在文件管理器里打开扩展目录
- `open_browser_extensions_page(browser_id)` → 打开浏览器的扩展管理页

判定「装没装」的逻辑在 `sdm/ipc/browser_capture.py`，**只读**：
它只回答「现在是什么状态」，安装动作全在 GUI 层（用户点了才做）。

**Safari 必须手动配对**：Safari 没有 Chrome 那种原生消息宿主，只能走
回环 HTTP 通道——而回环通道**不开 `/pair`**（那个端口带着
`Access-Control-Allow-Origin: *`，把 token 暴露给网页等于打开
「谁都能往下载器塞任务」的门）。所以 Safari 的扩展选项页和弹窗里
都要有 token 输入框。

**Safari 不签名**：本项目没有 Apple Developer Program 账号，走 Apple 官方的
「未签名的临时扩展」路径（设置 → 高级 → 显示网页开发者功能；
设置 → 开发者 → 允许未签名的扩展；设置 → 开发者 → 添加临时扩展…）。
平台限制必须在 UI 和文档里如实标注：退出 Safari 重置、临时扩展最长
24 小时、Safari 只能手动派单。

---

## 5. 系统集成

### 5.1 macOS 菜单栏（规格 §4.1）

菜单项顺序：

1. 状态行（实时）：`下载中 3 个 · 2.4 MB/s`（有速度上限时加 `· 限速 512 KB/s`）
2. 显示主窗口
3. 暂停全部
4. 继续全部
5. 剪贴板监控（勾选项）
6. 退出

- 状态行以 ~1.5 秒为周期刷新（不是每帧）。
- 「退出」必须显式调用 `StatusBarController.remove()` 把图标摘掉。

### 5.2 Windows（规格 §4.5）

- 系统托盘：图标 + 与 macOS 菜单栏行为一致的菜单。
- Toast 通知：已有。
- URL Scheme 注册表：`HKEY_CURRENT_USER\Software\Classes\superdownloadmanager`
  （含 `URL Protocol`、默认 `shell\open\command`）。
- 开机自启：`HKCU\...\Run`，已有。

### 5.3 AppleScript（规格 §4.3）

最小可用字典，命令走**同一个 Task Service**，不绕过引擎状态机：

- `add download "URL"` → 返回任务 id
- `pause all downloads`
- `resume all downloads`
- `get download list` → 返回「名称 + 状态」的简单列表

`.sdef` 文件放在 `pymain/sdm/gui/applescript/SuperDownloadManager.sdef`，
打包时放进 `Contents/Resources/`，`Info.plist` 写 `OSAScriptingDefinition`。

---

## 6. 设置新增键（`sdm/settings.py`）

```python
max_concurrent_tasks: int = 5        # 同时活跃任务数
theme: str = "system"                # system | light | dark
proxy_url: str = ""                  # 全局代理，空 = 不走代理
proxy_scheme: str = "http"           # http | https | socks4 | socks5
browser_pair_token: str = ""         # 浏览器配对 token
initial_connections: int = 2         # 初始连接数（动态分段的起点）
max_chunk_count: int = 32            # 分片数上限（引擎里强制 ≥ 连接数 × 4）
network_user_agent: str = ""         # 全局 UA，空 = 用程序内置的默认 UA
```

UA 的优先级（高 → 低）：**站点规则里的 UA** → 全局 UA → 内置默认 UA。
清空全局 UA = 恢复内置默认，不是「发一个空的 UA」。

新增键要同时写进 `_KEY_MAP`（设置键名映射）。没有历史对应键的用
`sdm.<字段名>` 形式。
