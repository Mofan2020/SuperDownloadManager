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


#: PyInstaller 的 --add-data 分隔符。Windows 用 ";"，其他平台用 ":"。
#: 用错的话 Windows 上会把整个参数当成源路径，直接报「找不到文件」退出。
SEP = ";" if sys.platform == "win32" else ":"


#: 各目标的额外依赖。
#:
#: CLI 只需要 IPC 客户端，不碰 pywebview 也不碰 httpx。把这些
#: 精简掉，onefile 的体积差一个量级——onefile 每次启动都要解包到
#: 临时目录，体积直接决定启动时间。
GUI_EXTRA_IMPORTS = [
    # pywebview 的后端是动态导入的，静态分析发现不了
    "webview.platforms.cocoa",
    "webview.platforms.edgechromium",
    "webview.platforms.gtk",
    "webview.platforms.winforms",
    "webview.platforms.mshtml",
    "webview.platforms.qt",
    "sdm.gui",
]

DAEMON_EXTRA_IMPORTS = [
    "sdm.daemon.main",
]

CLI_EXTRA_IMPORTS = [
    "sdm.cli.sdmdm",
]


def common_args(
    name: str = APP_NAME,
    windowed: bool = True,
    extra_imports: list[str] | None = None,
    with_gui_assets: bool = True,
    onefile: bool = False,
) -> list[str]:
    """两个平台共用的 PyInstaller 参数。

    `extra_imports` 控制要带哪些隐藏导入，`with_gui_assets` 控制
    要不要打前端资源和 i18n。CLI 两者都不需要。
    """
    args = [
        "--noconfirm",
        "--clean",
        "--name", name,
        "--distpath", str(DIST / platform_dir()),
        "--workpath", str(ROOT / "build" / platform_dir()),
        "--specpath", str(ROOT / "build"),
    ]
    for module in extra_imports or []:
        args.extend(["--hidden-import", module])

    if with_gui_assets:
        # httpx 的 certifi 根证书
        args.extend(["--collect-data", "certifi"])
        # i18n 文案
        args.extend(["--add-data", f"{ROOT / 'sdm' / 'i18n'}{SEP}sdm/i18n"])
        # 前端资源
        args.extend(["--add-data", f"{ROOT / 'sdm' / 'gui' / 'assets'}{SEP}sdm/gui/assets"])

    # 控制台模式。GUI 用 --windowed（不该有黑框），CLI / daemon
    # 用 --console（要看到输出和 traceback）。
    args.append("--windowed" if windowed else "--console")

    if onefile:
        args.append("--onefile")
    return args


def platform_dir() -> str:
    return "macos" if sys.platform == "darwin" else "windows" if sys.platform == "win32" else "linux"


#: 各可执行文件的入口：(产物名, 模块, 入口函数名, 是否隐藏控制台)
#
# GUI 是主程序；CLI 和 daemon 是配套工具。macOS 上它们是
# `python -m sdm.cli.sdmdm` 这样的模块调用，Windows 上没有
# 那种习惯，得打成独立的可执行文件。模块名保留 sdmdm（改名会跟
# 包名 sdm 撞），对外的命令名是 sdm。
#
# 入口函数名逐个写明：CLI 导出的是 `run()` 不是 `main()`，
# 写错的话打包能过，运行起来才报 ImportError。
#: 各可执行文件的入口。
#:
#: (产物名, 模块, 入口函数名, 隐藏导入, 是否带 GUI 资源, 是否 onefile)
#:
#: GUI 是主程序。CLI 和 daemon 是配套工具：macOS 上它们是
#: `python -m sdm.cli.sdmdm` 这样的模块调用，Windows 上没有那种
#: 习惯，得打成独立的可执行文件。模块名保留 sdmdm（改名会跟包名
#: sdm 撞），对外的命令名是 sdm。
#:
#: CLI 不带 GUI 资源也不带 pywebview，体积小一个量级。
#:
#: 全部用 onedir，不用 onefile。onefile 每次启动都要把整个包解到
#: 临时目录再重签名：实测 8.6 MB 的 CLI 要 5.9 秒，onedir 只要
#: 0.7 秒。命令行工具让人随手敲一下等几秒是不能接受的。
#: onedir 的产物是一个目录：可执行文件在顶层，依赖在同级的
#: _internal/ 里。
TARGETS: list[tuple[str, str, str, list[str], bool, bool]] = [
    ("SuperDownloadManager", "sdm.gui.main", "main", GUI_EXTRA_IMPORTS, True, False),
    ("sdm", "sdm.cli.sdmdm", "run", CLI_EXTRA_IMPORTS, False, False),
    ("sdmd", "sdm.daemon.main", "main", DAEMON_EXTRA_IMPORTS, True, False),
]


def build_entrypoint(name: str, module: str, func: str) -> Path:
    """生成入口脚本。

    PyInstaller 需要一个顶层脚本，不能直接指向包内的模块
    （那样 collect 子模块行为不可控）。
    """
    entry = ROOT / "build" / f"run_{name}.py"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        "import sys\n"
        f"from {module} import {func}\n"
        "if __name__ == '__main__':\n"
        f"    sys.exit({func}())\n",
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
    entry = build_entrypoint(APP_NAME, "sdm.gui.main", "main")
    args = [
        sys.executable, "-m", "PyInstaller",
        *common_args(APP_NAME, True, GUI_EXTRA_IMPORTS, True, False),
        str(entry),
    ]

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
        # 用 plutil 而不是 PlistBuddy。
        # PlistBuddy 的 Add 不带类型前缀时会把裸值当类型解析——
        # `Add :CFBundleVersion 1` 报 "Unrecognized Type: 1"。
        # plutil 的 insert/replace 类型明确，不吃这种歧义。
        def set_str(key: str, value: str) -> None:
            # 存在就 replace，不存在就 insert
            probe = subprocess.run(
                ["plutil", "-extract", key, "raw", "-o", "-", str(plist)],
                capture_output=True,
            )
            action = "replace" if probe.returncode == 0 else "insert"
            subprocess.run(
                ["plutil", "-replace" if action == "replace" else "-insert",
                 key, "-string", value, str(plist)],
                capture_output=True,
            )

        set_str("CFBundleShortVersionString", version)
        set_str("CFBundleVersion", str(build))
        set_str("CFBundleIdentifier", BUNDLE_ID)
        set_str("CFBundleName", "Super Download Manager")
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

    # CLI 和 daemon 也打出来，放在 .app/Contents/MacOS/ 旁边。
    # macOS 上用户可以直接调 Contents/MacOS/sdm。
    for name, module, func, imports, with_assets, onefile in TARGETS[1:]:
        entry = build_entrypoint(name, module, func)
        args = [
            sys.executable, "-m", "PyInstaller",
            *common_args(name, False, imports, with_assets, onefile),
            str(entry),
        ]
        if run(args) != 0:
            print(f"PyInstaller 失败：{name}")
            return False
        # 把产物挪进 .app。
        # onedir 的产物是目录：可执行文件 + 同级 _internal/。
        # 整个目录搬进去，_internal 要跟可执行文件平级。
        target_dir = app / "Contents" / "MacOS"
        built = DIST / "macos" / name
        if built.is_file():
            shutil.copy2(built, target_dir / name)
        elif built.is_dir():
            dest = target_dir / name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(built, dest)
        else:
            print(f"找不到 {name} 的产物")
            return False

    # 安装脚本放进 Resources。
    #
    # 不用 --add-data：PyInstaller 6 会把单个文件打成一个目录
    # （install.sh/install.sh），脚本就没法直接执行了。
    # 打包完直接复制最省事。
    resources = app / "Contents" / "Resources"
    for script_name in ("install.sh", "uninstall.sh"):
        src = ROOT / "scripts" / script_name
        dest = resources / script_name
        shutil.copy2(src, dest)
        dest.chmod(0o755)
    print("安装脚本已放进 Resources")

    # ad-hoc 签名
    subprocess.call(
        ["codesign", "--force", "--deep", "--sign", "-", str(app)],
        stderr=subprocess.DEVNULL,
    )
    subprocess.call(["codesign", "--verify", "--deep", "--strict", str(app)])

    print(f"macOS 打包完成（{time.time() - t0:.0f}s）：{app}")
    return smoke_test(app / "Contents" / "MacOS", darwin=True)


def build_windows() -> bool:
    if sys.platform != "win32":
        print("跳过 Windows 打包：当前不是 Windows（需要 Windows CI runner）")
        return False
    if not ensure_pyinstaller():
        return False

    t0 = time.time()
    for name, module, func, imports, with_assets, onefile in TARGETS:
        entry = build_entrypoint(name, module, func)
        args = [
            sys.executable, "-m", "PyInstaller",
            *common_args(name, name == APP_NAME, imports, with_assets, onefile),
            str(entry),
        ]
        if run(args) != 0:
            print(f"PyInstaller 失败：{name}")
            return False
        print(f"  {name}.exe 完成")
    print(f"Windows 打包完成（{time.time() - t0:.0f}s）")
    return smoke_test(DIST / "windows", darwin=False)


def smoke_test(bin_dir: Path, darwin: bool) -> bool:
    """验证打出来的可执行文件真能启动。

    打包成功不等于能运行——入口函数名写错、依赖没 collect 到，
    这些都要等到运行才暴露。所以打包完必须实际跑一次。

    GUI 不适合在这里启动（会开窗口），改用 `--self-test` 验证。
    """
    ok = True

    gui = bin_dir / ("SuperDownloadManager" if darwin else "SuperDownloadManager.exe")
    if gui.is_file():
        result = subprocess.run(
            [str(gui), "--self-test"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            print("  冒烟：SuperDownloadManager --self-test 通过")
        else:
            print(f"  冒烟：SuperDownloadManager --self-test 失败（exit {result.returncode}）")
            print(result.stdout[-1500:])
            print(result.stderr[-1500:])
            ok = False
    else:
        print(f"  冒烟：找不到 {gui}")
        ok = False

    # CLI：跑一个必然失败的子命令，看它是不是正常报「用法」而不是崩溃。
    # `ping` 在 daemon 没跑时应该报连接失败，退出码非 0 但不该有 traceback。
    #
    # CLI 是 onedir：可执行文件在 <name>/<name> 里。
    cli = bin_dir / ("sdm" if darwin else "sdm.exe")
    if not cli.is_file():
        # onedir 形态：<name>/<name> 才是可执行文件，
        # <name>/ 本身是目录。
        nested = bin_dir / "sdm" / ("sdm" if darwin else "sdm.exe")
        if nested.is_file():
            cli = nested
    if cli.is_file():
        started = time.monotonic()
        result = subprocess.run(
            [str(cli), "ping"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        elapsed = time.monotonic() - started
        combined = result.stdout + result.stderr
        if "Traceback" in combined:
            print(f"  冒烟：sdm 启动就崩了")
            print(combined[-1500:])
            ok = False
        else:
            print(f"  冒烟：sdm 能启动并正常报错（exit {result.returncode}）")
        # 启动时间上限。onefile 每次启动要解包到临时目录，
        # 实测能让 daemon 建 socket 花 13 秒——命令行工具不能这样。
        if elapsed > 5.0:
            print(f"  冒烟：sdm 启动 {elapsed:.1f}s，太慢（onefile 解包？）")
            ok = False
        else:
            print(f"  冒烟：sdm 启动 {elapsed:.2f}s")
    else:
        print(f"  冒烟：找不到 {cli}")
        ok = False

    return ok


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
