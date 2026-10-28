"""下载辅助文件的工作目录。

**为什么辅助文件不能放在下载目录里。** 早期版本把三样东西直接
丢在目标文件旁边：

    x.dmg
    x.dmg.sdm.json            ← 完成后的元信息（etag / 分片布局）
    x.dmg.sdm.session.json    ← 下载中的会话（布局 + 进度）
    x.dmg.sdm.partial-0       ← 每个分片一个临时文件
    x.dmg.sdm.partial-1
    ...

一个 16 分片的下载会在用户的下载文件夹里留下 19 个文件，而且
`.sdm.partial-*` 这种名字用户完全看不懂——他只会以为软件下坏了，
或者以为下载了一半就崩了。这些文件跟用户真正关心的「下载好的
文件」混在一起，也没法让用户随手清理。

所以统一搬到应用自己的地盘：

    ~/Library/Application Support/SuperDownloadManager/downloads/
    %LOCALAPPDATA%\\SuperDownloadManager\\downloads\\          （Windows）
    ~/.local/share/SuperDownloadManager/downloads/             （Linux）

下载目录里从此只剩用户要的那一个文件。

**命名带哈希是为了不撞名。** 同名文件（`movie.mp4`）在不同目录
里很常见，sidecar 的作用是「比对 etag / content_length 判断还是
不是同一个文件」——两个不同来源的同名文件共用一个 sidecar 会导致
错误的续传。所以 key 里带目标绝对路径的短哈希。

**旧位置的迁移。** 升级前下的东西还在老地方，不能让用户的续传
功能凭空消失。所以每个辅助文件都有「老位置」的对应物：新的没有、
老的还在，就把老的搬过来。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from ..paths import config_dir

#: 辅助文件统一后缀（沿用旧名字，方便用户/脚本识别）
SIDECAR_SUFFIX = ".sdm.json"
SESSION_SUFFIX = ".sdm.session.json"
PARTIAL_INFIX = ".sdm.partial-"


def work_root() -> Path:
    """辅助文件根目录：`.../SuperDownloadManager/downloads/`。

    **环境变量 `SDM_WORK_DIR` 优先**，理由同 `config_dir()`——
    测试隔离、便携模式都靠它。
    """
    override = os.environ.get("SDM_WORK_DIR")
    if override:
        return Path(override)
    return config_dir() / "downloads"


def task_key(destination: Path) -> str:
    """目标文件对应的 key：`<文件名主干>-<路径短哈希>`。

    哈希取的是**绝对路径**——同一个文件名放在不同目录是不同文件，
    它们的 sidecar 不能混。
    """
    path = Path(destination)
    try:
        absolute = path.resolve()
    except OSError:  # pragma: no cover - 极端情况（路径不可解析）
        absolute = Path(os.path.abspath(str(path)))
    digest = hashlib.sha256(str(absolute).encode("utf-8", "replace")).hexdigest()[:8]
    stem = path.stem or "download"
    # 文件名主干可能很长（还有非法字符），截断一下免得路径过长。
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in stem)[:40]
    return f"{safe}-{digest}"


def ensure_root() -> Path:
    """确保工作目录存在，返回它。"""
    root = work_root()
    if not root.is_dir():
        root.mkdir(parents=True, exist_ok=True)
    return root


def _ensure_root() -> Path:
    return ensure_root()


def sidecar_path(destination: Path) -> Path:
    """完成后的元信息文件：`downloads/<key>.sdm.json`。"""
    return _ensure_root() / (task_key(destination) + SIDECAR_SUFFIX)


def session_path(destination: Path) -> Path:
    """下载中的会话文件：`downloads/<key>.sdm.session.json`。"""
    return _ensure_root() / (task_key(destination) + SESSION_SUFFIX)


def partial_path(destination: Path, chunk_id: int) -> Path:
    """分片临时文件：`downloads/<key>.sdm.partial-<id>`。"""
    return _ensure_root() / (task_key(destination) + PARTIAL_INFIX + str(chunk_id))


def merging_path(destination: Path) -> Path:
    """合并中的临时目标文件。

    **必须留在目标文件旁边。** 合并完要用 `os.replace` 原子替换，
    而 `os.replace` 不能跨文件系统——工作目录和下载目录分属两个
    卷时会直接失败。用点开头命名，macOS/Linux 的文件管理器默认
    隐藏它，不污染用户的下载目录视图。
    """
    return destination.with_name("." + destination.name + ".sdm-merging")


# -- 旧位置的迁移 ---------------------------------------------------
#
# 升级前辅助文件就在目标文件旁边。新的没有、老的还在时，把老的
# 搬过来——否则用户正在下的东西会「消失」，续传也用不上了。

def legacy_sidecar_path(destination: Path) -> Path:
    return Path(destination).with_name(Path(destination).name + SIDECAR_SUFFIX)


def legacy_session_path(destination: Path) -> Path:
    return Path(destination).with_name(Path(destination).name + SESSION_SUFFIX)


def legacy_partial_path(destination: Path, chunk_id: int) -> Path:
    return Path(destination).with_name(
        Path(destination).name + PARTIAL_INFIX + str(chunk_id)
    )


def legacy_partial_paths(destination: Path) -> list[Path]:
    """目标文件旁边所有旧的分片临时文件。"""
    path = Path(destination)
    try:
        return sorted(path.parent.glob(path.name + PARTIAL_INFIX + "*"))
    except OSError:
        return []


def _move_legacy(legacy: Path, new: Path) -> bool:
    """把旧位置的辅助文件搬到新位置。

    跨文件系统时 `os.replace` 会失败，所以先试 replace，失败退回
    shutil.move。搬完把旧的删掉——不然下载目录里还留着垃圾。
    """
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(legacy, new)
        except OSError:
            shutil.move(str(legacy), str(new))
        return True
    except OSError:
        return False


def adopt_legacy_sidecar(destination: Path) -> Path | None:
    """返回该用哪个 sidecar 路径，必要时把旧的搬过来。

    返回 None 表示既没有新的也没有旧的。
    """
    new = sidecar_path(destination)
    if new.exists():
        return new
    legacy = legacy_sidecar_path(destination)
    if legacy.exists() and _move_legacy(legacy, new):
        return new
    if legacy.exists():
        return legacy
    return None


def adopt_legacy_session(destination: Path) -> Path | None:
    """返回该用哪个会话文件路径，必要时把旧的搬过来。"""
    new = session_path(destination)
    if new.exists():
        return new
    legacy = legacy_session_path(destination)
    if legacy.exists() and _move_legacy(legacy, new):
        return new
    if legacy.exists():
        return legacy
    return None


def adopt_legacy_partial(destination: Path, chunk_id: int) -> Path:
    """返回该分片的临时文件路径，必要时把旧的搬过来。

    分片文件可能在旧位置（升级前下载的），也可能在新位置。
    新位置优先——正在进行的下载用的就是新位置。
    """
    new = partial_path(destination, chunk_id)
    if new.exists():
        return new
    legacy = legacy_partial_path(destination, chunk_id)
    if legacy.exists() and _move_legacy(legacy, new):
        return new
    return legacy if legacy.exists() else new


def remove_legacy_partials(destination: Path) -> None:
    """清掉目标文件旁边的旧分片临时文件。"""
    for path in legacy_partial_paths(destination):
        try:
            path.unlink()
        except OSError:
            pass


def remove_legacy_sidecar(destination: Path) -> None:
    """清掉目标文件旁边的旧 sidecar。"""
    try:
        legacy_sidecar_path(destination).unlink()
    except OSError:
        pass
