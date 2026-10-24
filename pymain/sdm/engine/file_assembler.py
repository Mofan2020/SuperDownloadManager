"""分片写入与最终合并。

对应 Swift 版 Engine/FileAssembler.swift。

每个分片写一个独立的临时文件 `<目标>.sdm.partial-<chunkID>`，
全部完成后按分片顺序合并成目标文件。

为什么用分片独立文件而不是并发 seek-write 同一个文件：
并发写同一文件的字节位置容易错位。Swift 版 v1.1.2 之前用 actor
串行化所有 append，结果 8 个分片排队写，看起来是串行下载。
现在每个分片一个句柄 + 一把自己锁——同分片串行、跨分片并行。
"""

from __future__ import annotations

import os
import shutil
import time
import threading
from pathlib import Path
from typing import BinaryIO


class AssemblerError(Exception):
    pass


class FileAssembler:
    """管理单个下载的所有分片文件。"""

    def __init__(self, destination: Path) -> None:
        self.destination = Path(destination)
        self._handles: dict[int, BinaryIO] = {}
        self._chunk_locks: dict[int, threading.Lock] = {}
        self._partial_paths: dict[int, Path] = {}
        self._chunk_starts: dict[int, int] = {}
        self._chunk_written: dict[int, int] = {}
        #: 每个分片最后一次写入的时刻
        self._chunk_updated: dict[int, float] = {}
        self._queue_lock = threading.Lock()

    # -- 分片文件命名 ------------------------------------------------

    def partial_path(self, chunk_id: int) -> Path:
        """`<目标>.sdm.partial-<chunkID>`。"""
        return self.destination.with_name(self.destination.name + f".sdm.partial-{chunk_id}")

    # -- 打开 / 写入 ------------------------------------------------

    def open_chunk(self, chunk_id: int, start_offset: int) -> None:
        """打开分片文件，记录它在最终文件里的起始位置。

        以追加模式打开——v1.0.x 用覆盖模式，每次打开都把文件截断，
        暂停后恢复的下载会静默丢掉已下载的字节。
        """
        with self._queue_lock:
            if chunk_id in self._handles:
                return
            self.destination.parent.mkdir(parents=True, exist_ok=True)
            path = self.partial_path(chunk_id)
            pre_existing = 0
            if path.exists():
                pre_existing = path.stat().st_size
            else:
                path.touch()

            handle = open(path, "r+b")
            if pre_existing > 0:
                handle.seek(0, os.SEEK_END)
            self._handles[chunk_id] = handle
            self._chunk_locks[chunk_id] = threading.Lock()
            self._partial_paths[chunk_id] = path
            self._chunk_starts[chunk_id] = start_offset
            self._chunk_written[chunk_id] = pre_existing

    def append(self, data: bytes, chunk_id: int) -> int:
        """往分片文件追加字节，返回写入量。"""
        with self._queue_lock:
            lock = self._chunk_locks.get(chunk_id)
            handle = self._handles.get(chunk_id)
        if lock is None or handle is None:
            raise AssemblerError(f"chunk {chunk_id} is not open")

        # 只锁同一个分片的写入，不同分片互不阻塞。
        with lock:
            handle.write(data)
            handle.flush()
        n = len(data)
        with self._queue_lock:
            self._chunk_written[chunk_id] = self._chunk_written.get(chunk_id, 0) + n
            # 每个分片自己的最后写入时刻。详情页的「耗时」列用它——
            # 所有分片显示同一个值是没意义的。
            self._chunk_updated[chunk_id] = time.monotonic()
        return n

    def chunk_updated_at(self, chunk_id: int) -> float | None:
        """这个分片最后一次写入的时刻（monotonic 秒）。"""
        with self._queue_lock:
            return self._chunk_updated.get(chunk_id)

    def bytes_written(self, chunk_id: int) -> int:
        """这个分片已经落盘多少字节。

        重连时 Range 头要按这个值往后推——从「服务器发过的量」
        推算会在丢包时多下一段重复数据。
        """
        with self._queue_lock:
            if chunk_id in self._chunk_written:
                return self._chunk_written[chunk_id]
            path = self._partial_paths.get(chunk_id)
        if path is None:
            return 0
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def close_chunk(self, chunk_id: int) -> None:
        with self._queue_lock:
            handle = self._handles.pop(chunk_id, None)
            self._chunk_locks.pop(chunk_id, None)
            self._partial_paths.pop(chunk_id, None)
            self._chunk_updated.pop(chunk_id, None)
        if handle is not None:
            handle.close()

    # -- 合并 --------------------------------------------------------

    def finalize(self, chunks_in_order: list[int]) -> None:
        """把全部分片按顺序合并成目标文件，然后删掉分片文件。

        合并中途失败不会留下半个目标文件——目标文件是最后
        一步才出现的。
        """
        with self._queue_lock:
            for chunk_id in chunks_in_order:
                handle = self._handles.pop(chunk_id, None)
                if handle is not None:
                    handle.close()
            self._chunk_locks.clear()
            partial_paths = dict(self._partial_paths)
            starts = dict(self._chunk_starts)
            written = dict(self._chunk_written)
            self._partial_paths.clear()

        self.destination.parent.mkdir(parents=True, exist_ok=True)
        tmp_target = self.destination.with_name(self.destination.name + ".merging")
        if tmp_target.exists():
            tmp_target.unlink()

        # 按起始偏移排序，这样文件是按位置顺序写下去的，
        # 分片之间的空洞用零填充。
        ordered = sorted(
            ((cid, starts[cid]) for cid in chunks_in_order if cid in starts),
            key=lambda pair: pair[1],
        )
        if not ordered:
            return

        last_offset = ordered[-1][1]
        last_bytes = written.get(ordered[-1][0], 0)
        total_size = last_offset + last_bytes

        with open(tmp_target, "wb") as out:
            # 预分配总长度，让内核一次性铺好连续块，
            # 免得文件涨大时反复扩 FS 对象导致碎片。
            if total_size > 0:
                out.truncate(total_size)
            for chunk_id, start in ordered:
                out.seek(start)
                partial = partial_paths.get(chunk_id) or self.partial_path(chunk_id)
                if not partial.exists():
                    continue
                with open(partial, "rb") as src:
                    shutil.copyfileobj(src, out, length=256 * 1024)

        # 原子替换——用户不会看到一个写了一半的目标文件。
        #
        # 用 os.replace 而不是 unlink + rename：
        #   - POSIX 上 rename 静默覆盖，但 unlink + rename 中间有
        #     一个「目标文件不存在」的空窗，别人可能抓到。
        #   - Windows 上 Path.rename 在目标已存在时直接抛
        #     FileExistsError，目标正被占用时 unlink 也会失败。
        # os.replace 是跨平台的原子覆盖，语义一致。
        os.replace(tmp_target, self.destination)

        for chunk_id in chunks_in_order:
            path = partial_paths.get(chunk_id) or self.partial_path(chunk_id)
            try:
                path.unlink()
            except OSError:
                pass

    def cleanup_partials(self) -> None:
        """删掉所有分片临时文件（取消任务时用）。"""
        with self._queue_lock:
            for handle in self._handles.values():
                try:
                    handle.close()
                except OSError:
                    pass
            self._handles.clear()
            self._chunk_locks.clear()
            paths = list(self._partial_paths.values())
            self._partial_paths.clear()
        for path in paths:
            try:
                path.unlink()
            except OSError:
                pass
