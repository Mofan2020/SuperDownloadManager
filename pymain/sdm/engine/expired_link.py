"""过期链接处理：403/410/451 之后换新链接续传。

对应 Swift 版 Engine/ExpiredLinkHandler.swift。

服务器把下载链接判废时（403/410/451），任务不失败，而是进入
「等待新链接」状态——已下载的字节要留住。用户拿到新链接后，
先验证「是不是同一个文件」，再决定能不能续。

验证顺序（顺序有讲究）：
1. 长度不同 → 拒绝。是另一个文件，不能往旧空洞里写。
2. ETag 不同 → 拒绝。同一 URL 指向的内容被换掉了。
3. 新链接不支持 Range → 放行但警告。已有分片得丢掉重下。
4. 都对得上 → 安全续传。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from ..logging_setup import CAT_ENGINE, get_logger
from .content_sniffer import ContentProbe
from .http_downloader import HTTPDownloader
from .resume_state import ResumeState

log = get_logger()


class RelinkDecision(enum.Enum):
    SAFE_TO_RESUME = "safeToResume"
    UNSAFE_BUT_PROCEED = "unsafeButProceed"
    UNSAFE_REJECT = "unsafeReject"

    def to_dict(self) -> dict:
        return {"decision": self.value}


@dataclass
class RelinkResult:
    decision: RelinkDecision
    reason: str = ""

    def to_dict(self) -> dict:
        payload = self.decision.to_dict()
        if self.reason:
            payload["reason"] = self.reason
        return payload


class ExpiredLinkHandler:
    """评估一个新链接能不能用来续传。"""

    def __init__(self, original: ResumeState, downloader: HTTPDownloader | None = None) -> None:
        self.original = original
        self.downloader = downloader or HTTPDownloader()

    async def evaluate_relink(self, candidate: str) -> RelinkResult:
        try:
            probe = await self.downloader.probe(candidate)
        except Exception as err:  # noqa: BLE001
            return RelinkResult(
                RelinkDecision.UNSAFE_REJECT,
                f"新链接探测失败: {err}",
            )

        # 长度先比——经典坑是「CDN 换了内容但 ETag 规则一样」。
        new_len = probe.content_length
        if new_len is not None and self.original.content_length > 0 and new_len != self.original.content_length:
            return RelinkResult(
                RelinkDecision.UNSAFE_REJECT,
                f"文件长度不一致（原来 {self.original.content_length}，现在 {new_len}）",
            )

        if self.original.etag and probe.etag and self.original.etag != probe.etag:
            return RelinkResult(
                RelinkDecision.UNSAFE_REJECT,
                f"ETag 不一致（原来 {self.original.etag}，现在 {probe.etag}）",
            )

        if not probe.accepts_ranges:
            return RelinkResult(
                RelinkDecision.UNSAFE_BUT_PROCEED,
                "新链接不支持 Range 请求，已下载的分片会被丢弃重新下载",
            )

        log.notice(CAT_ENGINE, f"新链接验证通过，可以续传: {candidate}")
        return RelinkResult(RelinkDecision.SAFE_TO_RESUME)
