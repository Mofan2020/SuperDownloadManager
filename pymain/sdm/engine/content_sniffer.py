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
from urllib.parse import unquote

import httpx

from ..logging_setup import CAT_NET, get_logger

log = get_logger()

USER_AGENT = "SDM/2.0 (+https://github.com/Mofan2020/SuperDownloadManager)"


class ProbeError(Exception):
    """探测失败。带 HTTP 状态码，方便上层区分「链接失效」和「探测出错」。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status

    @property
    def is_expired_link(self) -> bool:
        return self.status in (403, 410, 451)


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

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "content_length": self.content_length,
            "accepts_ranges": self.accepts_ranges,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "content_type": self.content_type,
            "suggested_filename": self.suggested_filename,
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


def parse_headers(headers: dict[str, str], final_url: str, original_url: str) -> ContentProbe:
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
    )


async def probe(url: str, client: httpx.AsyncClient | None = None) -> ContentProbe:
    """发一个 `Range: bytes=0-0` 的 GET 探测目标。

    用 GET+Range 而不是 HEAD：很多服务器对 HEAD 的支持很差
    （返回 405，或者不返回 Content-Length）。
    """
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=None),
        )
    try:
        request = client.build_request(
            "GET",
            url,
            headers={
                "Range": "bytes=0-0",
                "User-Agent": USER_AGENT,
            },
        )
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
        return parse_headers(headers, final_url, url)
    finally:
        if owns_client:
            await client.aclose()
