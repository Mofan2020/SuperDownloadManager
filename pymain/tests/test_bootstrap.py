"""首次启动自动安装 CLI / daemon 的行为。

这些测试不能碰真机的 /usr/local/bin 和 launchd——用临时目录和
假 subprocess 把它们挡掉。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from sdm.gui import bootstrap


def _fake_resources(tmp_path: Path) -> Path:
    """造一个像打包版 .app 的 Resources 目录。"""
    res = tmp_path / "Resources"
    cli_dir = res / "sdm-cli"
    cli_dir.mkdir(parents=True)
    cli = cli_dir / "sdm"
    cli.write_text("#!/bin/sh\necho sdm\n", encoding="utf-8")
    cli.chmod(0o755)

    daemon_dir = res / "sdmd-daemon"
    daemon_dir.mkdir(parents=True)
    daemon = daemon_dir / "sdmd"
    daemon.write_text("#!/bin/sh\necho sdmd\n", encoding="utf-8")
    daemon.chmod(0o755)
    return res


# ---------------------------------------------------------------- CLI


def test_install_cli_creates_symlink(tmp_path, monkeypatch):
    resources = _fake_resources(tmp_path)
    target = tmp_path / "bin"
    monkeypatch.setattr(bootstrap, "_pick_cli_dir", lambda: (target, True))
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)

    result = bootstrap.install_cli(resources)

    assert result["ok"] is True
    assert result["reason"] == "installed"
    assert result["in_path"] is True
    link = target / "sdm"
    assert link.is_symlink()
    assert os.readlink(link) == str(resources / "sdm-cli" / "sdm")


def test_install_cli_is_idempotent(tmp_path, monkeypatch):
    """第二次跑不该重装，也不该报错。"""
    resources = _fake_resources(tmp_path)
    target = tmp_path / "bin"
    monkeypatch.setattr(bootstrap, "_pick_cli_dir", lambda: (target, True))
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)

    first = bootstrap.install_cli(resources)
    second = bootstrap.install_cli(resources)

    assert first["reason"] == "installed"
    assert second["ok"] is True
    assert second["reason"] == "already"
    assert second["path"] == first["path"]


def test_install_cli_repoints_after_app_moved(tmp_path, monkeypatch):
    """app 换了位置，软链要跟着改。"""
    resources = _fake_resources(tmp_path)
    target = tmp_path / "bin"
    monkeypatch.setattr(bootstrap, "_pick_cli_dir", lambda: (target, True))
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)

    bootstrap.install_cli(resources)

    # 模拟 app 搬家：Resources 里的 CLI 指向变了
    new_res = tmp_path / "Resources2"
    new_res.mkdir()
    (new_res / "sdm-cli").mkdir()
    new_cli = new_res / "sdm-cli" / "sdm"
    new_cli.write_text("#!/bin/sh\n", encoding="utf-8")

    result = bootstrap.install_cli(new_res)

    assert result["ok"] is True
    assert result["reason"] == "installed"
    assert os.readlink(target / "sdm") == str(new_cli)


def test_install_cli_does_not_touch_foreign_command(tmp_path, monkeypatch):
    """PATH 里已经有别的 sdm，不能覆盖。"""
    resources = _fake_resources(tmp_path)
    target = tmp_path / "bin"
    foreign = tmp_path / "elsewhere" / "sdm"
    foreign.parent.mkdir()
    foreign.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(bootstrap, "_pick_cli_dir", lambda: (target, True))
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: str(foreign))

    result = bootstrap.install_cli(resources)

    assert result["ok"] is False
    assert result["reason"] == "conflict"
    assert result["path"] == str(foreign)
    assert not (target / "sdm").exists()


def test_install_cli_missing_binary(tmp_path, monkeypatch):
    """包里没有 CLI 时要老实说不行，不能装个坏软链。"""
    resources = tmp_path / "Resources"
    resources.mkdir()
    monkeypatch.setattr(bootstrap, "_pick_cli_dir", lambda: (tmp_path / "bin", True))
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)

    result = bootstrap.install_cli(resources)

    assert result["ok"] is False
    assert result["reason"] == "bundle_missing_cli"


# -------------------------------------------------------------- daemon


class _FakeRun:
    """记录调用并返回可控结果的 subprocess.run 替身。"""

    def __init__(self, print_returncode: int = 0):
        self.calls: list[list[str]] = []
        self.print_returncode = print_returncode

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        code = subprocess.CompletedProcess(cmd, 0, "", "")
        if "print" in cmd:
            code = subprocess.CompletedProcess(cmd, self.print_returncode, "", "")
        return code


def test_register_daemon_writes_plist_and_loads(tmp_path, monkeypatch):
    resources = _fake_resources(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(bootstrap.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(bootstrap.os, "getuid", lambda: 501)

    fake = _FakeRun(print_returncode=1)  # print 说没加载
    monkeypatch.setattr(bootstrap.subprocess, "run", fake)

    result = bootstrap.register_daemon(resources)

    assert result["ok"] is True
    assert result["reason"] == "loaded"

    plist = home / "Library" / "LaunchAgents" / f"{bootstrap.LAUNCH_AGENT_LABEL}.plist"
    assert plist.is_file()
    body = plist.read_bytes()
    assert bootstrap.LAUNCH_AGENT_LABEL.encode() in body
    assert str(resources / "sdmd-daemon" / "sdmd").encode() in body

    # bootstrap + kickstart 都要跑到，target 必须是 gui/<uid>/<label>
    verbs = [c[1] for c in fake.calls if c[0] == "launchctl"]
    assert "bootstrap" in verbs
    assert "kickstart" in verbs
    for call in fake.calls:
        if call[0] == "launchctl" and call[1] in ("bootstrap", "kickstart"):
            assert any("gui/501" in part for part in call), call


def test_register_daemon_does_not_restart_when_loaded(tmp_path, monkeypatch):
    """daemon 已经在跑就别 kickstart——那会打断正在传的文件。"""
    resources = _fake_resources(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(bootstrap.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(bootstrap.os, "getuid", lambda: 501)

    fake = _FakeRun(print_returncode=0)  # print 说已加载
    monkeypatch.setattr(bootstrap.subprocess, "run", fake)

    result = bootstrap.register_daemon(resources)

    assert result["ok"] is True
    assert result["reason"] == "already_loaded"
    verbs = [c[1] for c in fake.calls if c[0] == "launchctl"]
    assert "kickstart" not in verbs
    assert "bootstrap" not in verbs


def test_register_daemon_missing_binary(tmp_path, monkeypatch):
    resources = tmp_path / "Resources"
    resources.mkdir()
    result = bootstrap.register_daemon(resources)
    assert result["ok"] is False
    assert result["reason"] == "bundle_missing_daemon"


# ------------------------------------------------------------ 入口行为


def test_ensure_installed_noop_when_not_frozen(monkeypatch):
    """源码运行时不能往用户 PATH 里塞东西。"""
    monkeypatch.delattr(bootstrap.sys, "frozen", raising=False)
    monkeypatch.setattr(bootstrap, "bundle_resources", lambda: None)

    result = bootstrap.ensure_installed()

    assert result["cli"]["reason"] == "not_bundle"
    assert result["daemon"]["reason"] == "not_bundle"


def test_bundle_resources_returns_none_when_not_frozen(monkeypatch):
    monkeypatch.delattr(bootstrap.sys, "frozen", raising=False)
    assert bootstrap.bundle_resources() is None
