"""daemon 启动守卫的测试：F166。

Swift 参照：`Daemon/main.swift:17-26`。

验收要点：
- 必须靠 `SDM_DAEMON=1` 才进守护模式，其他值一律不认
- macOS 的 LaunchAgent plist 必须带 `SDM_DAEMON=1`
- Windows 的计划任务命令行必须带 `SDM_DAEMON=1`

**Windows 的命令行没法在 macOS 上真跑**，所以这里断言命令串的
结构——引号嵌套是 cmd 的经典坑，写错了在 Windows 上才发现。
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.daemon import main as daemon_main
from sdm.gui import bootstrap

# -- 守卫本身 ------------------------------------------------------


@pytest.mark.parametrize("value", ["1"])
def test_daemon_enabled_only_for_exactly_one(monkeypatch, value):
    """值正好是 "1" 才放行。"""
    monkeypatch.setenv("SDM_DAEMON", value)
    assert daemon_main.daemon_mode_enabled() is True


@pytest.mark.parametrize("value", ["0", "true", "True", "yes", "", "2", "11"])
def test_daemon_disabled_for_other_values(monkeypatch, value):
    """其他值一律不放行。

    守卫要么放行要么拦住，半吊子的解析会让人猜「到底哪个值管用」。
    """
    monkeypatch.setenv("SDM_DAEMON", value)
    assert daemon_main.daemon_mode_enabled() is False


def test_daemon_disabled_when_env_missing(monkeypatch):
    """环境变量没设也不放行。"""
    monkeypatch.delenv("SDM_DAEMON", raising=False)
    assert daemon_main.daemon_mode_enabled() is False


def test_daemon_hint_mentions_the_variable():
    """提示里要说清楚是哪个变量。"""
    assert "SDM_DAEMON" in daemon_main.DAEMON_HINT


def test_daemon_without_env_exits_zero(monkeypatch, capsys):
    """没设环境变量时打印提示并 exit 0（不是 exit 1）。

    这不是错误，是「用法不对」——Swift 版也是 exit(0)。
    """
    monkeypatch.delenv("SDM_DAEMON", raising=False)
    rc = daemon_main.main()
    assert rc == 0
    assert "SDM_DAEMON" in capsys.readouterr().err


# -- macOS LaunchAgent ---------------------------------------------


def test_launch_agent_plist_sets_daemon_env(tmp_path, monkeypatch):
    """macOS 的 LaunchAgent 必须带 SDM_DAEMON=1。

    不带的话 daemon 打印提示就 exit 0，launchd 因为进程立刻退出
    会反复重启它——日志里全是崩溃，用户还看不出来。
    """
    daemon_bin = tmp_path / "sdmd"
    daemon_bin.write_text("#!/bin/sh\n")

    # plist 写到临时目录，别碰真实路径
    monkeypatch.setattr(bootstrap, "LAUNCH_AGENT_LABEL", "com.test.sdm.daemon")

    plist_path = bootstrap._launch_agent_plist(daemon_bin)

    # 读回来验证（plistlib 比 PlistBuddy 稳）
    with plist_path.open("rb") as fh:
        data = plistlib.load(fh)

    env = data.get("EnvironmentVariables")
    assert env is not None, "plist 里没有 EnvironmentVariables"
    assert env.get("SDM_DAEMON") == "1"


# -- Windows 计划任务 ----------------------------------------------


def test_windows_daemon_command_sets_env():
    """Windows 的命令行必须设 SDM_DAEMON=1。"""
    exe = Path(r"C:/Users/test/AppData/Local/SuperDownloadManager/sdmd.exe")
    cmd = bootstrap._windows_daemon_command(exe)

    assert "SDM_DAEMON=1" in cmd
    assert "sdmd.exe" in cmd


def test_windows_daemon_command_has_no_nested_quotes():
    """引号不能嵌套。

    `cmd /c "set X=1&&"C:/path/x.exe""` 这种写法 cmd 的 /c 解析
    会提前截断——外层引号包住整条命令时，路径自己的引号会被当成
    结束符，路径被截成 `C:/path/x.exe` 之外的东西。
    """
    exe = Path(r"C:/Users/test/AppData/Local/SuperDownloadManager/sdmd.exe")
    cmd = bootstrap._windows_daemon_command(exe)

    # 外层 cmd /c 后面不能有紧跟的引号
    after_cmd = cmd.split("/c", 1)[1]
    assert not after_cmd.startswith('"'), "cmd /c 后面不该有引号"
    # 引号总数必须是偶数（成对）
    assert cmd.count('"') % 2 == 0, f"引号不成对: {cmd}"


def test_windows_daemon_command_quotes_path_with_spaces():
    """路径带空格时要引起来。"""
    exe = Path(r"C:/Program Files/Super Download Manager/sdmd.exe")
    cmd = bootstrap._windows_daemon_command(exe)

    assert f'"{exe}"' in cmd


def test_windows_daemon_command_uses_and_separator():
    """用 && 分隔 set 和命令。

    cmd 的 `set VAR=VALUE` 里 VALUE 到下一个 `&` 为止，所以
    `set SDM_DAEMON=1&&...` 存进去的值正好是 `1`。
    """
    exe = Path("C:/x/sdmd.exe")
    cmd = bootstrap._windows_daemon_command(exe)

    assert "set SDM_DAEMON=1&&" in cmd
