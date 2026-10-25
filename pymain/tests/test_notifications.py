"""通知层测试：F126 / F127 / F128 / F129 / F130。

覆盖：
- F126 完成通知：identifier = `complete-<task_id>`，标题/副标题/正文
  按 Swift 版，sound = default。
- F127 失败通知：identifier = `failure-<task_id>`，正文是错误信息。
- F128 启动申请权限：requestAuthorization 的 options 是
  [.alert, .sound, .badge]。
- F129 前台也弹横幅：willPresent 返回 [.banner, .sound]。
- F130 点通知回主窗口：didReceive 触发点击回调。

**这些测试不碰真的通知中心。** UNUserNotificationCenter 在没有
bundle identity 的进程里调用会直接 abort 进程（NSException，Python
的 try/except 接不住），所以测试用假的 UN 模块替换掉。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm import notifications

# -- 假的 UserNotifications 模块 ----------------------------------


class _FakeContent:
    """假的 UNMutableNotificationContent。

    PyObjC 里 `alloc()` / `init()` 是类方法风格的链式调用，
    这里用 classmethod 还原。
    """

    def __init__(self) -> None:
        self.title = ""
        self.subtitle = ""
        self.body = ""
        self.sound = None
        self.user_info: dict = {}

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def setTitle_(self, value):
        self.title = value

    def setSubtitle_(self, value):
        self.subtitle = value

    def setBody_(self, value):
        self.body = value

    def setSound_(self, value):
        self.sound = value

    def setUserInfo_(self, value):
        self.user_info = value


class _FakeRequest:
    """假的 UNNotificationRequest。

    生产代码调的是类方法 `requestWithIdentifier_content_trigger_`，
    这里还原成 classmethod。
    """

    def __init__(self, identifier, content, trigger=None):
        self.identifier = identifier
        self.content = content
        self.trigger = trigger

    @classmethod
    def requestWithIdentifier_content_trigger_(cls, identifier, content, trigger):
        return cls(identifier, content, trigger)


class _FakeCenter:
    """假的 UNUserNotificationCenter。记录所有调用。"""

    def __init__(self) -> None:
        self.added: list[_FakeRequest] = []
        self.delegate = None
        self.auth_options = None
        self.auth_requested = False

    def setDelegate_(self, delegate):
        self.delegate = delegate

    def addRequest_withCompletionHandler_(self, request, handler):
        self.added.append(request)

    def requestAuthorizationWithOptions_completionHandler_(
        self, options, handler
    ):
        self.auth_options = options
        self.auth_requested = True


@pytest.fixture
def fake_un(monkeypatch):
    """装一个假的 UserNotifications 模块 + 假的中心。

    返回中心实例，测试直接查它记录了什么。
    """
    center = _FakeCenter()

    module = types.ModuleType("UserNotifications")
    module.UNMutableNotificationContent = _FakeContent
    module.UNNotificationRequest = _FakeRequest
    module.UNNotificationSound = types.SimpleNamespace(
        defaultSound=lambda: "default-sound"
    )
    module.UNUserNotificationCenter = types.SimpleNamespace(
        currentNotificationCenter=lambda: center
    )
    module.UNAuthorizationOptionAlert = 4
    module.UNAuthorizationOptionSound = 2
    module.UNAuthorizationOptionBadge = 1
    module.UNNotificationPresentationOptionBanner = 16
    module.UNNotificationPresentationOptionSound = 2

    monkeypatch.setitem(sys.modules, "UserNotifications", module)

    # 假的 Foundation —— 生产代码要从这儿拿 NSObject。
    #
    # **Linux 上没有 PyObjC**，真 Foundation 根本 import 不了。
    # 之前的 fake 只注入了 UserNotifications，在 macOS 上因为真
    # PyObjC 兜住了 Foundation 才没暴露，Linux runner 上直接
    # ImportError。
    #
    # 生产代码用 `type(name, (NSObject,), {...})` 建 delegate 类，
    # 不用 objc.registerClass——PyObjC 自己会注册。所以这里的
    # NSObject 只需要是个能当基类用的东西。
    class _FakeNSObject:
        """假的 NSObject。

        PyObjC 的 NSObject 子类自带 `alloc()` / `init()`——生产代码
        里是 `_delegate_class.alloc().init()`。这里得还原，否则
        装 delegate 那步会 AttributeError。

        真的 NSObject 还能 `type(name, (NSObject,), {...})` 建子类，
        Python 的普通类也能，所以直接用普通类。
        """

        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

    foundation = types.ModuleType("Foundation")
    foundation.NSObject = _FakeNSObject
    foundation.NSBundle = types.SimpleNamespace(
        mainBundle=lambda: types.SimpleNamespace(bundleIdentifier=lambda: "com.test.app")
    )
    monkeypatch.setitem(sys.modules, "Foundation", foundation)

    # 让 _un_available() 认为有 bundle identity、框架也装了
    monkeypatch.setattr(notifications, "_bundle_identifier", lambda: "com.test.app")
    # 清掉 delegate 类缓存——否则第二个测试用的是上一个测试建的
    # 类，测不到真实的建类路径。
    #
    # 生产里这个缓存是必须的：PyObjC 按类名往 ObjC 运行时注册，
    # 同名类第二次定义会抛「is overriding existing Objective-C
    # class」。
    monkeypatch.setattr(notifications, "_delegate_class", None)
    monkeypatch.setattr(notifications, "_delegate_keepalive", [])
    return center


@pytest.fixture(autouse=True)
def _reset_click_handler():
    notifications.set_click_handler(None)
    yield
    notifications.set_click_handler(None)


# -- F126 / F127 通知内容与 identifier ----------------------------


def test_complete_notification_matches_swift(fake_un):
    """完成通知：标题/副标题/正文/sound/identifier 跟 Swift 版一致。"""
    notifications.notify(
        "下载完成",
        subtitle="movie.mp4",
        body="1.2 GB",
        task_id="abc-123",
        kind="complete",
    )

    assert len(fake_un.added) == 1
    req = fake_un.added[0]
    assert req.identifier == "complete-abc-123"
    assert req.content.title == "下载完成"
    assert req.content.subtitle == "movie.mp4"
    assert req.content.body == "1.2 GB"
    assert req.content.sound == "default-sound"
    assert req.content.user_info == {"taskID": "abc-123"}


def test_failure_notification_matches_swift(fake_un):
    """失败通知：identifier 是 failure-<id>，正文是错误信息。"""
    notifications.notify(
        "下载失败",
        subtitle="broken.zip",
        body="连接被重置",
        task_id="def-456",
        kind="failure",
    )

    req = fake_un.added[0]
    assert req.identifier == "failure-def-456"
    assert req.content.title == "下载失败"
    assert req.content.body == "连接被重置"


def test_identifier_falls_back_to_content_hash(fake_un):
    """没给 task id 时，identifier 从内容派生——同一内容 identifier 相同。"""
    notifications.notify("下载完成", "a.mp4", "1 GB")
    notifications.notify("下载完成", "a.mp4", "1 GB")

    assert len(fake_un.added) == 2
    assert fake_un.added[0].identifier == fake_un.added[1].identifier
    assert fake_un.added[0].identifier.startswith("sdm-")


def test_distinct_tasks_get_distinct_identifiers(fake_un):
    """不同任务 → 不同 identifier，不会互相替换掉。"""
    notifications.notify("下载完成", "a.mp4", "1 GB", task_id="id-1", kind="complete")
    notifications.notify("下载完成", "b.mp4", "2 GB", task_id="id-2", kind="complete")

    assert fake_un.added[0].identifier != fake_un.added[1].identifier


# -- F128 启动申请权限 --------------------------------------------


def test_request_permission_uses_swift_options(fake_un):
    """权限申请的 options 是 [.alert, .sound, .badge]。"""
    assert notifications.request_permission() is True
    assert fake_un.auth_requested is True
    assert fake_un.auth_options == 4 | 2 | 1


def test_request_permission_without_bundle_returns_true(monkeypatch):
    """没有 bundle identity 时没法调 UN 的申请接口，但不算失败。"""
    monkeypatch.setattr(notifications, "_bundle_identifier", lambda: None)
    assert notifications.request_permission() is True


# -- F129 前台也弹横幅 --------------------------------------------


def test_will_present_returns_banner_and_sound(fake_un):
    """前台时返回 [.banner, .sound]——不能静默吞掉。"""
    assert notifications.install_delegate() is True
    delegate = fake_un.delegate
    assert delegate is not None

    captured: list[int] = []
    delegate.userNotificationCenter_willPresentNotification_withCompletionHandler_(
        None, None, captured.append
    )

    assert len(captured) == 1
    # UNNotificationPresentationOptionBanner(16) | Sound(2)
    assert captured[0] == 16 | 2


# -- F130 点通知回主窗口 ------------------------------------------


def test_did_receive_fires_click_handler(fake_un):
    """点通知触发点击回调。"""
    clicked: list[int] = []
    notifications.set_click_handler(lambda: clicked.append(1))

    assert notifications.install_delegate() is True
    delegate = fake_un.delegate

    done: list[int] = []
    delegate.userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
        None, None, lambda: done.append(1)
    )

    assert clicked == [1]
    assert done == [1]


def test_click_handler_exception_does_not_escape(fake_un):
    """点击回调自己抛异常，不能把通知流程带崩。"""
    def boom() -> None:
        raise RuntimeError("窗口没了")

    notifications.set_click_handler(boom)
    assert notifications.install_delegate() is True

    done: list[int] = []
    # 不抛异常就算过
    fake_un.delegate.userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
        None, None, lambda: done.append(1)
    )
    assert done == [1]


def test_install_delegate_without_bundle_returns_false(monkeypatch):
    """没有 bundle identity 时不能装 delegate（碰框架会 abort 进程）。"""
    monkeypatch.setattr(notifications, "_bundle_identifier", lambda: None)
    assert notifications.install_delegate() is False


def test_install_delegate_twice_does_not_crash(fake_un):
    """重复装 delegate 不能炸。

    PyObjC 按类名往 ObjC 运行时注册，同名类第二次定义会抛
    「is overriding existing Objective-C class」。所以类名里带
    了自增序号——这个测试锁住这个行为。
    """
    assert notifications.install_delegate() is True
    assert notifications.install_delegate() is True
    # 两次装的是不同类，但都是有效的 delegate
    assert fake_un.delegate is not None


# -- 兜底路径 ------------------------------------------------------


def test_falls_back_to_osascript_without_bundle(monkeypatch):
    """没有 bundle identity 时走 osascript，不碰 UN 框架。"""
    monkeypatch.setattr(notifications, "_bundle_identifier", lambda: None)
    calls: list[list[str]] = []

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(notifications.subprocess, "run", fake_run)

    assert notifications.notify("下载完成", "a.mp4", "1 GB") is True
    assert len(calls) == 1
    assert calls[0][0] == "osascript"
    assert "下载完成" in calls[0][-1]


def test_un_unavailable_without_bundle_id(monkeypatch):
    """没有 bundle identity 时 UN 路径必须判为不可用。

    **这是防 abort 进程的那道闸。** UserNotifications 框架在无
    bundle 的进程里调 `currentNotificationCenter()` 抛 ObjC 异常
    直接 abort 进程——Python 的 try/except 接不住。所以判定必须
    在碰框架之前就返回 True。

    框架本身要装上（fake 一个），否则「框架缺失」那条分支会
    先返回 True，把 bundle 探测的失效盖住。
    """
    monkeypatch.setattr(notifications, "_bundle_identifier", lambda: None)
    monkeypatch.setitem(
        sys.modules, "UserNotifications", types.ModuleType("UserNotifications")
    )
    assert notifications._un_available() is True


def test_un_unavailable_when_framework_missing(monkeypatch):
    """框架没装时 UN 路径判为不可用。"""
    monkeypatch.setattr(notifications, "_bundle_identifier", lambda: "com.test.app")
    monkeypatch.delitem(sys.modules, "UserNotifications", raising=False)

    real_import = __import__

    def blocked_import(name, *args, **kwargs):
        if name == "UserNotifications":
            raise ImportError("no UserNotifications")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", blocked_import)
    assert notifications._un_available() is True


def test_un_available_with_bundle_and_framework(monkeypatch):
    """有 bundle identity 且框架装了 → UN 路径可用。"""
    monkeypatch.setattr(notifications, "_bundle_identifier", lambda: "com.test.app")
    monkeypatch.setitem(
        sys.modules, "UserNotifications", types.ModuleType("UserNotifications")
    )
    assert notifications._un_available() is False


def test_falls_back_to_osascript_when_framework_missing(monkeypatch):
    """框架没装时也走 osascript，不抛异常。"""
    monkeypatch.setattr(notifications, "_bundle_identifier", lambda: "com.test.app")
    # 把 UserNotifications 从 sys.modules 里摘掉，模拟没装
    monkeypatch.delitem(sys.modules, "UserNotifications", raising=False)
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "UserNotifications":
            raise ImportError("no UserNotifications")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", blocked_import)

    calls: list[list[str]] = []

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(notifications.subprocess, "run", fake_run)

    assert notifications.notify("下载完成", "a.mp4", "1 GB") is True
    assert calls[0][0] == "osascript"
