"""浏览器接管的「装好了没有」体检 + 扩展目录定位。

契约 §4（浏览器接管）里的协议、扩展代码、回环服务其实都写好了，
但**用户没法用**：扩展文件只躺在源码仓库里，应用里没有任何地方
告诉用户「扩展在哪、装没装、配对 token 是多少」。

这个模块补的就是这一段：

- **扩展目录在哪**：打包版在 `Contents/Resources/extensions/<浏览器>`，
  源码运行时在仓库根的 `Extensions/<浏览器>`。用户要在浏览器的
  「加载已解压的扩展程序」里选这个目录——所以路径必须能直接给出去。
- **装没装**：扫各浏览器的扩展安装目录，看有没有名字对得上的
  manifest。Safari 走 `pluginkit`（临时扩展不进普通目录）。
- **配对 token**：Safari 没有原生消息宿主，只能手动粘；Chrome /
  Firefox 装了宿主就会自动配对。

**只读，不改用户的东西。** 这个模块只回答「现在是什么状态」，
安装动作全在 GUI 层（用户点了才做）。
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..logging_setup import CAT_IPC, get_logger

log = get_logger()

#: 扩展在 manifest 里的名字。装没装就看这个对不对得上。
EXTENSION_NAME = "Super Download Manager"

#: 打包版里扩展目录的名字（build.py 拷进 Resources 时用）。
BUNDLE_EXTENSIONS_DIRNAME = "extensions"


@dataclass(frozen=True)
class BrowserSpec:
    """一个浏览器（及其扩展）的静态信息。"""

    id: str
    label: str
    #: `Extensions/` 下的目录名。
    dirname: str
    #: 装扩展的入口（浏览器自己的扩展管理页）。前端用它做按钮。
    extensions_url: str
    #: **要不要手动粘配对 token。**
    #:
    #: Safari 没有 Chrome 那种原生消息宿主（本仓库里的
    #: `sdmd-native-host` 是 Chrome 协议的宿主，Safari 用不上），
    #: 只能走回环 HTTP 通道——而回环通道**不开 `/pair`**（那个端口
    #: 带着 `Access-Control-Allow-Origin: *`，任何网页都能调，
    #: 把 token 暴露给网页等于把「谁都能往下载器塞任务」的门打开）。
    #: 所以 Safari 用户得手动把 token 粘进扩展的选项页。
    needs_manual_pairing: bool
    #: 扩展能不能自动接管下载。
    #:
    #: Safari **没有** `downloads.onDeterminingFilename` 这种
    #: 「下载开始前拦一道」的钩子（契约 §4.4.5 明确要求如实标注）。
    #: 硬做的话只能等下载已经开始了再取消——那时候文件已经在路上，
    #: 取消 + 擦除的窗口里用户会丢文件。所以 Safari 只有手动派单。
    auto_capture: bool


#: 支持的浏览器。顺序 = 设置页里的显示顺序。
BROWSERS: tuple[BrowserSpec, ...] = (
    BrowserSpec(
        id="chrome",
        label="Chrome",
        dirname="Chrome",
        extensions_url="chrome://extensions/",
        needs_manual_pairing=False,
        auto_capture=True,
    ),
    BrowserSpec(
        id="firefox",
        label="Firefox",
        dirname="Firefox",
        extensions_url="about:debugging#/runtime/this-firefox",
        needs_manual_pairing=False,
        auto_capture=True,
    ),
    BrowserSpec(
        id="safari",
        label="Safari",
        dirname="Safari",
        extensions_url="",
        needs_manual_pairing=True,
        auto_capture=False,
    ),
)


def browser_spec(browser_id: str) -> BrowserSpec | None:
    for spec in BROWSERS:
        if spec.id == browser_id:
            return spec
    return None


# -- 扩展目录定位 ---------------------------------------------------


def _bundle_resources() -> Path | None:
    """打包版的资源目录。源码运行时返回 None。

    **Windows/Linux 的 onefile 走 `sys._MEIPASS`**——那种打包把资源
    解到临时目录里，`sys.executable` 旁边什么都没有。

    macOS 的 .app 走 `Contents/Resources`：onefile 打包时
    `sys.executable` 指向的是临时解包目录里的二进制，Resources
    根本不在那儿。
    """
    import sys

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    candidate = exe.parent.parent / "Resources"
    if candidate.is_dir():
        return candidate
    return None


def _repo_extensions_root() -> Path | None:
    """源码运行时仓库根的 `Extensions/`。

    `sdm/ipc/browser_capture.py` → 上三级是仓库根。
    """
    root = Path(__file__).resolve().parents[3] / "Extensions"
    return root if root.is_dir() else None


def extension_root() -> Path | None:
    """扩展根目录。找不到返回 None。"""
    resources = _bundle_resources()
    if resources is not None:
        bundled = resources / BUNDLE_EXTENSIONS_DIRNAME
        if bundled.is_dir():
            return bundled
    return _repo_extensions_root()


def extension_dir(browser_id: str) -> Path | None:
    """某个浏览器的扩展目录。"""
    spec = browser_spec(browser_id)
    if spec is None:
        return None
    root = extension_root()
    if root is None:
        return None
    candidate = root / spec.dirname
    return candidate if candidate.is_dir() else None


# -- 装没装 ---------------------------------------------------------


def _manifest_name(manifest_path: Path) -> str:
    """从 manifest.json 里读扩展名。

    `.xpi`（Firefox 装好的包）是 zip，manifest 在里面。
    """
    try:
        if manifest_path.suffix == ".xpi":
            with zipfile.ZipFile(manifest_path) as zf, zf.open("manifest.json") as fh:
                data = json.loads(fh.read().decode("utf-8", errors="replace"))
        else:
            data = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return ""
    name = data.get("name")
    return name if isinstance(name, str) else ""


def _profile_roots(browser_id: str) -> list[Path]:
    """浏览器 profile 目录（扩展装在 profile 下面）。"""
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        support = home / "Library" / "Application Support"
        if browser_id == "chrome":
            return [support / "Google" / "Chrome"]
        if browser_id == "chromium":
            return [support / "Chromium"]
        if browser_id == "edge":
            return [support / "Microsoft Edge"]
        if browser_id == "firefox":
            return [support / "Firefox" / "Profiles"]
        return []
    if system == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        if browser_id == "chrome":
            return [local / "Google" / "Chrome" / "User Data"]
        if browser_id == "edge":
            return [local / "Microsoft" / "Edge" / "User Data"]
        if browser_id == "firefox":
            return [roaming / "Mozilla" / "Firefox" / "Profiles"]
        return []
    return []


def _iter_installed_manifests(browser_id: str):
    """遍历浏览器里已安装扩展的 manifest 路径。"""
    for root in _profile_roots(browser_id):
        if not root.is_dir():
            continue
        try:
            profiles = [p for p in root.iterdir() if p.is_dir()]
        except OSError:
            continue
        if browser_id == "firefox":
            # Firefox：临时加载的解压成目录，装好的打包成 .xpi
            for profile in profiles:
                ext_dir = profile / "extensions"
                if not ext_dir.is_dir():
                    continue
                for item in ext_dir.iterdir():
                    if item.is_dir():
                        manifest = item / "manifest.json"
                        if manifest.is_file():
                            yield manifest
                    elif item.suffix == ".xpi":
                        yield item
        else:
            # Chromium 系：<profile>/Extensions/<id>/<version>/manifest.json
            for profile in profiles:
                ext_dir = profile / "Extensions"
                if not ext_dir.is_dir():
                    continue
                for ext_id in ext_dir.iterdir():
                    if not ext_id.is_dir():
                        continue
                    for version in ext_id.iterdir():
                        manifest = version / "manifest.json"
                        if manifest.is_file():
                            yield manifest


def is_extension_installed(browser_id: str) -> bool:
    """这个浏览器里装了我们的扩展没有。"""
    for manifest in _iter_installed_manifests(browser_id):
        if _manifest_name(manifest) == EXTENSION_NAME:
            return True
    return False


def is_safari_extension_installed() -> bool:
    """Safari 里装了我们的扩展没有。

    Safari 的临时扩展不进普通扩展目录，只有 `pluginkit` 知道。
    命令不存在（没装 Xcode 工具链）就当没装——不能因此报错。
    """
    if platform.system() != "Darwin":
        return False
    try:
        result = subprocess.run(
            ["pluginkit", "-mAvvv", "-p", "com.apple.Safari.web-extension"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as err:
        log.debug(CAT_IPC, f"查 Safari 扩展列表失败: {err}")
        return False
    return EXTENSION_NAME in (result.stdout or "")


def native_host_installed() -> list[str]:
    """哪些浏览器装了原生消息宿主清单。

    清单在浏览器的 NativeMessagingHosts 目录里（用户级目录，
    装的时候不需要管理员权限）。装了宿主的浏览器**自动配对**，
    不用手动粘 token。
    """
    from .native_host import APP_ID, host_manifest_dirs

    installed: list[str] = []
    for browser, directory in host_manifest_dirs():
        manifest = directory / f"{APP_ID}.json"
        if manifest.is_file():
            installed.append(browser)
    return installed


# -- 汇总 -----------------------------------------------------------


def capture_info() -> dict:
    """浏览器接管的整体状态。给 GUI 的设置页用。"""
    from .native_host import ensure_pair_token

    root = extension_root()
    token = ensure_pair_token()
    hosts = native_host_installed()

    browsers: list[dict] = []
    for spec in BROWSERS:
        directory = extension_dir(spec.id)
        if spec.id == "safari":
            installed = is_safari_extension_installed()
        else:
            installed = is_extension_installed(spec.id)
        browsers.append(
            {
                "id": spec.id,
                "label": spec.label,
                "extensions_url": spec.extensions_url,
                "needs_manual_pairing": spec.needs_manual_pairing,
                "auto_capture": spec.auto_capture,
                "extension_dir": str(directory) if directory else "",
                "available": directory is not None,
                "installed": installed,
                "native_host": spec.id in hosts,
            }
        )

    return {
        "ok": True,
        "token": token,
        "extension_root": str(root) if root else "",
        "browsers": browsers,
        "host_browsers": hosts,
    }
