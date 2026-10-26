"""内容嗅探：HTTP 探测 + 魔数识别。

对应 Swift 版 Engine/ContentSniffer.swift。

两个职责：
1. 发一个 `Range: bytes=0-0` 的 GET（比 HEAD 兼容性好得多），
   拿到 Content-Length / Accept-Ranges / ETag / Last-Modified /
   Content-Type / Content-Disposition。
2. 没有可靠文件名时，用魔数判断文件类型，让无扩展名的 .dmg/.zip
   能落进正确的分类目录。

Content-Disposition 的三种形态都支持——Swift v1.0.x 只认引号形式，
漏掉了 GitHub CDN 的 `filename=foo.zip` 不带引号，导致下载文件名
变成一串 UUID。这是必须复刻到位的地方。
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import httpx

from ..logging_setup import CAT_NET, get_logger
from .errors import ErrorCategory, classify_status

log = get_logger()

USER_AGENT = "SDM/2.0 (+https://github.com/Mofan2020/SuperDownloadManager)"


#: 已知「不是 HTTP 文件下载」的协议。
#:
#: SDM 只做 HTTP(S) 的普通文件下载。磁力 / BT / ed2k / 迅雷这些
#: 协议**一个都不支持**——要支持磁力得整个塞一个 BT 客户端进来
#: （tracker 协议 + DHT + peer 连接 + piece 校验），那是另一个量级的
#: 工程，不在当前范围内（见 `docs/notes.md` 的「v2.1.0 暂不做」）。
#:
#: 列在这里是为了**给用户一句人话**。没有这张表的话，磁力链接会
#: 在 httpx 里炸成 `unknown url type: '/?xt=urn:btih:...'`，再被
#: 重试 5 次，最后报一句「所有重试都失败」——用户完全看不出
#: 「哦原来这是磁力链接，我们不支持」。
UNSUPPORTED_SCHEMES: dict[str, str] = {
    "magnet": "磁力链接（BT）",
    "bt": "BitTorrent 链接",
    "ed2k": "电驴（eDonkey）链接",
    "thunder": "迅雷链接",
    "flash": "快播（Flash）链接",
    "mms": "MMS 媒体链接",
    "rtmp": "RTMP 直播流",
}


class ProbeError(Exception):
    """探测失败。带 HTTP 状态码，方便上层区分「链接失效」和「探测出错」。"""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        *,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        #: `None` = 按状态码自己判断。
        self._retryable = retryable

    @property
    def is_expired_link(self) -> bool:
        return self.status in (403, 410, 451)

    @property
    def is_retryable(self) -> bool:
        """这个失败值得重试吗？

        **4xx（408/429 除外）重试多少次都一样**：404 是文件不存在，
        416 是服务器不认这个 Range，403 是权限/防盗链。重试只是让
        用户多等好几秒，还会在日志里刷一串「第 N 次失败」——
        看起来就像「所有重试都失败」，其实一次都不会成功。

        值得重试的是：网络断了、超时了、5xx（服务端抽风）、429
        （被限流，等一会儿再来）、408（服务端让重试）。
        """
        if self._retryable is not None:
            return self._retryable
        if self.status is None:
            return True
        if self.status in (408, 429):
            return True
        return self.status >= 500

    @property
    def category(self) -> ErrorCategory:
        """这个探测失败该归到哪一类错误（规格 §5.1）。

        **放在 `ProbeError` 上，而不是散在各个调用方**，是为了让
        「新建任务对话框」和「真的开跑」两条路径给出**同一个类别**。
        之前只有对话框那侧分类，下载那侧一律按 `network` 算——
        磁力链接在任务列表里显示成「网络连接中断，将自动重试」，
        还标着可重试，跟真实原因完全对不上号。
        """
        if self.is_expired_link:
            return ErrorCategory.AUTH
        if self.status is not None:
            return classify_status(self.status)
        if not self.is_retryable:
            # 磁力链接这类：地址本身就不支持，不是网络问题。
            return ErrorCategory.CLIENT_ERROR
        return ErrorCategory.NETWORK


@dataclass
class ContentProbe:
    """一次探测的结果。"""

    url: str
    content_length: int | None = None
    accepts_ranges: bool = False
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None
    suggested_filename: str | None = None
    is_final: bool = True
    #: 响应状态码。206 = 服务器认了 Range，200 = 没认。
    status_code: int | None = None
    #: 服务器**实际上**支不支持分块。
    #:
    #: `accepts_ranges` 读的是 `Accept-Ranges` 头，缺头时是「宽松
    #: 默认 true」——但那只是声明，不是事实。规格 §1.2.4 要求看
    #: **行为**：对 `Range: bytes=0-0` 回 206 就是认，回 200 就是
    #: 不认。这个字段记的是事实，`None` 表示探测没给出结论。
    range_supported: bool | None = None

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "content_length": self.content_length,
            "accepts_ranges": self.accepts_ranges,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "content_type": self.content_type,
            "suggested_filename": self.suggested_filename,
            "status_code": self.status_code,
            "range_supported": self.range_supported,
        }


class SniffedKind(enum.Enum):
    MACHO = "macho"
    ZIP_FAMILY = "zip_family"
    RAR = "rar"
    SEVEN_ZIP = "seven_zip"
    DMG = "dmg"
    TAR = "tar"
    GZIP = "gzip"
    BZ2 = "bz2"
    XZ = "xz"
    PDF = "pdf"
    JPEG = "jpeg"
    PNG = "png"
    GIF = "gif"
    MP4_VIDEO = "mp4_video"
    MP3 = "mp3"
    HTML_TEXT = "html_text"
    WEBM = "webm"
    OGG = "ogg"
    WAV = "wav"
    JAVA_CLASS = "java_class"
    UNKNOWN = "unknown"


def sniff(data: bytes) -> SniffedKind:
    """按魔数判断文件类型。样本短于签名长度时返回 UNKNOWN，不猜。"""
    b = bytes(data[:512])

    def starts(prefix: bytes) -> bool:
        return len(b) >= len(prefix) and b[: len(prefix)] == prefix

    if starts(b"\x7fELF"):
        return SniffedKind.MACHO
    if starts(b"PK\x03\x04") or starts(b"PK\x05\x06") or starts(b"PK\x07\x08"):
        return SniffedKind.ZIP_FAMILY
    if starts(b"Rar!\x1a\x07"):
        return SniffedKind.RAR
    if starts(b"7z\xbc\xaf\x27\x1c"):
        return SniffedKind.SEVEN_ZIP
    if starts(b"\x78\x01\x73\x0d\x62\x62\x60\x60"):
        return SniffedKind.DMG
    if starts(b"\x1f\x8b"):
        return SniffedKind.GZIP
    if starts(b"BZh"):
        return SniffedKind.BZ2
    if starts(b"\xfd7zXZ\x00"):
        return SniffedKind.XZ
    if starts(b"%PDF-"):
        return SniffedKind.PDF
    if starts(b"\xff\xd8\xff"):
        return SniffedKind.JPEG
    if starts(b"\x89PNG\r\n\x1a\n"):
        return SniffedKind.PNG
    if starts(b"GIF87a") or starts(b"GIF89a"):
        return SniffedKind.GIF
    # ISO BMFF：ftyp box 在偏移 4
    if len(b) >= 12 and b[4:8] == b"ftyp":
        return SniffedKind.MP4_VIDEO
    if starts(b"ID3"):
        return SniffedKind.MP3
    if len(b) >= 2 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0:
        return SniffedKind.MP3
    if starts(b"\x1a\x45\xdf\xa3"):
        return SniffedKind.WEBM
    if starts(b"OggS"):
        return SniffedKind.OGG
    # RIFF....WAVE
    if len(b) >= 12 and b[0:4] == b"RIFF" and b[8:12] == b"WAVE":
        return SniffedKind.WAV
    if starts(b"\xca\xfe\xba\xbe"):
        return SniffedKind.JAVA_CLASS
    # tar：ustar 在偏移 257，512 字节样本刚好够
    if len(b) >= 262 and b[257:262] == b"ustar":
        return SniffedKind.TAR
    # HTML：纯 ASCII 且头部像标签
    if len(b) >= 5 and all(0x20 <= c < 0x7F or c in (0x09, 0x0A, 0x0D) for c in b):
        head = bytes(data[:64]).decode("utf-8", errors="ignore").lower()
        if head.startswith("<!doctype") or head.startswith("<html") or head.startswith("<?xml"):
            return SniffedKind.HTML_TEXT
    return SniffedKind.UNKNOWN


def parse_content_disposition(header: str) -> str | None:
    """解析 RFC 6266 / RFC 5987 的 Content-Disposition。

    支持四种组合：
      - `filename="foo.zip"`      引号形式
      - `filename=foo.zip`        不带引号（GitHub CDN 就是这种）
      - `filename*=UTF-8''foo.zip` 扩展值形式
      - 上面两种同时出现 → filename* 优先（RFC 6266 §4.3）
    """
    plain: str | None = None
    ext_val: str | None = None

    for segment in header.split(";"):
        seg = segment.strip()
        lowered = seg.lower()
        if lowered.startswith("filename*="):
            ext_val = _decode_ext_val(seg[len("filename*="):])
        elif lowered.startswith("filename="):
            plain = _decode_plain(seg[len("filename="):])

    return ext_val or plain


def _decode_ext_val(raw: str) -> str | None:
    """解析 ext-val = charset "'" [ language ] "'" value-chars。"""
    first_tick = raw.find("'")
    if first_tick < 0:
        return None
    second_tick = raw.find("'", first_tick + 1)
    if second_tick < 0:
        return None
    value = raw[second_tick + 1:]
    decoded = unquote(value)
    return decoded or None


def _decode_plain(raw: str) -> str | None:
    s = raw.strip()
    if not s:
        return None
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        inner = s[1:-1]
        return inner or None
    cleaned = s.strip(';" ')
    return cleaned or None


def merge_headers(*sources: dict[str, str] | None) -> dict[str, str]:
    """合并请求头，后面的覆盖前面的。

    **头部名大小写不敏感**——`User-Agent` 和 `user-agent` 是同一个
    头。直接 `dict.update()` 的话两个都会留下，httpx 会把两个都发
    出去，服务器看到两个 UA 就可能挑一个不认的用。

    所以这里按小写名去重：后来的覆盖先来的，同时把旧的那个
    （不管它什么大小写）从结果里删掉。
    """
    merged: dict[str, str] = {}
    seen: dict[str, str] = {}
    for source in sources:
        for key, value in (source or {}).items():
            lower = key.lower()
            old = seen.get(lower)
            if old is not None and old != key:
                merged.pop(old, None)
            seen[lower] = key
            merged[key] = value
    return merged


def parse_headers(
    headers: dict[str, str],
    final_url: str,
    original_url: str,
    status_code: int | None = None,
) -> ContentProbe:
    """从响应头解析出探测结果。"""
    lower = {k.lower(): v for k, v in headers.items()}

    accept_ranges = lower.get("accept-ranges", "").lower() != "none"

    # 优先用 Content-Range 的总长度——带 Range 的探测返回 206，
    # 此时 Content-Length 只是这一小段的长度，真实总量在
    # Content-Range 的 "/<total>" 尾巴上。
    length: int | None = None
    content_range = lower.get("content-range")
    if content_range:
        match = re.search(r"/(\d+)", content_range)
        if match:
            length = int(match.group(1))
    if length is None:
        raw_len = lower.get("content-length")
        if raw_len:
            try:
                length = int(raw_len.strip())
            except ValueError:
                length = None

    etag = lower.get("etag")
    last_modified = lower.get("last-modified")

    filename: str | None = None
    content_disposition = lower.get("content-disposition")
    if content_disposition:
        filename = parse_content_disposition(content_disposition)
    if not filename:
        # 兜底：从 URL 路径取最后一段。GitHub CDN 这里是一串 UUID，
        # 所以这个值只是占位，之后引擎会用 Content-Disposition 改名。
        path = final_url.split("?", 1)[0].rstrip("/")
        filename = path.rsplit("/", 1)[-1] if path else None

    return ContentProbe(
        url=final_url,
        content_length=length,
        accepts_ranges=accept_ranges,
        etag=etag,
        last_modified=last_modified,
        content_type=lower.get("content-type"),
        suggested_filename=filename,
        is_final=True,
        status_code=status_code,
        # **看行为不看声明。** 探测发的是 `Range: bytes=0-0`：
        # 回 206 说明服务器认这个头，回 200 说明它无视了。
        # `Accept-Ranges` 头可能压根没有，也可能写着 `bytes` 但
        # 实际不认——只有状态码说了算。
        range_supported=True if status_code == 206 else (False if status_code == 200 else None),
    )


async def probe(
    url: str,
    client: httpx.AsyncClient | None = None,
    extra_headers: dict[str, str] | None = None,
) -> ContentProbe:
    """发一个 `Range: bytes=0-0` 的 GET 探测目标。

    用 GET+Range 而不是 HEAD：很多服务器对 HEAD 的支持很差
    （返回 405，或者不返回 Content-Length）。

    `extra_headers` 用于叠加站点规则的请求头（Referer / Cookie /
    Basic Auth 等）。探测请求也是这个站点的请求——有的站点探测
    阶段就 403，规则不生效的话任务根本起不来。

    合并顺序：**默认 UA → extra_headers → Range**。

    - 默认 UA 只是兜底，extra_headers 里的 UA 覆盖它（站点规则
      指定的 UA 必须**替换**默认 UA，不是叠加在后面）。
    - `Range: bytes=0-0` 最后写死。探测语义就是「只要第一个字节」，
      外部传进来的头不能改它——改了探测拿到的长度就不是全长了。
    """
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=None),
        )
    try:
        # **先看协议，再发请求。**
        #
        # 磁力链接（magnet:）这类地址 httpx 根本解析不了，会炸成
        # `unknown url type: '/?xt=urn:btih:...'`。不拦住的话这个
        # 异常会被当成「网络错误」重试 5 次，最后报一句「所有重试
        # 都失败」——用户看到的是一句完全对不上号的话。
        #
        # 这里在发请求之前拦下来，直接给一句「这是磁力链接，我们不
        # 支持 BT」——用户看了就知道该换工具，而不是以为网络坏了。
        scheme = urlparse(url).scheme.lower()
        if scheme not in ("http", "https"):
            label = UNSUPPORTED_SCHEMES.get(scheme)
            if label:
                raise ProbeError(
                    f"这是{label}，SDM 只下载 HTTP(S) 的普通文件，不支持 BT/磁力",
                    retryable=False,
                )
            raise ProbeError(
                f"不支持的地址协议：{scheme or '（没有协议）'}。请用 http:// 或 https:// 开头的地址",
                retryable=False,
            )

        request_headers = merge_headers(
            {"User-Agent": USER_AGENT},
            extra_headers,
            {"Range": "bytes=0-0"},
        )
        request = client.build_request("GET", url, headers=request_headers)
        response = await client.send(request, stream=True)
        try:
            headers = dict(response.headers)
            final_url = str(response.url)
            status = response.status_code
        finally:
            await response.aclose()

        log.info(CAT_NET, f"probe {url} -> {status} finalURL={final_url} len={headers.get('content-length')}")
        if status >= 400:
            # 403/410/451 是「链接失效」，不是「探测失败」——两者在
            # 流程层的处理完全不同：前者进「等待新链接」状态，
            # 后者直接判任务失败。所以这里把状态码带出去，
            # 由调用方分类。
            raise ProbeError(f"HTTP {status}", status=status)
        return parse_headers(headers, final_url, url, status_code=status)
    finally:
        if owns_client:
            await client.aclose()
