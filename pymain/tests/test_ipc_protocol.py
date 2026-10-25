"""IPC 协议层测试：F165 / F166 / F173 / F174 / F053。

Swift 参照：
- `CLI/SDMDM.swift:210-245, 350-411`（F165）
- `Daemon/main.swift:17-26`（F166）
- `Daemon/IPC/Dispatcher.swift:52-91`（F173）

验收要点：
- 请求必须以换行结尾
- socket 不存在时给出可读的错误，路径原样带出来
- **响应上限 1 MB**，超过就报错，不静默截断
- daemon 必须靠 `SDM_DAEMON=1` 才进守护模式
- `add` 的 category 真的决定落盘目录，referer 真的传下去
- `list` 的结果里有 `total` 键（Swift 协议），大小未知时是 None
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm import paths as sdm_paths
from sdm.daemon import main as daemon_main
from sdm.ipc import client as ipc_client

# -- F165 响应上限 --------------------------------------------------


def test_max_response_bytes_is_1mb():
    """上限是 1 MB。Swift CLI/SDMDM.swift:406-408 同值。"""
    assert ipc_client.MAX_RESPONSE_BYTES == 1024 * 1024


def test_read_response_rejects_oversized():
    """超过上限直接报错，不静默截断。

    截断出来的 JSON 解析会报一个跟真实原因无关的错，排查时容易
    被带偏。
    """

    class _FakeSock:
        def __init__(self):
            self._sent = 0

        def recv(self, size):
            self._sent += size
            return b"x" * size

    sock = _FakeSock()
    with pytest.raises(ipc_client.ClientError) as exc:
        ipc_client.IPCClient._read_response(sock)
    assert "上限" in str(exc.value)


def test_read_response_returns_normal_payload():
    """正常响应照常返回（去掉尾部的换行）。"""

    class _FakeSock:
        def __init__(self, payload):
            self._buf = payload

        def recv(self, size):
            chunk, self._buf = self._buf[:size], self._buf[size:]
            return chunk

    got = ipc_client.IPCClient._read_response(_FakeSock(b'{"ok":true}\n'))
    assert got == '{"ok":true}'


def test_read_response_empty_raises():
    """对端没响应就断开时报错。"""

    class _FakeSock:
        def recv(self, size):
            return b""

    with pytest.raises(ipc_client.ClientError):
        ipc_client.IPCClient._read_response(_FakeSock())


def test_request_payload_ends_with_newline(monkeypatch, tmp_path):
    """请求必须以换行结尾。

    `_exchange_unix` 会先检查 socket 文件存不存在，不存在就直接
    报错、根本不发请求——所以这里要造一个真的存在的路径。
    """
    sent: list[bytes] = []

    class _FakeSock:
        def settimeout(self, value):
            pass

        def connect(self, path):
            pass

        def sendall(self, data):
            sent.append(data)

        def recv(self, size):
            return b'{"ok":true}\n'

        def close(self):
            pass

    sock_file = tmp_path / "sdmd.sock"
    sock_file.touch()

    monkeypatch.setattr(ipc_client.socket, "socket", lambda *a, **k: _FakeSock())
    monkeypatch.setattr(sdm_paths, "socket_path", lambda: str(sock_file))

    cli = ipc_client.IPCClient()
    cli.send({"id": "r1", "method": "ping"})

    assert sent, "没有发出任何请求"
    assert sent[0].endswith(b"\n")


def test_missing_socket_gives_readable_error(monkeypatch, tmp_path):
    """连不上守护进程时给出可读的错误。

    **两平台的错误信息不一样，断言也不一样：**

    - Unix：socket 文件不存在，报「守护进程不可达（<路径> 不存在）」，
      路径原样带出来
    - Windows：走 TCP 回环，socket 文件那一套不存在，错误是
      「连接守护进程失败: [WinError 10061] ...」

    两边都得说人话，都得提到守护进程。
    """
    missing = tmp_path / "nope.sock"
    monkeypatch.setattr(sdm_paths, "socket_path", lambda: str(missing))

    cli = ipc_client.IPCClient()
    with pytest.raises(ipc_client.ClientError) as exc:
        cli.send({"id": "r1", "method": "ping"})

    message = str(exc.value)
    assert "守护进程" in message, f"错误信息没说清楚：{message}"

    if sys.platform == "win32":
        # Windows 上没有 unix socket，路径不会出现在错误里。
        assert "连接守护进程失败" in message, message
    else:
        assert str(missing) in message, message


# -- F166 daemon 守卫 ----------------------------------------------


def test_daemon_mode_requires_env_1(monkeypatch):
    """只有 SDM_DAEMON=1 才进守护模式。"""
    monkeypatch.delenv("SDM_DAEMON", raising=False)
    assert daemon_main.daemon_mode_enabled() is False

    monkeypatch.setenv("SDM_DAEMON", "0")
    assert daemon_main.daemon_mode_enabled() is False

    monkeypatch.setenv("SDM_DAEMON", "true")
    assert daemon_main.daemon_mode_enabled() is False

    monkeypatch.setenv("SDM_DAEMON", "")
    assert daemon_main.daemon_mode_enabled() is False

    monkeypatch.setenv("SDM_DAEMON", "1")
    assert daemon_main.daemon_mode_enabled() is True


def test_daemon_without_env_exits_zero(monkeypatch, capsys):
    """没有 SDM_DAEMON=1 时打印提示并 exit 0（不是 exit 1）。"""
    monkeypatch.delenv("SDM_DAEMON", raising=False)
    rc = daemon_main.main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "SDM_DAEMON" in captured.err


# -- F165/F166 socket 路径覆盖 -------------------------------------


def test_socket_path_env_override(monkeypatch):
    """SDM_SOCKET 环境变量覆盖 socket 路径。

    Swift CLI/SDMDM.swift:212 和 Daemon/main.swift:24 都读这个变量。
    """
    monkeypatch.setenv("SDM_SOCKET", "/tmp/custom-sdm.sock")
    assert sdm_paths.socket_path() == "/tmp/custom-sdm.sock"


def test_socket_path_default_without_env(monkeypatch):
    """没有环境变量时走默认路径。"""
    monkeypatch.delenv("SDM_SOCKET", raising=False)
    got = sdm_paths.socket_path()
    assert "sdmd.sock" in got or got.startswith("npipe://")


# -- F173 / F174 dispatcher ----------------------------------------


def _make_dispatcher(tmp_path, monkeypatch):
    """造一个 dispatcher，引擎是假的。"""
    from sdm.ipc import dispatcher as disp_mod

    started: list = []

    class _FakeEngine:
        def __init__(self):
            self.tasks = {}

        async def start(self, specs):
            started.extend(specs)
            return ["task-1"]

        def get(self, task_id):
            from sdm.engine.download_engine import TaskSummary

            return TaskSummary(
                id=task_id,
                url="http://example.com/a.zip",
                state="downloading",
                destination=str(tmp_path / "a.zip"),
            )

        def snapshot(self):
            from sdm.engine.download_engine import TaskSummary

            return [
                TaskSummary(
                    id="task-1",
                    url="http://example.com/a.zip",
                    state="downloading",
                    bytes_done=100,
                    total_bytes=1000,
                ),
                TaskSummary(
                    id="task-2",
                    url="http://example.com/b.zip",
                    state="paused",
                    bytes_done=50,
                    total_bytes=None,
                ),
            ]

    monkeypatch.setattr(disp_mod.paths, "download_dir", lambda: tmp_path)
    # **固定住自动分类开关。** 不固定的话测试会读用户真实配置
    # （~/Library/Application Support/SuperDownloadManager/settings.json），
    # 用户的设置一变测试就红——而且测试不该依赖用户的环境。
    # 要测关闭分类的场景，单独 patch 这个函数。
    monkeypatch.setattr(disp_mod, "_classify_enabled", lambda: True)
    eng = _FakeEngine()
    d = disp_mod.Dispatcher(eng)
    return d, started


def _request(method: str, params=None) -> str:
    """构造一条 IPC 请求的 JSON 串。"""
    return json.dumps({"id": "r1", "method": method, "params": params or {}})


def _result(raw: str):
    """从 handle 的返回值里取出 result。"""
    payload = json.loads(raw)
    assert payload["ok"], payload.get("error")
    return payload["result"]


def test_add_category_decides_folder(tmp_path, monkeypatch):
    """category 真的决定落盘目录。

    Swift 版有个 bug：category 被读取后丢弃（`_ = defaultCategory`），
    落盘固定 ~/Downloads/。Python 版必须真正实现。
    """
    d, started = _make_dispatcher(tmp_path, monkeypatch)

    asyncio.run(d.handle(_request("add", {"url": "http://example.com/a.zip", "category": "video"})))

    assert started, "没有启动任何任务"
    spec = started[0]
    assert spec.destination_directory.name.lower() == "video"


def test_add_referer_passed_to_spec(tmp_path, monkeypatch):
    """referer 真的传进 StartSpec。"""
    d, started = _make_dispatcher(tmp_path, monkeypatch)

    asyncio.run(
        d.handle(_request("add", {"url": "http://example.com/a.zip", "referer": "http://ref/"}))
    )

    assert started[0].referer == "http://ref/"


def test_add_no_filename_leaves_override_empty(tmp_path, monkeypatch):
    """没给 filename 时 filename_override 必须是 None。

    填了猜测值的话，引擎的 `not spec.filename_override` 判据为假，
    服务器返回的真实文件名就被挡掉了（F053 失效）。
    """
    d, started = _make_dispatcher(tmp_path, monkeypatch)

    asyncio.run(d.handle(_request("add", {"url": "http://example.com/a.zip"})))

    assert started[0].filename_override is None


def test_add_explicit_filename_is_kept(tmp_path, monkeypatch):
    """用户真给了 filename 就用它。"""
    d, started = _make_dispatcher(tmp_path, monkeypatch)

    asyncio.run(
        d.handle(_request("add", {"url": "http://example.com/a.zip", "filename": "mine.zip"}))
    )

    assert started[0].filename_override == "mine.zip"


def test_list_has_total_key(tmp_path, monkeypatch):
    """list 的结果里有 `total` 键——Swift 协议用的是这个名。

    Python 的 TaskSummary 用 `total_bytes`，但外部脚本是按 Swift
    协议写的，读 `total`。没有这个键就落空。
    """
    d, _ = _make_dispatcher(tmp_path, monkeypatch)

    entries = _result(asyncio.run(d.handle(_request("list"))))
    assert isinstance(entries, list)
    assert len(entries) == 2
    assert "total" in entries[0]
    assert entries[0]["total"] == 1000
    # 大小未知时是 null，不是 0
    assert entries[1]["total"] is None


def test_list_keeps_swift_keys(tmp_path, monkeypatch):
    """Swift 协议里的四个键都在。"""
    d, _ = _make_dispatcher(tmp_path, monkeypatch)

    entry = _result(asyncio.run(d.handle(_request("list"))))[0]
    for key in ("id", "url", "state", "bytes_done"):
        assert key in entry, f"缺少 Swift 协议的键: {key}"


# -- F052 关闭自动分类 ----------------------------------------------


def test_add_respects_classify_disabled(tmp_path, monkeypatch):
    """关闭自动分类时落基础目录，不进分类子目录。

    Swift 对照：App/MainWindow/DownloadListStore.swift:367
    `if !classifyEnabled { return downloads }`。
    """
    d, started = _make_dispatcher(tmp_path, monkeypatch)

    monkeypatch.setattr(
        "sdm.ipc.dispatcher._classify_enabled", lambda: False
    )

    asyncio.run(
        d.handle(_request("add", {"url": "http://example.com/a.zip", "category": "video"}))
    )

    assert started, "没有启动任何任务"
    # 基础目录本身，不是 Video 子目录
    assert started[0].destination_directory == tmp_path


def test_add_uses_category_folder_when_enabled(tmp_path, monkeypatch):
    """开着分类时照常落分类子目录。"""
    d, started = _make_dispatcher(tmp_path, monkeypatch)

    monkeypatch.setattr(
        "sdm.ipc.dispatcher._classify_enabled", lambda: True
    )

    asyncio.run(
        d.handle(_request("add", {"url": "http://example.com/a.zip", "category": "video"}))
    )

    assert started[0].destination_directory.name.lower() == "video"


def test_classify_enabled_reads_shared_settings(monkeypatch):
    """读的是 GUI 和 CLI 共享的那份设置。"""
    from sdm.ipc import dispatcher as disp_mod

    class _FakeStore:
        class settings:
            classify_enabled = False

    monkeypatch.setattr(
        "sdm.settings.SettingsStore.shared", staticmethod(lambda: _FakeStore())
    )
    assert disp_mod._classify_enabled() is False


def test_classify_enabled_defaults_true_on_error(monkeypatch):
    """读不到设置就当开着——默认行为是分类。"""
    from sdm.ipc import dispatcher as disp_mod

    def boom():
        raise RuntimeError("配置读不了")

    monkeypatch.setattr("sdm.settings.SettingsStore.shared", staticmethod(boom))
    assert disp_mod._classify_enabled() is True
