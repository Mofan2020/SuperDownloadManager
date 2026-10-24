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

另外三个要点：
- 重连时 Range 头从**已落盘字节数**继续，不是从分片起点重来。
- 403/410/451 归为「链接失效」类错误，不重试——重试只是浪费时间。
- 忽略 SSL 验证是显式开关，默认关闭。
"""

from __future__ import annotations

import asyncio
import enum
import random
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from ..logging_setup import CAT_CHUNK, CAT_NET, get_logger
from .content_sniffer import USER_AGENT, ContentProbe, ProbeError, probe as sniffer_probe
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
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return self.value


class DownloadError(Exception):
    """带失败分类的下载错误。"""

    def __init__(self, kind: DownloadFailure, detail: str = "", status: int | None = None) -> None:
        super().__init__(f"{kind.value}: {detail}" if detail else kind.value)
        self.kind = kind
        self.detail = detail
        self.status = status

    @property
    def is_retryable(self) -> bool:
        """链接失效类错误重试没有意义——那是链接本身的问题。"""
        return self.kind not in (
            DownloadFailure.FORBIDDEN,
            DownloadFailure.NOT_PARTIAL_CONTENT,
            DownloadFailure.DECODING,
        )

    @property
    def is_expired_link(self) -> bool:
        return self.kind == DownloadFailure.FORBIDDEN


@dataclass
class ChunkResult:
    """一个分片的传输结果。"""

    chunk_id: int
    bytes_written: int
    elapsed: float
    first_byte_at: float | None = None


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
        max_retries: int = 6,
        retry_backoff: float = 0.5,
        probe_retry_budget: float = 8.0,
        chunk_retry_budget: float = 30.0,
    ) -> None:
        self.user_agent = user_agent
        self.ignore_ssl = ignore_ssl
        self.idle_timeout = idle_timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
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
        timeout = httpx.Timeout(
            connect=10.0,
            read=self.idle_timeout,
            write=30.0,
            pool=None,
        )
        verify: bool | ssl.SSLContext = True
        if self.ignore_ssl:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            verify = context
        return httpx.AsyncClient(
            limits=limits,
            timeout=timeout,
            follow_redirects=True,
            verify=verify,
            headers={"User-Agent": self.user_agent},
        )

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

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    # -- 探测 --------------------------------------------------------

    async def probe(self, url: str) -> ContentProbe:
        """探测目标，失败时重试。

        探测请求也���能失败——CDN 短暂返回 503、代理瞬断、
        DNS 抖动都会命中。如果探测一失败就把整个任务判死，
        用户看到的是「下载失败」，而实际上等一秒重试就好了。

        403/410/451 不重试——那是链接本身失效，重试没意义，
        要交给上层进「等待新链接」状态。
        """
        last_error: Exception | None = None
        elapsed = 0.0
        for attempt in range(self.max_retries + 1):
            try:
                return await sniffer_probe(url, client=self.client)
            except ProbeError as err:
                if err.is_expired_link:
                    raise
                last_error = err
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError) as err:
                last_error = err
            except Exception as err:  # noqa: BLE001
                last_error = err

            if attempt >= self.max_retries:
                break
            # 线性退避，且不超过总预算。
            delay = min(0.5 * (attempt + 1), 2.0)
            if elapsed + delay > self.probe_retry_budget:
                break
            log.notice(
                CAT_NET,
                f"探测 {url} 第 {attempt + 1} 次失败 ({last_error}) — {delay:.1f}s 后重试",
            )
            await asyncio.sleep(delay)
            elapsed += delay

        raise last_error if last_error else ProbeError("probe failed")

    # -- 分片传输 ----------------------------------------------------

    async def download_chunk(
        self,
        url: str,
        chunk: Chunk,
        assembler: FileAssembler,
        progress: Callable[[int], None] | None = None,
        headers: dict[str, str] | None = None,
        limiter: SpeedLimiter | None = None,
    ) -> ChunkResult:
        """把一个分片下完并落盘。

        失败时按可重试性重试；每次重试从**已落盘字节**继续，
        所以断线只损失一次重连，不损失已下载的数据。
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

            try:
                return await self._stream_chunk(
                    url=url,
                    chunk=chunk,
                    assembler=assembler,
                    start=start,
                    progress=progress,
                    headers=headers,
                    limiter=limiter,
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
            except Exception as err:  # noqa: BLE001
                last_error = DownloadError(DownloadFailure.UNKNOWN, str(err))

            backoff = self.retry_backoff * (2**attempt)
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
    ) -> ChunkResult:
        """一次流式传输尝试。"""
        request_headers = dict(headers or {})
        request_headers["Range"] = f"bytes={start}-{chunk.end - 1}"

        # 分片文件在字节流动前就打开并定位好。
        assembler.open_chunk(chunk.id, chunk.start)

        started = time.monotonic()
        first_byte_at: float | None = None
        written = 0

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
            if status >= 500 or status == 429:
                raise DownloadError(DownloadFailure.STATUS, f"HTTP {status}", status=status)
            if status not in (200, 206):
                raise DownloadError(DownloadFailure.STATUS, f"HTTP {status}", status=status)
            if status == 200 and start > chunk.start:
                # 服务器无视了 Range。续传请求拿到的是整个文件，
                # 直接写进去会污染分片。
                raise DownloadError(DownloadFailure.NOT_PARTIAL_CONTENT, "server ignored Range")
            if status == 206:
                # 服务器声称支持 Range，但返回的区间跟我们要的对不上。
                # 典型场景：CDN 边缘节点缓存了别的 Range 响应，
                # 或者代理把整个文件塞回来了。
                # 这种情况下把数据写进分片，文件就是错的——
                # 而且错得很隐蔽：大小看着差不多，内容全乱。
                # 所以必须当场拒绝，一个字节都不写。
                content_range = response.headers.get("content-range", "")
                if not self._content_range_matches(content_range, start, chunk.end - 1):
                    raise DownloadError(
                        DownloadFailure.NOT_PARTIAL_CONTENT,
                        f"Content-Range 不匹配：请求 bytes={start}-{chunk.end - 1}，"
                        f"收到 {content_range or '(缺失)'}",
                    )

            log.notice(
                CAT_CHUNK,
                f"chunk {chunk.id} GET {url} range=bytes={start}-{chunk.end - 1} status={status}",
            )

            async for block in response.aiter_bytes(chunk_size=64 * 1024):
                if first_byte_at is None:
                    first_byte_at = time.monotonic() - started
                if limiter is not None and limiter.is_enabled():
                    await limiter.consume(len(block))
                written += assembler.append(block, chunk.id)
                if progress is not None:
                    progress(written)

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

    def apply_site_rule(self, url: str, headers: dict[str, str], site_rules) -> dict[str, str]:
        """套用匹配到的站点规则（Referer / UA / Cookie / BasicAuth）。"""
        from ..site_rules import SiteRuleStore

        rule = SiteRuleStore.shared().match(url)
        if rule is None:
            return headers
        return SiteRuleStore.shared().apply_to_headers(rule, headers)
