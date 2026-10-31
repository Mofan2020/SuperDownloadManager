"""HTTP 下载器：探测 + 分片流式传输。

对应 Swift 版 Engine/HTTPDownloader.swift 与 Engine/ChunkStreamDelegate.swift。

**这是整个项目最关键的一处重构。** Swift 版 v1.1.9 用
`URLSession.data(for:)` 拉分片，那个 API 会把整个响应体缓存在内存里，
最后一个字节到了才返回。后果是用户看到的「0 KB/s」：行在那里冻住，
等整个分片下完才有动静。更糟的是它带总时长上限，慢但健康的连接
会被误判为失败。

这里用 httpx 的流式接口重写：字节一到就落盘、进度条实时动、
死连接由「空闲看门狗」判定（多久没来字节），而不是「总时长上限」。
慢而健康的连接永远不会被杀。

另外几个要点：
- 重连时 Range 头从**已落盘字节**继续，不是从分片起点重来。
- 403/410/451 归为「链接失效」类错误，不重试——重试只是浪费时间。
- 写入是**有上限的**：动态分段会在下载途中缩短分片的区间，超出
  上限的字节必须丢掉，否则两个分片的区间会重叠，文件就废了。
- 进度上报的是**增量**，不是累计值。累计值会让文件级进度虚高
  （2 MB 的文件报成 5 MB，取决于块大小）。
- 忽略 SSL 验证是显式开关，默认关闭。
"""

from __future__ import annotations

import asyncio
import enum
import random
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from ..logging_setup import CAT_CHUNK, CAT_NET, get_logger
from .content_sniffer import USER_AGENT, ContentProbe, ProbeError, merge_headers
from .content_sniffer import probe as sniffer_probe
from .errors import ErrorCategory, TaskError, error_for_status, parse_retry_after
from .file_assembler import FileAssembler
from .range_planner import Chunk
from .speed_limiter import SpeedLimiter

log = get_logger()


class DownloadFailure(enum.Enum):
    """分片传输的失败分类。"""

    STATUS = "status"                # 服务器返回了不可接受的状态码
    NOT_PARTIAL_CONTENT = "not_partial_content"  # 服务器无视 Range，返回 200
    FORBIDDEN = "forbidden"          # 403/410/451 — 链接失效
    TOO_MANY_REDIRECTS = "too_many_redirects"
    NETWORK = "network"              # 超时 / 连接重置 / DNS 失败
    DECODING = "decoding"
    #: **服务器给的数据跟它自己承诺的对不上。**
    #:
    #: 头说 `Content-Range: bytes 100-200`，body 却是文件开头；
    #: 或者 body 长度跟区间长度不符。
    #:
    #: **跟 `NOT_PARTIAL_CONTENT` 分开是有原因的。** 后者是
    #: 「服务器不支持分块」——退回单流下载是对的，服务器对整个
    #: 文件的响应通常没问题。
    #:
    #: 这一类是**服务器在骗人**：它给的数据根本不是它承诺的那一段。
    #: 退回单流也救不了——同一个服务器对整个文件的响应同样可能
    #: 是错的（CDN 边缘节点缓存了别的响应就是这种）。而且这正是
    #: 「文件大小正常但内容全错」那种损坏的来源（实测 2026-10-30：
    #: 用户下的 4 个文件全部损坏，每个分片的开头都等于文件开头）。
    #:
    #: 所以这类**直接失败，不回退**。
    CONTENT_MISMATCH = "content_mismatch"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return self.value


#: 失败类型 → 规格 §5.1 的错误类别。
#:
#: `NOT_PARTIAL_CONTENT` 单独归到 `range_error`：规格说它「触发回退」，
#: 不是「重试到失败」。
_CATEGORY_BY_FAILURE: dict[DownloadFailure, ErrorCategory] = {
    DownloadFailure.STATUS: ErrorCategory.SERVER_ERROR,
    DownloadFailure.NOT_PARTIAL_CONTENT: ErrorCategory.RANGE_ERROR,
    DownloadFailure.FORBIDDEN: ErrorCategory.AUTH,
    DownloadFailure.TOO_MANY_REDIRECTS: ErrorCategory.CLIENT_ERROR,
    DownloadFailure.NETWORK: ErrorCategory.NETWORK,
    DownloadFailure.DECODING: ErrorCategory.CLIENT_ERROR,
    DownloadFailure.CONTENT_MISMATCH: ErrorCategory.SERVER_ERROR,
    DownloadFailure.UNKNOWN: ErrorCategory.UNKNOWN,
}


class DownloadError(Exception):
    """带失败分类的下载错误。"""

    def __init__(
        self,
        kind: DownloadFailure,
        detail: str = "",
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"{kind.value}: {detail}" if detail else kind.value)
        self.kind = kind
        self.detail = detail
        self.status = status
        self.retry_after = retry_after

    @property
    def is_retryable(self) -> bool:
        """链接失效类错误重试没有意义——那是链接本身的问题。

        `NOT_PARTIAL_CONTENT` 也不重试：服务器无视 Range，重试一万次
        还是无视。它是给上层的**回退信号**，不是错误。

        `CONTENT_MISMATCH` 同样不重试：服务器给的数据跟它承诺的
        对不上，重试一万次还是对不上——而且每次重试都要把已经
        下好的部分重新拉一遍，纯浪费带宽。
        """
        return self.kind not in (
            DownloadFailure.FORBIDDEN,
            DownloadFailure.NOT_PARTIAL_CONTENT,
            DownloadFailure.CONTENT_MISMATCH,
            DownloadFailure.DECODING,
        )

    @property
    def is_expired_link(self) -> bool:
        return self.kind == DownloadFailure.FORBIDDEN

    @property
    def category(self) -> ErrorCategory:
        """这个失败对应的规格错误类别。"""
        if self.status is not None and self.kind is DownloadFailure.STATUS:
            return error_for_status(self.status).category
        return _CATEGORY_BY_FAILURE.get(self.kind, ErrorCategory.UNKNOWN)

    def to_task_error(self) -> TaskError:
        """转成对外的错误描述（前端要显示的 category / message）。"""
        if self.kind is DownloadFailure.STATUS and self.status is not None:
            err = error_for_status(self.status, self.detail, self.retry_after)
        else:
            err = TaskError(
                category=self.category,
                detail=self.detail or str(self),
                retryable=self.is_retryable,
                status=self.status,
                retry_after=self.retry_after,
            )
        return err


@dataclass
class ChunkResult:
    """一个分片的传输结果。"""

    chunk_id: int
    bytes_written: int
    elapsed: float
    first_byte_at: float | None = None


def _build_proxy_transport(proxy_url: str) -> httpx.AsyncBaseTransport | None:
    """给 SOCKS 代理造一个传输层。

    httpx 原生只支持 HTTP/HTTPS 代理。SOCKS4/5 需要 `httpx-socks`
    这个第三方包——它没装就返回 None，让调用方给用户一个能看懂的
    错误，而不是一个 ImportError 堆栈。
    """
    try:
        from httpx_socks import AsyncProxyTransport
    except ImportError:
        return None
    scheme = proxy_url.split("://", 1)[0].lower()
    if scheme == "socks5":
        return AsyncProxyTransport.from_url(proxy_url)
    if scheme == "socks4":
        # httpx-socks 的 SOCKS5 传输能兼容 SOCKS4 的大部分用法，
        # 但显式用 socks4 模式更保险。
        try:
            from httpx_socks import AsyncProxyTransport

            return AsyncProxyTransport.from_url(proxy_url, rdns=True)
        except Exception:
            return None
    return None


class HTTPDownloader:
    """共享一个 httpx 客户端的下载器。

    整个引擎共用一个客户端——每个分片各建一个连接池的话，
    每主机连接上限会按客户端计算，分片数再多也并发不起来。
    """

    def __init__(
        self,
        user_agent: str = USER_AGENT,
        ignore_ssl: bool = False,
        max_connections: int = 32,
        idle_timeout: float = 60.0,
        max_retries: int = 5,
        retry_backoff: float = 0.5,
        retry_backoff_max: float = 30.0,
        retry_jitter: float = 0.2,
        probe_retry_budget: float = 8.0,
        chunk_retry_budget: float = 30.0,
        probe_timeout: float = 15.0,
        proxy: str | None = None,
        pool_timeout: float = 30.0,
    ) -> None:
        self.user_agent = user_agent
        self.ignore_ssl = ignore_ssl
        self.idle_timeout = idle_timeout
        #: 等连接池空位的上限（秒）。None = 永不超时 = 会永久挂住。
        self.pool_timeout = pool_timeout
        #: 规格 §1.5：每分片默认 5 次尝试。
        self.max_retries = max_retries
        #: 规格 §1.5：退避基数 500ms，上限 30s，抖动 200ms 量级。
        self.retry_backoff = retry_backoff
        self.retry_backoff_max = retry_backoff_max
        self.retry_jitter = retry_jitter
        #: 探测阶段的总重试时间预算（秒）。
        #: 探测和分片是串行的，两层都按指数退避的话，
        #: 服务器一直返回 503 时用户要等 25 秒才看到失败——
        #: 界面卡在「下载中」半分钟，这不合理。
        self.probe_retry_budget = probe_retry_budget
        #: 单个分片重试的总时间预算（秒）。
        #: 服务器稳定地返回 503 时，7 次指数退避 = 31.5 秒。
        #: 用户看到的是「下载中」卡半分钟然后失败。
        #: 超过预算就直接失败——再等下去也不会成功。
        self.chunk_retry_budget = chunk_retry_budget
        #: 探测请求自己的超时（秒）。
        #:
        #: **不能用客户端的 read 超时**——那个是 60 秒的空闲看门狗，
        #: 给分片用是对的（慢而健康的连接不能被误杀），给探测用是
        #: 灾难：服务器不回响应头时一次探测就挂 60 秒，重试几轮
        #: 下来就是三四分钟静默。用户看到的正是「下载卡在 0 KB/s，
        #: 还没有报错」——实测撞过（用户 2026-10-25 报的）。
        #:
        #: 探测只取 1 个字节，服务器正常情况下秒回。15 秒还不行，
        #: 就是这个地址有问题，重试也没意义。
        self.probe_timeout = probe_timeout
        #: 全局代理（规格 §1.6）。空 = 直连。
        self.proxy = proxy or None
        self._client: httpx.AsyncClient | None = None
        self._max_connections = max_connections
        #: 连接建立时是否随机错开。默认开——避免所有分片同时建连接
        #: 撞上服务器的 listen backlog。
        self._jitter_connections = True

    # -- 客户端 ------------------------------------------------------

    def _build_client(self) -> httpx.AsyncClient:
        limits = httpx.Limits(
            max_connections=self._max_connections,
            max_keepalive_connections=self._max_connections,
        )
        # read 超时是「两个读之间」的超时，不是总时长上限。
        # 慢而健康的连接会不断来数据，read 永远不会超时。
        #
        # **pool 不能是 None。** `pool=None` 的意思是「等连接池的空位
        # 时永不超时」——池被占满（分片数撞上 max_connections、或者
        # 上一批连接还没回收）就会**永久挂起**，既不报错也不重试，
        # 用户看到的就是「一直卡在连接中」。
        # 给它一个上限：等不到空位就报超时，按可重试错误处理。
        timeout = httpx.Timeout(
            connect=10.0,
            read=self.idle_timeout,
            write=30.0,
            pool=self.pool_timeout,
        )
        verify: bool | ssl.SSLContext = True
        if self.ignore_ssl:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            verify = context

        kwargs: dict = {
            "limits": limits,
            "timeout": timeout,
            "follow_redirects": True,
            "verify": verify,
            "headers": {"User-Agent": self.user_agent},
        }

        if self.proxy:
            scheme = self.proxy.split("://", 1)[0].lower()
            if scheme in ("socks4", "socks5"):
                transport = _build_proxy_transport(self.proxy)
                if transport is None:
                    raise RuntimeError(
                        f"SOCKS 代理需要 httpx-socks 包才能用：{self.proxy}"
                    )
                kwargs["transport"] = transport
            else:
                kwargs["proxy"] = self.proxy

        return httpx.AsyncClient(**kwargs)

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def set_ignore_ssl(self, value: bool) -> None:
        """开关 SSL 校验。

        httpx 的 verify 在建客户端时固定，所以换开关要重建客户端。
        在途的分片会看到连接错误，引擎把它们当作可重试错误处理。
        """
        if self.ignore_ssl == value:
            return
        self.ignore_ssl = value
        old = self._client
        self._client = None
        if old is not None:
            # 不能直接 close——在途请求会炸。标记一下让它们自然结束。
            old._sdm_superseded = True  # type: ignore[attr-defined]

    def set_user_agent(self, value: str | None) -> None:
        """换全局 User-Agent（设置项里改的那个）。

        空的字符串 = 用内置默认 UA。用户清空输入框就是「恢复默认」，
        不是「发一个空的 UA」——空 UA 会被不少站点直接拒。

        **站点规则的 UA 仍然优先。** 规则是用户为某个主机显式配置的
        「跟这个站点说话的方式」，它覆盖全局设置；全局设置再覆盖
        内置默认值。优先级写死在 `probe` / `_stream_chunk` 的头合并里。
        """
        new = (value or "").strip() or USER_AGENT
        if new == self.user_agent:
            return
        self.user_agent = new
        old = self._client
        self._client = None
        if old is not None:
            # UA 写在客户端的默认头里，所以改 UA 等于换客户端。
            # 在途的分片会看到连接错误，引擎把它们当作可重试错误处理。
            old._sdm_superseded = True  # type: ignore[attr-defined]

    def set_proxy(self, value: str | None) -> None:
        """换全局代理。跟 SSL 开关一样要重建客户端。"""
        new = value or None
        if new == self.proxy:
            return
        self.proxy = new
        old = self._client
        self._client = None
        if old is not None:
            old._sdm_superseded = True  # type: ignore[attr-defined]

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # -- 探测 --------------------------------------------------------

    async def probe(
        self,
        url: str,
        extra_headers: dict[str, str] | None = None,
    ) -> ContentProbe:
        """探测目标，失败时重试。

        探测请求也**必须**带站点规则的头——有的站点探测阶段就
        403（需要 Referer 或特定 UA），规则不生效的话任务连
        启动都做不到。规则在重试循环外算一次：规则不会在一次
        探测的过程中变，重算只是白读文件。

        `extra_headers` 是任务级的头（比如任务的 referer）。
        **站点规则优先**——规则是用户在设置里为该主机显式配置的
        「跟这个站点说话的方式」（需要 Referer、特殊 UA、登录
        Cookie），任务级 referer 只是调用方的一次性补充。
        规则优先的代价是任务 referer 被规则盖掉时用户看不到提示，
        换来的是「配了规则就一定按规则走」这个可预期的语义。

        头的优先级（从低到高）：
        1. 下载器自己的默认 UA（`self.user_agent`）
        2. 任务级的头（`extra_headers`）
        3. 站点规则的头——规则里的 UA **覆盖**默认 UA，不是追加

        探测请求也失败——CDN 短暂返回 503、代理瞬断、
        DNS 抖动都会命中。如果探测一失败就把整个任务判死，
        用户看到的是「下载失败」，而实际上等一秒重试就好了。

        403/410/451 不重试——那是链接本身失效，重试没意义，
        要交给上层进「等待新链接」状态。

        **预算是总时间预算，请求本身耗的时间也算在里面。**
        早先只统计退避的 sleep，服务器不响应时每次请求都要
        等满 60 秒（客户端的 read 超时），5 次重试 = 5 分钟
        静默——预算形同虚设。现在每个请求用独立的短超时
        （`probe_timeout`），并且把请求耗时累加进预算，
        预算到了就停，最长也就二十几秒。
        """
        rule_headers = await asyncio.to_thread(self.apply_site_rule, url, {})
        request_headers = merge_headers(
            {"User-Agent": self.user_agent},
            extra_headers or {},
            rule_headers,
        )
        # 探测专用超时。写在请求上，不动共享客户端——
        # 客户端的 read 超时是分片的空闲看门狗。
        probe_timeout = httpx.Timeout(
            connect=10.0,
            read=self.probe_timeout,
            write=10.0,
            # 探测等连接池空位也要有上限——探测必须很快出结果，
            # 排在分片后面等半天没有意义。
            pool=self.probe_timeout,
        )
        last_error: Exception | None = None
        elapsed = 0.0
        for attempt in range(self.max_retries + 1):
            attempt_started = time.monotonic()
            try:
                return await sniffer_probe(
                    url,
                    client=self.client,
                    extra_headers=request_headers,
                    timeout=probe_timeout,
                )
            except ProbeError as err:
                # **不可重试的失败直接抛出去。**
                #
                # 404（文件不存在）、416（服务器不认这个 Range）、
                # 磁力链接这类地址——重试多少次结果都一样。硬重试
                # 只会让用户多等 7.5 秒，并在日志里刷一串「第 N 次
                # 失败」，看起来像「所有重试都失败」，其实一次都
                # 不会成功。
                #
                # 403/410/451（链接失效）也走这条路——它们本来就不
                # 重试，交给上层进「等待新链接」状态。
                if not err.is_retryable:
                    raise
                last_error = err
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError) as err:
                last_error = err
            except Exception as err:
                last_error = err

            # **请求本身耗掉的时间也算预算。** 只算退避 sleep 的
            # 时候，服务器不响应的情况下预算永远花不完，
            # 一次探测能静默好几分钟。
            elapsed += time.monotonic() - attempt_started

            if attempt >= self.max_retries:
                break
            # 线性退避，且不超过总预算。
            delay = min(0.5 * (attempt + 1), 2.0)
            if elapsed + delay > self.probe_retry_budget:
                break
            # **这条用 warning 而不是 notice。**
            # 用户常把日志级别调到 warning（默认是 info），
            # 那时 notice 级的日志一条都看不到——「探测第 N 次失败」
            # 恰恰是排查「卡在连接中」最关键的一条线索。
            log.warning(
                CAT_NET,
                f"探测 {url} 第 {attempt + 1} 次失败 ({last_error}) — {delay:.1f}s 后重试",
            )
            await asyncio.sleep(delay)
            elapsed += delay

        if isinstance(last_error, ProbeError):
            raise last_error
        raise _describe_probe_failure(last_error) if last_error else ProbeError("probe failed")

    async def fetch_range(
        self,
        url: str,
        start: int,
        end: int,
        extra_headers: dict[str, str] | None = None,
    ) -> bytes:
        """取一段字节（闭区间 `[start, end]`），不做任何落盘。

        **存在的理由：判断「换的新链接是不是同一个文件」，
        最硬的证据是逐字节比对，而不是长度或 ETag。**

        - 长度只能说明「大小一样」，1MB 的安装包和 1MB 的壁纸长度一样。
        - ETag 靠不住：很多 CDN 每次请求都换 ETag（弱 ETag 场景），
          有的还按节点给不同的 ETag。同一个文件两次请求 ETag 不同
          是常态，拿 ETag 判「换了文件」会误杀。
        - 字节比对是内容级的：同样的偏移取同样的字节，逐字节相同
          就说明是两个链接指向同一份内容。

        只取一小段（比如头 4KB）——够用来认亲，又不至于把整个
        文件拉一遍。
        """
        if end < start:
            return b""
        rule_headers = await asyncio.to_thread(self.apply_site_rule, url, {})
        request_headers = merge_headers(extra_headers or {}, rule_headers)
        request_headers = merge_headers(
            request_headers, {"Range": f"bytes={start}-{end}"}
        )
        # **打个标记**，让服务器（和测试）能认出这是「取样」而不是
        # 「下载」。取样读的也是文件内容，但它是校验用的，不该被
        # 当成下载字节统计——不然「新链接有没有重下旧字节」这种
        # 断言会被自己的校验请求搅浑。
        request_headers = merge_headers(request_headers, {"X-SDM-Verify": "1"})
        try:
            request = self.client.build_request("GET", url, headers=request_headers)
            response = await self.client.send(request, stream=True)
        except httpx.TimeoutException as err:
            raise DownloadError(DownloadFailure.NETWORK, f"timeout: {err}") from err
        except httpx.HTTPError as err:
            raise DownloadError(DownloadFailure.NETWORK, str(err)) from err

        status = response.status_code
        if status in (403, 410, 451):
            await response.aclose()
            raise DownloadError(
                DownloadFailure.FORBIDDEN,
                f"HTTP {status}",
                status=status,
            )
        if status == 416:
            await response.aclose()
            # 范围不合法——请求的区间超出了文件长度。
            raise DownloadError(
                DownloadFailure.STATUS,
                "HTTP 416 请求范围不合法",
                status=status,
            )
        if status >= 400:
            await response.aclose()
            raise DownloadError(DownloadFailure.HTTP_ERROR, f"HTTP {status}", status=status)

        # **只读要的那几字节，读完就断开。**
        #
        # 服务器可能无视 Range 把整个文件发回来（返回 200）。这时候
        # 一次性 `response.content` 会把整个文件读进内存——取样是为了
        # 认亲，不是为了下载，一个 4GB 的文件不该为了取 4KB 把内存
        # 撑爆。所以按块读、读够就停。
        expected = end - start + 1
        body = bytearray()
        try:
            async for block in response.aiter_bytes(chunk_size=64 * 1024):
                body.extend(block)
                if len(body) >= expected:
                    break
        except httpx.HTTPError as err:
            raise DownloadError(DownloadFailure.NETWORK, str(err)) from err
        finally:
            await response.aclose()

        # 服务器无视了 Range，直接把整个文件发回来了——那就取前
        # `expected` 字节，跟「它返回的就是我们要的那段」
        # 保持一致（否则比对的是文件头而不是目标偏移）。
        if len(body) > expected:
            body = body[:expected]
        return bytes(body)

    # -- 分片传输 ----------------------------------------------------

    async def download_chunk(
        self,
        url: str,
        chunk: Chunk,
        assembler: FileAssembler,
        progress: Callable[[int], None] | None = None,
        headers: dict[str, str] | None = None,
        limiter: SpeedLimiter | None = None,
        use_range: bool = True,
    ) -> ChunkResult:
        """把一个分片下完并落盘。

        失败时按可重试性重试；每次重试从**已落盘字节**继续，
        所以断线只损失一次重连，不损失已下载的数据。

        `progress` 收到的是**增量**（本次写入的字节数），不是累计值。
        上层把所有分片的增量求和，得到文件级进度——这个总量只会
        单调上升。传累计值会让进度虚高好几倍。
        """
        last_error: DownloadError | None = None
        waited = 0.0
        for attempt in range(self.max_retries + 1):
            current = asyncio.current_task()
            if current is not None and current.cancelled():
                raise asyncio.CancelledError()

            already = assembler.bytes_written(chunk.id)
            start = chunk.start + already
            if start >= chunk.end:
                return ChunkResult(chunk_id=chunk.id, bytes_written=already, elapsed=0.0)

            # **重试不能从头开始。**
            #
            # 已经落盘的字节是对的（它们来自服务器的正确区间），
            # 从头重下会把它们**覆盖**成同一段数据的另一次拷贝——
            # 分片文件里出现「重放」，合并出来的文件大小正常、
            # 内容全错。
            #
            # `already` 就是「已经落盘了多少」，`start` 由此推出。
            # 这里的检查是**第二道闸**：万一上游把 `already` 算错
            # （比如分片被拆分后重新调度），宁可明确失败，也不
            # 让它静默覆盖已有数据。
            if attempt > 0:
                on_disk = assembler.chunk_bytes_on_disk(chunk.id)
                if on_disk != already:
                    raise DownloadError(
                        DownloadFailure.CONTENT_MISMATCH,
                        f"重试时盘上字节数（{on_disk}）跟记录的对不上"
                        f"（{already}）——不能从头重下，会覆盖已下载的数据",
                    )

            try:
                return await self._stream_chunk(
                    url=url,
                    chunk=chunk,
                    assembler=assembler,
                    start=start,
                    progress=progress,
                    headers=headers,
                    limiter=limiter,
                    use_range=use_range,
                )
            except DownloadError as err:
                if not err.is_retryable:
                    raise
                last_error = err
            except httpx.ConnectError as err:
                # 连接建立失败是「瞬时」问题——服务器的 listen backlog
                # 溢出、或者对端刚重启。立刻重试通常就成功。
                # 走 1s/2s/4s 的退避会让用户看到「几个分片卡住不动，
                # 过好几秒才动」。所以连接类错误用短退避。
                #
                # 必须放在 NetworkError 前面——ConnectError 是它的子类。
                last_error = DownloadError(DownloadFailure.NETWORK, str(err))
                delay = 0.15 * (attempt + 1)
                if waited + delay > self.chunk_retry_budget:
                    break
                await asyncio.sleep(delay)
                waited += delay
                continue
            except httpx.RemoteProtocolError as err:
                # 对端在传输中途断了（"peer closed connection without
                # sending complete message body"）。这是可恢复的——
                # 已落盘的字节还在，重连后从断点继续。
                # 走 1s/2s/4s 的长退避会让用户以为卡死了。
                last_error = DownloadError(DownloadFailure.NETWORK, str(err))
                delay = 0.3 * (attempt + 1)
                if waited + delay > self.chunk_retry_budget:
                    break
                await asyncio.sleep(delay)
                waited += delay
                continue
            except (httpx.TimeoutException, httpx.NetworkError) as err:
                last_error = DownloadError(DownloadFailure.NETWORK, str(err))
            except asyncio.CancelledError:
                raise
            except Exception as err:
                last_error = DownloadError(DownloadFailure.UNKNOWN, str(err))

            # 指数退避 + 抖动（规格 §1.5）。抖动是必要的：所有分片
            # 同时失败时，没有抖动它们会**同时**重试，等于又一波
            # 冲击服务器，容易把刚恢复的服务再打挂。
            backoff = min(
                self.retry_backoff * (2**attempt),
                self.retry_backoff_max,
            )
            backoff += random.uniform(0, self.retry_jitter)
            # 限流：服务器给了 Retry-After 就听它的。
            if last_error.retry_after:
                backoff = max(backoff, last_error.retry_after)
            if waited + backoff > self.chunk_retry_budget:
                log.notice(
                    CAT_CHUNK,
                    f"chunk {chunk.id} 重试预算用尽（{waited:.1f}s / {self.chunk_retry_budget:.0f}s），放弃",
                )
                break
            log.notice(
                CAT_CHUNK,
                f"chunk {chunk.id} 第 {attempt + 1} 次失败 ({last_error}) — {backoff:.1f}s 后重试",
            )
            await asyncio.sleep(backoff)
            waited += backoff

        raise last_error or DownloadError(DownloadFailure.UNKNOWN, f"chunk {chunk.id} 重试耗尽")

    @staticmethod
    def _content_range_matches(header: str, want_start: int, want_end: int) -> bool:
        """校验 Content-Range 是不是我们要的区间。

        格式：`bytes {start}-{end}/{total}`。
        缺头、格式不对、区间不符都算不匹配。
        """
        if not header:
            return False
        value = header.strip()
        if not value.lower().startswith("bytes "):
            return False
        spec = value[6:].strip()
        span = spec.split("/", 1)[0].strip()
        if "-" not in span:
            return False
        start_str, _, end_str = span.partition("-")
        try:
            got_start = int(start_str)
            got_end = int(end_str)
        except ValueError:
            return False
        return got_start == want_start and got_end == want_end

    async def _stream_chunk(
        self,
        url: str,
        chunk: Chunk,
        assembler: FileAssembler,
        start: int,
        progress: Callable[[int], None] | None,
        headers: dict[str, str] | None,
        limiter: SpeedLimiter | None,
        use_range: bool = True,
    ) -> ChunkResult:
        """一次流式传输尝试。"""
        # 站点规则：按主机名匹配，命中就叠加 Referer / User-Agent /
        # Cookie / Basic Auth。
        #
        # 不加这步的话，站点规则整个功能是死的——规则存着、界面能编辑，
        # 但下载请求从来不带上这些头。需要 Referer 才能下的站点会直接
        # 403。
        #
        # 规则优先于任务级的头：规则是用户为该主机显式配置的全局设置，
        # 任务级 referer 只是一次性补充。
        #
        # 规则读取要碰磁盘（site_rules.json），Basic Auth 还要查钥匙串
        # （macOS 上是 `security` 子进程），放线程里做，别把事件循环
        # 卡住。
        rule_headers = await asyncio.to_thread(self.apply_site_rule, url, {})
        request_headers = merge_headers(headers or {}, rule_headers)
        # Range 最后写死：分片的区间是按已落盘字节算出来的，
        # 外部传进来的头不能改它。
        #
        # **请求的结束位置要在发请求之前定格。** 分片可能在请求发出
        # 之后被拆短（动态分段），那时候 `chunk.end` 已经不是这次
        # 请求要的范围了——用它去校验服务器回来的 Content-Range
        # 会误判成「区间不匹配」，把好端端的下载判成失败。
        requested_end = chunk.end - 1
        if use_range:
            request_headers = merge_headers(
                request_headers, {"Range": f"bytes={start}-{requested_end}"}
            )

        # 分片文件在字节流动前就打开并定位好。
        #
        # **必须 `resume=False`：这里打开的分片一定是「从头开始」的。**
        #
        # 流程层已经把「要续传的分片」按 `chunk.bytes_done > 0` 打开过了
        #（见 `DownloadFlow._run_once`）。走到这里还没被打开过的分片，
        # 只可能是**动态分段新切出来的那一片**——它对应的是一个全新的
        # 区间，盘上同名文件里残留的字节属于**上一次下载**，跟这次
        # 没关系。
        #
        # 按 `resume=True` 打开的后果：残留的旧字节被当成「已经下好的
        # 字节」，新字节接在旧字节后面 → 合并出来的是「旧内容 + 新
        # 内容」的拼接，**长度看着差不多，内容全乱**。
        #
        # **Windows 上这个坑必现**：上一次下载的分片文件删不掉（文件
        # 被句柄占着，`unlink` 抛 PermissionError），残留活到下一次
        # 下载。macOS 能删掉残留，所以只在 Windows 上复现。
        assembler.open_chunk(
            chunk.id,
            chunk.start,
            resume=False,
            # 记下「这个分片应该有多长」——合并前要拿它核对盘上的
            # 分片文件，长度不对就是坏数据。
            end_offset=chunk.end,
        )

        started = time.monotonic()
        first_byte_at: float | None = None
        written = 0

        # 写入上限的取值函数。分片被拆分时 `chunk.end` 会变小，
        # 这里每次写之前都在锁内读一次当前值。
        def _chunk_limit() -> int | None:
            return chunk.end

        try:
            # 所有分片同时发起连接会撞服务器的 listen backlog，
            # 也会被某些 CDN 判定为异常流量。随机错开几毫秒就能避开，
            # 对总耗时的影响可以忽略。
            if self._jitter_connections:
                await asyncio.sleep(random.uniform(0, 0.02))
            request = self.client.build_request("GET", url, headers=request_headers)
            response = await self.client.send(request, stream=True)
        except httpx.TimeoutException as err:
            raise DownloadError(DownloadFailure.NETWORK, f"timeout: {err}") from err
        except httpx.TooManyRedirects as err:
            raise DownloadError(DownloadFailure.TOO_MANY_REDIRECTS, str(err)) from err
        except httpx.HTTPError as err:
            raise DownloadError(DownloadFailure.NETWORK, str(err)) from err

        try:
            status = response.status_code
            if status in (403, 410, 451):
                raise DownloadError(
                    DownloadFailure.FORBIDDEN,
                    f"HTTP {status}",
                    status=status,
                )
            if status == 429:
                raise DownloadError(
                    DownloadFailure.STATUS,
                    f"HTTP {status}",
                    status=status,
                    retry_after=parse_retry_after(response.headers.get("retry-after")),
                )
            if status >= 500:
                raise DownloadError(DownloadFailure.STATUS, f"HTTP {status}", status=status)
            if status not in (200, 206):
                raise DownloadError(DownloadFailure.STATUS, f"HTTP {status}", status=status)
            if use_range and status == 200:
                # 服务器无视了 Range（规格 §1.2.4）。
                #
                # 这不是「重试一下就好」的错误——重试一万次它还是无视。
                # 这是给上层的**回退信号**：改成单流下载，不分块。
                #
                # 必须**一个字节都不写**：200 的响应体是整个文件，
                # 写进分片就废了。
                raise DownloadError(
                    DownloadFailure.NOT_PARTIAL_CONTENT,
                    "服务器不支持分块请求（对 Range 请求返回 200）",
                    status=status,
                )
            if status == 206:
                # 服务器声称支持 Range，但返回的区间跟我们要的对不上。
                # 典型场景：CDN 边缘节点缓存了别的 Range 响应，
                # 或者代理把整个文件塞回来了。
                # 这种情况下把数据写进分片，文件就是错的——
                # 而且错得很隐蔽：大小看着差不多，内容全乱。
                # 所以必须当场拒绝，一个字节都不写。
                #
                # 比的是**这次请求实际要的范围**，不是分片现在的
                # 结束位置——分片可能在请求发出之后被拆短。
                #
                # **这一段必须排在「单流却收到 206」的检查前面。**
                # 单流是分块下载失败之后的退路，服务器这时候还
                # 返回错的区间——真正的问题是「Content-Range 对不上」，
                # 不是「单流收到了 206」。先说清楚根因，用户看到的
                # 错误信息才指向正确的地方。
                content_range = response.headers.get("content-range", "")
                if not self._content_range_matches(content_range, start, requested_end):
                    # **硬失败，不回退单流。**
                    #
                    # 服务器给的数据不是它承诺的那一段——同一个服务器
                    # 对整个文件的响应同样可能是错的。退回单流只是
                    # 换个姿势再被它骗一次。
                    #
                    # 这是「文件大小正常但内容全错」那种损坏的直接
                    # 来源：分片拿到的全是文件开头，合并之后大小
                    # 完美、内容全乱。实测（2026-10-30）：用户下的
                    # 4 个文件全部损坏，每个分片的前 64KB 都等于
                    # 文件开头。
                    raise DownloadError(
                        DownloadFailure.CONTENT_MISMATCH,
                        f"Content-Range 不匹配：请求 bytes={start}-{requested_end}，"
                        f"收到 {content_range or '(缺失)'}",
                    )
            if not use_range and status == 206:
                # 单流模式却拿到 206——服务器主动切了分块。
                # 206 的 body 只是文件的一部分，按单流写会得到残缺文件。
                #
                # Content-Range 已经对上了（上面查过），说明服务器
                # 确实按范围给了数据——但单流模式不能处理这种情况，
                # 因为它按「整个文件」来写。
                raise DownloadError(
                    DownloadFailure.NOT_PARTIAL_CONTENT,
                    "单流请求却收到 206 响应",
                    status=status,
                )

            log.notice(
                CAT_CHUNK,
                f"chunk {chunk.id} GET {url} range=bytes={start}-{requested_end} status={status}",
            )

            # **本次请求应该收到的字节数。**
            #
            # **必须按「这次请求的范围」算，不能按分片当前区间算。**
            #
            # 分片区间会变——动态分段会在传输途中把它拆短。
            # 但**服务器是按我们发出去的 Range 头发的**，它不知道
            # 我们内部把分片拆了。所以「应该收到多少」永远由
            # **请求头**决定，不由内部状态决定。
            #
            # 拿 `chunk.end - chunk.start` 算会出大问题：分片被拆短
            # 之后这个数变小，而服务器照样按原来的范围发——收到的
            # 远多于「应该收到的」，于是校验被误判成「服务器多发」。
            #
            # `start` 已经含了续传的偏移（`chunk.start + already`），
            # 所以 `requested_end - start + 1` 正好是这次请求的字节数。
            expected_bytes = requested_end - start + 1
            received = 0
            truncated = False

            async for block in response.aiter_bytes(chunk_size=64 * 1024):
                if first_byte_at is None:
                    first_byte_at = time.monotonic() - started
                if limiter is not None and limiter.is_enabled():
                    await limiter.consume(len(block))

                # **服务器实际发来的字节数。**
                #
                # **必须统计这个，不能统计写入量。** 写入量会被
                # `append_limited` 按分片上限截断——服务器多发的那部分
                # 正好被截掉，写入量看起来「正好是区间长度」，校验就
                # 白做了。服务器发来的字节数才是它真正承诺的东西。
                received += len(block)

                # **写入是有上限的。** 动态分段会在下载途中把在途分片
                # 对半拆开，分片的区间随之缩短。传**取值函数**而不是
                # 取好的数值——拆分线程可能刚刚把它改小了，而取值
                # 函数是在分片锁内调用的，读和写不会错开。
                #
                # 超出上限的字节属于新分片的区间，留着就会重叠，
                # 合并出来的文件是坏的（而且大小看着正常，极难发现）。
                n, truncated = assembler.append_limited(block, chunk.id, _chunk_limit)
                if n:
                    written += n
                    if progress is not None:
                        # 报**增量**，不是累计值。
                        progress(n)
                if truncated:
                    # 分片被拆短了，这一段到此为止。
                    # 不算失败——剩下的字节归新分片。
                    log.notice(
                        CAT_CHUNK,
                        f"chunk {chunk.id} 因分片拆分停止于 {chunk.start + written}",
                    )
                    break

            # **校验实际收到的字节数。**
            #
            # 这是**文件损坏最隐蔽的一条路**：服务器的头完全正常
            # （206 + Content-Range 对得上），但**响应体不是它承诺
            # 的那一段**——CDN 边缘节点缓存了别的 Range 响应、代理
            # 把整个文件塞了回来。
            #
            # 后果：每个分片拿到的都是**文件开头**的内容（而不是
            # 自己区间的内容），写进分片、合并之后——**文件大小完全
            # 正常，内容却全是错的**。用户看到的现象是「文件损坏但
            # 大小对」，最难查的那种。实测（2026-10-30）：用户下的
            # 4 个文件全部损坏，7z/apk/dmg 三个格式的校验工具一致
            # 报「数据已损坏」，而每个分片的前 64KB 都等于文件开头。
            #
            # 所以必须当场比对。
            #
            # **分三种情况：**
            #
            # - **收到的比请求的多** → 服务器无视了 Range，把别的
            #   内容塞过来了。这是**服务器的问题**，重试没用
            #   （重试一万次它还是无视）——直接失败。
            #
            # - **收到的比请求的少，但 `truncated` 为真** → **分片
            #   被拆了**，这是合法的提前结束。剩下的字节归新分片。
            #
            #   **这一条是关键，判错它就会毁掉整个下载。**
            #
            #   动态分段会在传输途中把在途分片对半拆开。写入被上限
            #   挡住 → `truncated=True` → 传输提前结束。这时候收到
            #   的字节数**必然**少于请求量（服务器还在按原始 Range
            #   发，我们不收了）。
            #
            #   把它当成「连接中断」会怎样？抛可重试错误 → 传输
            #   重试 → **重试从头开始**（`already` 算的是分片已写
            #   字节数，但重试时的写入起点和已有数据对不上）→ 新写的
            #   字节**覆盖**掉已经下好的部分 → 分片文件里出现
            #   「同一段数据从头重放」→ 合并出来的文件大小正常、
            #   内容全错。
            #
            #   实测（2026-10-30）：13MB 的 dmg 下载，7 个分片内容
            #   全错，错位量正好是 64KB（一个数据块）的整数倍。
            #
            # - **收到的比请求的少，且 `truncated` 为假** → 连接
            #   中途断了。httpx 在 chunked 传输中断时**不一定抛
            #   异常**，body 就是短的。这是**网络问题**，正常做法是
            #   重试续传（已落盘的字节还在，重连后从断点接着下）。
            if received > expected_bytes:
                # 服务器给了**更多**——它无视了 Range。
                #
                # 硬失败，不回退单流：这跟 Content-Range 不匹配
                # 是同一类问题——服务器给的不是我们要的那一段。
                raise DownloadError(
                    DownloadFailure.CONTENT_MISMATCH,
                    f"服务器无视了 Range：请求 bytes={start}-{requested_end}"
                    f"（{expected_bytes} 字节），实际收到 {received} 字节",
                )
            if received < expected_bytes and not truncated:
                # 收到的**少了**，而且不是分片被拆导致的提前结束
                # ——连接中途断了。重试续传。
                raise DownloadError(
                    DownloadFailure.NETWORK,
                    f"连接提前断开：请求 bytes={start}-{requested_end}"
                    f"（{expected_bytes} 字节），只收到 {received} 字节",
                )

            elapsed = time.monotonic() - started
            log.notice(CAT_CHUNK, f"chunk {chunk.id} 完成 bytes={written} elapsed={elapsed:.2f}s")
            return ChunkResult(
                chunk_id=chunk.id,
                bytes_written=written,
                elapsed=elapsed,
                first_byte_at=first_byte_at,
            )
        finally:
            await response.aclose()

    # -- 站点规则 ----------------------------------------------------

    def apply_site_rule(self, url: str, headers: dict[str, str]) -> dict[str, str]:
        """套用匹配到的站点规则（Referer / UA / Cookie / BasicAuth）。

        规则**覆盖**已有的同名头——UA 尤其重要：规则里填了 UA 就是
        「这个站点只认这个 UA」，追加在默认 UA 后面等于没填。

        头名大小写不敏感（`User-Agent` 和 `user-agent` 是同一个
        头），合并走 `merge_headers`，不会留下两个 UA 让服务器挑花眼。

        没有匹配的规则就原样返回。规则里没填的字段不动已有的头——
        规则只做叠加，不做清空。

        **同步方法**——读 JSON 文件、查钥匙串都是阻塞操作。
        async 路径上要用 `asyncio.to_thread` 包一层，别把事件循环卡住。
        """
        from ..site_rules import SiteRuleStore

        store = SiteRuleStore.shared()
        rule = store.match(url)
        if rule is None:
            return headers
        # apply_to_headers 会就地改 headers，所以给它一份副本。
        rule_headers = store.apply_to_headers(rule, {})
        return merge_headers(headers, rule_headers)


def _describe_probe_failure(err: Exception) -> ProbeError:
    """把传输层的异常翻译成一句用户看得懂的话。

    原来直接把 httpx 的异常往上抛，界面上显示的是
    `ReadTimeout` 这种英文类名——用户看不出是超时还是断网。
    归一成 `ProbeError` 之后，分类（网络）、可重试性和文案
    跟 HTTP 状态码那条路径是同一套。
    """
    if isinstance(err, httpx.TimeoutException):
        return ProbeError("连接服务器超时")
    if isinstance(err, httpx.ConnectError):
        return ProbeError("连不上服务器")
    if isinstance(err, httpx.NetworkError):
        return ProbeError("网络错误")
    if isinstance(err, httpx.HTTPError):
        return ProbeError("HTTP 请求失败")
    return ProbeError(str(err) or type(err).__name__)
