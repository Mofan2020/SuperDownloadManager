"""浏览器扩展的**持久安装** + 版本指纹。

扩展文件有两个来源：

- **包内副本**（bundle）：跟着应用一起分发的。源码运行时是仓库根的
  `Extensions/<浏览器>`，打包后是 `Resources/extensions/<浏览器>`。
  Windows 的 GUI 是 onefile——它被解到 `sys._MEIPASS`，那是**临时
  目录，进程退出就被 PyInstaller 删掉**。
- **持久副本**（persistent）：`config_dir()/extensions/<浏览器>`。
  用户在浏览器里「加载已解压的扩展程序」选的就是这个目录，它必须
  在应用退出之后还在，浏览器也得能一直读到它。

两边内容用 SHA-256 指纹对比：一样就什么都不做，不一样就把持久
副本整体换掉。指纹算的是**文件内容 + 相对路径**，不是 mtime——
重新解压一遍的同一个扩展，指纹必须一样。

**替换是原子的**：先拷到同级的临时目录，再 rename 覆盖。中间失败
不会留下半个目录——浏览器加载半个扩展只会报一句看不懂的错。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

from .. import paths
from ..logging_setup import CAT_IPC, get_logger

log = get_logger()

#: 包内扩展根目录的名字（build.py 拷进 Resources 时用）。
BUNDLE_EXTENSIONS_DIRNAME = "extensions"

#: 持久扩展根目录的名字（在 `config_dir()` 下面）。
PERSISTENT_EXTENSIONS_DIRNAME = "extensions"

#: 浏览器标识 → `Extensions/` 下的目录名。
#:
#: 目录名跟仓库里的一致（`Chrome` / `Edge` / `Firefox` / `Safari`），
#: 否则打包时拷过去对不上，用户加载的是空目录。
BROWSER_DIRNAMES: dict[str, str] = {
    "chrome": "Chrome",
    "edge": "Edge",
    "firefox": "Firefox",
    "safari": "Safari",
}


# -- 位置 -----------------------------------------------------------


def persistent_root() -> Path:
    """持久扩展的根目录。`config_dir()` 底下。"""
    return paths.config_dir() / PERSISTENT_EXTENSIONS_DIRNAME


def persistent_dir(browser_id: str) -> Path | None:
    """某个浏览器的**持久**扩展目录。浏览器不认识返回 None。

    macOS：`~/Library/Application Support/SuperDownloadManager/extensions/<浏览器>`
    Windows：`%LOCALAPPDATA%\\SuperDownloadManager\\extensions\\<浏览器>`
    Linux：`~/.local/share/SuperDownloadManager/extensions/<浏览器>`

    **目录不一定存在**——调用方要自己判断（或者调
    `ensure_persisted()` 让它长出来）。
    """
    dirname = BROWSER_DIRNAMES.get(browser_id)
    if dirname is None:
        return None
    return persistent_root() / dirname


def _repo_extensions_root() -> Path:
    """源码运行时仓库根的 `Extensions/`。

    `sdm/ipc/extension_store.py` → 上三级是仓库根。
    """
    return Path(__file__).resolve().parents[3] / "Extensions"


def _bundle_candidates(browser_id: str) -> list[Path]:
    """包内扩展目录的候选位置，按优先级排。

    冻结运行时（`sys.frozen`）的候选多到必须挨个试：

    - `sys._MEIPASS/extensions/<浏览器>`——Windows onefile 就是它。
      但 **macOS 的 onedir .app 里 `_MEIPASS` 指向
      `Contents/Frameworks`**，那儿根本没有 extensions。
    - `sys.executable` 的上两级 + `Resources/extensions/<浏览器>`——
      macOS .app 布局（`Contents/MacOS/` ← `Contents/Resources/`）。
    - `sys.executable` 所在目录的祖先里找 `Resources/extensions`——
      兜底：.app 挪过位置、或者被包在别的目录里。

    没冻结时只有仓库根的 `Extensions/<浏览器>`。
    """
    dirname = BROWSER_DIRNAMES.get(browser_id)
    if dirname is None:
        return []

    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / BUNDLE_EXTENSIONS_DIRNAME / dirname)
        exe = Path(sys.executable).resolve()
        candidates.append(exe.parent.parent / "Resources" / BUNDLE_EXTENSIONS_DIRNAME / dirname)
        for ancestor in exe.parents:
            candidates.append(ancestor / "Resources" / BUNDLE_EXTENSIONS_DIRNAME / dirname)
    else:
        candidates.append(_repo_extensions_root() / dirname)
    return candidates


def bundle_dir(browser_id: str) -> Path | None:
    """应用包内随包分发的扩展目录。找不到返回 None。

    打包后 macOS 是 `.app/Contents/Resources/extensions/<浏览器>`，
    Windows onefile 是 `sys._MEIPASS/extensions/<浏览器>`
    （**临时的**——所以另有一份持久副本）。
    """
    for candidate in _bundle_candidates(browser_id):
        if candidate.is_dir():
            return candidate
    return None


# -- 指纹 -----------------------------------------------------------


def fingerprint_dir(path: Path) -> str:
    """目录内容的 SHA-256 指纹。

    算的是「相对路径 + 文件内容」：光比文件数量不行（改一个文件
    数量不变），光比内容也不行（两个文件换个名字就认不出来了）。

    相对路径按字典序排序，保证同一个目录每次算出来的值一样。
    空目录返回空输入的哈希（一个固定值）。

    **文件读不出来（权限、被占用）不抛异常**——跳过它并在日志里
    记一条。扩展文件读不到是常态，不是崩溃的理由。
    """
    digest = hashlib.sha256()
    if not path.is_dir():
        return digest.hexdigest()

    files = [item for item in path.rglob("*") if item.is_file()]
    files.sort(key=lambda item: item.relative_to(path).as_posix())
    for item in files:
        relative = item.relative_to(path).as_posix()
        try:
            data = item.read_bytes()
        except OSError as err:
            log.debug(CAT_IPC, f"扩展文件读不了，跳过：{relative}（{err}）")
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def bundle_fingerprint(browser_id: str) -> str | None:
    """包内副本的指纹。包内没有这个浏览器返回 None。"""
    directory = bundle_dir(browser_id)
    if directory is None:
        return None
    return fingerprint_dir(directory)


def persisted_fingerprint(browser_id: str) -> str | None:
    """持久副本的指纹。目录不存在返回 None。"""
    directory = persistent_dir(browser_id)
    if directory is None or not directory.is_dir():
        return None
    return fingerprint_dir(directory)


# -- 安装 -----------------------------------------------------------


def _replace_dir(source: Path, target: Path) -> None:
    """用 `source` 的内容整体替换 `target`。

    先拷到同级的临时目录，再 rename 覆盖——`rename` 在同一个文件系统
    内是原子的，所以要么还是旧的，要么已经是新的，不会停在半个目录上。
    """
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".{target.name}.staging"
    backup = parent / f".{target.name}.backup"
    for leftover in (staging, backup):
        if leftover.is_dir():
            shutil.rmtree(leftover, ignore_errors=True)
        elif leftover.exists():
            leftover.unlink()

    shutil.copytree(source, staging)

    had_old = target.exists()
    if had_old:
        if target.is_dir():
            os.rename(target, backup)
        else:
            target.unlink()
    try:
        os.rename(staging, target)
    except OSError:
        if had_old and backup.exists():
            os.rename(backup, target)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if had_old:
        shutil.rmtree(backup, ignore_errors=True)


def ensure_persisted(browser_id: str) -> dict:
    """把包内扩展装到持久目录，保证是最新版本。

    幂等：

    - 持久目录不存在 → 从包内复制，`updated=True`
    - 持久目录在、指纹跟包内一样 → 什么都不做，`updated=False`
    - 指纹不一样 → 整体替换（临时目录 + rename），`updated=True`

    返回 `{"ok", "path", "updated", "fingerprint", "message"}`。
    包内没有这个浏览器时 `ok=False`，**不抛异常**——设置页要照常
    显示其它浏览器的状态。
    """
    result: dict = {
        "ok": False,
        "path": "",
        "updated": False,
        "fingerprint": "",
        "message": "",
    }

    target = persistent_dir(browser_id)
    if target is None:
        result["message"] = f"不认识的浏览器：{browser_id}"
        return result
    result["path"] = str(target)

    source = bundle_dir(browser_id)
    if source is None:
        result["message"] = "应用包里没有这个浏览器的扩展"
        return result

    source_fingerprint = fingerprint_dir(source)
    if target.is_dir() and fingerprint_dir(target) == source_fingerprint:
        result.update(
            ok=True,
            updated=False,
            fingerprint=source_fingerprint,
            message="扩展目录已经是最新版本",
        )
        return result

    try:
        _replace_dir(source, target)
    except OSError as err:
        result["message"] = f"复制扩展失败：{err}"
        log.warning(CAT_IPC, f"扩展持久化失败（{browser_id}）：{err}")
        return result

    result.update(
        ok=True,
        updated=True,
        fingerprint=fingerprint_dir(target),
        message="扩展目录已更新到最新版本",
    )
    return result
