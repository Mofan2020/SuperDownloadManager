"""单流续传：从已落盘字节继续。

对应 Swift 版 Engine/ResumeFlow.swift。

Swift 版只做单流续传（一条 Range 请求把剩下的字节拉完），
因为当时的 DownloadFlow 没法按分片续。这次重写里主路径已经
支持按分片续传（分片文件扫盘 + Range 从已落盘字节继续），
这个类保留给「只有一个 .part 文件的旧任务」和「分片布局变了
只能整体重来」的场景。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx

from ..logging_setup import CAT_ENGINE, get_logger
from .content_sniffer import ContentProbe
from .http_downloader import DownloadError, DownloadFailure, HTTPDownloader

log = get_logger()


class ResumeFailure(Exception):
    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


class ResumeFlow:
    """把 `.part` 文件补完整。"""

    def __init__(
        self,
        url: str,
        destination: Path,
        probe: ContentProbe,
        downloader: HTTPDownloader | None = None,
    ) -> None:
        self.url = url
        self.destination = Path(destination)
        self.probe = probe
        self.downloader = downloader or HTTPDownloader()

    @property
    def part_path(self) -> Path:
        return self.destination.with_name(self.destination.name + ".part")

    async def run(self) -> int:
        """补完 .part，返回最终字节数。"""
        bytes_done = self.part_path.stat().st_size if self.part_path.exists() else 0
        if bytes_done <= 0:
            return 0
        total = self.probe.content_length or 0
        if total > 0 and total <= bytes_done:
            return bytes_done

        headers = {"Range": f"bytes={bytes_done}-{total - 1}"}
        request = self.downloader.client.build_request("GET", self.url, headers=headers)
        try:
            response = await self.downloader.client.send(request, stream=True)
        except httpx.HTTPError as err:
            raise ResumeFailure("io_error", str(err)) from err

        try:
            if response.status_code != 206:
                raise ResumeFailure("range_rejected", f"服务器返回 {response.status_code}，不支持续传")
            written = 0
            mode = "r+b" if self.part_path.exists() else "wb"
            with open(self.part_path, mode) as fh:
                fh.seek(0, 2)  # 接到已有内容后面
                async for block in response.aiter_bytes(chunk_size=64 * 1024):
                    fh.write(block)
                    written += len(block)
            # 补完了，把 .part 落成最终文件。
            # 用 os.replace 而不是 rename——Windows 上目标已存在时
            # Path.rename 会抛 FileExistsError。
            total_written = bytes_done + written
            os.replace(self.part_path, self.destination)
            log.notice(
                CAT_ENGINE,
                f"续传完成 {self.destination.name} 追加 {written} 字节 "
                f"（累计 {total_written}）",
            )
            return total_written
        finally:
            await response.aclose()
