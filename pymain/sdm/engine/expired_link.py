"""过期链接处理：403/410/451 之后换新链接续传。

对应 Swift 版 Engine/ExpiredLinkHandler.swift。

服务器把下载链接判废时（403/410/451），任务不失败，而是进入
「等待新链接」状态——已下载的字节要留住。用户拿到新链接后，
先验证「是不是同一个文件」，再决定能不能续。

**验证顺序（顺序有讲究，强的证据要走在前面）：**

1. **长度不同 → 拒绝。** 是另一个文件，不能往旧空洞里写。
2. **字节采样比对 → 这是最硬的证据。** 拿盘上已经下好的那段
   字节，去新链接取同样的偏移，逐字节比。
   - 一样 → 同一个文件，可以安全续传（**这一步会跳过 ETag 比较**，
     因为很多 CDN 每次请求都换 ETag，拿它判「换了文件」会误杀）。
   - 不一样 → 拒绝。
3. **ETag 不同 → 拒绝**（仅当第 2 步没法做、也就是采不到样字节时）。
4. **新链接不支持 Range → 放行但警告。** 已有分片得丢掉重下。
5. 都对得上 → 安全续传。

**为什么字节比对排在 ETag 前面。** ETag 靠不住：CDN 每次请求
可能给不同的弱 ETag，同一个文件两次请求 ETag 不同是常态。
拿 ETag 判「换了文件」，会把大量正常的换链误判成不同文件，
用户就永远下不了。反过来，长度也一样靠不住——1MB 的安装包和
1MB 的壁纸长度相同。字节比对是内容级的，唯一的硬证据。

**换链之后为什么还要改写会话文件。** 会话文件里记着分片布局和
「这些字节属于哪个 URL」。流程层恢复时会检查「会话的 URL 跟本次
下载的 URL 一致吗」，不一致就判定「同名文件重新下载」，把整个
会话丢掉、从零重下。用户明明已经下了一半，却看到进度条归零——
这就是「换链接之后变成重新下载」的根因。所以判定通过之后，
必须把会话文件里的 URL 改成新链接（并同步新的 ETag）。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

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
    #: 新链接的探测结果。判定通过后，引擎要拿它把会话文件里的
    #: ETag / Last-Modified 更新成新链接的值——否则恢复时
    #: 「资源已变化」的校验会拿旧 ETag 跟新链接比，把刚续上的
    #: 下载又判成 stale。
    probe: ContentProbe | None = None
    #: 是不是「同一个文件」已经被字节比对确认了。
    verified_same_file: bool = False

    def to_dict(self) -> dict:
        payload = self.decision.to_dict()
        if self.reason:
            payload["reason"] = self.reason
        payload["verifiedSameFile"] = self.verified_same_file
        return payload


@dataclass
class RelinkSample:
    """盘上已经下好的一段字节，用来跟新链接认亲。

    `offset` 是这段字节在**整个文件**里的起始偏移，`data` 是
    字节内容。分片文件里存的就是从 `chunk.start` 开始的连续字节，
    所以偏移就是分片的 start。
    """

    offset: int
    data: bytes = field(repr=False)


class ExpiredLinkHandler:
    """评估一个新链接能不能用来续传。"""

    def __init__(
        self,
        original: ResumeState,
        downloader: HTTPDownloader | None = None,
        samples: list[RelinkSample] | None = None,
    ) -> None:
        self.original = original
        self.downloader = downloader or HTTPDownloader()
        #: 已经下到盘上的字节样本。空列表 = 没有可比对的样本
        #: （任务还没下到任何字节，或者分片文件丢了）。
        self.samples = list(samples or [])

    async def evaluate_relink(self, candidate: str) -> RelinkResult:
        try:
            probe = await self.downloader.probe(candidate)
        except Exception as err:
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
                probe=probe,
            )

        # 字节认亲。有样本就一定要比——这是唯一能确定
        # 「同一个文件」的办法。
        if self.samples:
            verdict = await self._verify_by_bytes(candidate, probe)
            if verdict is not None:
                return verdict

        if self.original.etag and probe.etag and self.original.etag != probe.etag:
            # **同一个 URL** 的 ETag 变了 = 服务器把内容换掉了，
            # 必须拒绝。
            #
            # **不同 URL** 的 ETag 变了 = 正常现象：换了 CDN 节点、
            # 换了签名 URL、弱 ETag 每次都变。这种情况下不能拒绝
            # ——拒绝就等于「因为链接失效所以永远下不了」，
            # 正是用户要解决的问题。降级成「放行但警告」：
            # 清掉已下的分片，从头下，界面把风险说清楚。
            if candidate == self.original.url:
                return RelinkResult(
                    RelinkDecision.UNSAFE_REJECT,
                    f"ETag 不一致（原来 {self.original.etag}，现在 {probe.etag}）",
                    probe=probe,
                )
            return RelinkResult(
                RelinkDecision.UNSAFE_BUT_PROCEED,
                "无法用字节确认新链接是同一个文件（ETag 不同），将重新下载",
                probe=probe,
            )

        if not probe.accepts_ranges:
            return RelinkResult(
                RelinkDecision.UNSAFE_BUT_PROCEED,
                "新链接不支持 Range 请求，已下载的分片会被丢弃重新下载",
                probe=probe,
            )

        log.notice(CAT_ENGINE, f"新链接验证通过，可以续传: {candidate}")
        return RelinkResult(RelinkDecision.SAFE_TO_RESUME, probe=probe)

    async def _verify_by_bytes(
        self,
        candidate: str,
        probe: ContentProbe,
    ) -> RelinkResult | None:
        """用盘上已下载的字节跟新链接认亲。

        返回 None 表示「没法比」（取样失败），调用方要退回
        ETag 那一层的判断。
        """
        if not probe.accepts_ranges:
            # 新链接不支持 Range，取不到指定偏移的字节，比不了。
            return None

        for sample in self.samples:
            if not sample.data:
                continue
            end = sample.offset + len(sample.data) - 1
            try:
                remote = await self.downloader.fetch_range(candidate, sample.offset, end)
            except Exception as err:
                # 取样失败不等于「不是同一个文件」——网络抖一下、
                # 新链接暂时不通，都会走到这里。退回 ETag 判断。
                log.warning(
                    CAT_ENGINE,
                    f"新链接取样失败（偏移 {sample.offset}）: {err}",
                )
                return None
            if not remote:
                # 拿回来是空的：服务器没给 Range 内容，不能拿来判。
                return None
            if remote != sample.data:
                # **长度相同的两个不同文件，比对到这里就露馅了。**
                log.notice(
                    CAT_ENGINE,
                    f"新链接在偏移 {sample.offset} 处的字节与原文件不同，拒绝换链",
                )
                return RelinkResult(
                    RelinkDecision.UNSAFE_REJECT,
                    f"新链接的内容与原文件不同（偏移 {sample.offset} 处比对失败）",
                    probe=probe,
                )

        log.notice(
            CAT_ENGINE,
            f"新链接与原文件字节一致（比对了 {len(self.samples)} 段），可以续传",
        )
        return RelinkResult(
            RelinkDecision.SAFE_TO_RESUME,
            "字节比对确认是同一个文件",
            probe=probe,
            verified_same_file=True,
        )
