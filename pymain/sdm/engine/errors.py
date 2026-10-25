"""错误分类。

规格 §5.1 要求把每个错误归到 12 个类别之一，每个类别带 `retryable`
标志。分类不是为了好看——**重试策略由它决定**：

- 可重试的类别走指数退避，重试预算耗尽才失败。
- 不可重试的类别**立即**让该分片失败，而且**不消耗其它分片的
  重试预算**。403 重试 5 次只是浪费用户的时间和服务器的带宽。

`retryable` 不是硬编码在枚举上的，因为同一个类别在不同场景下
处理方式不同：`disk` 错误本身不可重试，但用户腾出空间之后手动
点「重试」当然要能继续——那是**全任务级**重试（规格 §5.2 最后
一条），跟分片级重试是两回事。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class ErrorCategory(enum.StrEnum):
    """错误类别。枚举值就是规格里的字符串，前端和 CLI 直接用。"""

    NETWORK = "network"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    RATE_LIMITED = "rate_limited"
    CLIENT_ERROR = "client_error"
    AUTH = "auth"
    NOT_FOUND = "not_found"
    RANGE_ERROR = "range_error"
    DISK = "disk"
    STALE_SESSION = "stale_session"
    NAME_CONFLICT = "name_conflict"
    UNKNOWN = "unknown"


#: 默认可重试性。`disk` / `stale_session` / `name_conflict` 都不在
#: 里面——它们要么要用户动手，要么重试多少次都一样。
RETRYABLE_BY_DEFAULT: frozenset[ErrorCategory] = frozenset({
    ErrorCategory.NETWORK,
    ErrorCategory.TIMEOUT,
    ErrorCategory.SERVER_ERROR,
    ErrorCategory.RATE_LIMITED,
})


#: 各类别给用户看的中文短句（规格 §5.1 的「用户可见文案方向」）。
#:
#: 刻意写成人话，不写「HTTP 403」。用户看到「需要登录或权限不足」
#: 知道该干什么，看到「403」得自己查文档。
USER_MESSAGE: dict[ErrorCategory, str] = {
    ErrorCategory.NETWORK: "网络连接中断，将自动重试",
    ErrorCategory.TIMEOUT: "连接超时，重试中",
    ErrorCategory.SERVER_ERROR: "服务器错误",
    ErrorCategory.RATE_LIMITED: "服务器限流，降低并发后重试",
    ErrorCategory.CLIENT_ERROR: "请求被服务器拒绝",
    ErrorCategory.AUTH: "需要登录或权限不足",
    ErrorCategory.NOT_FOUND: "文件不存在",
    ErrorCategory.RANGE_ERROR: "服务器拒绝分块请求，尝试单流下载",
    ErrorCategory.DISK: "磁盘写入失败，检查空间和权限",
    ErrorCategory.STALE_SESSION: "服务器文件已变化，无法续传",
    ErrorCategory.NAME_CONFLICT: "目标文件已存在",
    ErrorCategory.UNKNOWN: "未知错误，查看日志",
}


@dataclass
class TaskError:
    """一次失败的结构化描述。

    `message` 是给用户看的短句，`detail` 是给日志和「复制错误详情」
    用的技术细节。两者分开是因为用户不该看到 traceback，但报障时
    又必须能拿到完整信息。
    """

    category: ErrorCategory
    message: str = ""
    detail: str = ""
    retryable: bool = False
    status: int | None = None
    #: 限流场景下服务器给的 `Retry-After`（秒）。
    retry_after: float | None = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.message:
            self.message = USER_MESSAGE.get(self.category, USER_MESSAGE[ErrorCategory.UNKNOWN])

    @property
    def is_retryable(self) -> bool:
        return self.retryable

    def to_dict(self) -> dict:
        return {
            "category": str(self.category),
            "message": self.message,
            "detail": self.detail,
            "retryable": self.retryable,
            "status": self.status,
            "retry_after": self.retry_after,
        }


def classify_status(status: int) -> ErrorCategory:
    """HTTP 状态码 → 错误类别。

    403 有歧义：既可能是「链接过期」（本项目的 `awaiting_new_link`
    语义），也可能是「权限不足」（规格的 `auth`）。这里按规格归到
    `auth`——任务状态走 `awaiting_new_link` 是**引擎层**的另一个
    决定，跟错误类别不冲突。
    """
    if status in (401, 403):
        return ErrorCategory.AUTH
    if status == 404:
        return ErrorCategory.NOT_FOUND
    if status == 416:
        return ErrorCategory.RANGE_ERROR
    if status == 429:
        return ErrorCategory.RATE_LIMITED
    if 500 <= status <= 599:
        return ErrorCategory.SERVER_ERROR
    if 400 <= status <= 499:
        return ErrorCategory.CLIENT_ERROR
    if status >= 200:
        return ErrorCategory.UNKNOWN
    return ErrorCategory.NETWORK


def error_for_status(
    status: int,
    detail: str = "",
    retry_after: float | None = None,
) -> TaskError:
    """按状态码造一个 `TaskError`。"""
    category = classify_status(status)
    return TaskError(
        category=category,
        detail=detail or f"HTTP {status}",
        retryable=category in RETRYABLE_BY_DEFAULT,
        status=status,
        retry_after=retry_after,
    )


def error_from_exception(exc: BaseException, detail: str = "") -> TaskError:
    """把底层异常归类。

    httpx 的异常树很细（`ConnectError` / `ReadTimeout` /
    `RemoteProtocolError` …），但对用户来说只有三档：网络断了、
    超时了、还是不知道。分太细的话重试策略要写十几条分支，
    反而更容易漏。
    """
    import httpx

    text = detail or f"{type(exc).__name__}: {exc}"

    if isinstance(exc, httpx.TimeoutException):
        category = ErrorCategory.TIMEOUT
    elif isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else None
        if status is not None:
            return error_for_status(status, text)
        category = ErrorCategory.UNKNOWN
    elif isinstance(exc, httpx.NetworkError):
        category = ErrorCategory.NETWORK
    elif isinstance(exc, httpx.InvalidURL):
        category = ErrorCategory.CLIENT_ERROR
    elif isinstance(exc, httpx.HTTPError):
        category = ErrorCategory.NETWORK
    elif isinstance(exc, OSError):
        # 磁盘满、权限拒绝、只读文件系统——全是磁盘层的问题。
        category = ErrorCategory.DISK
    else:
        category = ErrorCategory.UNKNOWN

    return TaskError(
        category=category,
        detail=text,
        retryable=category in RETRYABLE_BY_DEFAULT,
    )


def parse_retry_after(value: str | None) -> float | None:
    """解析 `Retry-After` 头。

    支持两种形态：秒数（`Retry-After: 120`）和 HTTP 日期
    （`Retry-After: Wed, 21 Oct 2015 07:28:00 GMT`）。日期形态在
    限流场景不常见，但服务器爱发，实现一下免得解析失败反而把
    重试打乱。
    """
    if not value:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        return max(0.0, seconds)

    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None

    import datetime as _dt

    now = _dt.datetime.now(_dt.UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.UTC)
    return max(0.0, (when - now).total_seconds())
