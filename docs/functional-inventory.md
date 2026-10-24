# SuperDownloadManager（Swift 版）功能台账

> **用途**：本文是 Swift 版 SuperDownloadManager（v1.1.9 / v1.1.10-WIP）的完整功能台账，
> 作为 **Python 重构（pywebview GUI + httpx 引擎）的验收基准**。
>
> - 每条功能给出**触发路径**、**现有行为**、**Swift 实现位置（file:line）**、
>   **可测试的行为约定**、**Python 重构验收要点**。
> - 行号基于提交 `1560583`（"WIP: 保存 v1.1.10 流式传输改造"）。
> - 代码事实优先于文档措辞。凡源码与 `docs/notes.md` / `docs/extension-install.md`
>   描述不一致处，本文以源码为准，并在第 5 章「重构风险清单」中标出。

---

## 0. 阅读指引

| 标记 | 含义 |
|---|---|
| ✅ | Swift 版已完整实现，Python 版必须等价 |
| ⚠️ | Swift 版**只实现了一半 / 有 bug**，Python 版必须做对（详见第 5 章） |
| 🚧 | Swift 版只有骨架 / 死代码，Python 版可选择性实现（详见第 4 章） |

### 0.1 代码规模

| 模块 | 文件数 | 行数 | 说明 |
|---|---|---|---|
| `Engine/` | 24 | ~4,700 | 纯 Swift 框架 `SuperDownloadManagerManagerCore`，无 UI 依赖 |
| `App/` | 12 | ~2,900 | SwiftUI 界面层 + AppDelegate + 状态桥 |
| `CLI/` | 2 | 435 | `sdmdm` 命令行客户端 |
| `Daemon/` | 4 | 437 | `sdmd` 守护进程 + UDS server + Dispatcher |
| `Extensions/` | 8 | ~350 | Firefox(MV3) / Safari Web Extension |
| `Tests/` | 11 | ~2,000 | 84 个 test 函数 |

### 0.2 全局不变式（Python 版必须保持）

| # | 不变式 | 依据 |
|---|---|---|
| G-1 | 下载任务状态集合固定为 `queued / downloading / paused / completed / failed / cancelled`（另有设计上存在但**从不写入**的 `awaiting_new_link`） | `Engine/TaskQueue.swift:20` |
| G-2 | 文件总字节数 `totalBytes` 来自 HTTP 探针，**不是** 累加得到的字节数 | `Engine/ContentSniffer.swift:261-270` |
| G-3 | 所有分片写入**独立**的 per-chunk partial 文件，合并只在全部完成后发生一次 | `Engine/FileAssembler.swift:111-182` |
| G-4 | 合并后的目标文件先 `ftruncate` 到最终大小再逐段拷贝；partial 在拷贝后立即删除 | `Engine/FileAssembler.swift:163-181` |
| G-5 | 进度上报必须是**文件级单调递增**的聚合值，不是单分片百分比 | `Engine/DownloadFlow.swift:314-333` |
| G-6 | 终态（`completed/failed/cancelled`）与 `paused` 状态，速度显示必须**立即为 0**，不允许 EMA 衰减 | `Engine/SpeedDisplay.swift:62-69` |
| G-7 | 取消/暂停是**协作式**的：先改状态，再取消 Task，最后由 catch 读标记决定是否报 failed | `Engine/DownloadEngine.swift:313-334, 343-348` |
| G-8 | 所有落盘字节必须**边到边写**（不能整块缓冲到内存再落盘） | `Engine/ChunkStreamDelegate.swift:1-41` |
| G-9 | 重连后 `Range` 头必须**从已落盘字节数继续**，不能从 chunk 起点重来 | `Engine/HTTPDownloader.swift:231-233` |
| G-10 | 被拒绝的响应（403 等）**一个字节都不能写进 partial** | `Engine/ChunkStreamDelegate.swift:129-152` + `Tests/EngineTests/StreamingTransportTests.swift:147-172` |
| G-11 | 用户语言切换**立即生效**，不需重启 | `Engine/L10n.swift:582-585` |

---

## 1. 功能台账

### A. 下载引擎 / 网络传输

| 编号 | 功能名 | 触发路径 | 现有行为描述 | Swift 实现位置 | 关键行为约定（可测试） | Python 重构验收要点 |
|---|---|---|---|---|---|---|
| F001 | HTTP 探针（取元数据） | 引擎内部，每个任务启动时自动 | 发 `GET` + `Range: bytes=0-0`，读取响应头后丢弃 body。**不使用 HEAD**（HEAD 在行为不端的服务器上兼容性差） | `Engine/ContentSniffer.swift:229-248` | 请求方法必须是 `GET`；必须带 `Range: bytes=0-0`；`timeoutInterval = 30`；body 丢弃但连接要正常关闭 | httpx 客户端必须实现 `probe()` 等价函数；用 `GET`+Range 而非 `HEAD`；单次探测超时 30 s |
| F002 | 总长度解析（Content-Range 优先） | 探针后自动 | 先从 `Content-Range: bytes a-b/TOTAL` 的 `/TOTAL` 取总长；取不到才回退 `Content-Length` | `Engine/ContentSniffer.swift:261-270` | 探针返回 206 时 `Content-Length` 只是 1 字节，**必须**从 `Content-Range` 尾部取真实总量；两者都缺失时 `contentLength = nil`，引擎应报 `noContentLength` | 复刻 `parse_content_range_total` 优先级；缺失总长必须报错而不是当成 0 |
| F003 | `Accept-Ranges` 探测 | 探针后自动 | 头值小写后 `!= "none"` 即认为支持 Range。**只记录，不用于决策** | `Engine/ContentSniffer.swift:255` | 头缺失时 `acceptsRanges = true`（宽松默认）；该字段目前不参与分片决策 | 保留字段；宽松默认；不要因为缺头就拒绝下载 |
| F004 | ETag / Last-Modified 读取 | 探针后自动 | 读取 `ETag` 与 `Last-Modified`，`HTTPURLResponse` 会把 `ETag` 规范化成 `Etag`，两种拼写都要查 | `Engine/ContentSniffer.swift:273-274` | 必须同时尝试 `ETag` 和 `Etag`；`Last-Modified` 同理 | httpx headers 大小写不敏感，但要显式处理两种拼写 |
| F005 | Content-Disposition 文件名解析 | 探针后自动 | 支持 4 种形态：`filename="foo.zip"`（quoted）、`filename=foo.zip`（unquoted）、`filename*=UTF-8''foo%20bar.zip`（RFC 5987）、以及 1+3 / 2+3 混合。**`filename*` 优先于 `filename`** | `Engine/ContentSniffer.swift:336-356`, `362-379`, `384-397` | 解析按 `;` 切段，逐段 case-insensitive 匹配 `filename*=` / `filename=` 前缀；ext-val 解码 `UTF-8''<pct-encoded>`；quoted 值去掉首尾引号；unquoted 值去掉尾部 `;` 与引号；`filename*` 存在时**忽略** `filename` | 必须完整实现 RFC 6266 + RFC 5987；**GitHub release CDN 用的就是 unquoted 形态**，漏掉它会导致文件名变成随机 UUID |
| F006 | URL 路径兜底文件名 | Content-Disposition 缺失或为空时 | 取 `originalURL.pathComponents.last`；最终引擎再退到 `url.lastPathComponent` → `host` → `download.bin` | `Engine/ContentSniffer.swift:291-297`, `Engine/DownloadEngine.swift:499-504` | 空字符串与 `nil` **都要**走兜底（v1.1.1 修过这个）；三级兜底顺序固定 | 三级兜底顺序一致；`download.bin` 是最后一级 |
| F007 | 最终 URL 回填（跟随重定向） | 探针后自动 | 探针的 `ContentProbe.url` 保存 `http.url`（重定向后的最终地址），后续**所有分片请求都用这个 URL**，跳过重定向链 | `Engine/ContentSniffer.swift:300`, `Engine/DownloadFlow.swift:104` | 分片的 `Range` 请求必须打在**最终 URL**上，不是用户给的原始 URL | httpx 必须 `follow_redirects=True` 探针，并把 `response.url` 传给分片层 |
| F008 | 魔数内容嗅探 | 目前无调用方（🚧） | 19 种签名：Mach-O / ZIP 族 / RAR / 7z / DMG / gzip / bz2 / xz / PDF / JPEG / PNG / GIF / MP4(ftyp) / MP3(ID3 & 帧同步) / WebM / Ogg / WAV / Java class / HTML | `Engine/ContentSniffer.swift:34-56, 68-223` | 只看前 512 字节；签名不在偏移 0 的（tar@257）**不检测**；不匹配返回 `.unknown` | 可选实现；若实现必须能区分 ZIP 三个变体 `03 04`/`05 06`/`07 08` |
| F009 | 初始分段规划 | 每个下载开始 | `n = max(1, min(defaultChunks, contentLength / initialChunkSize))`，等分 `[0,total)`，最后一块吃掉余数 | `Engine/RangeManager.swift:83-96` | 默认 `defaultChunks=32`、`initialChunkSize=2 MB`；`n` 至少为 1；分片**必须覆盖整个文件且不重叠** | 复刻 `start_plan` 的 n 计算与等分逻辑；覆盖性用测试断言 |
| F010 | 并发分片下载 | 每个下载开始 | `TaskGroup` 并行跑 `n = max(1, min(chunks.count, parallelismCap))` 个分片 worker；只跑 `bytesDone < size` 的分片 | `Engine/DownloadFlow.swift:135-198` | `parallelismCap` 是**硬上限**；已完成的分片不重跑；分片间**不共享文件句柄** | asyncio 下用 `asyncio.Semaphore` 或任务组实现同样的并发上限；`httpx` 单 client 的连接池上限也要 ≥ 分片数 |
| F011 | Range 头语义 | 每个分片请求 | `Range: bytes=<start>-<end-1>`，其中 `end` 是**开区间上界** | `Engine/HTTPDownloader.swift:280-281` | 闭区间！`chunk.end` 是 exclusive，头里必须写 `end-1`；单字节分片写 `bytes=start-start`（`Chunk.rangeHeader()`） | 闭区间语义，off-by-one 是最高频 bug 源；写单测锁死 |
| F012 | 流式传输（边收边写盘） | 每个分片 | 用 `URLSessionDataDelegate.didReceive data:` 逐块回调，`ChunkWriter` actor 串行落盘。**禁止**整块缓冲到内存 | `Engine/ChunkStreamDelegate.swift:43-197`, `Engine/HTTPDownloader.swift:265-391` | 传输**进行中**磁盘上就必须有字节；这是 v1.1.0–v1.1.9 "0 KB/s" 的根因 | httpx 必须用 `client.stream()` + `aiter_bytes()`，**不能**用 `await client.get()` 读全量 body |
| F013 | 空闲超时看门狗 | 每个分片 | `idleTimeout = 60 s`：**没有字节到达**超过 60 s 判定连接死亡，抛 `URLError(.timedOut)`。**不是**总时长上限 | `Engine/HTTPDownloader.swift:71`, `Engine/HTTPDownloader.swift:453-474` | 慢但持续出字节的链路**绝不能**被杀；判定条件是「距上一次收到字节的间隔」，不是「传输总时长」 | httpx 用 `read` timeout 或自建 watchdog 任务；**不要**设 wall-clock 上限 |
| F014 | 分片重试 + 指数退避 | 分片失败后 | 最多 `maxRetries = 3` 次重试（共 4 次尝试），退避 `retryBackoff * 2^attempt` = 1/2/4/8 s | `Engine/HTTPDownloader.swift:76-80, 228-259` | 退避基数 1 s，指数翻倍；重试**前**先算已落盘字节 | 重试策略参数化；退避序列 1/2/4/8 |
| F015 | 重试从已落盘字节续传 | 重试时 | 每次尝试前读 `assembler.bytesWritten(chunk.id)`，`start = chunk.start + alreadyOnDisk` | `Engine/HTTPDownloader.swift:230-233` | **重连后 Range 头必须从已落盘字节数继续，而不是从 chunk 起点重来**（G-9）；重试不得重复写已落盘字节 | 硬性要求；写单测：断开后重连，断言第二次 Range 的起始偏移 == 首次已写字节数 |
| F016 | HTTP 状态分类 | 分片响应 | 200/206 接受；403/410/451 → `Failure.forbidden`；429/5xx → `Failure.status(code)`；其他 → `Failure.unknown` | `Engine/HTTPDownloader.swift:320-330, 356-368` | 分类结果直接决定「是否重试」：`forbidden` / `notPartialContent` / `decoding` **不重试**（链接本身的问题，重试只是烧时间） | forbidden 家族不重试；429/5xx 要重试 |
| F017 | 过期链接识别（403/410/451） | 分片响应 | 三种状态码归为 `Failure.forbidden`，语义上标记「链接已过期/无权限」 | `Engine/HTTPDownloader.swift:324-325`, `Engine/ExpiredLinkHandler.swift:1-31` | 这三个码是「过期链接」的唯一判定来源 | 同上 |
| F018 | 响应前置校验（拒绝前不写盘） | 分片响应到达时 | `didReceive response:` 里跑 validator，被拒绝则 `completionHandler(.cancel)`，**body 一个字节都不落盘**；且把**拒绝原因**（不是后续的 cancel 错误）交给调用方 | `Engine/ChunkStreamDelegate.swift:129-152, 165-184` | 403 场景下 partial 文件大小必须 **== 0**（已有单测 `testForbiddenResponseWritesNoBytes`） | 硬性要求；httpx 需在 `aiter_bytes` 前检查 `response.status_code`，非 200/206 直接抛错 |
| F019 | per-chunk partial 文件 | 每个分片 | 路径 `<dest>.sdm.partial-<chunkID>`；用 `FileHandle(forUpdating:)` 打开（**追加**语义，不截断） | `Engine/FileAssembler.swift:218-258`, `Engine/DownloadFlow.swift:120-121` | 文件名规则 `<最终文件名>.sdm.partial-<分片id>`；用 append 模式打开，**不能**用 `forWritingTo`（那会截断已下载字节，v1.0.x 的 bug） | 文件名规则与追加语义都是硬性契约；Python 用 `open(..., "ab")` |
| F020 | 分片合并（finalize） | 全部完成后 | 关闭全部句柄 → 删除同名目标 → 创建目标 → 按 `startOffset` 升序 seek+拷贝 256 KB 块 → 拷完即删该 partial | `Engine/FileAssembler.swift:111-182` | 合并**按 startOffset 升序**；先 `ftruncate` 到 `totalSize`（seek 到 `totalSize-1` 写 1 字节）再拷贝；partial 拷完立即删除；合并失败时目标文件不创建 | 合并顺序、预分配、partial 清理都要复刻；合并后目标文件大小 == totalBytes |
| F021 | 文件预分配 | 合并时 | seek 到 `totalSize-1` 写 1 字节实现 `ftruncate` 效果，避免稀疏文件 | `Engine/FileAssembler.swift:158-167` | 预分配后再拷贝；写入必须落到正确偏移 | Python 用 `f.truncate(total_size)` 即可 |
| F022 | 断点续传 sidecar（`.sdm.json`） | 下载成功后 | 写 `<stem>.sdm.json`，内容含 url / content_length / etag / last_modified / accepts_ranges / filename / chunks[] / saved_at（ISO8601，pretty + sortedKeys，原子写） | `Engine/ResumeState.swift:29-80` | sidecar 路径用 `deletingPathExtension`（`x.dmg` → `x.sdm.json`）；**但同文件的 `URL.sdmSidecarPath` 扩展属性给出的是 `x.dmg.sdm.json`** —— 两处不一致；`ResumeState.read()` **无调用方** | Python 版应统一为一种路径并真正实现 read（用于跨进程续传） |
| F023 | 单流 Range 续传（ResumeFlow） | `resume(id:)` 且存在 `.part` | 发 `Range: bytes=<已下载>-<total-1>` 单请求，要求 206；200 → `rangeRejected` → 引擎回退「从头重下」 | `Engine/ResumeFlow.swift:45-79` | **⚠️** 该路径用 `URLSession.shared.data(for:)`（**整块缓冲到内存**）且用 `forWritingTo`（**截断**已下载字节）——见第 5 章 R-08/R-09；且 `DownloadFlow` 根本不写 `.part`，所以该路径在真实流程中**几乎不可达** | Python 版续传必须用流式追加写，且 Range 起点 == 已落盘字节 |
| F024 | 全局限速（令牌桶） | Settings → Bandwidth | `SpeedLimiter` 令牌桶，粒度 100 ms，容量 = `rate * granularity`；`rate <= 0` 表示不限速 | `Engine/SpeedLimiter.swift:14-77` | **⚠️** `consume()` 在下载路径中**从未被调用**（只被单测调用）——限速当前**不生效**，见第 5 章 R-04 | Python 版必须真正把令牌桶挂到每个分片的字节流上 |
| F025 | 单任务限速 | 行右键 →「限速…」 | 独立 `SpeedLimiter`，与全局桶**互不共享 token**；0 = 清除回落到全局 | `Engine/DownloadEngine.swift:133-147, 244` | 每任务限速是**独占**桶，不与全局桶分摊；清除后回落到全局限速 | 同上 |
| F026 | 动态分裂 / 合并策略 | 🚧 无调用方 | `RangePlanner.rearrange(samples:now:)` 计算 split/merge delta：`firstRearrangeAfter=10 s` 后才生效、两次重排间隔 ≥1 s、slow chunk 比 < 1/1.5 且 ≥ 4 MB 才 split、fast 邻居比 > 0.3 且合并后 ≤ 16 MB 才 merge、chunk 总数 < 128 | `Engine/RangeManager.swift:43-56, 139-193, 196-223` | 策略常量：split 阈值 1.5、merge 阈值 0.3、采样窗 5 s、持续 5 s 判定、最小分片 1 MB、最大 16 MB | 🚧 Swift 版**从未调用** `rearrange()`。Python 版可选择实现；一旦实现必须保证「split 后两半的 bytesDone 不重复计算」 |
| F027 | 站点规则注入（UA/Referer/Cookie/BasicAuth） | 下载请求时自动 | 按 host 后缀匹配规则后注入请求头；默认 UA = `SDM/1.0 (+https://github.com/Mofan2020/SuperDownloadManager)` | `Engine/HTTPDownloader.swift:172-200` | **每个请求（含 probe）都要带 UA**；站点规则的 UA **覆盖**默认 UA；Referer/Cookie/BasicAuth 仅在规则命中时加；BasicAuth 走 Keychain 取密码 | httpx 的 `headers` + `auth=` 即可；UA 默认串保持一致（可作为 UA 指纹的兼容点） |
| F028 | 忽略 SSL 证书错误开关 | Settings → Network | `IgnoreSSLAuthChallengeDelegate` 在 `serverTrust` 挑战时直接 `.useCredential` 接受；开关**运行时可切换**（原子标志） | `Engine/IgnoreSSLAuthChallengeDelegate.swift:36-108` | 默认**关闭**；只对 `NSURLAuthenticationMethodServerTrust` 生效，其它认证方式走默认处理；绕过时会写一条 warning 日志（含 host/port） | httpx 用自定义 `SSLContext`（`verify=False`）；开关必须在运行中可切换且影响新连接 |
| F029 | 共享 URLSession 与每主机连接上限 | 引擎初始化 | 全引擎**共用一个** `URLSession`，`httpMaximumConnectionsPerHost = 32`；`timeoutIntervalForRequest=60`、`timeoutIntervalForResource=3600`、`waitsForConnectivity=false`、缓存策略 `reloadIgnoringLocalCacheData` | `Engine/HTTPDownloader.swift:89-142` | 必须是**一个** session；每主机连接数 ≥ 分片数（否则分片串行）；`waitsForConnectivity=false` | httpx 的 `Limits(max_connections=...)` 必须 ≥ 分片上限，否则退化成串行 |
| F030 | 任务队列与并发预算 | 🚧 部分实现 | `TaskQueue` 是 actor，存 `TaskSummary` 字典；`availableSlots(for:)` 设计为「独占带宽」策略（第二个任务分到 `max(4, cap/(n+1))`） | `Engine/TaskQueue.swift:16-70` | **⚠️** `availableSlots()` **无调用方**，并发预算未生效；实际并发由 `parallelismCap` + `defaultChunks` 决定 | Python 版若要实现多任务公平调度，用这个语义 |
| F031 | 任务文件名推导 | 引擎 start | `url.lastPathComponent` → `host` → `download.bin` | `Engine/DownloadEngine.swift:499-504` | 三级兜底顺序固定 | 同上 |
| F032 | 默认下载目录 | 引擎初始化 | `~/Downloads`（`FileManager.downloadsDirectory`，取不到则 `~/Downloads`） | `Engine/DownloadEngine.swift:93-99` | `setDefaultDownloadDir()` **无调用方**（GUI 侧用 `downloadDirectory(for:)` 另算） | 默认目录规则一致即可 |
| F033 | 进度通道（AsyncStream + 50 ms 合并） | 引擎内部 | `ProgressChannel` 是 `AsyncStream<Int64>`，`.bufferingNewest(16)`；50 ms 窗口内只保留**最新**值 | `Engine/DownloadFlow.swift:245-307` | 合并窗口 50 ms；窗口内**保留最后一个值**（v1.0.5/0.6 曾保留第一个值，导致进度乱窜——第 5 章 R-03）；`flush()` 在分片完成时强制把 pending 发出 | Python 用 `asyncio.Queue(maxsize=16)` + 定时 flush；语义必须是「保留最新」 |
| F034 | ChunkProgress 聚合器 | 每个分片每次落盘 | 各分片上报**自己这次落盘的字节增量**，聚合器在锁内累加后交给通道 | `Engine/DownloadFlow.swift:314-333` | 上报值必须是**增量**（不是分片累计偏移），累加后才是文件级总进度 | 同上；**注意** Swift 版有个 bug（R-07），Python 版要修 |
| F035 | 过期链接重链评估 | 🚧 无调用方 | `evaluateRelink(candidate:)` → 探针候选 URL，比对 contentLength（不同 → reject）、ETag（不同 → reject）、`Accept-Ranges`（无 → 但仍继续） | `Engine/ExpiredLinkHandler.swift:27-67` | 判定顺序：**长度 → ETag → Accept-Ranges**；长度不一致直接 reject，ETag 不一致直接 reject，缺 Accept-Ranges 只警告不阻止 | Python 版若实现 relink，必须保留这个判定顺序与三种结论 `safeToResume` / `unsafeButProceed` / `unsafeReject` |
| F036 | 进度溢出钳制 | 进度计算 | `TaskSummary.fractionDone` = `min(1.0, max(0.0, bytesDone/total))` | `App/MainWindow/TaskSummary.swift:31-34` | 百分比**永远**在 [0,1]；`totalBytes` 为 nil 时返回 0 | 同上；v1.1.6 修过一次进度溢出（第 5 章 R-05） |

### B. 任务控制 / 状态机

| 编号 | 功能名 | 触发路径 | 现有行为描述 | Swift 实现位置 | 关键行为约定 | Python 重构验收要点 |
|---|---|---|---|---|---|---|
| F037 | 添加下载（入队） | 引擎 API / GUI / CLI / 扩展 | 为每个 spec 生成 UUID，入队为 `queued`，立刻起 `Task` 跑 `runFlow`，状态转 `downloading` | `Engine/DownloadEngine.swift:157-181, 186-188` | 返回新分配的 task id 数组；入队后**立即**开始下载（无排队等待） | 入队即开跑；id 由服务端生成 |
| F038 | 暂停 | 行内 ⏸ / 右键 / 工具栏「全部暂停」 | 置 `isPaused`，`CancelGuard.mark(id)`，取消 live Task，状态置 `paused`，速度置 0。**分片字节保留在磁盘** | `Engine/DownloadEngine.swift:353-370` | 暂停后 partial 文件**必须保留**；状态是 `paused` 不是 `failed`；速度立刻为 0 | 同上；协作取消不能误报 failed |
| F039 | 恢复 | 行内 ▶ / 右键 / 工具栏「全部继续」 | 检查 `<dest>.part` 大小：为 0 则等价「取消 + 重新 start」；否则跑 `ResumeFlow` 续传 | `Engine/DownloadEngine.swift:377-404, 406-479` | **⚠️** 真实分片写的是 `.sdm.partial-N` 不是 `.part`，所以 `bytesOnDisk` 恒为 0 → 恢复实际走「重新 start」路径（第 5 章 R-08） | Python 版：恢复必须真的从已落盘字节继续，而不是重下 |
| F040 | 取消 | 行内 ✕ / 右键 | 先把状态置 `cancelled`（除非已完成），再 `CancelGuard.mark` + `live.task.cancel()`；GUI 侧同时删 `<dest>.part` | `Engine/DownloadEngine.swift:313-334`, `App/MainWindow/DownloadListStore.swift:219-233` | 状态**先置后取消**（否则 catch 会覆盖成 failed）；**⚠️** GUI 删除的是 `.part`，真正的 `.sdm.partial-N` **不会被删**（R-10） | Python 版取消时必须清理全部 partial 文件 |
| F041 | 从列表移除 | 右键 →「从列表移除」 | 先 `cancel` 再从 queue 移除；**磁盘文件保留** | `Engine/DownloadEngine.swift:483-487` | 移除 ≠ 删除文件；文件留在原地 | 语义区分「移除」与「删除文件」 |
| F042 | 清除已完成 | 工具栏 🗑 | 遍历快照，移除所有 `completed` 的行（连带 cancel） | `Engine/DownloadEngine.swift:489-494` | 只清 `completed`；`failed`/`cancelled` 不清 | 同上 |
| F043 | 状态机与状态集合 | 全局 | 7 个状态：`queued / downloading / paused / completed / failed / cancelled`（+ 未使用的 `awaiting_new_link`） | `Engine/TaskQueue.swift:20`, `App/MainWindow/DownloadListView.swift:425-460` | 每个状态有独立的图标 / 文案 / 颜色；状态字符串是跨层契约 | 状态字符串原样保留（CLI/JSON 里出现） |
| F044 | CancelGuard 协作取消标记 | 引擎内部 | 进程级 `Set<UUID>`，`cancel`/`pause` 写入，catch 读取后清除 | `Engine/DownloadEngine.swift:343-348` | 判定条件是 `CancelGuard.wasCancelled(id) \|\| Task.isCancelled`；命中则**不**置 failed | Python 版用 `asyncio.CancelledError` + 显式状态机，不要吞异常 |
| F045 | 单任务线程数覆盖 | 剪贴板确认弹窗 | `StartSpec.maxThreadsOverride`，该任务的 `parallelismCap` 用覆盖值而非引擎全局值 | `Engine/DownloadEngine.swift:24-38, 245` | 覆盖值优先级高于全局；`nil` = 用全局；范围 1–128 | 同上 |
| F046 | 设置全局线程数 | 引擎 API | `setMaxThreads(n)` 同时改 `strategy.defaultChunks` 与 `parallelismCap`，钳制到 [1,128] | `Engine/DownloadEngine.swift:105-109` | **⚠️** 该方法**无调用方**——Settings 的「默认线程数」滑块**没有接到引擎**（R-11） | Python 版必须让该设置真正生效 |
| F047 | 设置全局限速 | 引擎 API | `setSpeedLimit(bps)` → `limiter.setRate()` 重置令牌桶 | `Engine/DownloadEngine.swift:120-127` | 改速率时桶重置为「按新速率满桶」，立即生效 | 同上 |
| F048 | 设置单任务限速 | 引擎 API | `setTaskSpeedLimit(id:bps)`：`bps<=0` 清除覆盖，`bps>0` 建/更新专用 limiter | `Engine/DownloadEngine.swift:133-147` | 同上 | 同上 |
| F049 | 引擎快照 | GUI 轮询 / CLI | `snapshot()` 返回 `[TaskQueue.TaskSummary]` | `Engine/TaskQueue.swift:54` | 快照是**只读值拷贝**；GUI 每 200 ms 轮询一次 | 提供同样的轮询快照接口 |

### C. 分类 / 命名 / 目录

| 编号 | 功能名 | 触发路径 | 现有行为描述 | Swift 实现位置 | 关键行为约定 | Python 重构验收要点 |
|---|---|---|---|---|---|---|
| F050 | 扩展名 → 分类表 | 添加下载时 | 7 类：`video / music / pictures / archives / documents / software / other` | `App/MainWindow/TaskSummary.swift:108-128` | video: mp4,mov,mkv,webm,avi,flv,m4v,wmv,ts；music: mp3,flac,wav,aac,ogg,m4a,opus；pictures: jpg,jpeg,png,gif,webp,heic,svg,bmp,tiff；archives: zip,7z,rar,tar,gz,bz2,xz,iso,tgz,tbz2,txz；documents: pdf,doc,docx,xls,xlsx,ppt,pptx,txt,md,epub,mobi,rtf,csv；software: exe,msi,deb,rpm,apk,app,dmg,pkg；其余 → other | 表内容必须**一字不差**；`Engine/FileURLHeuristic.swift:35-54` 有**重复的第二份表**，两边必须保持同步（源码注释明确要求） |
| F051 | 分类目录落盘 | 添加下载时 | `~/Downloads/<Category>/`，目录自动创建 | `App/MainWindow/DownloadListStore.swift:362-373` | 目录名 `Video/Music/Pictures/Archives/Documents/Software/Other`（首字母大写） | 目录名与分类枚举的映射保持一致 |
| F052 | 关闭分类开关 | Settings → 通用 | `sdm.classify.enabled = false` 时直接落 `~/Downloads/` | `App/MainWindow/DownloadListStore.swift:367` | 开关只影响**新任务**的目录选择 | 同上 |
| F053 | 服务器文件名实时重锚 | 任务启动后（探针成功后） | 若探针拿到 `suggestedFilename` 且用户没指定文件名，则把目标路径改写为同目录下的服务器文件名 | `Engine/DownloadEngine.swift:209-220` | 用户显式指定了文件名时**不覆盖**；重锚**保持同一目录**（只换 basename） | 同上；这是 GitHub 下载文件名正确的关键路径 |
| F054 | 分类图标与色板 | 列表行 / 侧边栏 | 每类一个 SF Symbol + 一个 tint 颜色 | `App/MainWindow/TaskSummary.swift:68-91` | 图标名与颜色是 UI 契约，Python 版换图标集时需保留语义 | 语义映射保持 |

### D. 采集入口

| 编号 | 功能名 | 触发路径 | 现有行为描述 | Swift 实现位置 | 关键行为约定 | Python 重构验收要点 |
|---|---|---|---|---|---|---|
| F055 | Add URL 弹窗 | 工具栏 ＋ / ⌘N | 输入 URL + 可选文件名；校验必须是 http/https；URL 留空时禁用「添加」按钮 | `App/MainWindow/DownloadListView.swift:336-394` | **只接受 http/https**；其他 scheme 报「请输入有效的 http(s) URL。」；文件名留空 = 用服务器文件名 | 同上 |
| F056 | 拖拽 URL 入队 | 把 URL/文本拖进主窗口 | `.onDrop(of: [.url, .text, .fileURL])`；URL 对象与字符串两条路径都试；只收 http/https | `App/MainWindow/DownloadListView.swift:107-110, 398-421` | 拖字符串必须能解析成 URL 才入队；非 http/https 静默忽略 | 同上 |
| F057 | 剪贴板监听 | 后台每 500 ms 轮询 `NSPasteboard.changeCount` | 只在 changeCount 变化时处理；要求 http(s) + `isLikelyFileURL`（扩展名必须命中分类表）；同 URL 5 分钟内去重；已在队列中的同 URL 不再提示 | `App/MainWindow/DownloadListStore.swift:377-431` | 轮询间隔 500 ms；去重窗口 300 s；**裸域名 / HTML 页面不提示**（v1.0.6 的「见到任何链接就下载」太烦，v1.0.8 改掉） | 必须保留「只提示像文件的 URL」这一收敛条件 |
| F058 | 剪贴板候选确认弹窗 | 剪贴板命中后 | 弹窗让用户选分类、改保存目录（可选 NSOpenPanel）、调本任务线程数（1–128） | `App/MainWindow/ClipboardConfirmView.swift:45-212` | 「下载」= `addWithConfig(url:category:saveDirectory:maxThreads:)`；「跳过」= 仅关闭弹窗（**不是**永久禁用）；分类切换时自动同步默认目录（除非用户已手动选过） | 三个按钮的语义不能混淆 |
| F059 | 剪贴板 kill switch | 弹窗内复选框 / Settings | 关掉 `sdm.capture.clipboard` 即停止监听 | `App/MainWindow/ClipboardConfirmView.swift:136-141` | 开关立即生效（下一轮轮询就停） | 同上 |
| F060 | Firefox 扩展接管 | 浏览器里点链接 | MV3 扩展：`onDeterminingFilename` 先 `suggest()` 保留文件名，再异步把 URL 派给 SDM，然后 `downloads.cancel` + `downloads.erase` 掉浏览器自己的下载 | `Extensions/Firefox/background.js` | 派单 URL：`GET http://127.0.0.1:7955/add?url=&filename=&referer=`；超时 4 s（AbortController） | Python 版必须**先派单成功再取消浏览器下载**（顺序反了会丢下载） |
| F061 | Safari 扩展派单 | 网页悬停按钮 | 只做「用户点按钮 → 派单」，**没有**下载拦截 | `Extensions/Safari/background.js` | 同上 GET 协议；Safari 无 `downloads` API | 同上 |
| F062 | 悬停按钮（Firefox / Safari 共用） | 鼠标悬停在媒体/文件链接 | 注入紫色 `↓ SDM` 浮动按钮，点击后 `browser.runtime.sendMessage({type:"sniff", url, suggestedFilename, referer})`；成功显示「✓ Sent」 | `Extensions/Firefox/sniffer.js`, `Extensions/Safari/sniffer.js` | 可嗅探判定：A 标签 href 匹配 `\.(zip|dmg|pkg|iso|exe|pdf|mp4|mkv|mp3|flac|rar|7z|tar|gz)$`；或 `<VIDEO>`/`<AUDIO>` 元素 | 同上；`referer` 必须带 `location.href` |
| F063 | 扩展协议（manifest） | 安装扩展 | Firefox MV3，权限 `downloads / downloads.open / activeTab / storage / scripting / nativeMessaging`，`host_permissions: ["<all_urls>"]`；Safari MV3 + `service_worker` | `Extensions/Firefox/manifest.json`, `Extensions/Safari/manifest.json` | 两端 manifest 必须保持等价（Safari 无 `downloads` 权限） | 同上 |
| F064 | URL scheme 处理 | `superdownloadmanager://show` / `://add?url=` | `NSAppleEventManager` 注册 `kInternetEventClass`/`kAEGetURL`；`show`/`open` → 激活窗口；`add?url=` → 直接 `engine.start` | `App/SDMApp.swift:387-422` | **⚠️** `Info.plist` 里**没有** `CFBundleURLTypes` 注册，所以系统层面根本唤不起这个 scheme（历史上也没注册过）——见 R-13 | Python 版若要保留 URL scheme，必须在 `Info.plist` / pywebview 侧真正注册 |
| F065 | 菜单栏图标 | 菜单栏右侧 | `NSStatusItem` + `arrow.down.circle` 模板图 + tooltip | `App/SDMApp.swift:291-303` | 菜单栏模式是**主要交互入口**（SwiftUI `MenuBarExtra` 在 macOS 26 上不能可靠传 `openWindow`，所以自管） | Python 版 pywebview 需自建原生菜单栏入口或托盘 |
| F066 | 菜单栏菜单项 | 菜单栏图标点击 | 「Open Main Window」(⌘0) / 「Add URL…」(⌘N) / 分隔 / 「Show Downloads in Finder」 / 分隔 / 「About Super Download Manager」 / 「Settings…」(⌘,) / 分隔 / 「Quit」(⌘Q) | `App/SDMApp.swift:305-351` | 菜单项顺序与快捷键固定 | 同上 |
| F067 | 打开主窗口 | 菜单 / ⌘0 | `NSApp.activate` + `makeKeyAndOrderFront` | `App/SDMApp.swift:355-357, 424-435` | 菜单栏点「Open Main Window」必须真的把窗口带到前台 | 同上 |
| F068 | 菜单栏「Add URL…」 | 菜单 / ⌘N | 激活窗口 + 广播 `.sdmShowAddSheet` 通知 | `App/SDMApp.swift:359-362, 571-579` | **⚠️** `MainWindow` 收到该通知后只把侧边栏选中项重置为「全部下载」，**没有**把 `DownloadListView.showAddSheet` 置 true —— 菜单栏的「Add URL…」实际不弹窗（R-14） | Python 版必须让菜单的「Add URL…」真的弹窗 |
| F069 | 在访达中显示下载目录 | 菜单栏 | `NSWorkspace.activateFileViewerSelecting([~/Downloads])` | `App/SDMApp.swift:364-370` | 打开的是 Downloads 根目录 | 同上 |
| F070 | 关于面板 | 菜单栏 | `NSApp.orderFrontStandardAboutPanel` | `App/SDMApp.swift:372-374` | 系统标准面板 | 同上 |
| F071 | 打开设置 | 菜单栏 ⌘, | 激活窗口 + 广播 `.sdmShowSettings` | `App/SDMApp.swift:376-379` | **⚠️** `.sdmShowSettings` **没有任何订阅方** —— 菜单栏「Settings…」不打开设置窗口（R-15） | Python 版必须真的打开设置页 |

### E. 主窗口 / 列表 UI

| 编号 | 功能名 | 触发路径 | 现有行为描述 | Swift 实现位置 | 关键行为约定 | Python 重构验收要点 |
|---|---|---|---|---|---|---|
| F072 | 侧边栏结构 | 主窗口左侧 | 三个 section：**Active**（全部下载 / 下载中 / 等待中 / 已暂停 / 已完成 / 已失败，每项带计数）、**Categories**（7 类，每类带活跃计数 + tint）、**Other**（历史 / 计划） | `App/SDMApp.swift:647-763` | 计数只统计 `store.tasks`；分类计数按 `task.category` | 同上 |
| F073 | 按状态过滤 | 点侧边栏状态项 | `visibleTasks` 按 `state == st` 过滤 | `App/MainWindow/DownloadListView.swift:85-94` | 过滤发生在排序之前 | 同上 |
| F074 | 按分类过滤 | 点侧边栏分类项 | 按 `task.category == cat` 过滤 | 同上 | 同上 | 同上 |
| F075 | 列表 7 列 | 主窗口表格 | 状态图标 / 文件名+host / 大小 / 进度条+百分比 / 速度 / ETA / 状态胶囊(+分类图标, +限速 speedometer 图标) / 行内操作按钮 | `App/MainWindow/DownloadListView.swift:133-252` | 列顺序固定；`totalBytes` 缺失显示「—」；进度缺失显示「—」 | 表格列语义保持 |
| F076 | 列排序 | 点列头 | `KeyPathComparator` 排序，默认按 `state` 升序；`totalBytesOrZero` 让未知大小排到末尾 | `App/MainWindow/DownloadListView.swift:78-80, 254-256`, `App/MainWindow/TaskSummary.swift:29` | 排序键：`filename / totalBytesOrZero / speedBytesPerSec / state` | 同上 |
| F077 | 行内操作按钮 | 表格最后一列 | 下载中/等待 → ⏸ + ✕；已暂停 → ▶ + ✕；失败/已取消 → ↻（重试）；已完成 → 打开 + 在访达中显示 | `App/MainWindow/DownloadListView.swift:261-294` | 按状态互斥显示；重试复用 `resume(id:)` | 同上 |
| F078 | 右键上下文菜单 | 右键行 | 查看详情 / 打开 / 在访达中显示 / 暂停 / 取消 / 继续 / 复制 URL / 打开来源页 / 限速… / 计算校验和 / 从列表移除 / 删除文件 | `App/MainWindow/DownloadListView.swift:299-328` | 菜单项按状态条件出现；顺序固定 | 同上 |
| F079 | 打开任务详情 | 双击行 / 右键「查看详情」 | 弹出 `TaskDetailView` sheet | `App/MainWindow/DownloadListView.swift:240-251` | sheet 持有 **taskID** 而非快照 | 同上 |
| F080 | 工具栏 4 键 | 表格上方 | ＋添加 URL / 全部暂停 / 全部继续 / 清除已完成；后三者按状态计数禁用 | `App/MainWindow/DownloadListView.swift:484-544` | 「全部暂停」作用于 `downloading + queued`；「全部继续」作用于 `paused` | 同上 |
| F081 | 工具栏图标/文字切换 | Settings → 工具栏 | `sdm.toolbar.showLabels` 为 true 显示 `Label`，false 只显示 22×22 图标（附 `accessibilityLabel`） | `App/MainWindow/DownloadListView.swift:551-573` | 两种模式的按钮都要能点（v1.1.3 的液态玻璃把点击吞了，见 R-16） | pywebview 里避免 overlay 抢事件 |
| F082 | 空状态 | 列表无任务 | 大图标 + 应用名 + 「还没有下载」+ 提示（⌘N / 扩展 / `sdmdm add` / 拖拽）+ 语言提示 | `App/MainWindow/DownloadListView.swift:577-601` | 提示文案要提到 4 种添加入口 | 文案可沿用 |
| F083 | 任务详情元数据 | 详情 sheet | 目标路径 / 大小(已/总) / 速度 / ETA / 单任务限速 / 错误信息 | `App/MainWindow/TaskDetailView.swift:141-177` | 路径与错误信息**可选中复制** | 同上 |
| F084 | 任务详情分片列表 | 详情 sheet →「显示分段 (N)」 | **合成**的 N = `max(1, min(32, total/2MB+1))` 个分片，按 `bytesDone` 反推每个分片的完成度 | `App/MainWindow/TaskDetailView.swift:42-64, 181-223` | **⚠️** 这是**近似值**，不是引擎真实分片（R-17）；分片数上限 32；每次渲染重算 | Python 版可直接接引擎真实分片状态（更好），但 UI 语义保持 |
| F085 | 分片 elapsed 列 | 详情 sheet 分片行 | 取目录下**最新** `.sdm.partial-*` 的 mtime，算「距上次写入秒数」；> 15 s 变红 | `App/MainWindow/TaskDetailView.swift:273-321` | **⚠️** 所有分片行显示**同一个值**（最新 partial 的年龄），不是每分片各自的（R-18） | Python 版应改成每分片各自的真实 elapsed |
| F086 | Copy all as JSON | 详情 sheet | 复制含 id/filename/url/state/bytesDone/totalBytes/speedBytesPerSec/speedLimitBytesPerSec/destination/error 的 pretty+sorted JSON | `App/MainWindow/TaskDetailView.swift:239-258` | 字段名是**驼峰原样**（与 IPC 的 snake_case 不同） | 字段名保持 |
| F087 | 速度显示（EWMA） | 每 200 ms tick | `SpeedDisplay.compute`：终态/暂停 → 显示 0 且 EMA 清零；否则 EMA α=0.4；raw 优先取引擎值，否则 `deltaBytes/0.2` | `Engine/SpeedDisplay.swift:59-86`, `App/MainWindow/DownloadListStore.swift:108-163` | 终态**硬清零**，不走 EMA 衰减（R-02） | 严格复刻这条 |
| F088 | ETA 计算 | 列表 ETA 列 | `(total - done) / speed`，speed ≤ 0 或已满 → 显示「—」 | `App/MainWindow/TaskSummary.swift:40-46` | 条件：`totalBytes` 存在、`total > bytesDone`、`speed > 0` | 同上 |
| F089 | 格式化：字节 / 速率 / 时间 | 全局 | 字节用 `ByteCountFormatter(.file)`；速率 `%.2f MB/s` / `%.1f KB/s` / `%.0f B/s`；时长 `<60s` → `Ns`，`<60m` → `Nm Ss`，否则 `Nh Nm` | `App/MainWindow/TaskSummary.swift:144-161` | 三档速率格式与两档时长格式固定 | 同上 |
| F090 | 进度百分比 | 列表进度列 | `Int(fractionDone * 100)%`，钳制在 [0,100] | `App/MainWindow/DownloadListView.swift:164-178` | 钳制 | 同上 |
| F091 | 复制 URL | 右键 | 写 `NSPasteboard` | `App/MainWindow/DownloadListView.swift:313-316` | — | 同上 |
| F092 | 打开来源页 | 右键 | `https://<host>` 用系统浏览器打开 | `App/MainWindow/DownloadListView.swift:317-319` | 用 `https` 而非原 scheme | 同上 |
| F093 | 打开文件 | 行内 / 右键 | `NSWorkspace.open(destinationPath)`，文件不存在则无操作 | `App/MainWindow/DownloadListStore.swift:241-246` | 文件不存在不报错 | 同上 |
| F094 | 在访达中显示文件 | 行内 / 右键 | `activateFileViewerSelecting` | `App/MainWindow/DownloadListStore.swift:248-253` | — | 同上 |
| F095 | 计算 SHA-256 | 右键「计算校验和」 | 仅 `completed` 状态可用；后台线程读全文件算 SHA-256，弹窗显示并可复制 | `App/MainWindow/DownloadListStore.swift:332-355` | **⚠️** 全文件读进内存（大文件会炸，R-19） | Python 版必须流式分块哈希 |
| F096 | 删除文件 | 右键「删除文件」 | `FileManager.trashItem`（**移到废纸篓**，不是 rm），随后把行从列表移除 | `App/MainWindow/DownloadListStore.swift:295-301` | 移到废纸篓而非直接删（v1.0.6 计划里澄清过「不做 Move to Trash, 直接 rm」，但代码实现是 trashItem——见 R-20） | 明确选一种语义并写进文档 |
| F097 | 移除行 | 右键「从列表移除」 | 只移除行，文件保留 | `App/MainWindow/DownloadListStore.swift:234-236` | — | 同 F041 |
| F098 | 单任务限速对话框 | 右键「限速…」 | NSAlert + NSTextField，单位 KB/s，0 = 清除覆盖；显示时把 bytes/s 换算成 KB/s | `App/MainWindow/DownloadListStore.swift:305-329` | 输入单位是 **KB/s**，存进引擎是 **bytes/s**（×1024） | 单位换算一致 |
| F099 | 清除已完成 | 工具栏 🗑 | 同 F042 | `App/MainWindow/DownloadListStore.swift:237-239` | — | 同上 |

### F. 历史记录

| 编号 | 功能名 | 触发路径 | 现有行为描述 | Swift 实现位置 | 关键行为约定 | Python 重构验收要点 |
|---|---|---|---|---|---|---|
| F100 | 历史落库 | 任务进入终态时自动 | `MainWindow.onChange(of: store.tasks)` 检测 `isTerminal` 的行，写入 SQLite | `App/SDMApp.swift:610-626` | 每次 store 变化都会重写**所有**终态行（`INSERT OR REPLACE`，同 id 不重复）；`startedAt` 是伪造的 `now-60s` | 幂等写入；真实记录 startedAt |
| F101 | 历史列表 + 搜索 | 侧边栏「历史」 | Table 四列（文件+URL / 大小 / 状态 / 时间）+ 顶部搜索框（匹配文件名或 URL，大小写不敏感） | `App/MainWindow/HistoryView.swift:15-133` | 搜索是**子串匹配**；默认取 500 条 | 同上 |
| F102 | 历史重新下载 | 历史行 ↻ | `store.add(url:)` 用历史记录的 URL 重新入队 | `App/MainWindow/HistoryView.swift:85-89` | 用 URL 重新下载，**不**复用原路径 | 同上 |
| F103 | 历史打开文件 | 历史行 ⤴ | 文件存在时显示按钮，点击 `NSWorkspace.open` | `App/MainWindow/HistoryView.swift:90-96` | 文件不存在时**隐藏按钮**（不是禁用） | 同上 |
| F104 | 历史移除 | 历史行 🗑 | **空实现**——`HistoryViewModel.remove` 里只有注释 | `App/MainWindow/HistoryView.swift:165-171` | **⚠️** 点击无任何效果（R-21） | Python 版必须真正 DELETE |
| F105 | 30 天自动清理 | 每次 `open()` 和每次 `insert()` | `DELETE FROM history WHERE finished_at < now-30d` | `Engine/HistoryStore.swift:218-229` | 保留窗口 **30 天**；清理在插入后立即执行 | 同上 |
| F106 | 历史表结构 | 建表 | 见第 2 章「数据与状态契约」 | `Engine/HistoryStore.swift:106-129` | 两个索引：`finished_at DESC`、`url` | schema 保持 |

### G. 计划任务

| 编号 | 功能名 | 触发路径 | 现有行为描述 | Swift 实现位置 | 关键行为约定 | Python 重构验收要点 |
|---|---|---|---|---|---|---|
| F107 | 添加计划 | 侧边栏「计划」 | 输入 URL + DatePicker（日期+时分）+ 备注 | `App/MainWindow/ScheduleView.swift:19-92` | 校验 http/https；时间必须 ≥ 现在 | 同上 |
| F108 | 定时触发 | 视图出现时起 1 Hz 定时器 | 每秒扫一遍 `fireAt <= now` 的计划，调用 `store.add(url:)` 后从列表移除 | `App/MainWindow/ScheduleView.swift:94-109` | 扫描周期 1 s；到点即入队并从计划列表移除（**不重复触发**） | 同上 |
| F109 | 取消计划 | 计划行 ✕ | 从 `@State` 数组移除 | `App/MainWindow/ScheduleView.swift:65-69` | **⚠️** 计划只存在 `@State` 里，**不持久化**；切换视图/重启即丢（R-22）；且 `.onAppear` 起定时器但没有 `onDisappear` 取消 | Python 版必须持久化（至少存 UserDefaults/JSON） |

### H. 站点规则

| 编号 | 功能名 | 触发路径 | 现有行为描述 | Swift 实现位置 | 关键行为约定 | Python 重构验收要点 |
|---|---|---|---|---|---|---|
| F110 | 规则增删查 | Settings → 站点规则 | 添加规则（host 必填、小写化）+ 删除单条 + 全部清除；已有规则列表显示 host / Referer / UA / Cookie / Basic 用户名 | `App/MainWindow/SiteRulesView.swift:25-114` | host 转小写；空 host 报错「Hostname is required.」 | 同上 |
| F111 | 密码入钥匙串 | 添加规则时 | 密码**不进 JSON**，存 Keychain（service = `com.skyc8266.superdownloadmanager.siteRules`，account = `<user>@<host>`，`kSecAttrAccessibleWhenUnlocked`） | `Engine/SiteRule.swift:48-69, 87-114` | UserDefaults 里的 JSON **必须剥离** `basicAuthPass`；「全部清除」要连 Keychain 一起清 | Python 版用 `keyring` 或 macOS Keychain 等价方案 |
| F112 | 主机后缀匹配 | 每个请求 | `host == rule.host \|\| host.hasSuffix(".\(rule.host)")` | `Engine/SiteRule.swift:79-83` | 后缀匹配（`cdn.example.com` 命中 `example.com`）；多条命中取**第一条** | 同上 |
| F113 | 请求头注入 | 每个请求 | Referer / User-Agent / Cookie / `Authorization: Basic <base64(user:pass)>` | `Engine/HTTPDownloader.swift:172-191` | 站点规则的 UA **覆盖**默认 UA；Basic Auth 用 Base64 | 同上 |

### I. 日志系统

| 编号 | 功能名 | 触发路径 | 现有行为描述 | Swift 实现位置 | 关键行为约定 | Python 重构验收要点 |
|---|---|---|---|---|---|---|
| F114 | 五级日志 | 全局 | `debug(0) / info(1) / notice(2) / warning(3) / error(4)`；低于最小级别直接丢弃 | `Engine/SDMLogger.swift:40-88, 263-294` | 映射到 `OSLogType`：debug→.debug，info→.info，notice→.default，warning→.error，error→.fault；短标签 `DBG/INF/NTC/WRN/ERR` | 级别语义与标签保持 |
| F115 | 文件落盘 | 每次 emit | 每条同时写 OSLog（总是）和磁盘（`persistenceEnabled` 时）；磁盘写走**单条串行队列** | `Engine/SDMLogger.swift:263-288, 330-354` | **总是**写 OSLog；文件可关；写入必须串行（否则行会互相穿插） | Python 用 `logging.handlers` + 单线程 handler |
| F116 | 日志轮转 | 文件超限 | `sdm.log` → `sdm-<yyyy-MM-dd>.log`；同日二次轮转追加序号 `sdm-<date>2.log` | `Engine/SDMLogger.swift:360-393` | 轮转阈值 = `maxBytesPerFile`；轮转名带日期；同日不覆盖 | 同上（`logging.handlers.RotatingFileHandler` 近似） |
| F117 | 行数上限 | 清理时 | 清理后行数 > `maxLines` 时重写文件，只保留**最后** N 行 | `Engine/SDMLogger.swift:459-473` | 保留尾部（最新），丢头部 | 同上 |
| F118 | 保留天数清理 | 清理时 | 删除 mtime 早于 `now - retentionDays` 的 `sdm-*.log` 归档 | `Engine/SDMLogger.swift:446-457` | 只清归档（`sdm-` 前缀 + `.log` 后缀），**不清**当前 `sdm.log` | 同上 |
| F119 | 每日定时清理 | 应用启动 | `Timer.scheduledTimer(24h)`；设置变更时也立刻跑一次 | `Engine/SDMLogger.swift:226-229, 237-248` | 每天一次 + 设置变更时一次 | 同上 |
| F120 | 日志预览 sheet | Settings → 日志 →「预览日志…」 | 760×540 弹窗，显示时间戳/级别/分类/消息，可过滤 | `App/MainWindow/SettingsView.swift:421-524` | 加载走 `SDMLogger.readAll()`（异步，不卡主线程）；**倒序**渲染（最新在上） | 同上 |
| F121 | 预览过滤 | 预览 sheet 搜索框 | 对 `message` 和 `category` 做大小写不敏感子串匹配 | `App/MainWindow/SettingsView.swift:428-434` | 无正则 | 同上 |
| F122 | 复制日志 | 预览 sheet「复制」 | 复制过滤后的全部行，格式 `<ts> [LVL] [cat] msg` | `App/MainWindow/SettingsView.swift:487-493` | 复制格式与磁盘格式一致 | 同上 |
| F123 | 在 Console.app 中打开 | Settings → 日志 | `x-man-page://console.app/subsystem/com.skyc8266.superdownloadmanager` | `App/MainWindow/SettingsView.swift:374-384` | 子系统 ID 固定 `com.skyc8266.superdownloadmanager` | Python 版用 `logging` + `os_log` 或给出等价指引 |
| F124 | 在访达中显示日志 | Settings → 日志 | `activateFileViewerSelecting([SDMLogger.fileURL])` | `App/MainWindow/SettingsView.swift:385-387` | 路径 `~/Library/Logs/SuperDownloadManager/sdm.log` | 同上 |
| F125 | 引擎日志代理 | 全局 | `engineLog` / `downloadLog` 两个代理把旧 `os.Logger` 调用形状转成 `SDMLogger` 调用 | `Engine/EngineLog.swift:18-46` | 旧调用点不用改；`log(level:_:)` 按 `rawValue` 映射，未来的 OSLogType 落到 `.default` | Python 版可统一走 `logging` |

### J. 通知

| 编号 | 功能名 | 触发路径 | 现有行为描述 | Swift 实现位置 | 关键行为约定 | Python 重构验收要点 |
|---|---|---|---|---|---|---|
| F126 | 下载完成通知 | 任务转 `completed` | `UNMutableNotificationContent`，标题「下载完成 / Download complete」，副标题=文件名，正文=总大小格式化，带默认提示音，identifier = `complete-<uuid>` | `App/MainWindow/DownloadListStore.swift:463-480` | **每个任务只通知一次**（`notifiedCompletionIDs` 去重） | 同上 |
| F127 | 下载失败通知 | 任务转 `failed` | 标题「下载失败 / Download failed」，正文 = 错误信息或「未知错误」，identifier = `failure-<uuid>` | `App/MainWindow/DownloadListStore.swift:482-494` | 每个任务只通知一次 | 同上 |
| F128 | 通知权限申请 | 应用启动 | `requestAuthorization(options: [.alert, .sound, .badge])` | `App/SDMApp.swift:260-270`, `App/MainWindow/DownloadListStore.swift:453-461` | **在启动时就申请**（v1.0.7 只在窗口 `onAppear` 申请，容易错过） | 同上 |
| F129 | 前台也弹横幅 | 前台时 | `willPresent` 返回 `[.banner, .sound]` | `App/SDMApp.swift:174-185` | **前台也必须弹**（v1.0.7 曾静默吞掉） | 同上 |
| F130 | 点通知回到主窗口 | 点击横幅 | `didReceive` → `AppDelegate.bringMainWindowForward()` | `App/SDMApp.swift:187-201, 275-281` | 点击后窗口必须前置 | 同上 |

### K. 设置页（7 个 section）

| 编号 | 功能名 | 触发路径 | 现有行为描述 | Swift 实现位置 | 关键行为约定 | Python 重构验收要点 |
|---|---|---|---|---|---|---|
| F131 | 设置窗口结构 | Settings 菜单 / ⌘, | `NavigationSplitView`，左栏 7 项：通用 / 默认 / 分类 / 站点规则 / 启动 / 日志 / 关于；窗口 720×480 | `App/MainWindow/SettingsView.swift:62-131` | 左栏导航（不是 Form 里的灰色 NavigationLink） | 同上 |
| F132 | 语言选择 | Settings → 通用 | 三选一：跟随系统 / English / 简体中文；**立即生效**（每次 body 重算读 `@AppStorage`） | `App/MainWindow/SettingsView.swift:139-152` | 切换**不需要重启**（v1.0.4 的 Localizable.strings 方案需要重启） | 同上 |
| F133 | 自动分类开关 | Settings → 通用 | `sdm.classify.enabled`，默认 true | `App/MainWindow/SettingsView.swift:154` | 见 F052 | 同上 |
| F134 | 剪贴板捕获开关 | Settings → 通用 | `sdm.capture.clipboard`，默认 true | `App/MainWindow/SettingsView.swift:155` | 见 F057/F059 | 同上 |
| F135 | 系统通知开关 | Settings → 通用 | `sdm.notify.onComplete`，默认 true；**同时控制完成与失败**两类通知 | `App/MainWindow/SettingsView.swift:156` | 一个开关管两类通知 | 同上 |
| F136 | 关闭窗口隐藏 Dock | Settings → 通用 | `sdm.hideDockWhenWindowClosed`，默认 false | `App/MainWindow/SettingsView.swift:165-167` | 见 F139 | 同上 |
| F137 | 工具栏显示文字 | Settings → 通用 | `sdm.toolbar.showLabels`，默认 true | `App/MainWindow/SettingsView.swift:175-177` | 见 F081 | 同上 |
| F138 | 忽略 SSL 证书错误 | Settings → 通用 | `sdm.network.ignoreSSL`，默认 false；切换时立刻 `engine.setIgnoreSSL()` | `App/MainWindow/SettingsView.swift:185-188` | 开关立即影响新连接；`onAppear` 时也会把持久化值推给引擎 | 同上 |
| F139 | 默认线程数 | Settings → 默认 | Slider 1–128 + 数字输入框；标签「默认线程数 / 每个下载的默认线程数」 | `App/MainWindow/SettingsView.swift:205-213, 529-557` | **⚠️** 只写 `sdm.threads.default`，**不推给引擎**（R-11）；实际只被剪贴板弹窗当默认值用 | 必须真正生效 |
| F140 | 带宽限制 | Settings → 默认 | 数字输入框，单位 KB/s，0 = 不限速 | `App/MainWindow/SettingsView.swift:216-224, 559-575` | 存的是 **KB/s**，`applyPersistedBandwidth` 时 ×1024 转 bytes/s；运行时通过 `UserDefaults.didChangeNotification` 实时推送 | 同上；注意单位 |
| F141 | 开机自启 | Settings → 启动 | `sdm.openAtLogin`；绑定 `LaunchAtLogin.setEnabled`（`SMAppService.mainApp`） | `App/MainWindow/SettingsView.swift:234-236, 259-267` | 开关**立即**注册/注销；应用启动时 `syncFromStorage()` 补齐（v1.0.4 只存 Bool 不注册，R-23） | 同上 |
| F142 | 更新通道 | Settings → 启动 | `sdm.updateChannel`：`stable` / `pre`（**🚧 无实现**，Sparkle 未接） | `App/MainWindow/SettingsView.swift:243-249` | 值被存下来但**没有任何消费者** | 可保留为占位，或接真实更新源 |
| F143 | 分类规则查看 | Settings → 分类 | 列出 7 类 + 扩展名清单（只读）；带自动分类开关 | `App/MainWindow/SettingsView.swift:581-629` | **只读**，不能编辑（v1.0.5+ 的编辑功能未实现） | 可只读保留 |
| F144 | 站点规则页 | Settings → 站点规则 | 见 F110-F113 | `App/MainWindow/SiteRulesView.swift` | — | — |
| F145 | 关于页 | Settings → 关于 | 应用图标（96×96）+ 名称 + 版本（读 `CFBundleShortVersionString`）+ MIT License + 版权 | `App/MainWindow/SettingsView.swift:269-286` | 版本号从 Info.plist 读 | 同上 |
| F146 | 日志设置项 | Settings → 日志 | 最低级别 / 保存到磁盘开关 / 保留天数 / 轮转 KB / 行数上限；改动即时 `SDMLogger.updateSettings` | `App/MainWindow/SettingsView.swift:292-400, 405-412` | 每次改动都重算完整 `SDMLogSettings` 快照（不是逐项改）；**⚠️** 应用启动时不从 UserDefaults 恢复（R-24） | 启动时必须从持久化恢复 |

### L. 窗口 / 系统集成

| 编号 | 功能名 | 触发路径 | 现有行为描述 | Swift 实现位置 | 关键行为约定 | Python 重构验收要点 |
|---|---|---|---|---|---|---|
| F147 | 关窗不退出 | 点窗口关闭按钮 | `applicationShouldTerminateAfterLastWindowClosed` 返回 `false` | `App/SDMApp.swift:283-289` | 关窗后应用继续在菜单栏活着 | 同上 |
| F148 | Dock 图标隐藏 / 恢复 | 关窗 / 开窗 | 关窗后（0.15 s 宽限）切 `NSApp.setActivationPolicy(.accessory)`；任一窗口变 key 立刻切回 `.regular` | `App/SDMApp.swift:444-541` | 切换有 0.15 s 宽限，避免 cmd-W 再点托盘时 Dock 闪烁；`willClose` 时**忽略** sheet / nonactivating panel | 同上 |
| F149 | 启动时同步开机自启 | 应用启动 | `LaunchAtLogin.syncFromStorage()` | `App/SDMApp.swift:239`, `App/LaunchAtLogin.swift:69-74` | 幂等 | 同上 |
| F150 | 窗口默认尺寸 / 最小尺寸 | 主窗口 | 默认 960×600，最小 720×480 | `App/SDMApp.swift:125-133` | — | 同上 |
| F151 | 应用菜单命令 | 主菜单 | `CommandGroup(replacing: .newItem)` 里放「Open Main Window」(⌘0) 与「Add URL…」(⌘N) | `App/SDMApp.swift:134-143` | — | 同上 |
| F152 | 更新检查 | 🚧 | `UpdateService.checkForUpdates()` 只打印「auto-update is not enabled in v1.0」 | `App/UpdateService.swift:20-30` | Sparkle 未接入（`project.yml` 里包被注释掉） | 可实现真实更新或明确不做 |

### M. 国际化 (L10n)

| 编号 | 功能名 | 触发路径 | 现有行为描述 | Swift 实现位置 | 关键行为约定 | Python 重构验收要点 |
|---|---|---|---|---|---|---|
| F153 | 双语字典 | 全局 | `Engine/L10n.swift` 内联字典，**208** 个 key，每个是 `(en, zh-Hans)` 二元组 | `Engine/L10n.swift:39-535` | 不用 `.strings` 文件（因为 `LocalizedStringKey` 不支持 printf 参数、且切语言要重启）；带参数的 key 用方法（`detailShowChunks(count:)`、`historyEntriesCount(_:)`、`listChunkPrefix(_:)`、`siteRules*Short(_:)`、`settingsDefaultThreadsValue(_:)`、`threadUnit(_:)`、`listSha256(_:)`） | 键名与文案保持；带参数的必须用方法而非 format 串 |
| F154 | 语言解析 | `LocaleTag.system` | 先查 `Bundle.main.preferredLocalizations`，回退 `Locale.preferredLanguages`；`zh*` → 中文，`en*` → 英文，其余 → 英文 | `Engine/L10n.swift:543-573` | **中文用户一律映射到简体**（含繁体/HK/TW） | 同上 |
| F155 | 环境注入 | SwiftUI | `StringsKey` EnvironmentKey；`Strings.current` 静态属性按 `sdm.locale` 解析 | `Engine/L10n.swift:577-599, 925-933` | 通知文案也走 `Strings.current`（跟随语言） | 同上 |
| F156 | 旧 .strings 资源 | 遗留 | `Engine/Resources/{en,zh-Hans}.lproj/Localizable.strings` 各 30 key，**代码里已无人使用** | `Engine/Resources/en.lproj/Localizable.strings` | 遗留物；`.strings` 里还有 `%lld` 占位符的旧 bug | Python 版可丢弃 |

### N. CLI（`sdmdm`）

| 编号 | 功能名 | 触发路径 | 现有行为描述 | Swift 实现位置 | 关键行为约定 | Python 重构验收要点 |
|---|---|---|---|---|---|---|
| F157 | `sdmdm add <url>` | 终端 | 可选 `--referer R` / `--filename F` / `--category C` / `--threads N`；未知 flag 报错 | `CLI/SDMDM.swift:107-133, 250-256` | 参数名 `referer`/`filename`/`category`/`threads` | 同上 |
| F158 | `sdmdm list` | 终端 | 无参数 | `CLI/SDMDM.swift:134-135, 257-258` | — | 同上 |
| F159 | `sdmdm pause <id>` / `resume <id>` / `cancel <id>` | 终端 | 各接一个 task id | `CLI/SDMDM.swift:136-144, 259-264` | — | 同上 |
| F160 | `sdmdm relink <id> <new_url>` | 终端 | 两个位置参数 | `CLI/SDMDM.swift:145-147, 265-267` | IPC 参数名是 `url`（不是 `new_url`） | 同上 |
| F161 | `sdmdm config speed <bytes_per_sec>` | 终端 | 整数 | `CLI/SDMDM.swift:148-160, 268-271` | **单位是 bytes/s**（与 Settings 的 KB/s 不同！） | 明确单位并写进 `--help` |
| F162 | `sdmdm config threads <count>` | 终端 | 整数 | 同上 | — | 同上 |
| F163 | `sdmdm ping` | 终端 | 无参数 | `CLI/SDMDM.swift:161-162, 276-277` | — | 同上 |
| F164 | CLI 输出约定 | 全部子命令 | 成功：把响应的 `result` 字段 pretty+sorted 打印到 **stdout**，exit 0；失败：`sdmdm: <msg>` 打印到 **stderr**，exit 1；解析失败：usage 打印到 stderr，exit **64** | `CLI/SDMDM.swift:67-85, 326-346` | 退出码 0/1/64 三档；结果**只打印 result**，不打印整个信封 | 同上；这是给 AI agent 用的接口，格式不能变 |
| F165 | CLI ↔ daemon 连接 | 全部子命令 | POSIX `AF_UNIX`/`SOCK_STREAM`；socket 路径默认 `~/Library/Application Support/SuperDownloadManager/sdmd.sock`，可用环境变量 `SDM_SOCKET` 覆盖；请求以 `\n` 结尾，响应读到 `\n` | `CLI/SDMDM.swift:210-245, 350-411` | **请求必须以换行结尾**；socket 不存在时返回「daemon not reachable at <path>; is sdmd running?」；响应上限 1 MB | 协议逐字兼容（见第 3 章） |

### O. Daemon IPC（`sdmd`）

| 编号 | 功能名 | 触发路径 | 现有行为描述 | Swift 实现位置 | 关键行为约定 | Python 重构验收要点 |
|---|---|---|---|---|---|---|
| F166 | 守护进程启动 | LaunchAgent / `SDM_DAEMON=1` | 环境变量 `SDM_DAEMON != "1"` 时打印提示并 exit 0；socket 路径取 `SDM_SOCKET` 或默认路径 | `Daemon/main.swift:17-26` | 必须靠 `SDM_DAEMON=1` 才进入守护模式 | 同上 |
| F167 | UDS server | 守护进程启动 | `AF_UNIX`/`SOCK_STREAM`，`listen(fd, 16)`；启动前删掉陈旧 socket 文件 | `Daemon/IPC/SocketServer.swift:31-73` | backlog 16；路径过长（> 104 字节）报错 | 同上 |
| F168 | JSONL 帧协议 | 每个请求 | 逐行读（`read(2)` 语义，读多少算多少），按 `\n` 切帧；每帧回一行 JSON + `\n` | `Daemon/IPC/SocketServer.swift:100-137` | **必须用「读多少算多少」的语义**——`FileHandle.read(upToCount:)` 会等满 count 造成握手死锁（v1.1.10 修的 bug） | Python 直接用 `sock.makefile('rwb')` 即可，天然正确 |
| F169 | 并发处理 | 每个连接 | 每个客户端一个 Task，独立处理 | `Daemon/IPC/SocketServer.swift:81-98` | 多客户端可并发 | 同上 |
| F170 | 请求信封 | — | `{"id": "...", "method": "...", "params": {...}?}` | `Engine/IPCProtocol.swift:31-45` | `params` 可省略；`id` 原样回显 | 同上 |
| F171 | 响应信封 | — | 成功 `{"id":"...","ok":true,"result":{...}}`；失败 `{"id":"...","ok":false,"error":"..."}` | `Engine/IPCProtocol.swift:47-59` | 失败时**没有** `result` 字段 | 同上 |
| F172 | 方法 `ping` | — | 返回 `{"pong": true}` | `Daemon/IPC/Dispatcher.swift:34-35` | — | 同上 |
| F173 | 方法 `add` | — | 必填 `url`；可选 `referer`/`filename`/`category`/`threads`。返回 `task_id / url / state / destination` | `Daemon/IPC/Dispatcher.swift:52-91` | **⚠️** `category` 被读取后丢弃（`_ = defaultCategory`）；`referer` **完全没读**；落盘目录固定 `~/Downloads/`（不分分类）；**不走 engine**，而是自己 new 一个 `DownloadFlow`（R-25） | Python 版必须把这些真正实现 |
| F174 | 方法 `list` | — | 映射 `engine.snapshot()` → `[{id,url,state,bytes_done,total}]` | `Daemon/IPC/Dispatcher.swift:93-107` | 字段名 snake_case；**⚠️** 因为 `add` 不进 engine 队列，`list` 永远返回空数组（R-25） | 同上 |
| F175 | 方法 `pause`/`resume`/`cancel` | — | 只校验 id 在 registry 里，然后回 `{"method": "...", "applied": true}` | `Daemon/IPC/Dispatcher.swift:109-118` | **⚠️** **完全不执行**任何控制动作（R-25） | 必须真正执行 |
| F176 | 方法 `config` | — | `key=speed` → 调 `engine.setSpeedLimit`；`key=threads` → **只回显不改**；未知 key 报错 | `Daemon/IPC/Dispatcher.swift:120-138` | **⚠️** `threads` 未生效 | 同上 |
| F177 | 方法 `relink` | — | 校验 id 与 url，返回 `{"decision": "safeToResume", "new_url": "..."}` | `Daemon/IPC/Dispatcher.swift:140-153` | **⚠️** decision 是**硬编码常量**，没跑 `ExpiredLinkHandler`（R-25） | 必须真正评估 |
| F178 | 未知方法 | — | 返回 `{"ok":false,"error":"unknown method '<m>'"}` | `Daemon/IPC/Dispatcher.swift:46-49` | 错误文案格式固定 | 同上 |
| F179 | 解码失败 | — | 返回 `{"id":"?","ok":false,"error":"decode: ..."}` | `Daemon/IPC/Dispatcher.swift:25-29` | id 用 `"?"` 占位 | 同上 |

### P. 扩展派单 HTTP 接口

| 编号 | 功能名 | 触发路径 | 现有行为描述 | Swift 实现位置 | 关键行为约定 | Python 重构验收要点 |
|---|---|---|---|---|---|---|
| F180 | 派单端口 | 应用启动 | `DispatchServer(port: 7955)` 在 **loopback** 上监听 | `Engine/DispatchServer.swift:20-36`, `App/MainWindow/DownloadListStore.swift:435-449` | 端口 **7955**；只监听回环；启动失败（端口占用）只 `NSLog` 警告，**不崩溃** | Python 版用 `aiohttp`/`asyncio` 绑 `127.0.0.1:7955` |
| F181 | `GET /add` | 扩展 | 查询参数 `url`（必填）、`filename`、`referer`（都可选，空串视为 nil）；成功返回 `{"ok":true,"result":{"accepted":true,"url":"..."}}` | `Engine/DispatchServer.swift:79-98` | 缺 `url` → 400 + `{"ok":false,"error":"missing url"}`；参数 percent-decode；**⚠️** 实际响应是 `accepted`，而 `docs/extension-install.md` 写的是 `taskId`（R-26） | 统一成一种并同步文档 |
| F182 | `GET /ping` | 健康检查 | 返回 `{"ok":true,"result":{"pong":true}}` | `Engine/DispatchServer.swift:75-78` | 200；`/ping/`（带斜杠）等价 | 同上 |
| F183 | 其它路径 | — | 404 + `{"ok":false,"error":"not found"}` | `Engine/DispatchServer.swift:99` | — | 同上 |
| F184 | HTTP 响应头 | — | `Content-Type: application/json`、`Content-Length`、`Connection: close` | `Engine/DispatchServer.swift:102-119` | 每次请求后关闭连接 | 同上 |

### Q. 安装 / 运维 / CI

| 编号 | 功能名 | 触发路径 | 现有行为描述 | Swift 实现位置 | 关键行为约定 | Python 重构验收要点 |
|---|---|---|---|---|---|---|
| F185 | 安装脚本 | `scripts/install.sh` | 幂等：把 `sdmdm` 软链到 `~/.local/bin/sdmdm`；生成 LaunchAgent plist（指向 `Contents/MacOS/SuperDownloadManager` + `SDM_DAEMON=1`）；`launchctl bootout` → `bootstrap` → `kickstart -k` | `scripts/install.sh` | 幂等；plist 路径 `~/Library/LaunchAgents/com.skyc8266.superdownloadmanager.daemon.plist` | 同上 |
| F186 | 卸载脚本 | `scripts/uninstall.sh` | `launchctl bootout` + 删 plist + 删 CLI 软链；**不删 app** | `scripts/uninstall.sh` | — | 同上 |
| F187 | LaunchAgent 模板 | 打包进 app | `RunAtLoad=true`、`KeepAlive=true`、`ProcessType=Background`、stdout/stderr 到 `/tmp/com.skyc8266.superdownloadmanager.daemon.{out,err}.log` | `Launcher/templates/com.skyc8266.superdownloadmanager.daemon.plist` | **⚠️** 模板指向 `Contents/MacOS/sdmd`，而 `install.sh` 生成的是指向 `Contents/MacOS/SuperDownloadManager` + `SDM_DAEMON=1` 的版本——两套并存且不一致（R-27） | 统一为一套 |
| F188 | DMG 打包 | `scripts/make-dmg.sh` | 优先 `create-dmg`，失败回退 `hdiutil`；输出 `~/Downloads/Archives/SuperDownloadManager-<ver>-<arch>.dmg` | `scripts/make-dmg.sh` | 输出路径固定 | 同上 |
| F189 | CI 门禁 | push / PR | Debug + Release 两套构建都必须 **0 warning 0 error**（过滤 `appintentsmetadataprocessor` SDK 噪音）；跑 `SDMSelfTest` scheme，必须出现 `Test Suite 'All tests' passed` | `.github/workflows/ci.yml` | 用 `SDMSelfTest` scheme 而非主 scheme（Xcode 27 + XcodeGen 2.46 下会撞 duplicate `.swiftmodule`） | 同等门禁 |
| F190 | 发布流程 | tag `v*.*.*` | Release 构建 → `SDMSelfTest` → `make-dmg.sh` → 打 zip → `gh release create --prerelease --generate-notes` → 输出 sha256 | `.github/workflows/release.yml` | **必须先发 Pre-release**，人工手测通过后才 `gh release edit --prerelease=false`（见 `docs/release.md`） | 同上 |
| F191 | 端到端测试门禁 | CI | `SDM_E2E=0` 可跳过；本地用 `SDM_E2E=1` 或 `-only-testing` 触发；需要一个支持 Range 的本地 Python 服务器 | `Tests/EngineTests/DownloadFlowEndToEndTests.swift:20-42` | 本地默认跳过；CI 默认跑 | 同等覆盖 |

---

## 2. 数据与状态契约

### 2.1 UserDefaults 键表

所有键都在 `com.skyc8266.superdownloadmanager.app` 的默认域（`UserDefaults.standard`）。

| Key | 类型 | 默认值 | 语义 | 写入方 | 消费方 | 备注 |
|---|---|---|---|---|---|---|
| `sdm.locale` | String | `"system"` | UI 语言（`system`/`en`/`zh-Hans`） | `SettingsView` | `SDMApp`, `Strings.current`, `SettingsView` | 切换立即生效 |
| `sdm.threads.default` | Int | `32` | 每个下载的默认线程数（1–128） | `SettingsView` | `DownloadListStore`（仅喂给剪贴板弹窗） | ⚠️ **没有推给引擎**（R-11） |
| `sdm.bandwidth.bytesPerSec` | Int | `0` | 全局带宽上限，**单位是 KB/s**（键名与单位不符） | `SettingsView` | `DownloadListStore.applyPersistedBandwidth`（×1024 → bytes/s） | ⚠️ 限速器未被字节流消费（R-04） |
| `sdm.openAtLogin` | Bool | `false` | 是否注册为登录项 | `SettingsView` | `LaunchAtLogin.syncFromStorage` | |
| `sdm.updateChannel` | String | `"stable"` | 更新通道（`stable`/`pre`） | `SettingsView` | **无** | 🚧 占位 |
| `sdm.classify.enabled` | Bool | `true` | 是否按分类落到子目录 | `SettingsView` / `CategoryRulesEditor` | `DownloadListStore.downloadDirectory` | |
| `sdm.capture.clipboard` | Bool | `true` | 剪贴板监听开关 | `SettingsView` / `ClipboardConfirmView` | `DownloadListStore.checkClipboard` | |
| `sdm.notify.onComplete` | Bool | `true` | 完成/失败通知开关 | `SettingsView` | `DownloadListStore.tick` | 一个开关管两类 |
| `sdm.hideDockWhenWindowClosed` | Bool | `false` | 关窗后隐藏 Dock 图标 | `SettingsView` | `AppDelegate.updateDockVisibilityForCurrentState` | 直接读 `UserDefaults.standard.bool` |
| `sdm.toolbar.showLabels` | Bool | `true` | 工具栏按钮显示文字标签 | `SettingsView` | `DownloadToolbar` | |
| `sdm.network.ignoreSSL` | Bool | `false` | 忽略 TLS 证书校验 | `SettingsView` | `SettingsView.onAppear` → `engine.setIgnoreSSL` | |
| `sdm.logs.level` | Int | `1` (info) | 最低日志级别（0..4 对应 debug..error） | `SettingsView` | `SettingsView.currentLogSettings` | ⚠️ 启动时不恢复（R-24） |
| `sdm.logs.persistence` | Bool | `true` | 是否落盘 | `SettingsView` | 同上 | 同上 |
| `sdm.logs.retentionDays` | Int | `14` | 轮转归档保留天数 | `SettingsView` | 同上 | 钳制 [1,365] |
| `sdm.logs.maxBytesKB` | Int | `4096` | 轮转阈值（KB） | `SettingsView` | 同上 | 钳制 [32KB, 256MB] |
| `sdm.logs.maxLines` | Int | `5000` | 清理后总行数上限 | `SettingsView` | 同上 | 钳制 [100, 1_000_000] |
| `sdm.siteRules` | Data (JSON) | 无 | 站点规则数组（**不含密码**） | `SiteRuleStore.save` | `SiteRuleStore.match` | 密码在 Keychain |
| `sdm.speed.rules` | — | — | 时间窗口限速规则 | **无写入方** | **无读取方** | 🚧 `SpeedPolicy` 注释里提到，代码没实现 |

### 2.2 Keychain

| 项 | 值 |
|---|---|
| Service | `com.skyc8266.superdownloadmanager.siteRules` |
| Class | `kSecClassGenericPassword` |
| Account | `<basicAuthUser>@<host>` |
| Accessible | `kSecAttrAccessibleWhenUnlocked` |
| 读 | `SiteRuleStore.getPassword(account:host:)` |
| 写 | `SiteRuleStore.setPassword(_:account:host:)`（先 `SecItemDelete` 再 `SecItemAdd`） |
| 删 | `SiteRuleStore.clearAll()` 逐条删 |

### 2.3 SQLite（下载历史）

路径：`~/Library/Application Support/SuperDownloadManager/history.sqlite`
（回退：`~/.sdm-history.sqlite`）

```sql
CREATE TABLE IF NOT EXISTS history (
  id              TEXT PRIMARY KEY,   -- UUID 字符串
  url             TEXT NOT NULL,
  filename        TEXT NOT NULL,
  destination_path TEXT NOT NULL,
  total_bytes     INTEGER,            -- 可空
  bytes_done      INTEGER,
  state           TEXT NOT NULL,      -- completed / failed / cancelled
  error_message   TEXT,               -- 可空
  started_at      REAL NOT NULL,      -- Unix 时间戳（秒，浮点）
  finished_at     REAL NOT NULL,
  sha256          TEXT                -- 可空
);
CREATE INDEX IF NOT EXISTS idx_history_finished_at ON history(finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_url ON history(url);
```

- 打开标志：`SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX`
- 写入语句：`INSERT OR REPLACE INTO history (...) VALUES (?,...)`
- 查询：`SELECT ... ORDER BY finished_at DESC LIMIT ?`，默认 limit **500**
- 清理：`DELETE FROM history WHERE finished_at < ?`（30 天前），在 `open()` 与每次 `insert()` 后各执行一次

### 2.4 落盘文件命名规则

| 模式 | 位置 | 产生者 | 生命周期 |
|---|---|---|---|
| `<最终文件名>` | `~/Downloads/<Category>/` | `FileAssembler.finalize` | 合并产物 |
| `<最终文件名>.sdm.partial-<chunkID>` | 同目录 | `FileAssembler._openChunk` | 下载中写、合并后删 |
| `<最终文件名>.part` | 同目录 | `ResumeFlow`（理论上） | 续传中间态；**实际流程几乎不产生**（R-08） |
| `<stem>.sdm.json` | 同目录 | `ResumeState.write` | 下载成功后写 sidecar；**无读取方** |
| `<最终文件名>.sdm.json` | 同目录 | `URL.sdmSidecarPath`（未使用） | 与上一条**路径规则冲突**（R-23b） |

> **命名细节**：`appendingPathExtension("sdm.partial-\(id)")` 会把 `x.bin` 变成 `x.bin.sdm.partial-0`；
> 该文件的 `pathExtension` 是 `.partial-0`，**不是** `.part` —— 这一点直接影响孤儿清理逻辑（R-06）。

### 2.5 日志文件

| 项 | 值 |
|---|---|
| 目录 | `~/Library/Logs/SuperDownloadManager/` |
| 当前文件 | `sdm.log` |
| 轮转归档 | `sdm-<yyyy-MM-dd>.log`；同日二次轮转 → `sdm-<yyyy-MM-dd>2.log`、`…3.log` |
| 行格式 | `yyyy-MM-dd HH:mm:ss.SSS [LVL] [category] message` |
| 时间戳格式 | `en_US_POSIX` + 本地时区（保证跨语言可解析） |
| 级别标签 | `DBG` / `INF` / `NTC` / `WRN` / `ERR` |
| 子系统 ID | `com.skyc8266.superdownloadmanager`（**不得更改**，否则 Console.app 过滤规则失效） |
| 序列化队列 | `com.skyc8266.superdownloadmanager.logger`（utility QoS，单线程） |

### 2.6 Socket / 端口

| 项 | 值 |
|---|---|
| Daemon UDS 路径 | `~/Library/Application Support/SuperDownloadManager/sdmd.sock` |
| UDS 覆盖 | 环境变量 `SDM_SOCKET` |
| Daemon 模式开关 | 环境变量 `SDM_DAEMON=1` |
| UDS backlog | 16 |
| 扩展派单 HTTP | `http://127.0.0.1:7955/add`，`/ping` |
| 派单端口覆盖 | **无**（硬编码 7955） |
| 扩展请求超时 | 4 s（`AbortController`） |

### 2.7 LaunchAgent plist 关键字段

| 字段 | 值 |
|---|---|
| `Label` | `com.skyc8266.superdownloadmanager.daemon` |
| `ProgramArguments` | `install.sh` 生成：`/Applications/SuperDownloadManager.app/Contents/MacOS/SuperDownloadManager`（带 `SDM_DAEMON=1`）；模板文件：`.../Contents/MacOS/sdmd`（**不一致**） |
| `RunAtLoad` | `true` |
| `KeepAlive` | `true` |
| `ProcessType` | `Background` |
| `EnvironmentVariables` | `SDM_DAEMON = 1`（仅 `install.sh` 生成的版本有） |
| `StandardOutPath` | `/tmp/com.skyc8266.superdownloadmanager.daemon.out.log` |
| `StandardErrorPath` | `/tmp/com.skyc8266.superdownloadmanager.daemon.err.log` |

### 2.8 内存态状态契约

| 结构 | 字段 |
|---|---|
| `TaskQueue.TaskSummary`（引擎） | `id: UUID`, `url: URL`, `state: String`, `bytesDone: Int64`, `totalBytes: Int64?`, `speedBytesPerSec: Double`, `errorMessage: String?`, `destination: URL`, `speedLimitBytesPerSec: Double` |
| `TaskSummary`（GUI） | `id`, `filename`, `urlString`, `urlHost: String?`, `state`, `bytesDone`, `totalBytes`, `speedBytesPerSec`, `destinationPath`, `errorMessage`, `category: DownloadCategory?`, `speedLimitBytesPerSec` |
| `ContentProbe` | `url`, `contentLength: Int64?`, `acceptsRanges: Bool`, `etag: String?`, `lastModified: String?`, `contentType: String?`, `suggestedFilename: String?`, `isFinal: Bool` |
| `Chunk` | `id: Int`, `start: Int64`, `end: Int64`（**开区间**）, `bytesDone: Int64` |
| `ResumeState` | `url`, `contentLength`, `etag?`, `lastModified?`, `acceptsRanges`, `filename`, `chunks: [Chunk]`, `savedAt: Date` |
| `SiteRule` | `id: UUID`, `host`, `referer?`, `userAgent?`, `cookies?`, `basicAuthUser?`, `basicAuthPass?`（**持久化时剥离**） |
| `SpeedRule` | `fromMinute: Int`, `toMinute: Int`（可跨界），`bytesPerSec: Double` |
| `SDMLogSettings` | `level`, `retentionDays`, `maxBytesPerFile`, `maxLines`, `persistenceEnabled` |
| `DownloadCategory` | `video / music / pictures / archives / documents / software / other` |

---

## 3. IPC / CLI 协议契约

### 3.1 Daemon IPC（UDS + JSONL）

**传输**：`AF_UNIX` / `SOCK_STREAM`，默认 `~/Library/Application Support/SuperDownloadManager/sdmd.sock`。
**分帧**：一请求一行（以 `\n` 结尾），一响应一行（以 `\n` 结尾）。
**并发**：每客户端独立处理。

> **实测基线（2026-10-24，`SDM_DAEMON=1 sdmd` 已在 macOS 27.2 上跑通）**：
> `sdmdm ping` → `{"pong" : true}` / exit 0；
> `sdmdm add <url>` → 返回 `task_id` / `state` / `url` / `destination`；
> `sdmdm list` → **返回空数组**（因为 `add` 不进 engine 队列 —— 见 R-25）；
> daemon 未启动时 `sdmdm ping` → stderr `sdmdm: daemon not reachable at <path>; is sdmd running?` / exit 1。
> 另注：裸 `build/.../sdmdm` 因 `@rpath` 找不到框架而无法直接运行，**必须**用 app bundle 里的
> `SuperDownloadManager.app/Contents/MacOS/sdmdm`（`install.sh` 软链的也是这个路径）。

**请求信封**

```json
{
  "id": "0E4A2C11-3C7F-4E1B-9A55-8D0C2B7E1F42",
  "method": "add",
  "params": {
    "url": "https://example.com/file.zip",
    "referer": "https://example.com/page",
    "filename": "file.zip",
    "category": "archives",
    "threads": 8
  }
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 请求 id，原样回显 |
| `method` | string | 是 | `ping` / `add` / `list` / `pause` / `resume` / `cancel` / `config` / `relink` |
| `params` | object | 否 | 按方法而定；`ping`/`list` 省略 |

**响应信封（成功）**

```json
{
  "id": "0E4A2C11-3C7F-4E1B-9A55-8D0C2B7E1F42",
  "ok": true,
  "result": { }
}
```

**响应信封（失败）**

```json
{
  "id": "0E4A2C11-3C7F-4E1B-9A55-8D0C2B7E1F42",
  "ok": false,
  "error": "add: 'url' (string) required"
}
```

> 失败信封里**没有** `result` 键（`IPCResponse.failure` 把 `result` 置 nil，JSONEncoder 对 nil Optional **会省略**该键）。

**⚠️ 键序实测（2026-10-24，`SDM_DAEMON=1 sdmd` + 裸 socket 客户端）**

`JSONEncoder` 未设 `.sortedKeys`，**键序未定义**，Swift 实际输出为 `ok / result / id`：

```json
{"ok":true,"result":{"pong":true},"id":"t1"}
{"ok":true,"result":[],"id":"t3"}
{"ok":false,"error":"unknown method 'nope'","id":"t4"}
{"ok":true,"result":{"task_id":"…","state":"downloading","url":"https:\/\/example.com\/a.zip","destination":"\/Users\/me\/Downloads\/a.zip"},"id":"t5"}
```

→ 消费者必须**按键名**解析；Python 的 `json.dumps` 默认不转义 `/`，逐字节比较时需显式对齐（见 R-25b）。

**各方法的 `result` 形状**

| 方法 | params | result | 错误情形 |
|---|---|---|---|
| `ping` | 无 | `{"pong": true}` | — |
| `add` | `url`（必填）, `referer?`, `filename?`, `category?`, `threads?` | `{"task_id":"<uuid>","url":"<绝对URL>","state":"downloading","destination":"<绝对路径>"}` | `add: 'url' (string) required` |
| `list` | 无 | `[{"id":"<uuid>","url":"...","state":"downloading","bytes_done":0,"total":null}]` | — |
| `pause`/`resume`/`cancel` | `id`（必填，须在 registry 中） | `{"method":"pause","applied":true}` | `control: 'id' required and must be registered` |
| `config` | `key`（必填）、`value` | `speed` → `{"speed": <value>}`；`threads` → `{"threads": <value>}` | `config: 'key' required` / `config: unknown key '<k>'` |
| `relink` | `id`（必填，须在 registry 中）、`url`（必填） | `{"decision":"safeToResume","new_url":"..."}` | `relink: 'id' and 'url' required` |
| 其它 | — | — | `unknown method '<m>'` |

**解码失败**

```json
{ "id": "?", "ok": false, "error": "decode: <DecodingError 描述>" }
```

### 3.2 CLI（`sdmdm`）

```
Usage:
  sdmdm add <url> [--referer R] [--filename F] [--category C] [--threads N]
  sdmdm list
  sdmdm pause <id> | resume <id> | cancel <id>
  sdmdm relink <id> <new_url>
  sdmdm config speed <bytes/sec> | config threads <count>
  sdmdm ping

Talks to sdmd at ~/Library/Application Support/SuperDownloadManager/sdmd.sock.
```

| 项 | 约定 |
|---|---|
| socket 路径覆盖 | 环境变量 `SDM_SOCKET` |
| 请求编码 | `JSONEncoder`（无排序、无 pretty），末尾追加 `\n` |
| 响应解析 | 读至 `\n`；`ok == true` → 打印 `result`（pretty + sortedKeys）；`ok == false` → stderr 打印 `sdmdm: <error>` |
| 退出码 | `0` 成功 / `1` 业务失败 / `64` 参数解析失败（附 usage） |
| `config speed` 单位 | **bytes/s**（与 Settings 的 KB/s 不同） |

**逐字示例**

```console
$ sdmdm ping
{
  "pong" : true
}

$ sdmdm add https://example.com/file.zip
{
  "destination" : "/Users/me/Downloads/file.zip",
  "state" : "downloading",
  "task_id" : "6F1C2D3E-4A5B-6C7D-8E9F-0A1B2C3D4E5F",
  "url" : "https://example.com/file.zip"
}

$ sdmdm list
[ ]

$ sdmdm pause 6F1C2D3E-4A5B-6C7D-8E9F-0A1B2C3D4E5F
{
  "applied" : true,
  "method" : "pause"
}

$ sdmdm ping; echo "exit=$?"
sdmdm: daemon not reachable at /Users/me/Library/Application Support/SuperDownloadManager/sdmd.sock; is sdmd running?
exit=1
```

### 3.3 扩展派单 HTTP 接口

```
GET /add?url=<percent-encoded>&filename=<...>&referer=<...>
GET /ping
```

**成功（`/add`）** — HTTP 200

```json
{ "ok": true, "result": { "accepted": true, "url": "https://example.com/file.zip" } }
```

**成功（`/ping`）** — HTTP 200

```json
{ "ok": true, "result": { "pong": true } }
```

**缺 url** — HTTP 400

```json
{ "ok": false, "error": "missing url" }
```

**未知路径** — HTTP 404

```json
{ "ok": false, "error": "not found" }
```

**请求头**：`Content-Type: application/json`、`Content-Length: <n>`、`Connection: close`

> ⚠️ `docs/extension-install.md` 里写的是 `{"ok":true,"result":{"taskId":"..."}}`，
> 与源码实际的 `accepted` **不一致**（R-26）。Python 版需要选定一种并同步两边。

### 3.4 任务详情「Copy all as JSON」

GUI 内部格式（**驼峰**，与 IPC 的 snake_case 不同）：

```json
{
  "bytesDone": 1048576,
  "destination": "/Users/me/Downloads/Archives/file.zip",
  "error": null,
  "filename": "file.zip",
  "id": "6F1C2D3E-4A5B-6C7D-8E9F-0A1B2C3D4E5F",
  "speedBytesPerSec": 125000.0,
  "speedLimitBytesPerSec": 0,
  "state": "downloading",
  "totalBytes": 2097152,
  "url": "https://example.com/file.zip"
}
```

---

## 4. 已知妥协与不做的事

### 4.1 明确不做的（`docs/notes.md` v1.0 暂不做）

| # | 项 | 原因 | Python 版 |
|---|---|---|---|
| 1 | Live2D 模型接管 | Apple cubism SDK 法律约束；非工程能力 | 同样不做 |
| 2 | 3D 模型预览 | macOS 原生 3D 够用但渲染工作量大 | 同样不做 |
| 3 | BT / Magnet 下载 | 与「普通文件下载」定位冲突；需补 tracker 协议 + DHT + 跨网 NAT | 同样不做 |
| 4 | 自动解包 zip / 自动挂载 dmg | 安全顾虑——自动执行任意归档内容会扩大用户机器的信任半径 | 同样不做 |
| 5 | 公网 URL 上传 / 分享 | 隐私——SDM 持有下载链接是私密行为 | 同样不做 |
| 6 | 多账号云同步 | 同上；本地 SQLite 足够 | 同样不做 |

### 4.2 刻意保留的妥协

| # | 项 | 妥协 | 影响 | Python 版决策 |
|---|---|---|---|---|
| 1 | Safari 接管能力有限 | macOS 15+ Safari Web Extension 强制「7 天重签」是平台设计 | Safari 用户需**手动点按钮**派单，不能自动接管 | 保留平台限制 |
| 2 | ad-hoc 签名本地跑 | Developer ID 是 v2/v3 目标 | DMG 安装需用户手动「Open Anyway」 | Python 版若用 pyinstaller 签名同样是 ad-hoc |
| 3 | 动态分段策略激进版 | 初版 merge 阈值 0.3、split 阈值 1.5 | 可切保守；实测数据后再迭代 | 🚧 该策略在 Swift 版**根本没接线**（R-24b），Python 版可按「激进版」参数实现 |
| 4 | 无 CLI 的 `sdmd` 子进程实际启动 | 注释写「P0 只声明源码层 stub」——**该注释已过期**，v1.1.10 已有真正的 `sdmd` tool target + 打包脚本 | 现在的状态是 daemon 可用 | 以现状为准 |
| 5 | GUI 截图实测 | TCC（Screen Recording / Accessibility）限制，agent session 无截图权限 | GUI 验证靠人工手测；CLI 端到端可自动测 | Python 版同理，pywebview 截图也受 TCC 限制 |
| 6 | ~~Little Snitch + ad-hoc 应用出站 0 字节~~ | **已收回**（误判）。用户现场确认 Little Snitch 为 Silent Allow 且 SDM 请求未被拦截 | 「URLSession 卡死 ≠ Little Snitch」 | 不作为约束 |
| 7 | `release-assets.githubusercontent.com` 在 SDM 里 0 KB/s，curl 同 URL 正常 | 详见 `docs/notes.md` 第 7 条与第 5 章 R-01 | v1.1.9 的修复方向是「probe 与 chunk 共用同一个 downloader + 加重诊断日志 + 加日志系统」；v1.1.10 进一步把传输改成流式 | **这是 Python 版的第一号验收项** |

### 4.3 结构性妥协（源码级，Swift 版就已存在）

| # | 项 | 说明 |
|---|---|---|
| 1 | 无后台常驻 daemon 的 GUI ↔ CLI 共享状态 | GUI 与 daemon 是**两个独立的 engine 实例**，CLI 看到的是 daemon 的队列，GUI 看到的是自己的队列 |
| 2 | 无计划任务持久化 | 计划只活在 `@State` |
| 3 | 无历史删除 | SQLite 里没有 DELETE 语句 |
| 4 | 无自动更新 | Sparkle 未接入 |
| 5 | 无真实分片状态透出到 GUI | 详情页的分片是合成的 |
| 6 | 无时间窗口限速 | `SpeedPolicy` 是死代码 |
| 7 | 无内容嗅探驱动的分类 | `ContentSniffer.sniff` 是死代码，分类只看 URL 扩展名 |
| 8 | 单 URL 单类别 | 不支持一个 URL 拆成多类 |

---

## 5. 重构风险清单

> 每一条都是 Swift 版**实际踩过或明确存在**的坑。Python 版必须**不再出现**。
> 严重度：🔴 用户可见的功能性故障 / 🟠 能力缺失或语义错误 / 🟡 契约不一致或文档漂移。

### R-01 🔴 「0 KB/s / 下载永远卡住」— 传输缓冲 + 总时长预算

- **历史**：v1.0.0–v1.1.9 用 `URLSession.data(for:)` 拉整个分片 → **整个 body 缓冲在内存**，最后一字节到达才返回。
  - 表现：整个分片期间磁盘上 0 字节、UI 0 KB/s（用户在 GitHub release CDN 上实测 ~109 KB/s，一个 2.19 MB 分片卡 ~20 s）。
  - 附带：请求带 wall-clock 预算，链路健康但慢时被 `-1001` 超时打断，**整个下载失败且无重试、无单流回退**。
- **v1.1.10 的修法**：`URLSessionDataDelegate` 流式 + **空闲看门狗**（无字节 60 s 才判死）替代总时长上限。
- **代码**：`Engine/HTTPDownloader.swift:18-34, 265-391`；`Engine/ChunkStreamDelegate.swift:1-41`；
  回归测试 `Tests/EngineTests/StreamingTransportTests.swift:43-83`（中途采样磁盘必须 > 0）
  与 `:218-257`（强制旧传输时必须 == 0，证明测试非空跑）。
- **Python 验收**：httpx **必须**用 `client.stream()` + `aiter_bytes()`；**禁止** `await client.get()` 读全量。
  写一个「慢速滴流服务器，中途采样 partial 文件必须 > 0」的测试。

### R-02 🔴 进度到 100% 但速度还在跳

- **历史**：v1.0.7 用 EMA(α=0.4) 平滑速度；引擎完成时把 `speedBytesPerSec` 置 0，但 **EMA 缓存没清**，
  下一 tick 变成 `0.4*0 + 0.6*prev` → 完成后仍显示 ~300 KB/s，要 1.2 s 才衰到 0。
- **代码**：`Engine/SpeedDisplay.swift:59-86`；修复提交 `bc309f8`；测试 `Tests/EngineTests/SpeedDisplayTests.swift`
  （`testCompletedRowShowsZeroImmediately` / `testPausedRowShowsZeroImmediately` 等 5 个）。
- **Python 验收**：终态与暂停态**硬清零**（不衰减）；引擎报的 speed 为 0 时不得用历史 EMA 兜底。

### R-03 🔴 进度条在 0–100% 之间乱窜

- **历史**：v1.0.6 的 progress 回调报的是 **单个分片的累积偏移**（`chunkStart + byteCount`），
  3 个分片并行时 UI 看到 chunk0=33%、chunk1=66%、chunk2=100% 来回跳。
- **代码**：`Engine/DownloadFlow.swift:309-333`（`ChunkProgress` 聚合器）；修复提交 `933d2d9`；
  测试 `Tests/EngineTests/ChunkProgressTests.swift`（`testCoalesceKeepsMonotonic` / `testMultiChunkMonotonicAggregate`）。
- **Python 验收**：进度值必须是**所有分片增量之和**（文件级），且**单调不减**；写单调性断言。

### R-04 🟠 限速完全不生效

- **现状**：`SpeedLimiter.consume()` 在下载路径中**没有任何调用方**（只有单测调用）。
  `DownloadFlow` 持有 `limiter` 但从不消费它，`HTTPDownloader` 的 `limiter` 属性永远是 `nil`。
- **代码**：`Engine/SpeedLimiter.swift:52-63`；`Engine/DownloadFlow.swift:37, 51`（只存不用）。
- **历史**：v1.0.x 连 `setRate` 都没接（存了速率不重配桶），v1.0.6 才修好「桶」这一层，但**始终没接到字节流上**。
- **Python 验收**：全局限速与单任务限速都必须在**每个分片的字节写入点**扣令牌；
  写测试：限速 100 KB/s 下载 1 MB，耗时应在 8–14 s 区间。

### R-05 🟠 进度溢出

- **历史**：v1.1.6 修过「进度溢出 clamp」。
- **代码**：`App/MainWindow/TaskSummary.swift:31-34`（`min(1.0, max(0.0, ...))`）。
- **Python 验收**：百分比恒在 [0,100]；`bytesDone > totalBytes` 时显示 100% 而不是 >100%。

### R-06 🟠 剪贴板误报（见到任何链接就下载）

- **历史**：v1.0.6 的剪贴板监听见到任何 http(s) 链接就自动下载 —— 用户反馈「太烦」。
  v1.0.8 改成：① `FileURLHeuristic.isLikelyFileURL` 严格按扩展名过滤；② 弹**确认对话框**而不是直接下。
- **代码**：`Engine/FileURLHeuristic.swift:21-26`；`App/MainWindow/DownloadListStore.swift:387-431`；
  `App/MainWindow/ClipboardConfirmView.swift`；测试 `Tests/EngineTests/FileURLHeuristicTests.swift`
  （`testRejectsWebPages` / `testRejectsUnknownExtensions`）。
- **Python 验收**：① 裸域名 / HTML 页面 / 未知扩展名**不弹窗**；② 必须是**确认弹窗**，不自动下载；
  ③ 5 分钟去重；④ 已在队列中的 URL 不重复提示。

### R-07 🟠 Content-Disposition 漏解析 → 文件名变成随机 UUID

- **历史**：v1.0.x 的正则 `#filename\s*=\s*"([^"]+)"#` **强制要求双引号**。
  GitHub release CDN 发的是 `attachment; filename=SuperDownloadManager.app.zip`（**无引号**）→
  退化到 URL 路径 → GitHub 的 URL 末段是随机 UUID → 下载下来的文件叫 `efb1` / `download.bin`。
- **代码**：`Engine/ContentSniffer.swift:336-397`；修复提交 `13137d5`；
  测试 `Tests/EngineTests/ContentSnifferTests.swift`
  （`testContentDispositionUnquotedGitHub` / `Quoted` / `ExtVal` / `ExtValNoEncoding` / `Inline`）。
- **Python 验收**：4 种形态全支持；`filename*` 优先；**空字符串也走 URL 兜底**（v1.1.1 修过）。
  必须有一个用真实 GitHub 头做断言的测试。

### R-08 🔴 暂停被报成「失败」

- **历史**：协作取消让 `URLSession` 抛 `URLError(.cancelled)`，被 flow 重新包成 `FlowError.partial`，
  catch 里一律置 `failed` → 用户点「暂停」，行却变成「失败」。
- **代码**：`Engine/DownloadEngine.swift:272-297`（`runFlow`）与 `:437-478`（`runResume`）；
  修复提交 `d286043` / `16edeed`；机制是 `CancelGuard`（`:343-348`）+ `Task.isCancelled`。
- **Python 验收**：暂停/取消导致的异常**绝不能**进 `failed` 分支；`asyncio.CancelledError` 必须被单独捕获并重新抛出/吞掉，
  同时把状态置为 `paused`/`cancelled`。

### R-09 🔴 续传其实不续传（重头下）

- **现状**：`DownloadFlow` 的分片写的是 `<dest>.sdm.partial-<id>`；而 `DownloadEngine.resume` 检查的是
  `<dest>.part`（`:382-384`）→ `bytesOnDisk` **恒为 0** → 走「取消 + 从头 start」。
  而 `start` 后的 `DownloadFlow.run()` 会**扫描并复用**已存在的 `.sdm.partial-N`（`:117-133`），
  所以「分片级续传」在绕一圈后其实生效了，但：
  - **task id 变了**（旧行被 cancel 掉、新行是另一个 id）；
  - 进度从 0 开始重新报（见 R-10b）；
  - `ResumeFlow`（真正的单流 Range 续传）几乎不可达，且它自身有 bug：
    - 用 `URLSession.shared.data(for:)`（**整块缓冲**，大文件会 OOM）；
    - 用 `FileHandle(forWritingTo:)`（**截断**已下载字节）再 `seekToEnd()` → **丢失**此前下载的字节。
- **代码**：`Engine/DownloadEngine.swift:377-404`；`Engine/ResumeFlow.swift:45-79`（尤其 `:57` 与 `:72`）。
- **Python 验收**：暂停→恢复后，**同一个 task id** 继续，Range 起点 == 已落盘字节数，
  目标文件内容 == 完整文件，且总字节数不多不少。

### R-10 🟠 取消后残留分片文件 / 进度回退

- **R-10a 残留**：`DownloadListStore.cancel` 只删 `<dest>.part`（`:229`），真正的 `.sdm.partial-N` **不被删**；
  `cleanupOrphanedPartFiles` 又因为 `url.pathExtension == "part"` 而**永远匹配不到** `.sdm.partial-0`
  （它的 `pathExtension` 是 `.partial-0`）→ **孤儿分片永远不会被清理**。
- **R-10b 进度回退**：`DownloadFlow.run()` 先 `progressChannel.report(preExistingBytes)`（`:141-143`），
  紧接着 `ChunkProgress` 从 0 开始累加并上报 → 第二次上报把进度**覆盖成更小的值** → 进度条倒退。
- **代码**：`App/MainWindow/DownloadListStore.swift:219-233, 259-278`；`Engine/DownloadFlow.swift:117-143, 314-333`。
- **Python 验收**：① 取消后目录里**不留**任何 `.sdm.partial-*`；② 孤儿清理能匹配到分片残留；
  ③ 续传时进度**从已落盘字节开始**，不倒退。

### R-11 🟠 「默认线程数」滑块不生效

- **现状**：`sdm.threads.default` 只写进 UserDefaults，`engine.setMaxThreads()` **无调用方**。
  `parallelismCap` 永远是引擎初始化时的 32，`defaultChunks` 也是 32。
  滑块唯一的作用是给剪贴板弹窗当默认值。
- **代码**：`App/MainWindow/SettingsView.swift:26, 205-213`；`Engine/DownloadEngine.swift:105-109`。
- **Python 验收**：改滑块后新建任务的**实际并发分片数**必须变化。

### R-12 🔴 分片被 URLSession 每主机 4 连接限制卡住（并发退化）

- **历史**：`URLSession.shared` 在 macOS 上静默把 `httpMaximumConnectionsPerHost` 限到 **4** →
  第 5–8 个分片 worker 开不出连接，只能排队 → 表现为「分片一个接一个完成」而不是并行。
- **修法**：自建 `URLSessionConfiguration` 并设 `httpMaximumConnectionsPerHost = 32`，**全引擎共用一个 session**。
- **代码**：`Engine/HTTPDownloader.swift:93-142`；`Engine/DownloadEngine.swift:52-62`；
  回归测试 `Tests/EngineTests/DownloadFlowEndToEndTests.swift:153-233`
  （8 个各需 ~0.8 s 的分片，总耗时必须 < 4 s）。
- **Python 验收**：httpx 的 `Limits(max_connections=...)` 必须 ≥ 分片上限，否则同样退化；
  必须有「N 个分片总耗时 ≈ 单分片耗时」的并行性测试。

### R-13 🟠 URL scheme 没在 Info.plist 注册

- **现状**：`SDMApp.handleURLEvent` 实现了 `superdownloadmanager://show|add?url=`，
  但 `App/Info.plist` 里**没有** `CFBundleURLTypes`，历史上也从未有过 → 系统根本不会把 URL 交给 app。
- **代码**：`App/SDMApp.swift:387-422`；`App/Info.plist`（无该键）。
- **Python 验收**：要么真正注册 scheme，要么明确从功能清单里删掉（不能留一个「看起来有、其实不通」的入口）。

### R-14 🟠 菜单栏「Add URL…」不弹窗

- **现状**：菜单栏点「Add URL…」广播 `.sdmShowAddSheet`；`MainWindow` 的接收器只把侧边栏选中项重置为「全部下载」，
  **没有**把 `DownloadListView` 里的 `showAddSheet` 置 true → 什么也不发生。
- **代码**：`App/SDMApp.swift:359-362, 606-609`；`App/MainWindow/DownloadListView.swift:73, 111`。
- **Python 验收**：菜单栏与主菜单的「Add URL…」都必须真的打开添加对话框。

### R-15 🟠 菜单栏「Settings…」不打开设置

- **现状**：广播 `.sdmShowSettings`，但**没有任何订阅方**。
- **代码**：`App/SDMApp.swift:376-379, 551`。
- **Python 验收**：菜单栏「Settings…」必须打开设置面板。

### R-16 🟠 液态玻璃吞掉工具栏点击

- **历史**：v1.1.2 用了 `.glassEffect(.regular.interactive())`，macOS 26 上 Pause All / Resume All / Clear Completed
  **三个按钮全部点不动**（只有没有 `.disabled` 的「Add URL」还能用，因为它是首个获得焦点的）。
- **修法**：改成 `.regularMaterial` + 手绘描边。
- **代码**：`App/MainWindow/DownloadListView.swift:5-47`（`GlassCapsule`）；修复提交 `d286043`。
- **Python 验收**：四个工具栏按钮全部可点击（写 UI 测试或人工验收清单）。

### R-17 🟡 详情页分片是合成值，不是真实分片

- **现状**：详情页按 `total / 2MB` 反推 N 个分片并用 `bytesDone` 均摊，与引擎真实的分片划分**无关**。
- **代码**：`App/MainWindow/TaskDetailView.swift:34-64`（注释里已自承）。
- **Python 验收**：直接透出引擎真实分片（id / start / end / bytesDone / 状态）。

### R-18 🟡 分片 elapsed 列所有行显示同一个值

- **现状**：`chunkElapsed` 取目录下**最新**的 `.sdm.partial-*` 的 mtime，所有分片行共用这个值。
- **代码**：`App/MainWindow/TaskDetailView.swift:266-321`。
- **Python 验收**：每行显示**该分片自己的**最后写入时间。

### R-19 🟠 SHA-256 把整个文件读进内存

- **现状**：`Data(contentsOf:)` 全量读入再哈希 —— 大文件直接 OOM。
- **代码**：`App/MainWindow/DownloadListStore.swift:332-355`。
- **Python 验收**：流式分块哈希（1 MB 一块），大文件不爆内存。

### R-20 🟡 「删除文件」的语义与计划文档矛盾

- **现状**：代码用 `FileManager.trashItem`（移到废纸篓），但 v1.0.6 的计划澄清写的是「不做 Move to Trash, 直接 rm」。
- **代码**：`App/MainWindow/DownloadListStore.swift:295-301`；计划澄清见提交 `2cd25dc`。
- **Python 验收**：二选一并写进文档。

### R-21 🟠 历史「移除」按钮点了没反应

- **现状**：`HistoryViewModel.remove(id:)` 是空实现（只有注释说「v1.0.5 加 proper DELETE」——但一直没加）。
- **代码**：`App/MainWindow/HistoryView.swift:165-171`。
- **Python 验收**：SQLite 里有真正的 `DELETE FROM history WHERE id = ?`，按钮生效。

### R-22 🟡 计划任务不持久化 + 定时器泄漏

- **现状**：`schedules` 只在 `@State`；`.onAppear` 起 1 Hz 定时器但没有 `onDisappear` 取消；切换到别的侧边栏项后定时器仍在跑。
- **代码**：`App/MainWindow/ScheduleView.swift:16-17, 75, 94-109`。
- **Python 验收**：计划持久化（JSON 文件或 UserDefaults）；离开页面即停定时器。

### R-23 🟠 开机自启只存值不注册 / R-23b sidecar 路径不一致

- **R-23**：`v1.0.4` 只写 `sdm.openAtLogin` 的 Bool，从不调 `SMAppService` → 开关无效。v1.1.2 补了
  `LaunchAtLogin.setEnabled` 与启动时的 `syncFromStorage()`。
- **R-23b**：`ResumeState.sidecarPath` 用 `deletingPathExtension`（`x.dmg` → `x.sdm.json`），
  同文件的 `URL.sdmSidecarPath` 用 `appendingPathExtension`（`x.dmg.sdm.json`），两处不一致；文档注释写的是后者。
- **代码**：`App/LaunchAtLogin.swift`；`Engine/ResumeState.swift:53-61, 82-87`。
- **Python 验收**：开机自启真的注册/注销；sidecar 路径统一一种并写进文档。

### R-24 🟠 日志设置不持久化生效 / R-24b 动态分段策略没接线

- **R-24**：`SDMLogger.shared` 用 `.default` 初始化，**启动时不从 UserDefaults 恢复** `sdm.logs.*`；
  只有用户在设置页动过某个旋钮之后，那一刻的快照才生效；下次启动又回到默认值。
- **R-24b**：`RangePlanner.rearrange()` / `apply()` **无调用方** → 动态分裂/合并策略**从未运行过**；
  `SpeedPolicy` / `SpeedRule`（时间窗口限速）同样是死代码；`ContentSniffer.sniff`（魔数嗅探）也是死代码。
- **代码**：`Engine/SDMLogger.swift:216-230`；`Engine/RangeManager.swift:139-223`；`Engine/SpeedPolicy.swift`；`Engine/ContentSniffer.swift:68-223`。
- **Python 验收**：日志设置启动即恢复；若实现动态分段/时间窗限速/魔数嗅探，必须真的接线并有测试。

### R-25 🔴 Daemon 的 IPC 方法全是「空壳」

- **现状**（`Daemon/IPC/Dispatcher.swift`）：
  - `add`：`category` 读了又丢（`:59-62`）、`referer` 根本没读、目录固定 `~/Downloads/`（不分分类）、
    而且**不走 engine**，自己 new 一个 `DownloadFlow` 跑（`:76-84`）→ 任务**不进 engine 队列**；
  - `list`：读 `engine.snapshot()` → 因为 add 没进队列 → **永远返回空数组**；
  - `pause`/`resume`/`cancel`：只回 `{"applied": true}`，**不执行任何动作**（`:109-118`）；
  - `config threads`：只回显，不改引擎（`:133-134`）；
  - `relink`：decision 是**硬编码** `"safeToResume"`，没跑 `ExpiredLinkHandler`（`:140-153`）。
- **附带（已实测修正，2026-10-24）**：`Daemon/main.swift` 用 `SocketServer(path: socketPath) { ... }` 的尾随闭包写法，
  而 `SocketServer` 的初始化签名是 `init(path:handler:)`。**这在 Swift 里是合法的**（尾随闭包匹配最后一个参数），
  经 `xcodebuild clean build` 实测编译通过，**不是**编译错误。原判有误，特此更正。
- **Python 验收**：CLI 的每个子命令都必须**真正生效**；`sdmdm add` 后 `sdmdm list` 必须能看到这条任务；
  `sdmdm pause <id>` 必须真的停掉下载。

### R-25b 🟡 Daemon 响应的 JSON 键序与声明序不一致（实测）

- **实测结果**（`SDM_DAEMON=1 sdmd` + 裸 socket 客户端，2026-10-24）：
  - `ping` → `{"ok":true,"result":{"pong":true},"id":"t1"}`
  - `list` → `{"ok":true,"result":[],"id":"t3"}`
  - 未知方法 → `{"ok":false,"error":"unknown method 'nope'","id":"t4"}`
  - `add` → `{"ok":true,"result":{"task_id":"…","state":"downloading","url":"…","destination":"…"},"id":"t5"}`
- **含义**：`JSONEncoder`（无 `.sortedKeys`）的输出键序是**未定义**的，Swift 实际给出的是
  `ok / result / id` 而不是结构体声明的 `id / ok / result`。**任何按位置解析的消费者都会出错**，
  按键名解析则安全。
- **Python 验收**：若要与 Swift 版做**逐字节**兼容的黄金测试，先决定是否固定键序；
  推荐 **按键名解析**（Python `json.loads` 天然如此），并把「键序不保证」写进协议文档。
  同时注意 Swift 的 `JSONEncoder` 会把 `/` 转义成 `\/`（实测 `"url":"https:\/\/example.com\/a.zip"`），
  Python 的 `json.dumps` 默认**不转义斜杠** —— 逐字节比较时需要 `separators` / `ensure_ascii` 对齐。


### R-26 🟡 扩展派单接口的响应形状与文档不一致

- **现状**：代码返回 `{"accepted": true, "url": ...}`；`docs/extension-install.md` 写的是 `{"taskId": "..."}`。
- **代码**：`Engine/DispatchServer.swift:96-97`；`docs/extension-install.md:36`。
- **Python 验收**：代码与文档一致；扩展实测能派单成功。

### R-27 🟡 两套 LaunchAgent 配置并存

- **现状**：`Launcher/templates/com.skyc8266.superdownloadmanager.daemon.plist` 指向 `Contents/MacOS/sdmd`；
  `scripts/install.sh` 生成另一份指向 `Contents/MacOS/SuperDownloadManager` + `SDM_DAEMON=1`。
  两份都打进 app bundle，行为不同。
- **Python 验收**：只保留一套。

### R-28 🟡 CI 断言了错误的资源文件名

- **现状**：CI 断言 `$APP/Contents/Resources/com.superdownloadmanager.daemon.plist`，
  而实际资源名是 `com.skyc8266.superdownloadmanager.daemon.plist`（v1.1.0 加包名前缀时漏改 CI）。
- **代码**：`.github/workflows/ci.yml:111`；`Launcher/templates/com.skyc8266.superdownloadmanager.daemon.plist`。
- **Python 验收**：CI 断言的资源名与实际一致。

### R-29 🟠 「服务器忽略 Range」会产出**损坏**的文件

- **现状**：响应校验器接受 200 和 206（`Engine/HTTPDownloader.swift:321-323`）。
  若服务器忽略 `Range` 返回 200 + **完整文件**，每个分片都会把**整个文件**写进自己的 partial，
  合并后文件内容错乱（且 `notPartialContent` 这个错误枚举**从未被构造**）。
  单分片时看起来正常，多分片时静默损坏。
- **代码**：`Engine/HTTPDownloader.swift:320-330, 356-368`；`Engine/HTTPDownloader.swift:38`（未被使用的枚举值）。
- **Python 验收**：服务器回 200（忽略 Range）时，要么明确降级为单流整文件下载（且只下载一次），
  要么明确报错；**绝不能**把整文件写进每个分片。

### R-30 🟡 剪贴板/扩展/URL scheme 三条入口的分类与落盘目录不一致

- **现状**：`add(url:)` 走 `downloadDirectory(for:)`（分分类）；`addWithConfig` 走 `category ?? from(url:)`；
  Dispatcher 的 `add` 固定 `~/Downloads/`（不分分类）。
- **Python 验收**：三条入口 + CLI 的落盘规则统一（建议：统一走「分类目录」规则）。

### R-31 🟠 「过期链接 → 换新链接」整条链路从未跑通

- **现状**：`ExpiredLinkHandler.evaluateRelink()` 只有单测在用；引擎从**不**把任务置成 `awaiting_new_link`，
  `ExpiredLinkBanner` 视图**从未被任何地方引用**，`relink` 的 IPC 方法返回硬编码 decision（R-25）。
  即：用户遇到 403/410/451，界面上只会看到一个「失败」行 + 错误消息，**没有任何换链入口**。
- **代码**：`Engine/ExpiredLinkHandler.swift:42-67`；`App/MainWindow/ExpiredLinkBanner.swift`（孤儿文件）；
  `Engine/DownloadEngine.swift`（无 `awaiting_new_link` 写入点）。
- **Python 验收**：403/410/451 时任务进 `awaiting_new_link` 状态（**不是** `failed`），
  界面显示换链横幅，用户提交新 URL 后按 F035 的判定顺序（长度 → ETag → Accept-Ranges）决定
  `safeToResume` / `unsafeButProceed` / `unsafeReject`。

### R-32 🟠 「服务器忽略 Range」的反向问题：206 但 Content-Range 不匹配

- **现状**：`streamChunk` 只检查 `statusCode ∈ {200, 206}`，**不校验** `Content-Range` 头是否与请求的
  `Range` 一致。返回 206 但 `Content-Range` 指向别的区间（或缺失）时，字节会被写到**错误的偏移**上，
  合并后文件静默损坏。
- **代码**：`Engine/HTTPDownloader.swift:320-330, 353-371`（只读 `Content-Length` / `Content-Range` 用于日志，不做断言）。
- **Python 验收**：收到 206 时必须断言 `Content-Range` 的起止与请求一致，不一致立刻报错并**不写盘**。

### R-33 🟡 `RangeStrategy` 的分片大小常量与实际分片不自洽

- **现状**：`initialChunkSize = 2 MB` 用来算分片数（`n = contentLength / 2MB`），
  但 `minChunkSize = 1 MB` / `maxChunkSize = 16 MB` / `maxChunks = 128` 只被**未接线的** `rearrange()` 使用；
  `defaultChunks`（32）在设置线程数时才被 `setMaxThreads` 改（而它无调用方，R-11）。
  实际分片大小 = `contentLength / n`，可能远大于 2 MB（例如 1 GB 文件、32 分片 → 每片 31 MB）。
- **代码**：`Engine/RangeManager.swift:43-56, 83-96`。
- **Python 验收**：分片大小策略要明确（建议：按 `maxChunkSize` 反推分片数，或显式暴露给用户），
  并保证分片数 ≤ 连接池上限。

### R-34 🟡 `RangePlanner.replaceChunks` 的前置条件会在续传路径上炸

- **现状**：`replaceChunks` 有 `precondition(newChunks.count == chunks.count)`，
  任何导致分片数变化的续传（内容长度变化 → 重新规划）都会**直接 crash**而不是报错。
- **代码**：`Engine/RangeManager.swift:105-113`。
- **Python 验收**：分片方案变化时抛显式异常并重新规划，绝不 crash。

---

## 6. 汇总

- **盘点功能点总数：191 条**（F001–F191），分布在 17 个域：
  下载引擎 36 / 任务控制 13 / 分类命名 5 / 采集入口 17 / 列表 UI 28 / 历史 7 / 计划 3 /
  站点规则 4 / 日志 12 / 通知 5 / 设置 16 / 窗口系统 6 / L10n 4 / CLI 9 / Daemon IPC 14 /
  扩展 HTTP 5 / 运维 7。
- **其中 Swift 版只实现了一半或存在 bug 的高风险项共 35 条**（第 5 章 R-01 ~ R-34 + R-25b），
  按严重度分：**🔴 7 条**（R-01 传输缓冲致 0 KB/s、R-02 完成后速度仍在跳、R-03 进度乱窜、
  R-08 暂停被报成失败、R-09 续传其实重头下、R-12 每主机 4 连接把分片串行化、
  R-25 Daemon IPC 全是空壳）、**🟠 17 条**、**🟡 11 条**。
- **最需要 Python 版优先处理的 5 项**：
  1. **R-01** 流式传输（httpx `stream()` + `aiter_bytes()`）+ 空闲看门狗，绝不设总时长上限；
  2. **R-09** 真续传：同一个 task id、Range 起点 = 已落盘字节、追加写不截断；
  3. **R-04** 把令牌桶真正接到字节流上（全局 + 单任务两个独立桶）；
  4. **R-25** CLI/Daemon 的每个方法真正生效（`add` 进队列、`list` 能看到、`pause` 真停）；
  5. **R-03 / R-02** 进度单调递增 + 终态速度硬清零。
- **Swift 版可判定为「未接线/死代码」的功能点**：动态分裂合并（R-24b）、时间窗口限速（R-24b）、
  魔数嗅探（R-24b）、过期链接重链与 `awaiting_new_link` 状态（F035 无调用方，见 R-31）、
  Sparkle 自动更新（F152）、计划任务持久化（F109 / R-22）、历史删除（F104 / R-21）、
  `RangePlanner` 在 GUI 的真实透出（F084 / R-17）。
- **跨语言协议必须逐字兼容的四处**：Daemon JSONL 请求/响应信封（第 3.1 节）、
  CLI 的 stdout 形状与 0/1/64 退出码（F164）、扩展派单 HTTP 的路径与 JSON（F180–F184）、
  落盘文件名规则（`.sdm.partial-<id>` / `.part` / `.sdm.json`，第 2.4 节）。
- **可直接沿用的资产**：`Engine/L10n.swift` 的 208 个双语键、`DownloadCategory` 的扩展名表（两处需同步）、
  `FileURLHeuristic` 的白名单、`ContentSniffer.parseContentDisposition` 的 4 形态解析、
  `RangeStrategy` 的 8 个策略常量、`SDMLogSettings.validated()` 的 4 个钳制区间。
