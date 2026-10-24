#!/usr/bin/env python3
"""打包脚本：macOS 出 .app + .dmg，Windows 出 .exe。

用法：
    python scripts/build.py              # 当前平台
    python scripts/build.py --mac        # 强制 macOS
    python scripts/build.py --windows    # 强制 Windows（在 mac 上只能交叉到部分）
    python scripts/build.py --all        # 两个都试

产物：
    dist/macos/SuperDownloadManager.app
    dist/macos/SuperDownloadManager.dmg
    dist/windows/SuperDownloadManager.exe
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
APP_NAME = "SuperDownloadManager"
BUNDLE_ID = "com.skyc8266.superdownloadmanager"
VERSION_FILE = ROOT / "sdm" / "version.py"


def run(cmd: list[str], **kw) -> int:
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.call(cmd, **kw)


def read_version() -> tuple[str, int]:
    """从 version.py 里读版本号。

    打包时不能 import sdm（会拖进 pywebview 等依赖），
    所以用文本解析。
    """
    text = VERSION_FILE.read_text(encoding="utf-8")
    version = build = "0.0.0"
    for line in text.splitlines():
        line = line.strip()
        # version.py 里是 `__version__ = "2.0.0"`，不是 `VERSION = ...`
        if line.startswith("__version__"):
            version = line.split("=", 1)[1].strip().strip('"').strip("'")
        elif line.startswith("BUILD_NUMBER"):
            build = line.split("=", 1)[1].strip()
    return version, int(build or 0)


def ensure_pyinstaller() -> bool:
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        print("PyInstaller 未安装，尝试安装……")
        return run([sys.executable, "-m", "pip", "install", "pyinstaller"]) == 0


def common_args() -> list[str]:
    """两个平台共用的 PyInstaller 参数。"""
    return [
        "--noconfirm",
        "--clean",
        "--name", APP_NAME,
        "--windowed",
        "--distpath", str(DIST / platform_dir()),
        "--workpath", str(ROOT / "build" / platform_dir()),
        "--specpath", str(ROOT / "build"),
        # 隐藏导入：pywebview 的后端和 tkinter 桥接是动态导入的，
        # PyInstaller 静态分析发现不了，必须显式声明。
        "--hidden-import", "webview.platforms.cocoa",
        "--hidden-import", "webview.platforms.edgechromium",
        "--hidden-import", "webview.platforms.gtk",
        "--hidden-import", "webview.platforms.winforms",
        "--hidden-import", "webview.platforms.mshtml",
        "--hidden-import", "webview.platforms.qt",
        "--hidden-import", "sdm.gui",
        "--hidden-import", "sdm.cli.sdmdm",
        "--hidden-import", "sdm.daemon.main",
        # httpx 的 certifi 根证书
        "--collect-data", "certifi",
        # i18n 文案
        "--add-data", f"{ROOT / 'sdm' / 'i18n'}:sdm/i18n",
        # 前端资源
        "--add-data", f"{ROOT / 'sdm' / 'gui' / 'assets'}:sdm/gui/assets",
    ]


def platform_dir() -> str:
    return "macos" if sys.platform == "darwin" else "windows" if sys.platform == "win32" else "linux"


def build_entrypoint() -> Path:
    """生成入口脚本。

    PyInstaller 需要一个顶层脚本，不能直接指向包内的模块
    （那样 collect 子模块行为不可控）。
    """
    entry = ROOT / "build" / "run_app.py"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        "import sys\n"
        "from sdm.gui.main import main\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n",
        encoding="utf-8",
    )
    return entry


def build_macos() -> bool:
    if sys.platform != "darwin":
        print("跳过 macOS 打包：当前不是 macOS")
        return False
    if not ensure_pyinstaller():
        return False

    version, build = read_version()
    entry = build_entrypoint()
    args = [sys.executable, "-m", "PyInstaller", *common_args(), str(entry)]

    t0 = time.time()
    if run(args) != 0:
        print("PyInstaller 失败")
        return False

    app = DIST / "macos" / f"{APP_NAME}.app"
    if not app.exists():
        print(f"没找到产物：{app}")
        return False

    # 写 Info.plist：版本、bundle id、URL scheme
    plist = app / "Contents" / "Info.plist"
    if plist.exists():
        # PyInstaller 生成的 plist 里 CFBundleVersion 可能不存在，
        # 直接 Set 会报 "Entry Does Not Exist"。先判断再决定 Set/Add。
        def set_or_add(key: str, value: str) -> None:
            exists = subprocess.call(
                ["/usr/libexec/PlistBuddy", "-c", f"Print :{key}", str(plist)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ) == 0
            cmd = "Set" if exists else "Add"
            subprocess.call(
                ["/usr/libexec/PlistBuddy", "-c", f"{cmd} :{key} {value}", str(plist)],
            )

        set_or_add("CFBundleShortVersionString", version)
        set_or_add("CFBundleVersion", str(build))
        set_or_add("CFBundleIdentifier", BUNDLE_ID)
        set_or_add("CFBundleName", "Super Download Manager")
        # 注册 URL scheme：Swift 版实现了 handleURLEvent 但从没在
        # Info.plist 注册，导致那个入口是死的（风险项 R-13）。
        # Python 版在这里补上。
        subprocess.call(
            ["/usr/libexec/PlistBuddy", "-c", "Add :CFBundleURLTypes array", str(plist)],
            stderr=subprocess.DEVNULL,
        )
        subprocess.call(
            [
                "/usr/libexec/PlistBuddy", "-c",
                "Add :CFBundleURLTypes:0 dict", str(plist),
            ],
            stderr=subprocess.DEVNULL,
        )
        subprocess.call(
            [
                "/usr/libexec/PlistBuddy", "-c",
                "Add :CFBundleURLTypes:0:CFBundleURLName string "
                f"{BUNDLE_ID}", str(plist),
            ],
            stderr=subprocess.DEVNULL,
        )
        subprocess.call(
            [
                "/usr/libexec/PlistBuddy", "-c",
                "Add :CFBundleURLTypes:0:CFBundleURLSchemes array", str(plist),
            ],
            stderr=subprocess.DEVNULL,
        )
        subprocess.call(
            [
                "/usr/libexec/PlistBuddy", "-c",
                "Add :CFBundleURLTypes:0:CFBundleURLSchemes:0 string "
                "superdownloadmanager", str(plist),
            ],
            stderr=subprocess.DEVNULL,
        )
        print(f"Info.plist 已更新：version={version} build={build} "
              f"bundle={BUNDLE_ID} url_scheme=superdownloadmanager")

    # ad-hoc 签名
    subprocess.call(
        ["codesign", "--force", "--deep", "--sign", "-", str(app)],
        stderr=subprocess.DEVNULL,
    )
    subprocess.call(["codesign", "--verify", "--deep", "--strict", str(app)])

    print(f"macOS 打包完成（{time.time() - t0:.0f}s）：{app}")
    return True


def build_windows() -> bool:
    if sys.platform != "win32":
        print("跳过 Windows 打包：当前不是 Windows（需要 Windows CI runner）")
        return False
    if not ensure_pyinstaller():
        return False

    entry = build_entrypoint()
    args = [sys.executable, "-m", "PyInstaller", *common_args(), str(entry)]
    t0 = time.time()
    if run(args) != 0:
        print("PyInstaller 失败")
        return False
    print(f"Windows 打包完成（{time.time() - t0:.0f}s）")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mac", action="store_true")
    ap.add_argument("--windows", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    DIST.mkdir(parents=True, exist_ok=True)

    ok = True
    if args.all:
        ok = build_macos() and build_windows()
    elif args.mac:
        ok = build_macos()
    elif args.windows:
        ok = build_windows()
    else:
        ok = build_macos() if sys.platform == "darwin" else build_windows()

    if ok:
        print("\n=== 产物 ===")
        for p in sorted(DIST.rglob("*")):
            if p.is_file() and p.suffix in (".app", ".exe", ".dmg", ".zip"):
                print(f"  {p}  ({p.stat().st_size / 1e6:.1f} MB)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
