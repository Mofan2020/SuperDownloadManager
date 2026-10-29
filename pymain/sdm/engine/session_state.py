"""会话文件：分片布局的持久化 + 续传前的资源校验。

规格 §1.3 要求「每个分片的已完成字节区间定期写入磁盘的会话文件」，
并且「恢复前重新请求服务器元数据，按 ETag → Last-Modified →
Content-Length 的优先级判断资源是否已变化」。

**为什么必须有这个文件。** 动态分段（规格 §1.2）意味着分片布局
是**运行时长出来的**，不是开始时一次性切死的。分片 id 决定了
临时文件名（`<目标名>.sdm.partial-<id>`），所以崩溃之后要接着下，
必须知道每个 id 对应哪一段字节。文件名里没有偏移量，光靠扫盘
复原不出来。

所以：布局写进会话文件，临时文件只管存字节。两边对不上就重来。

会话文件和「完成后的 sidecar」是两回事：
- 会话文件 `<目标名>.sdm.session.json`——下载**中**的进度，完事就删。
- sidecar `<目标名>.sdm.json`——下载**完成**后的元信息，长期保留。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..logging_setup import CAT_STORE, get_logger
from . import work_dir
from .content_sniffer import ContentProbe
from .errors import ErrorCategory, TaskError

log = get_logger()


SESSION_SUFFIX = ".sdm.session.json"

#: 会话文件格式版本。改结构时 +1，旧版本读不出来就当没有会话
#: （重新下载），而不是硬解一个可能解错的布局。
SESSION_VERSION = 1


def _is_strong_etag(etag: str | None) -> bool:
    """这个 ETag 能不能用来判断「资源变没变」。

    **弱 ETag（`W/"..."`）不行。** RFC 7232：弱校验器只保证「语义
    等价」，不保证字节一致，而且明确禁止用于 Range 请求
    （If-Range 只接受强 ETag）。很多 CDN 每次请求都换弱 ETag——
    拿它判「资源变了」，用户的每一次「继续下载」都会被打成
    「从头重下」。
    """
    if not etag:
        return False
    return not etag.lstrip().startswith("W/")


@dataclass
class SessionChunk:
    """会话文件里的一个分片。"""

    id: int
    start: int
    end: int
    bytes_done: int = 0

    @property
    def size(self) -> int:
        return self.end - self.start

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "start": self.start,
            "end": self.end,
            "bytes_done": self.bytes_done,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> SessionChunk:
        return cls(
            id=int(raw["id"]),
            start=int(raw["start"]),
            end=int(raw["end"]),
            bytes_done=int(raw.get("bytes_done", 0)),
        )


@dataclass
class SessionState:
    """一次下载的持久化状态。"""

    url: str = ""
    etag: str | None = None
    last_modified: str | None = None
    content_length: int | None = None
    chunks: list[SessionChunk] = field(default_factory=list)
    saved_at: float = 0.0
    version: int = SESSION_VERSION

    # -- 路径 --------------------------------------------------------

    @staticmethod
    def path_for(destination: Path) -> Path:
        """会话文件路径：`downloads/<key>.sdm.session.json`。

        **不在下载目录里**——会话文件是软件的内部状态，用户在
        下载文件夹里看到一堆 `.sdm.session.json` 只会困惑。
        """
        return work_dir.session_path(destination)

    # -- 序列化 ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "url": self.url,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "content_length": self.content_length,
            # 分片可能已经是 dict（手写会话文件、手工构造），
            # 两种形态都能序列化。
            "chunks": [
                c.to_dict() if hasattr(c, "to_dict") else dict(c) for c in self.chunks
            ],
            "saved_at": self.saved_at,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> SessionState:
        return cls(
            url=str(raw.get("url", "")),
            etag=raw.get("etag"),
            last_modified=raw.get("last_modified"),
            content_length=raw.get("content_length"),
            chunks=[SessionChunk.from_dict(c) for c in raw.get("chunks", [])],
            saved_at=float(raw.get("saved_at", 0.0)),
            version=int(raw.get("version", SESSION_VERSION)),
        )

    # -- 读写 --------------------------------------------------------

    @classmethod
    def read(cls, destination: Path) -> SessionState | None:
        """读会话文件。读不出来（不存在 / 坏了 / 版本不对）返回 None。

        返回 None 的语义是「没有可用的会话」——调用方要重新下载。
        **不能**抛异常：会话文件是加速器，不是必需品。
        """
        path = cls.path_for(destination)
        if not path.exists():
            # 升级前下的会话文件还在老位置（目标文件旁边）。
            # 把它搬过来，用户正在下的东西不能凭空消失。
            adopted = work_dir.adopt_legacy_session(destination)
            if adopted is not None:
                path = adopted
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as err:
            log.warning(CAT_STORE, f"会话文件解析失败，重新下载: {path} ({err})")
            return None
        if not isinstance(data, dict):
            return None
        version = int(data.get("version", 0))
        if version != SESSION_VERSION:
            log.warning(CAT_STORE, f"会话文件版本 {version} != {SESSION_VERSION}，重新下载")
            return None
        try:
            state = cls.from_dict(data)
        except (KeyError, TypeError, ValueError) as err:
            log.warning(CAT_STORE, f"会话文件字段损坏，重新下载: {path} ({err})")
            return None
        if not state.chunks:
            return None
        return state

    def write(self, destination: Path) -> None:
        """原子写会话文件。

        先写临时文件再 `os.replace`——直接覆盖的话，进程在写到
        一半时被杀，会话文件就是半个 JSON，下次读不出来，
        已下载的字节全部作废。
        """
        self.saved_at = time.time()
        path = self.path_for(destination)
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as err:
            log.warning(CAT_STORE, f"会话文件写入失败 {path}: {err}")
            # 临时文件留着会变成垃圾——下次写会覆盖，但用户可能在
            # 下载目录里看到它。
            try:
                tmp.unlink()
            except OSError:
                pass

    @classmethod
    def remove(cls, destination: Path) -> None:
        """删掉会话文件。下载完成 / 取消后调用。"""
        path = cls.path_for(destination)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as err:
            log.warning(CAT_STORE, f"会话文件删除失败 {path}: {err}")
        # 老位置（升级前）也清一下，别留在下载目录里。
        legacy = work_dir.legacy_session_path(destination)
        try:
            legacy.unlink()
        except OSError:
            pass

    # -- 校验 --------------------------------------------------------

    def staleness(self, probe: ContentProbe) -> str | None:
        """拿新的探测结果比对，资源变了就返回原因。

        **优先级：ETag → Last-Modified → Content-Length**（规格 §1.3）。

        实现上不是「三个都查」，而是**取第一个两边都有、可以比的
        信号**。理由：ETag 变了但 Content-Length 没变，资源确实换了
        （比如换了内容但大小相同），按 ETag 判定才是对的。反过来，
        有些服务器每次请求都换 ETag（弱 ETag 场景），那按 ETag 判
        定会把每次续传都判成「变了」——所以只有当两边的 ETag
        **都在**、且确实不同时才判定为变化。

        **弱 ETag（W/"..."）直接跳过，不参与判断。** RFC 7232 说得很
        清楚：弱校验器只保证「语义等价」，不保证字节一致，而且
        **明确禁止用于 Range 请求**（If-Range 只接受强 ETag）。很多
        CDN 每次请求都给不同的弱 ETag，拿它判「资源变了」，会把
        用户的每一次「继续下载」都变成「从头重下」——正是最该避免
        的行为。跳过后退回 Last-Modified / Content-Length。

        三种信号都比不了时返回 None（没法验证就当没变）——
        宁可多下一次，也不要把用户的下载清零。
        """
        if _is_strong_etag(self.etag) and _is_strong_etag(probe.etag):
            return None if self.etag == probe.etag else "etag"
        if self.last_modified and probe.last_modified:
            return None if self.last_modified == probe.last_modified else "last_modified"
        if self.content_length and probe.content_length:
            return None if self.content_length == probe.content_length else "content_length"
        return None

    def stale_error(self, reason: str) -> TaskError:
        """把「资源变了」变成一个结构化错误。"""
        labels = {
            "etag": "ETag",
            "last_modified": "Last-Modified",
            "content_length": "文件大小",
        }
        label = labels.get(reason, reason)
        return TaskError(
            category=ErrorCategory.STALE_SESSION,
            message="服务器文件已变化，无法续传",
            detail=f"资源标识 {label} 与下载开始时不一致",
            retryable=False,
        )
