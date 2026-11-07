"""Windows 上命令行的可用性。

**这里守的是三件具体的事。**

1. **`.cmd` shim 不能因为中文路径而写不出来。**
   `%LOCALAPPDATA%` 里带用户名——中文用户名（`C:\\Users\\张三\\...`）
   在中文 Windows 上很常见。shim 用 ASCII 编码写绝对路径会直接抛
   `UnicodeEncodeError`，**整个 CLI 安装失败**，用户在 PATH 上根本
   没有 `sdm`。改用相对路径（`%~dp0..\\runtime\\...`）就全是 ASCII，
   跟用户名无关。

2. **GUI 启动不能杀掉正在跑的 daemon。**
   每次启动都 `schtasks /Delete` 再重建，正在下载的任务会被打断。
   任务已经存在且指向同一个 exe 时，只 `/Run` 不重建。

3. **命令行的任务要在 GUI 里看得见。**
   CLI 优先连 GUI 托管的端点（用的是 GUI 那个引擎），GUI 没跑才
   退回 daemon。

macOS 上这些路径大多不执行，所以这里用 monkeypatch 强制走 Windows
分支——CI 的 Windows runner 是最终裁判，本机也要能提前抓到。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from sdm import paths
from sdm.gui import bootstrap
from sdm.ipc import client as ipc_client
from sdm.version import GUI_CLI_PORT_WINDOWS, SOCKET_PORT_WINDOWS


@pytest.fixture
def as_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 paths.is_windows() 返回 True。"""
    monkeypatch.setattr(paths, "is_windows", lambda: True)


# -- 1. .cmd shim ----------------------------------------------------


def _write_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """跑一遍 shim 写入逻辑，返回 shim 路径。"""
    base = tmp_path / "AppData" / "Local" / "SuperDownloadManager"
    runtime = base / "runtime"
    (runtime / "sdm").mkdir(parents=True)
    (runtime / "sdm" / "sdm.exe").write_bytes(b"MZ")
    (runtime / "sdmd").mkdir(parents=True)
    (runtime / "sdmd" / "sdmd.exe").write_bytes(b"MZ")

    monkeypatch.setattr(bootstrap, "_windows_appdata", lambda: base)
    # zip 都当成「已经解过」——这里只关心 shim 和计划任务。
    monkeypatch.setattr(bootstrap, "_extract_zip_once", lambda archive, dest: True)
    monkeypatch.setattr(bootstrap, "install_native_host_windows", lambda exe: {"ok": True, "path": ""})
    monkeypatch.setattr(bootstrap, "subprocess", _FakeSubprocess())
    # winreg 在 macOS 上没有——PATH 那段直接跳过。
    monkeypatch.setitem(sys.modules, "winreg", None)

    monkeypatch.setattr(bootstrap.sys, "frozen", True, raising=False)
    monkeypatch.setattr(bootstrap.sys, "_MEIPASS", str(tmp_path / "meipass"), raising=False)

    bootstrap._ensure_windows()
    return base / "bin" / "sdm.cmd"


def test_shim_survives_a_non_ascii_install_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """装到中文路径下，shim 照样写得出来。

    用户名是中文的 Windows 上，`%LOCALAPPDATA%` 就是
    `C:\\Users\\张三\\AppData\\Local\\...`。shim 写绝对路径 + ASCII
    编码 = `UnicodeEncodeError` = **CLI 根本没装上**。
    """
    # 造一个带中文的安装根目录
    base = tmp_path / "用户名字" / "AppData" / "Local" / "SuperDownloadManager"
    runtime = base / "runtime"
    (runtime / "sdm").mkdir(parents=True)
    (runtime / "sdm" / "sdm.exe").write_bytes(b"MZ")
    (runtime / "sdmd").mkdir(parents=True)
    (runtime / "sdmd" / "sdmd.exe").write_bytes(b"MZ")

    monkeypatch.setattr(bootstrap, "_windows_appdata", lambda: base)
    monkeypatch.setattr(bootstrap, "_extract_zip_once", lambda archive, dest: True)
    monkeypatch.setattr(bootstrap, "install_native_host_windows", lambda exe: {"ok": True, "path": ""})
    monkeypatch.setattr(bootstrap, "subprocess", _FakeSubprocess())
    monkeypatch.setitem(sys.modules, "winreg", None)
    monkeypatch.setattr(bootstrap.sys, "frozen", True, raising=False)
    monkeypatch.setattr(bootstrap.sys, "_MEIPASS", str(tmp_path / "meipass"), raising=False)

    # 不该抛异常——原来这里抛 UnicodeEncodeError
    bootstrap._ensure_windows()

    shim = base / "bin" / "sdm.cmd"
    assert shim.is_file(), "shim 没写出来"
    body = shim.read_bytes()
    # **内容必须是纯 ASCII**——cmd 读 .cmd 按系统 ANSI 代码页，
    # 里面塞非 ASCII 会被拆成乱码。
    body.decode("ascii")
    # 走相对路径，不带绝对路径
    assert b"~dp0" in body
    assert b"sdm.exe" in body


def test_shim_points_at_the_right_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """shim 要指向 runtime 里的 CLI exe。"""
    shim = _write_shim(tmp_path, monkeypatch)
    body = shim.read_text(encoding="ascii")
    assert "runtime" in body
    assert "sdm.exe" in body
    # 相对路径：从 bin\ 目录出发
    assert "..\\runtime\\sdm\\sdm.exe" in body


# -- 2. 计划任务不能杀正在跑的 daemon -------------------------------


class _FakeSubprocess:
    """记录 schtasks 调用。"""

    def __init__(self, task_xml: str = "") -> None:
        self.calls: list[list[str]] = []
        self.task_xml = task_xml

    def run(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        code = subprocess.CompletedProcess(cmd, 0, "", "")
        if "/XML" in cmd:
            code = subprocess.CompletedProcess(cmd, 0, self.task_xml, "")
        return code

    def verbs(self) -> list[str]:
        """schtasks 的子命令（去掉前导斜杠，好比较）。"""
        return [c[1].lstrip("/") for c in self.calls if c[0] == "schtasks"]


def _run_ensure_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake) -> dict:
    base = tmp_path / "AppData" / "Local" / "SuperDownloadManager"
    runtime = base / "runtime"
    (runtime / "sdm").mkdir(parents=True)
    (runtime / "sdm" / "sdm.exe").write_bytes(b"MZ")
    (runtime / "sdmd").mkdir(parents=True)
    (runtime / "sdmd" / "sdmd.exe").write_bytes(b"MZ")

    monkeypatch.setattr(bootstrap, "_windows_appdata", lambda: base)
    monkeypatch.setattr(bootstrap, "_extract_zip_once", lambda archive, dest: True)
    monkeypatch.setattr(bootstrap, "install_native_host_windows", lambda exe: {"ok": True, "path": ""})
    monkeypatch.setattr(bootstrap, "subprocess", fake)
    monkeypatch.setitem(sys.modules, "winreg", None)
    monkeypatch.setattr(bootstrap.sys, "frozen", True, raising=False)
    monkeypatch.setattr(bootstrap.sys, "_MEIPASS", str(tmp_path / "meipass"), raising=False)
    return bootstrap._ensure_windows()


def test_existing_task_is_not_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """任务已经指向同一个 exe 时，不能 Delete 再重建。

    Delete 会**杀掉正在跑的 daemon**——正在下载的文件被打断，
    用户得手动重试。GUI 每次启动都这么干一次的话，开一次 GUI
    断一次下载。
    """
    base = tmp_path / "AppData" / "Local" / "SuperDownloadManager"
    daemon_exe = base / "runtime" / "sdmd" / "sdmd.exe"
    xml = f"<Task><Actions><Exec><Command>{daemon_exe}</Command></Exec></Actions></Task>"
    fake = _FakeSubprocess(task_xml=xml)
    result = _run_ensure_windows(tmp_path, monkeypatch, fake)

    assert result["daemon"]["ok"] is True
    assert result["daemon"]["reason"] == "task_already_registered"
    assert "Delete" not in fake.verbs(), "把正在跑的计划任务删了"
    assert "Create" not in fake.verbs(), "重建了已经正确的计划任务"
    # 但要确保它在跑
    assert "Run" in fake.verbs()


def test_stale_task_is_recreated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """任务指向别的 exe（升级换路径了）时，要重建。"""
    xml = "<Task><Actions><Exec><Command>C:\\old\\sdmd.exe</Command></Exec></Actions></Task>"
    fake = _FakeSubprocess(task_xml=xml)
    result = _run_ensure_windows(tmp_path, monkeypatch, fake)

    assert result["daemon"]["ok"] is True
    assert result["daemon"]["reason"] == "task_registered"
    assert "Delete" in fake.verbs()
    assert "Create" in fake.verbs()


def test_missing_task_is_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """任务不存在（查不到）时，要创建。"""
    fake = _FakeSubprocess(task_xml="")
    result = _run_ensure_windows(tmp_path, monkeypatch, fake)

    assert result["daemon"]["ok"] is True
    assert result["daemon"]["reason"] == "task_registered"
    assert "Create" in fake.verbs()


# -- 3. 命令行连 GUI，任务在 GUI 里可见 -----------------------------


def test_client_prefers_the_gui_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认先连 GUI 的端点，daemon 排第二。

    GUI 跑着的时候它自己托管一个端点，用的是 GUI 那个引擎。
    命令行加的任务进了它，界面上立刻可见。
    """
    monkeypatch.setattr(paths, "is_windows", lambda: True)
    client = ipc_client.IPCClient()
    candidates = client._candidates()

    assert len(candidates) == 2
    assert candidates[0].port == GUI_CLI_PORT_WINDOWS
    assert candidates[1].port == SOCKET_PORT_WINDOWS


def test_explicit_endpoint_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式指定端点时不做 fallback——调用方要的就是这一个。"""
    monkeypatch.setattr(paths, "is_windows", lambda: True)
    client = ipc_client.IPCClient(port=12345)
    candidates = client._candidates()

    assert len(candidates) == 1
    assert candidates[0].port == 12345


def test_client_falls_back_to_daemon_when_gui_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GUI 端点连不上时，退回 daemon。

    GUI 没跑的时候命令照样得能用——这是 CLI 在 Windows 上最基本
    的可用性。
    """
    monkeypatch.setattr(paths, "is_windows", lambda: True)
    attempted: list[int] = []

    class _FakeSock:
        def settimeout(self, value):
            pass

        def connect(self, target):
            attempted.append(target[1])
            if target[1] == GUI_CLI_PORT_WINDOWS:
                raise ConnectionRefusedError("GUI 没跑")
            self._sent = True

        def sendall(self, data):
            pass

        def recv(self, size):
            return b'{"ok":true}\n'

        def close(self):
            pass

    monkeypatch.setattr(ipc_client.socket, "socket", lambda *a, **k: _FakeSock())

    client = ipc_client.IPCClient()
    response = client.send({"id": "r1", "method": "ping"})

    assert response["ok"] is True
    assert attempted == [GUI_CLI_PORT_WINDOWS, SOCKET_PORT_WINDOWS]


def test_client_reports_both_endpoints_when_both_are_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两个端点都不通时，错误信息要说清楚试过哪些。"""
    monkeypatch.setattr(paths, "is_windows", lambda: True)

    class _FakeSock:
        def settimeout(self, value):
            pass

        def connect(self, target):
            raise ConnectionRefusedError("没人监听")

        def sendall(self, data):
            pass

        def recv(self, size):
            return b""

        def close(self):
            pass

    monkeypatch.setattr(ipc_client.socket, "socket", lambda *a, **k: _FakeSock())

    client = ipc_client.IPCClient()
    with pytest.raises(ipc_client.ClientError) as exc:
        client.send({"id": "r1", "method": "ping"})

    message = str(exc.value)
    assert str(GUI_CLI_PORT_WINDOWS) in message
    assert str(SOCKET_PORT_WINDOWS) in message


def test_unix_client_prefers_the_gui_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unix 上先连 GUI 的 socket。"""
    monkeypatch.setattr(paths, "is_windows", lambda: False)
    gui_sock = tmp_path / "sdm-gui.sock"
    daemon_sock = tmp_path / "sdmd.sock"
    monkeypatch.setattr(paths, "gui_socket_path", lambda: str(gui_sock))
    monkeypatch.setattr(paths, "socket_path", lambda: str(daemon_sock))

    client = ipc_client.IPCClient()
    candidates = client._candidates()

    assert len(candidates) == 2
    assert candidates[0].path == str(gui_sock)
    assert candidates[1].path == str(daemon_sock)


def test_gui_socket_path_differs_from_daemon_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GUI 的 socket 不能跟 daemon 的同一个。

    同一个路径的话，GUI 一起来就把 daemon 的 socket 抢了——daemon
    还在跑但连不上，CLI 的行为变得不可预测。
    """
    monkeypatch.delenv("SDM_SOCKET", raising=False)
    monkeypatch.delenv("SDM_GUI_SOCKET", raising=False)
    assert paths.gui_socket_path() != paths.socket_path()


def test_gui_socket_path_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDM_GUI_SOCKET 可以覆盖 GUI 的 socket 路径。"""
    monkeypatch.setenv("SDM_GUI_SOCKET", "/tmp/custom-gui.sock")
    assert paths.gui_socket_path() == "/tmp/custom-gui.sock"


def test_gui_and_daemon_ports_are_different() -> None:
    """两个端点不能撞端口。"""
    assert GUI_CLI_PORT_WINDOWS != SOCKET_PORT_WINDOWS
    # 也不能撞浏览器扩展的 dispatch 端口
    from sdm.ipc.dispatch_server import DISPATCH_PORT

    assert GUI_CLI_PORT_WINDOWS != DISPATCH_PORT
