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
import platform as _platform
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
    # 子进程（PyInstaller）也要用 UTF-8 输出。
    #
    # Windows 上子进程默认用 locale 编码（GBK）写 stdout，父进程
    # 按 UTF-8 解就会乱码。PYTHONIOENCODING 是子进程读得懂的
    # 环境变量，PyInstaller 是 Python 程序，会认。
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    kw.setdefault("env", env)
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
        # version.py 里是 `__version__ = "2.0.1"`，不是 `VERSION = ...`
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

# macOS GUI 专用的隐藏导入。
#
# UserNotifications 的 PyObjC 绑定是可选依赖（requirements.txt 里
# 用环境标记限制只在 macOS 装）。PyInstaller 靠静态分析找不到它
# ——notifications.py 里是 `import UserNotifications` 包在
# try/except ImportError 里，分析器会跳过。不显式声明的话打包
# 出来的 .app 里没有这个模块，通知会静默退回 osascript 通道，
# 少了「点通知回主窗口」。
#
# 只给 macOS GUI 加：daemon 和 CLI 不发通知。
MACOS_GUI_EXTRA_IMPORTS = [
    "UserNotifications",
]


def common_args(
    name: str = APP_NAME,
    windowed: bool = True,
    extra_imports: list[str] | None = None,
    with_gui_assets: bool = True,
    onefile: bool = False,
    extra_data: list[tuple[str, str]] | None = None,
) -> list[str]:
    """两个平台共用的 PyInstaller 参数。

    `extra_imports` 控制要带哪些隐藏导入，`with_gui_assets` 控制
    要不要打前端资源和 i18n。CLI 两者都不需要。

    `extra_data` 是额外的 (源路径, 包内目标目录) 对。Windows 上
    GUI 是 onefile 单文件，装不下第二份 Python 运行时，所以 CLI
    和 daemon 以 zip 形式塞进去，首次运行时再解出来。
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

    # 应用图标。
    #
    # 不传 --icon 的话 PyInstaller 会用它自己那个默认图标，用户在
    # 访达和 Dock 里看到的是一把扳手。
    #
    # macOS 要 .icns，Windows 要 .ico，平台不一样不能混用——传错
    # 格式 PyInstaller 会直接报错退出。
    if windowed:
        icon = ROOT / "resources" / (
            "AppIcon.icns" if platform_dir() == "macos" else "AppIcon.ico"
        )
        if icon.is_file():
            args.extend(["--icon", str(icon)])

    for src, dest in extra_data or []:
        args.extend(["--add-data", f"{src}{SEP}{dest}"])

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
    # 浏览器原生消息宿主（规格 §3.1）。浏览器按清单里的 path 拉起
    # 它，通过 stdio 收发 JSON 帧。
    #
    # **必须是独立可执行文件**——浏览器拉起的是清单里写死的那个
    # 路径，不能是 GUI 自己。GUI 每次接管一个下载就得起一个宿主
    # 进程出来，所以它得是个小东西（不带 GUI 资源）。
    ("sdmd-native-host", "sdm.ipc.native_host", "main", DAEMON_EXTRA_IMPORTS, False, False),
]

#: 辅助产物在 .app 的 Resources/ 下各自待的目录名。
#:
#: 目录名要避开 sdm / sdmd：PyInstaller 给 GUI 打的 .app 里
#: 已经有 Resources/sdm 软链（指向 ../Resources/sdm），同名会
#: 撞上，变成自指软链。
MACOS_HELPER_DIRS = {
    "sdm": "sdm-cli",
    "sdmd": "sdmd-daemon",
    "sdmd-native-host": "native-host",
}


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
        *common_args(
            APP_NAME,
            True,
            [*GUI_EXTRA_IMPORTS, *MACOS_GUI_EXTRA_IMPORTS],
            True,
            False,
        ),
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
        # AppleScript 字典（规格 §4.3）。
        #
        # **必须先有这个键，Script Editor 才认这个 app 的命令。**
        # 没有 OSAScriptingDefinition 的话，词典文件放在包里也是
        # 死的——用户看不到「add download」这些命令。
        sdef_name = "SuperDownloadManager.sdef"
        if (ROOT / "sdm" / "gui" / "applescript" / sdef_name).is_file():
            set_str("OSAScriptingDefinition", sdef_name)
            # 规格 §4.3 点名要这个键。没有它，系统不认为这个 app
            # 能响应 Apple Event——词典在包里也是死的。
            set_str("NSAppleScriptEnabled", "true")
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

    # CLI 和 daemon 也打出来。
    #
    # 放 Contents/Resources/ 下面，不放 Contents/MacOS/ 或
    # Contents/Frameworks/。
    #
    # 原因：codesign 签 .app 时会递归 Contents/ 下的目录树，把含
    # Mach-O 的目录当成 nested bundle 处理。onedir 的 _internal/ 里
    # 有 python3.11/lib-dynload/*.so 这样的结构，codesign 一路递归
    # 下去撞到非 bundle 目录，报 "bundle format unrecognized,
    # invalid, or unsuitable"，整个签名失败。
    #
    # Resources/ 是 codesign 当作「资源」处理的位置，不递归、不
    # 签里面的代码——验证能过，CLI/daemon 照常能跑。
    #
    # 目录名要避开 sdm / sdmd：PyInstaller 给 GUI 打的 .app 里
    # 已经有 Resources/sdm 软链（指向 ../Resources/sdm），同名会
    # 撞上，变成自指软链。
    resources = app / "Contents" / "Resources"
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
        built = DIST / "macos" / name
        dest = resources / MACOS_HELPER_DIRS.get(name, name)
        if built.is_file():
            shutil.copy2(built, dest / name)
        elif built.is_dir():
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
    for script_name in ("install.sh", "uninstall.sh"):
        src = ROOT / "scripts" / script_name
        dest = resources / script_name
        shutil.copy2(src, dest)
        dest.chmod(0o755)
    print("安装脚本已放进 Resources")

    # AppleScript 字典（规格 §4.3）。
    #
    # 放 Contents/Resources/ 下，Info.plist 里的
    # OSAScriptingDefinition 指向它。缺了这个词典，Script Editor
    # 里看不到任何命令——整个 AppleScript 通道就是死的。
    sdef_src = ROOT / "sdm" / "gui" / "applescript" / "SuperDownloadManager.sdef"
    if sdef_src.is_file():
        shutil.copy2(sdef_src, resources / "SuperDownloadManager.sdef")
        print("AppleScript 字典已放进 Resources")
    else:
        print("警告：找不到 AppleScript 字典，跳过")

    # 浏览器扩展放进 Resources/extensions/。
    #
    # **用户得能拿到扩展文件。** 扩展在源码仓库的 Extensions/ 下，
    # 只装应用的用户永远看不到它——而浏览器的「加载已解压的扩展
    # 程序」要选的就是这个目录。放进 Resources/ 之后，应用的
    # 「设置 → 浏览器接管」板块可以直接把目录在访达里打开，
    # 用户选一下就装好了。
    #
    # 目录名用 extensions（小写）——Resources/ 下已经有 sdm / sdmd
    # 这些软链了，名字别撞。
    #
    # `native-messaging-host/` 不是浏览器扩展（是宿主清单模板和启动
    # 脚本模板），不复制过去——用户打开这个目录时看到四个浏览器就够，
    # 混一个不是扩展的文件夹只会让人以为装错了。
    ext_src = ROOT.parent / "Extensions"
    if ext_src.is_dir():
        ext_dest = resources / "extensions"
        if ext_dest.exists():
            shutil.rmtree(ext_dest)
        shutil.copytree(
            ext_src,
            ext_dest,
            ignore=shutil.ignore_patterns("native-messaging-host"),
        )
        names = sorted(p.name for p in ext_dest.iterdir() if p.is_dir())
        print(f"浏览器扩展已放进 Resources/extensions：{', '.join(names)}")
    else:
        print("警告：找不到 Extensions 目录，浏览器接管没有扩展可装")

    # ad-hoc 签名。
    #
    # 不带 --deep。--deep 会递归进 Contents/ 下的每个目录，把含
    # Mach-O 的目录当 nested bundle 处理，一路递归下去撞上
    # _internal/click-8.4.2.dist-info 这种非 bundle 目录，报
    # "bundle format unrecognized, invalid, or unsuitable"。
    #
    # CLI/daemon 放在 Contents/Resources/ 下正是为了这个——Resources
    # 是 codesign 当资源处理的位置，不递归，签名能过，程序照常能跑。
    #
    # 每一步都检查返回码：签名失败如果不报出来，产物带着坏签名
    # 发出去，用户装上打不开。
    if not _codesign(app):
        print("签名失败")
        return False

    verify = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(app)],
        capture_output=True,
        errors="replace",
        text=True,
    )
    if verify.returncode != 0:
        print(f"签名验证失败：{verify.stderr.strip()[:400]}")
        return False
    print("签名验证通过")

    print(f"macOS 打包完成（{time.time() - t0:.0f}s）：{app}")

    # 生成 DMG。
    #
    # 参考了 Swift 版的 make-dmg.sh，但直接在 build.py 里做更简单：
    # - 不需要额外的脚本文件
    # - build.py 一次打包完成所有产物（.app + dmg）
    # - 输出目录和版本号与 .app 一致
    #
    # 输出路径：~/Downloads/Archives/SuperDownloadManager-<version>-<arch>.dmg
    # 优先用 create-dmg（可选工具），回退到 hdiutil。
    if not _make_dmg(app, version, _platform.system()):
        print("DMG 生成失败")
        return False

    return smoke_test(app / "Contents" / "MacOS", darwin=True, helpers=resources)


def build_windows() -> bool:
    if sys.platform != "win32":
        print("跳过 Windows 打包：当前不是 Windows（需要 Windows CI runner）")
        return False
    if not ensure_pyinstaller():
        return False

    t0 = time.time()
    win_dist = DIST / "windows"

    # 先打 CLI 和 daemon（onedir）。
    #
    # GUI 是 onefile 单文件，装不下第二份 Python 运行时。所以把
    # CLI/daemon 打成 zip 塞进 GUI 里，首次运行时解到
    # %LOCALAPPDATA%\SuperDownloadManager\ 下——解出来的还是
    # onedir，启动快（onefile 每次启动要解包，慢十几秒）。
    for name, module, func, imports, with_assets, _onefile in TARGETS:
        if name == APP_NAME:
            continue
        entry = build_entrypoint(name, module, func)
        args = [
            sys.executable, "-m", "PyInstaller",
            *common_args(name, False, imports, with_assets, False),
            str(entry),
        ]
        if run(args) != 0:
            print(f"PyInstaller 失败：{name}")
            return False
        print(f"  {name}.exe 完成")

    # CLI/daemon/原生宿主打成 zip
    zips: list[tuple[Path, str]] = []
    for name, archive_name in (
        ("sdm", "sdm-cli.zip"),
        ("sdmd", "sdmd-daemon.zip"),
        ("sdmd-native-host", "sdmd-native-host.zip"),
    ):
        src = win_dist / name
        if not src.is_dir():
            print(f"找不到 {src}")
            return False
        archive = win_dist / archive_name
        shutil.make_archive(str(archive.with_suffix("")), "zip", root_dir=str(src))
        zips.append((archive, "."))
        print(f"  {archive_name} 完成（{archive.stat().st_size // 1024} KB）")

    # 浏览器扩展也塞进 GUI（onefile 会解到临时目录）。
    #
    # 用户得能拿到扩展文件——浏览器的「加载已解压的扩展程序」要选
    # 的就是这个目录。应用的「设置 → 浏览器接管」板块直接从
    # `sys._MEIPASS/extensions` 里找。
    #
    # 先拷一份到暂存目录，把 `native-messaging-host` 剔掉——那是宿主
    # 清单模板，不是浏览器扩展。`--add-data` 没有「排除子目录」的参数，
    # 只能先自己拷干净。
    ext_src = ROOT.parent / "Extensions"
    if ext_src.is_dir():
        staging = ROOT / "build" / "extensions-staging"
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(
            ext_src,
            staging,
            ignore=shutil.ignore_patterns("native-messaging-host"),
        )
        zips.append((staging, "extensions"))
        print("  浏览器扩展已打包进 GUI")

    # GUI（onefile），带上三个 zip
    entry = build_entrypoint(APP_NAME, "sdm.gui.main", "main")
    args = [
        sys.executable, "-m", "PyInstaller",
        *common_args(APP_NAME, True, GUI_EXTRA_IMPORTS, True, True, extra_data=zips),
        str(entry),
    ]
    if run(args) != 0:
        print("PyInstaller 失败：GUI")
        return False
    print(f"  {APP_NAME}.exe 完成")

    print(f"Windows 打包完成（{time.time() - t0:.0f}s）")
    return smoke_test(win_dist, darwin=False)


def _codesign(path: Path) -> bool:
    """ad-hoc 签名一个文件或目录。返回是否成功。"""
    result = subprocess.run(
        ["codesign", "--force", "--sign", "-", str(path)],
        capture_output=True,
        errors="replace",
        text=True,
    )
    if result.returncode != 0:
        print(f"  codesign 失败 {path.name}: {result.stderr.strip()[:200]}")
        return False
    return True


def _make_dmg(app: Path, version: str, system: str) -> bool:
    """从 .app 生成 DMG，输出到 ~/Downloads/Archives/"""
    out_dir = Path.home() / "Downloads" / "Archives"
    # 架构名从真实机器读。之前写死「macOS 就是 arm64」——
    # Intel Mac 上文件名会标错，跟实际产物对不上。
    arch = _platform.machine() or ("arm64" if system == "Darwin" else "x86_64")
    out_name = f"SuperDownloadManager-{version}-{arch}.dmg"
    out = out_dir / out_name

    # 检查 .app 是否存在
    if not app.exists():
        print(f"[make-dmg] .app 不存在：{app}")
        return False

    # 创建输出目录
    out_dir.mkdir(parents=True, exist_ok=True)

    # 临时目录
    tmp_dir = Path("/tmp/sdm-dmg-tmp") / str(os.getpid())
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(app, tmp_dir / app.name)
    except Exception as e:
        print(f"[make-dmg] 临时目录操作失败：{e}")
        return False

    # 优先用 create-dmg（可选工具）
    if shutil.which("create-dmg") is not None:
        try:
            subprocess.run(
                [
                    "create-dmg",
                    "--volname", "Super Download Manager",
                    "--window-pos", "200", "120",
                    "--window-size", "600", "400",
                    "--icon-size", "100",
                    "--icon", f"{app.name}", "175", "190",
                    "--hide-extension", app.name,
                    "--app-drop-link", "425", "190",
                    str(out),
                    str(tmp_dir),
                ],
                capture_output=True,
                errors="replace",
            )
            # create-dmg 遇到 ad-hoc 签名问题会非零退出，但 dmg 仍然可能生成
            if out.exists():
                print(f"[make-dmg] create-dmg 生成成功：{out}")
                print(f"[make-dmg] shasum: {subprocess.run(['shasum', '-a', '256', str(out)], capture_output=True, text=True).stdout.strip()}")
                return True
        except Exception as e:
            print(f"[make-dmg] create-dmg 失败，回退到 hdiutil：{e}")

    # 回退到 hdiutil
    try:
        subprocess.run(
            [
                "hdiutil", "create",
                "-ov",
                "-volname", "Super Download Manager",
                "-fs", "HFS+",
                "-srcfolder", str(tmp_dir),
                "-format", "UDZO",
                str(out),
            ],
            capture_output=True,
            errors="replace",
            check=True,
        )
        print(f"[make-dmg] hdiutil 生成成功：{out}")
        print(f"[make-dmg] shasum: {subprocess.run(['shasum', '-a', '256', str(out)], capture_output=True, text=True).stdout.strip()}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[make-dmg] hdiutil 失败：{e.stderr.strip()[:200] if e.stderr else str(e)}")
        return False
    finally:
        # 清理临时目录
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


def smoke_test(bin_dir: Path, darwin: bool, helpers: Path | None = None) -> bool:
    """验证打出来的可执行文件真能启动。

    打包成功不等于能运行——入口函数名写错、依赖没 collect 到，
    这些都要等到运行才暴露。所以打包完必须实际跑一次。

    GUI 不适合在这里启动（会开窗口），改用 `--self-test` 验证。
    """
    ok = True

    gui_name = "SuperDownloadManager" if darwin else "SuperDownloadManager.exe"
    gui = bin_dir / gui_name
    if not gui.is_file():
        # onedir 形态：可执行文件在 <name>/ 目录里
        nested = bin_dir / gui_name / gui_name
        if nested.is_file():
            gui = nested
    if gui.is_file():
        result = subprocess.run(
            [str(gui), "--self-test"],
            capture_output=True,
            errors="replace",
            text=True,
            timeout=180,
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
    #
    # macOS 上 CLI 在 Contents/Resources/sdm-cli/sdm。
    # Windows 上 CLI 和 GUI 一起打包，在 bin_dir/sdm/sdm.exe。
    cli_name = "sdm" if darwin else "sdm.exe"
    cli = None
    search_dirs = [bin_dir]
    if helpers is not None:
        search_dirs.insert(0, helpers)
    for search_dir in search_dirs:
        for candidate in (
            search_dir / cli_name,
            search_dir / "sdm" / cli_name,
            search_dir / "sdm-cli" / cli_name,
        ):
            if candidate.is_file():
                cli = candidate
                break
        if cli is not None:
            break
    if cli.is_file():
        started = time.monotonic()
        result = subprocess.run(
            [str(cli), "ping"],
            capture_output=True,
            errors="replace",
            text=True,
            timeout=120,
        )
        elapsed = time.monotonic() - started
        combined = result.stdout + result.stderr
        if "Traceback" in combined:
            print("  冒烟：sdm 启动就崩了")
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


def _force_utf8_stdio() -> None:
    """把 stdout/stderr 切到 UTF-8。

    Windows 控制台默认用 locale 编码（简体中文系统是 GBK）。
    这个脚本打印中文，编码对不上会抛 UnicodeEncodeError——
    打包跑到一半崩掉，比打包失败更难查。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main() -> int:
    _force_utf8_stdio()
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
