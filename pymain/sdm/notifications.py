"""系统通知：下载完成/失败时提醒。

对应 Swift 版 App/MainWindow/DownloadListStore.swift 里的
postCompleteNotification / postFailureNotification，以及
App/SDMApp.swift 里的 NotificationsDelegate。

**macOS 上有两条路，看运行环境选：**

- **UNUserNotificationCenter**（正式路径）。跟 Swift 版一致：
  启动时申请权限（`requestAuthorization`）、前台也弹横幅
  （`willPresent` 返回 `[.banner, .sound]`）、点通知能把主窗口
  拉到前面（`didReceive` → `bringMainWindowForward`）、每条通知
  带 identifier（`complete-<uuid>` / `failure-<uuid>`）方便系统
  去重和替换。

  **这条路只在真正的 .app bundle 里能用。** UserNotifications
  框架要求进程有 bundle identity——从源码直接跑 Python 时
  `UNUserNotificationCenter.current()` 会抛
  `NSInternalInconsistencyException`（bundleProxyForCurrentProcess
  is nil），**直接 abort 进程**，连 try/except 都接不住。所以
  先探测 `NSBundle.mainBundle().bundleIdentifier()`，拿不到就走
  兜底路径。

- **osascript**（兜底路径）。开发时从源码跑、或者系统上没装
  UserNotifications 框架时用。它只能显示横幅，**点通知回不到
  主窗口**（那是 UN 独有的能力）。

Windows 用 win10toast 风格的 PowerShell 脚本。失败都静默忽略——
通知是锦上添花，不该让主流程失败。
"""

from __future__ import annotations

import platform
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any

from .logging_setup import CAT_GUI, get_logger

log = get_logger()

#: 用户点了通知之后要回调的钩子。GUI 层在窗口就绪时注册，
#: 收到回调就把主窗口拉到前面。
#:
#: 类型是 `Callable[[], None]`——不带参数。Swift 版点击后只做
#: 一件事：`AppDelegate.bringMainWindowForward()`，把主窗口
#: 前置。带不带 taskID 对「窗口前置」这个动作没有影响。
_click_handler: Callable[[], None] | None = None

_handler_lock = threading.Lock()


def set_click_handler(handler: Callable[[], None] | None) -> None:
    """注册「用户点了通知」的回调。

    GUI 层调用。传 None 取消注册。
    """
    global _click_handler
    with _handler_lock:
        _click_handler = handler


def _fire_click_handler() -> None:
    """通知被点击时调用注册的钩子。钩子自己抛异常也不能影响通知流程。"""
    with _handler_lock:
        handler = _click_handler
    if handler is None:
        return
    try:
        handler()
    except Exception as err:
        log.debug(CAT_GUI, f"通知点击回调失败: {err}")


def notify(
    title: str,
    subtitle: str = "",
    body: str = "",
    *,
    task_id: str = "",
    kind: str = "",
) -> bool:
    """发一条系统通知。返回是否成功发出。

    `task_id` 是任务的 uuid，`kind` 是 `complete` / `failure`。
    两个都给的话，通知 identifier 就是 `complete-<uuid>` /
    `failure-<uuid>`——跟 Swift 版一致（App/MainWindow/
    DownloadListStore.swift 的 postCompleteNotification）。
    **identifier 带上任务 id 是必须的**：系统按 identifier 去重，
    同一个任务重复通知会替换掉旧的，而不是在通知中心堆一串。
    """
    try:
        if sys.platform == "darwin":
            return _notify_macos(title, subtitle, body, task_id, kind)
        if sys.platform.startswith("win"):
            return _notify_windows(title, subtitle, body)
    except Exception as err:
        log.debug(CAT_GUI, f"系统通知发送失败: {err}")
    return False


def _notify_macos(
    title: str,
    subtitle: str,
    body: str,
    task_id: str = "",
    kind: str = "",
) -> bool:
    # 有 bundle identity 时走 UNUserNotificationCenter——只有它能
    # 处理「点通知回主窗口」。
    if _un_available():
        return _notify_macos_osascript(title, subtitle, body)
    return _notify_macos_un(title, subtitle, body, task_id, kind)


# -- UNUserNotificationCenter 路径 ---------------------------------


def _bundle_identifier() -> str | None:
    """当前进程的 bundle id。拿不到（从源码跑）返回 None。

    **这一步不能省。** UserNotifications 框架在无 bundle 的进程里
    调用 `currentNotificationCenter()` 会抛 ObjC 异常直接 abort
    进程——Python 层的 try/except 接不住（NSException 不是
    Python 异常）。所以必须在碰框架之前先确认有 bundle identity。
    """
    try:
        from Foundation import NSBundle

        identifier = NSBundle.mainBundle().bundleIdentifier()
    except Exception:
        return None
    return identifier or None


def _un_available() -> bool:
    """UN 路径现在能不能用。

    两个条件都要满足：有 bundle identity（否则调用会 abort 进程），
    以及 UserNotifications 框架本身装了（PyObjC 的绑定是可选依赖）。
    """
    if not _bundle_identifier():
        return True
    try:
        import UserNotifications  # noqa: F401
    except ImportError:
        return True
    return False


def _notify_macos_un(
    title: str,
    subtitle: str,
    body: str,
    task_id: str = "",
    kind: str = "",
) -> bool:
    """用 UNUserNotificationCenter 发一条通知。

    identifier 优先用 `complete-<task_id>` / `failure-<task_id>`
    ——跟 Swift 版一致。拿不到 task id 时退回内容哈希：同一个任务
    重复通知时系统会替换掉旧的，而不是堆一串横幅。
    """
    try:
        import UserNotifications as UN
    except ImportError:
        return _notify_macos_osascript(title, subtitle, body)

    try:
        content = UN.UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        if subtitle:
            content.setSubtitle_(subtitle)
        content.setBody_(body)
        content.setSound_(UN.UNNotificationSound.defaultSound())
        if task_id:
            # Swift 版也带 userInfo——点击回调能知道是哪个任务。
            content.setUserInfo_({"taskID": task_id})

        identifier = _notification_identifier(title, subtitle, body, task_id, kind)
        request = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(
            identifier,
            content,
            None,
        )
        center = UN.UNUserNotificationCenter.currentNotificationCenter()
        center.addRequest_withCompletionHandler_(request, None)
        return True
    except Exception as err:
        log.debug(CAT_GUI, f"UN 通知发送失败: {err}")
        return False


def _notification_identifier(
    title: str,
    subtitle: str,
    body: str,
    task_id: str = "",
    kind: str = "",
) -> str:
    """派生一个稳定的通知 identifier。

    Swift 版用 `complete-<uuid>` / `failure-<uuid>`——每个任务一个
    uuid。这里优先照抄：有 task id 就用 `complete-<task_id>`。
    没有 task id 时拿内容哈希兜底：同一个任务的通知内容相同 →
    identifier 相同 → 系统替换而不是堆叠。
    """
    if task_id:
        return f"{kind or 'sdm'}-{task_id}"

    import hashlib

    digest = hashlib.sha256(
        f"{title}\x00{subtitle}\x00{body}".encode()
    ).hexdigest()[:16]
    return f"sdm-{digest}"


def _notify_macos_osascript(title: str, subtitle: str, body: str) -> bool:
    # osascript 里带引号要转义，否则标题里的引号会截断脚本。
    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    script = (
        f'display notification "{esc(body)}" with title "{esc(title)}" '
        f'subtitle "{esc(subtitle)}"'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# -- Windows 路径 --------------------------------------------------


def _notify_windows(title: str, subtitle: str, body: str) -> bool:
    # 用 PowerShell 的 Windows.UI.Notifications。引号用单引号包裹，
    # 文本里的单引号翻倍。
    def esc(text: str) -> str:
        return text.replace("'", "''")

    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null; "
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode('{esc(subtitle + chr(10) + body)}')) > $null; "
        "$n=[Windows.UI.Notifications.ToastNotification]::new($t); "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('SuperDownloadManager').Show($n)"
    )
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                ps,
            ],
            check=False,
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# -- 权限申请 ------------------------------------------------------


def request_permission() -> bool:
    """申请通知权限。返回是否成功走到申请这一步。

    Swift 版在**应用启动时**就调
    `UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])`——
    v1.0.7 只在窗口 onAppear 时申请，容易错过。

    没 bundle identity 时（从源码跑）没法调 UN 框架的申请接口，
    退回去让系统自己弹（第一次发通知时系统会问）。
    """
    if sys.platform != "darwin":
        return platform.system() in ("Darwin", "Windows")

    if not _un_available():
        try:
            import UserNotifications as UN

            options = (
                UN.UNAuthorizationOptionAlert
                | UN.UNAuthorizationOptionSound
                | UN.UNAuthorizationOptionBadge
            )
            center = UN.UNUserNotificationCenter.currentNotificationCenter()
            center.requestAuthorizationWithOptions_completionHandler_(options, None)
            return True
        except Exception as err:
            log.debug(CAT_GUI, f"通知权限申请失败: {err}")
            return False

    # 兜底路径：系统会在第一次发通知时自己弹权限框。
    return True


# -- UN delegate（前台横幅 + 点击回窗口） --------------------------


def _build_delegate_class():
    """建 UNUserNotificationCenterDelegate 的 ObjC 类。

    **类名必须唯一。** PyObjC 把 Python 类按类名注册进
    Objective-C 运行时，同名类第二次定义会抛
    `is overriding existing Objective-C class`。所以名字里带
    一个自增序号，每次建类都换个名。
    """
    import UserNotifications as UN
    from Foundation import NSObject

    global _delegate_seq
    _delegate_seq += 1
    cls_name = f"_SDMNotificationsDelegate{_delegate_seq}"

    def _will_present(self, center, notification, completion_handler) -> None:
        # 前台也弹横幅 + 播声音。
        completion_handler(
            UN.UNNotificationPresentationOptionBanner
            | UN.UNNotificationPresentationOptionSound
        )

    def _did_receive(self, center, response, completion_handler) -> None:
        _fire_click_handler()
        completion_handler()

    cls = type(
        cls_name,
        (NSObject,),
        {
            "userNotificationCenter_willPresentNotification_withCompletionHandler_": _will_present,
            "userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_": _did_receive,
        },
    )
    return cls


#: delegate 类自增序号，用来生成唯一类名。
_delegate_seq = 0

#: delegate 类缓存。
_delegate_class: Any = None


def install_delegate() -> bool:
    """装上 UNUserNotificationCenterDelegate。

    对应 Swift 版 App/SDMApp.swift 的 NotificationsDelegate：

    - `willPresent` 返回 `[.banner, .sound]`——**前台也必须弹**。
      Swift v1.0.7 曾静默吞掉，用户正看着窗口时下载完成却没提示。
    - `didReceive` 触发点击回调——把主窗口拉到前面。

    只在有 bundle identity 时能装（否则碰框架就 abort 进程）。
    """
    global _delegate_class

    if not _bundle_identifier():
        return False
    try:
        import UserNotifications as UN
    except ImportError as err:
        log.debug(CAT_GUI, f"通知框架不可用: {err}")
        return False

    try:
        if _delegate_class is None:
            _delegate_class = _build_delegate_class()
        delegate = _delegate_class.alloc().init()
        # PyObjC 的 delegate 必须是强引用——局部变量被回收后
        # 系统回调就找不到对象了。
        _delegate_keepalive.append(delegate)
        center = UN.UNUserNotificationCenter.currentNotificationCenter()
        center.setDelegate_(delegate)
        return True
    except Exception as err:
        log.debug(CAT_GUI, f"通知 delegate 安装失败: {err}")
        return False

#: 持有 delegate 的强引用，防止被 GC 回收。
_delegate_keepalive: list[Any] = []
