"""文件名清洗。

Swift 版靠 `URL.appendingPathComponent` 顺带挡掉了一部分非法
字符，Python 版没有这层保护——而 Windows 的限制比 macOS 严得多：

    \\ / : * ? " < > |          保留字（CON/PRN/AUX/NUL/COM1..9/LPT1..9）
    控制字符 0x00-0x1F          结尾的点或空格

macOS 只禁 `/` 和 `:`。也就是说同一个从 Content-Disposition
解析出来的文件名，在 macOS 上能存，在 Windows 上直接创建失败。

不处理的后果不是「报错」这么温和——是下载跑完了、进度 100%、
最后一步落盘时炸掉，用户看到的是「下载失败」，白等一场。

这个模块在两处调用：
    - ContentSniffer 解析出 suggested_filename 之后
    - 落盘前最后一次兜底（防止中途规则变更漏掉）
"""

from __future__ import annotations

import re
import time
import unicodedata
from pathlib import Path

#: Windows 保留的设备名（不区分大小写，带不带扩展名都算）
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

#: Windows 文件名里非法的字符
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

#: Windows 保留的结尾（点或空格）
_TRAILING_JUNK = re.compile(r"[. ]+$")

#: 连续空白压成一个空格
_MULTI_SPACE = re.compile(r"\s+")

#: 归一化后完全空掉的文件名（全是空白或非法字符）
_BLANK = re.compile(r"^\s*$")

#: 文件名最大长度。Windows 的 MAX_PATH 是 260，
#: 整条路径（目录 + 文件名）都要算在里面，留 120 给目录。
_MAX_FILENAME_LEN = 120

#: 保留文件名的扩展名——截断时不能把扩展名切掉，
#: 否则 .zip 变成 .zi 之类，分类和双击打开都会出问题。
_EXT_SPLIT = re.compile(r"^(?P<stem>.*?)(?P<ext>\.[^.]*)?$", re.DOTALL)


def sanitize_filename(name: str, fallback: str = "download.bin") -> str:
    """把任意来源的文件名清洗成跨平台安全的文件名。

    处理顺序：
      1. Unicode 归一化（NFKC）——全角字符、兼容字符统一
      2. 替换 Windows 非法字符
      3. 压缩连续空白
      4. 去掉 Windows 不允许的结尾（点和空格）
      5. 保留字加后缀
      6. 长度截断（保住扩展名）

    清洗后如果什么都不剩，用 `fallback`。
    """
    if not name:
        return fallback

    # NFKC 归一化：把 ＆ Ａ 这类全角字符折成半角，
    # 顺便把组合字符压平，避免 macOS 上存成 NFC、
    # Windows 上看着像 NFD 导致显示成两行。
    cleaned = unicodedata.normalize("NFKC", name)

    # 只保留最后一个路径段的字符（有的服务器会塞完整路径进来）
    cleaned = cleaned.replace("\\", "/")
    if "/" in cleaned:
        cleaned = cleaned.rsplit("/", 1)[-1]

    cleaned = _ILLEGAL_CHARS.sub("_", cleaned)
    cleaned = _MULTI_SPACE.sub(" ", cleaned)
    cleaned = cleaned.strip()
    cleaned = _TRAILING_JUNK.sub("", cleaned)

    if _BLANK.match(cleaned):
        return fallback

    # 保留字：CON.txt 也是保留字，所以要看「去掉扩展名的部分」
    stem, ext = _split_ext(cleaned)
    if stem.upper() in _WINDOWS_RESERVED:
        cleaned = f"{stem}_{ext}" if ext else f"{stem}_"

    # 长度截断，保住扩展名
    if len(cleaned) > _MAX_FILENAME_LEN:
        stem, ext = _split_ext(cleaned)
        keep = _MAX_FILENAME_LEN - len(ext)
        if keep < 1:
            # 扩展名自己就超长了（病态情况）——直接截断整个名字
            cleaned = cleaned[:_MAX_FILENAME_LEN]
        else:
            cleaned = stem[:keep] + ext

    return cleaned or fallback


def _split_ext(name: str) -> tuple[str, str]:
    """拆成 (主干, 扩展名含点)。没有扩展名时扩展名为空串。"""
    match = _EXT_SPLIT.match(name)
    if not match:
        return name, ""
    return match.group("stem") or name, match.group("ext") or ""


def unique_path(path: Path) -> Path:
    """如果目标已存在，生成一个不冲突的路径。

    **编号的格式跟 macOS 一致：`名字 1.扩展名`。**
    也就是在文件名的**末尾**（扩展名之前）加一个空格，再加编号，
    表示这是第几个同名文件。跟系统保持一致，用户不用适应第二套
    规则——Finder、Safari、邮件客户端都是这么处理重名的。

    之前用的是 `名字-1.扩展名`（连字符），那是 Windows 资源管理器
    之外的第三种风格，用户看着别扭。
    """
    path = Path(path)
    if not path.exists():
        return path
    stem = path.stem
    ext = path.suffix
    parent = path.parent
    for i in range(1, 1000):
        candidate = parent / f"{stem} {i}{ext}"
        if not candidate.exists():
            return candidate
    # 1000 个同名文件，不现实。给个时间戳兜底。
    return parent / f"{stem} {int(time.time())}{ext}"
