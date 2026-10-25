"""路径解析的边界情况。

unix socket 的路径长度上限是真实存在的限制——macOS 104 字节，
Linux 108。超了 socket 建不出来，daemon 启动直接失败。
"""

from __future__ import annotations

import os
import sys

import pytest

from sdm import paths


def test_socket_path_is_short_enough_for_unix(tmp_path, monkeypatch):
    """算出来的 socket 路径必须在 unix 的长度限制内。

    macOS 是 104 字节，Linux 108。这里按最严的 104 算。
    """
    monkeypatch.setenv("HOME", str(tmp_path / "h" / "a-fairly-long-home-directory-name"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    path = paths.socket_path()
    if paths.is_windows():
        pytest.skip("Windows 走 npipe，不受路径长度限制")
    assert len(path.encode("utf-8")) < 104, f"socket 路径太长：{len(path)} 字节 -> {path}"


def test_socket_path_falls_back_when_too_long(tmp_path, monkeypatch):
    """HOME 路径长到装不下时，退回 /tmp 下的短路径。"""
    if paths.is_windows():
        pytest.skip("Windows 走 npipe")
    deep = tmp_path / ("d" * 40) / ("e" * 40) / ("f" * 40)
    monkeypatch.setenv("HOME", str(deep))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    path = paths.socket_path()

    assert len(path.encode("utf-8")) < 104
    assert path.startswith("/tmp/")
    assert str(os.getuid()) in path


def test_socket_path_normal_case_uses_config_dir(monkeypatch):
    """正常情况下还是用配置目录，不用 /tmp。

    这里不能用 pytest 的 tmp_path——它在 /var/folders/... 下，路径本来
    就长，会把 fallback 触发掉。直接给一个短 HOME。
    """
    if paths.is_windows():
        pytest.skip("Windows 走 npipe")
    monkeypatch.setenv("HOME", "/tmp/sdm-home")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    path = paths.socket_path()

    assert "sdmd.sock" in path
    # fallback 路径是 /tmp/sdm-<uid>.sock，一整个路径就是这个
    assert path != f"/tmp/sdm-{os.getuid()}.sock"


def test_socket_path_daemon_and_client_agree(tmp_path, monkeypatch):
    """daemon 和 CLI 必须算出同一个路径。

    两边都调 paths.socket_path()，但 HOME 被改时不能一个用长路径
    一个用短路径——那 daemon 起了 CLI 却连不上。
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    first = paths.socket_path()
    second = paths.socket_path()
    assert first == second


def test_config_dir_respects_xdg_data_home(tmp_path, monkeypatch):
    """Linux 下 XDG_DATA_HOME 优先。"""
    if paths.is_macos() or paths.is_windows():
        pytest.skip("只 Linux 用 XDG_DATA_HOME")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.config_dir() == tmp_path / "xdg" / "SuperDownloadManager"


def test_download_dir_is_home_downloads(tmp_path, monkeypatch):
    # Path.home() 在 Windows 上读 USERPROFILE，POSIX 上读 HOME——
    # 改错环境变量的话测试在 Windows 上必然失败。
    home = tmp_path / "home"
    if paths.is_windows():
        monkeypatch.setenv("USERPROFILE", str(home))
    else:
        monkeypatch.setenv("HOME", str(home))
    assert paths.download_dir() == home / "Downloads"


def test_ensure_dirs_creates_everything(tmp_path, monkeypatch):
    # 每个平台用的环境变量不一样，全设上——漏一个就会在真机上
    # 往真实目录里建文件夹。
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    paths.ensure_dirs()
    assert paths.config_dir().is_dir()
    assert paths.log_dir().is_dir()


def test_paths_use_forward_slashes_on_posix():
    """socket 路径在 posix 上用正斜杠。"""
    if paths.is_windows():
        pytest.skip("Windows 走 npipe")
    assert "\\" not in paths.socket_path()


def test_all_platform_helpers_agree():
    """is_windows / is_macos 不能同时为真。"""
    assert not (paths.is_windows() and paths.is_macos())
    if sys.platform == "darwin":
        assert paths.is_macos()
