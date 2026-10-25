"""应用图标必须真的打进产物。

v2.0.2 发出去的 macOS 包用的是 PyInstaller 默认图标（一把扳手）。
PyInstaller 找不到 --icon 时会静默退回它自带的
bootloader/images/icon-windowed.icns，不报错、不警告——用户装上
才发现自己看到的是个默认图标。

这里测两件事：
1. 图标源文件本身是合法的（icns / ico 能被解析）
2. build.py 拼出来的 PyInstaller 参数里带了 --icon，且路径存在
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from sdm import version as version_mod

ROOT = Path(__file__).resolve().parent.parent
RESOURCES = ROOT / "resources"


def test_icns_source_exists():
    assert (RESOURCES / "AppIcon.icns").is_file(), "macOS 图标源文件缺失"


def test_ico_source_exists():
    assert (RESOURCES / "AppIcon.ico").is_file(), "Windows 图标源文件缺失"


def test_icns_is_valid_macos_icon():
    """icns 头是 'icns' + 总长度，后面跟一堆长度前缀的块。"""
    data = (RESOURCES / "AppIcon.icns").read_bytes()
    assert data[:4] == b"icns", f"不是 icns 文件：{data[:4]!r}"
    total = struct.unpack(">I", data[4:8])[0]
    assert total == len(data), f"icns 头里的长度 {total} != 实际 {len(data)}"


def test_icns_contains_multiple_sizes():
    """icns 里要有多种尺寸，否则高分屏下会糊。"""
    data = (RESOURCES / "AppIcon.icns").read_bytes()
    # 遍历块：type(4) + length(4) + payload
    offset = 8
    types: list[str] = []
    while offset + 8 <= len(data):
        block_type = data[offset : offset + 4].decode("ascii", errors="replace")
        block_len = struct.unpack(">I", data[offset + 4 : offset + 8])[0]
        if block_len < 8:
            break
        types.append(block_type)
        offset += block_len
    assert len(types) >= 8, f"icns 里只有 {len(types)} 个尺寸块：{types}"
    assert any(t.startswith("ic") for t in types), f"没有 ic* 图标块：{types}"


def test_ico_is_valid():
    """ico 头：reserved(2)=0, type(2)=1, count(2)。"""
    data = (RESOURCES / "AppIcon.ico").read_bytes()
    assert len(data) > 6
    reserved, ico_type, count = struct.unpack("<HHH", data[:6])
    assert reserved == 0, "ICO reserved 字段不为 0"
    assert ico_type == 1, f"ICO type 不是 1（图标）：{ico_type}"
    assert count >= 1, "ICO 里没有图标"
    # 每个条目 16 字节，目录不能越界
    assert 6 + count * 16 <= len(data), "ICO 目录越界"


def test_ico_contains_multiple_sizes():
    data = (RESOURCES / "AppIcon.ico").read_bytes()
    count = struct.unpack("<H", data[4:6])[0]
    sizes = set()
    for i in range(count):
        off = 6 + i * 16
        w = data[off] or 256
        h = data[off + 1] or 256
        sizes.add((w, h))
    assert len(sizes) >= 3, f"ICO 里只有 {len(sizes)} 种尺寸：{sizes}"
    assert (256, 256) in sizes, f"ICO 缺 256x256：{sizes}"


def test_build_args_include_icon():
    """build.py 拼出来的参数里必须带 --icon，且路径真实存在。

    这是最关键的一条：少了 --icon，PyInstaller 会静默用默认图标。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("sdm_build", ROOT / "scripts" / "build.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    args = mod.common_args("SuperDownloadManager", True, [], True, False)
    assert "--icon" in args, f"GUI 打包参数里没有 --icon：{args}"

    icon_path = Path(args[args.index("--icon") + 1])
    assert icon_path.is_file(), f"--icon 指向的文件不存在：{icon_path}"
    # 图标格式跟 build.py 的判断走。
    #
    # **不要用 sys.platform 判断**——build.py 用的是 platform_dir()，
    # 两者在 Linux 上不一致：build.py 给 .ico（platform_dir() 不是
    # "macos"），而 sys.platform == "win32" 为假会被推成 .icns。
    # 在 macOS 和 Windows 上两边恰好一致，Linux 上才暴露。
    expected = ".icns" if mod.platform_dir() == "macos" else ".ico"
    assert icon_path.suffix == expected, f"当前平台的图标应该是 {expected}：{icon_path}"


def test_cli_build_has_no_icon():
    """CLI / daemon 是命令行工具，不需要图标，也不该白占空间。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("sdm_build_cli", ROOT / "scripts" / "build.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    args = mod.common_args("sdm", False, [], False, False)
    assert "--icon" not in args, f"CLI 不该带 --icon：{args}"


def test_pyinstaller_default_icon_differs():
    """我们的图标不能跟 PyInstaller 的默认图标一样。

    CI 上就是靠这个判断「--icon 有没有生效」——相同就说明 PyInstaller
    静默退回了默认图标。
    """
    import PyInstaller

    default = Path(PyInstaller.__file__).parent / "bootloader" / "images" / "icon-windowed.icns"
    if not default.is_file():
        pytest.skip("这个 PyInstaller 版本没有内置默认图标文件")
    ours = (RESOURCES / "AppIcon.icns").read_bytes()
    assert ours != default.read_bytes(), "我们的图标跟 PyInstaller 默认图标一样"


def test_version_is_consistent():
    """版本号在 version.py 和 pyproject.toml 里要一致。"""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{version_mod.__version__}"' in pyproject, (
        f"pyproject.toml 的版本跟 version.py（{version_mod.__version__}）对不上"
    )
