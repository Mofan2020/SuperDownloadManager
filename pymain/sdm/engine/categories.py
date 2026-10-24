"""下载分类与 URL 启发式。

对应 Swift 版 Engine/FileURLHeuristic.swift 与 App/MainWindow/TaskSummary.swift
里的 DownloadCategory。两处扩展名表必须保持一致——加新分类时三处一起改。
"""

from __future__ import annotations

import enum
from urllib.parse import urlparse


class DownloadCategory(enum.Enum):
    VIDEO = "video"
    MUSIC = "music"
    PICTURES = "pictures"
    ARCHIVES = "archives"
    DOCUMENTS = "documents"
    SOFTWARE = "software"
    OTHER = "other"

    @property
    def folder_name(self) -> str:
        return {
            DownloadCategory.VIDEO: "Video",
            DownloadCategory.MUSIC: "Music",
            DownloadCategory.PICTURES: "Pictures",
            DownloadCategory.ARCHIVES: "Archives",
            DownloadCategory.DOCUMENTS: "Documents",
            DownloadCategory.SOFTWARE: "Software",
            DownloadCategory.OTHER: "Other",
        }[self]

    @property
    def icon(self) -> str:
        return {
            DownloadCategory.VIDEO: "video",
            DownloadCategory.MUSIC: "music",
            DownloadCategory.PICTURES: "image",
            DownloadCategory.ARCHIVES: "archive",
            DownloadCategory.DOCUMENTS: "file-text",
            DownloadCategory.SOFTWARE: "package",
            DownloadCategory.OTHER: "help-circle",
        }[self]

    @property
    def tint(self) -> str:
        return {
            DownloadCategory.VIDEO: "#ec4899",
            DownloadCategory.MUSIC: "#a855f7",
            DownloadCategory.PICTURES: "#14b8a6",
            DownloadCategory.ARCHIVES: "#f59e0b",
            DownloadCategory.DOCUMENTS: "#3b82f6",
            DownloadCategory.SOFTWARE: "#6366f1",
            DownloadCategory.OTHER: "#9ca3af",
        }[self]


_EXTENSION_MAP: dict[str, DownloadCategory] = {
    # 视频
    "mp4": DownloadCategory.VIDEO, "mov": DownloadCategory.VIDEO,
    "mkv": DownloadCategory.VIDEO, "webm": DownloadCategory.VIDEO,
    "avi": DownloadCategory.VIDEO, "flv": DownloadCategory.VIDEO,
    "m4v": DownloadCategory.VIDEO, "wmv": DownloadCategory.VIDEO,
    "ts": DownloadCategory.VIDEO,
    # 音乐
    "mp3": DownloadCategory.MUSIC, "flac": DownloadCategory.MUSIC,
    "wav": DownloadCategory.MUSIC, "aac": DownloadCategory.MUSIC,
    "ogg": DownloadCategory.MUSIC, "m4a": DownloadCategory.MUSIC,
    "opus": DownloadCategory.MUSIC,
    # 图片
    "jpg": DownloadCategory.PICTURES, "jpeg": DownloadCategory.PICTURES,
    "png": DownloadCategory.PICTURES, "gif": DownloadCategory.PICTURES,
    "webp": DownloadCategory.PICTURES, "heic": DownloadCategory.PICTURES,
    "svg": DownloadCategory.PICTURES, "bmp": DownloadCategory.PICTURES,
    "tiff": DownloadCategory.PICTURES,
    # 压缩包
    "zip": DownloadCategory.ARCHIVES, "7z": DownloadCategory.ARCHIVES,
    "rar": DownloadCategory.ARCHIVES, "tar": DownloadCategory.ARCHIVES,
    "gz": DownloadCategory.ARCHIVES, "bz2": DownloadCategory.ARCHIVES,
    "xz": DownloadCategory.ARCHIVES, "iso": DownloadCategory.ARCHIVES,
    "tgz": DownloadCategory.ARCHIVES, "tbz2": DownloadCategory.ARCHIVES,
    "txz": DownloadCategory.ARCHIVES,
    # 文档
    "pdf": DownloadCategory.DOCUMENTS, "doc": DownloadCategory.DOCUMENTS,
    "docx": DownloadCategory.DOCUMENTS, "xls": DownloadCategory.DOCUMENTS,
    "xlsx": DownloadCategory.DOCUMENTS, "ppt": DownloadCategory.DOCUMENTS,
    "pptx": DownloadCategory.DOCUMENTS, "txt": DownloadCategory.DOCUMENTS,
    "md": DownloadCategory.DOCUMENTS, "epub": DownloadCategory.DOCUMENTS,
    "mobi": DownloadCategory.DOCUMENTS, "rtf": DownloadCategory.DOCUMENTS,
    "csv": DownloadCategory.DOCUMENTS,
    # 软件
    "exe": DownloadCategory.SOFTWARE, "msi": DownloadCategory.SOFTWARE,
    "deb": DownloadCategory.SOFTWARE, "rpm": DownloadCategory.SOFTWARE,
    "apk": DownloadCategory.SOFTWARE, "app": DownloadCategory.SOFTWARE,
    "dmg": DownloadCategory.SOFTWARE, "pkg": DownloadCategory.SOFTWARE,
}


def category_for_extension(ext: str) -> DownloadCategory | None:
    """按扩展名判断分类。未知扩展名返回 None。"""
    return _EXTENSION_MAP.get(ext.lower().lstrip("."))


def category_from_url(url: str) -> DownloadCategory:
    """按 URL 路径扩展名判断分类，判不出来归 Other。"""
    path = urlparse(url).path
    ext = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
    cat = category_for_extension(ext)
    return cat or DownloadCategory.OTHER


def is_likely_file_url(url: str) -> bool:
    """判断一个 URL 是不是「几乎肯定是文件」。

    规则刻意保守——宁可漏掉候选，也别每复制一条聊天里的链接
    就弹一次下载确认。扩展名能映射到已知分类才算。
    """
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return False
    ext = name.rsplit(".", 1)[-1]
    if not ext or "/" in ext or "?" in ext:
        return False
    return category_for_extension(ext) is not None
