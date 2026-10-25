"""测试隔离守卫。

**不许碰用户的真实数据。** 测试跑完，用户的下载历史、日志、
下载目录都得跟跑之前一模一样。

这个文件里的用例检查的是「隔离机制本身」——机制哪天被拆了，
这里就红。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sdm import paths


def test_config_dir_is_sandboxed():
    """配置目录指向沙箱，不是用户的 Application Support。"""
    config = paths.config_dir()
    assert config != Path.home() / "Library" / "Application Support" / "SuperDownloadManager"
    assert "sdm-pytest" in str(config), f"配置目录没被隔离: {config}"


def test_log_dir_is_sandboxed():
    """日志目录指向沙箱。"""
    log = paths.log_dir()
    assert log != Path.home() / "Library" / "Logs" / "SuperDownloadManager"
    assert "sdm-pytest" in str(log), f"日志目录没被隔离: {log}"


def test_download_dir_is_sandboxed():
    """下载目录指向沙箱，不是 ~/Downloads。"""
    dl = paths.download_dir()
    assert dl != Path.home() / "Downloads"
    assert "sdm-pytest" in str(dl), f"下载目录没被隔离: {dl}"


def test_history_db_is_sandboxed():
    """历史数据库在沙箱里。"""
    db = paths.history_db_path()
    assert "sdm-pytest" in str(db), f"历史数据库没被隔离: {db}"


def test_env_overrides_are_set():
    """环境变量确实设了——机制本身没被误删。"""
    for name in ("SDM_CONFIG_DIR", "SDM_LOG_DIR", "SDM_DOWNLOAD_DIR"):
        assert os.environ.get(name), f"{name} 没设"


def test_env_override_wins_over_platform_default(monkeypatch):
    """环境变量的优先级高于平台默认路径。"""
    monkeypatch.setenv("SDM_CONFIG_DIR", "/tmp/sdm-override-test")
    monkeypatch.setenv("SDM_LOG_DIR", "/tmp/sdm-override-test-logs")
    monkeypatch.setenv("SDM_DOWNLOAD_DIR", "/tmp/sdm-override-test-dl")

    assert paths.config_dir() == Path("/tmp/sdm-override-test")
    assert paths.log_dir() == Path("/tmp/sdm-override-test-logs")
    assert paths.download_dir() == Path("/tmp/sdm-override-test-dl")


def test_platform_default_when_no_override(monkeypatch):
    """没有环境变量时走平台默认路径。"""
    monkeypatch.delenv("SDM_CONFIG_DIR", raising=False)
    monkeypatch.delenv("SDM_LOG_DIR", raising=False)
    monkeypatch.delenv("SDM_DOWNLOAD_DIR", raising=False)

    if paths.is_macos():
        assert paths.config_dir() == (
            Path.home() / "Library" / "Application Support" / "SuperDownloadManager"
        )
        assert paths.log_dir() == Path.home() / "Library" / "Logs" / "SuperDownloadManager"
    assert paths.download_dir() == Path.home() / "Downloads"


def test_logger_writes_to_sandbox():
    """真的往日志写一条，确认落在沙箱而不是用户目录。"""
    from sdm.logging_setup import get_logger

    log = get_logger()
    log.info("test", "隔离守卫测试")

    # 落盘是异步的，等一下。
    import time

    for _ in range(50):
        path = log.file_path
        if path.exists() and path.stat().st_size > 0:
            break
        time.sleep(0.05)

    assert "sdm-pytest" in str(log.file_path), f"日志写到用户目录了: {log.file_path}"
