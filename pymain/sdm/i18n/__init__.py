"""界面文案：中文 / 英文。

对应 Swift 版 Engine/L10n.swift + 两份 Localizable.strings。

用 JSON 而不是 .strings 文件——Python 读 JSON 更自然，
而且打包成单文件时不用处理 bundle 资源路径。

键名保持和 Swift 版一致，方便对照。
"""

from __future__ import annotations

import json
import locale
from pathlib import Path

# 文案 JSON 与本模块同级
_STRINGS_DIR = Path(__file__).parent

CATALOGS: dict[str, dict[str, str]] = {}


def _load_catalogs() -> None:
    if CATALOGS:
        return
    for path in sorted(_STRINGS_DIR.glob("*.json")):
        try:
            CATALOGS[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            CATALOGS[path.stem] = {}


def system_locale() -> str:
    """系统语言。中文环境返回 zh-Hans，其余返回 en。"""
    try:
        code = locale.getlocale()[0] or ""
    except ValueError:
        code = ""
    if not code:
        code = locale.getdefaultlocale()[0] or ""
    lowered = code.lower()
    if lowered.startswith("zh"):
        # 繁体中文也用简体文案，英文是兜底
        return "zh-Hans"
    return "en"


class I18n:
    """按语言查文案。"""

    def __init__(self, language: str = "system") -> None:
        _load_catalogs()
        self.language = language

    @property
    def resolved(self) -> str:
        if self.language in ("system", "", None):
            return system_locale()
        if self.language == "zh-Hans":
            return "zh-Hans"
        return "en"

    def t(self, key: str, **kwargs) -> str:
        """查文案。找不到就返回 key 本身——比崩掉好。"""
        catalog = CATALOGS.get(self.resolved) or CATALOGS.get("en") or {}
        text = catalog.get(key)
        if text is None:
            text = CATALOGS.get("en", {}).get(key, key)
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                return text
        return text

    def catalog(self) -> dict[str, str]:
        """当前语言的完整文案表——一次性发给前端。"""
        _load_catalogs()
        merged = dict(CATALOGS.get("en") or {})
        merged.update(CATALOGS.get(self.resolved) or {})
        return merged

    def all_catalogs(self) -> dict[str, dict[str, str]]:
        _load_catalogs()
        return dict(CATALOGS)


def available_languages() -> list[dict[str, str]]:
    return [
        {"id": "system", "name": "跟随系统"},
        {"id": "en", "name": "English"},
        {"id": "zh-Hans", "name": "简体中文"},
    ]
