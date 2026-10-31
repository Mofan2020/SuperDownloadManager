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
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from . import work_dir


def _unlink_with_retry(path: Path, attempts: int = 20, delay: float = 0.05) -> bool:
    """删文件，失败就重试。

    Windows 上文件被别的句柄打开时 unlink 会抛 PermissionError。
    分片任务取消后还在收尾，句柄要过一会儿才释放，所以重试几次。

    macOS 上第一次就成功，这条路径主要是给 Windows 用的。
    """
    for i in range(attempts):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if i == attempts - 1:
                return False
            time.sleep(delay)
    return False


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
        #: 每个分片**应该**有多长（区间长度）。
        #:
        #: 合并前要拿它核对盘上的分片文件——长度不对说明这个分片
        #: 没下完（空洞）或者下多了（溢出覆盖邻居）。两种都产出
        #: 「大小正常、内容全错」的文件。
        #:
        #: 动态分段会改分片的长度，`shrink_chunk` 负责同步这个值。
        self._chunk_lengths: dict[int, int] = {}
        #: 每个分片最后一次写入的时刻
        self._chunk_updated: dict[int, float] = {}
        #: 每个分片**首次**写入的时刻——「这个分片从什么时候开始下」
        self._chunk_started: dict[int, float] = {}
        #: 每个分片**写满**的时刻——「这个分片什么时候下完的」
        self._chunk_finished: dict[int, float] = {}
        self._queue_lock = threading.Lock()

    # -- 分片文件命名 ------------------------------------------------

    def partial_path(self, chunk_id: int) -> Path:
        """分片临时文件：`downloads/<key>.sdm.partial-<id>`。

        **不在下载目录里。** 分片文件是软件的内部实现细节，
        十几个躺在用户的下载文件夹里只会让人以为下载坏了。
        """
        return work_dir.adopt_legacy_partial(self.destination, chunk_id)

    # -- 打开 / 写入 ------------------------------------------------

    def open_chunk(
        self,
        chunk_id: int,
        start_offset: int,
        resume: bool = True,
        end_offset: int | None = None,
    ) -> None:
        """打开分片文件，记录它在最终文件里的起始位置。

        以追加模式打开——v1.0.x 用覆盖模式，每次打开都把文件截断，
        暂停后恢复的下载会静默丢掉已下载的字节。

        `resume=False` 表示这个分片**从头开始**：盘上残留的旧字节
        必须清掉。

        **为什么必须清。** 残留的分片文件（取消过的下载、删不掉的
        旧文件、Windows 上被占用没删掉的）会被当成「这个分片已经
        下好了这么多字节」——于是：

        1. 新下载**跳过**这个分片（`start >= chunk.end` 直接返回），
           但规划器里的 `bytes_done` 还是 0 → `all_complete()` 永远
           不成立 → **worker 无限空转，下载永远卡住**。
        2. 就算没卡住，合并出来的文件也是**旧内容 + 新内容**的拼接，
           静默损坏。

        这不是假想：Windows 上分片文件被句柄占着时删不掉（`unlink`
        抛 PermissionError），旧文件就留在那儿了。所以**不靠删除，
        靠截断**——文件开着也能截。

        `end_offset` 是这个分片在最终文件里的结束位置。给了就记下
        「这个分片应该有多长」，合并前拿它核对盘上的分片文件。
        """
        with self._queue_lock:
            if chunk_id in self._handles:
                return
            self.destination.parent.mkdir(parents=True, exist_ok=True)
            path = self.partial_path(chunk_id)
            pre_existing = 0
            if path.exists():
                if resume:
                    pre_existing = path.stat().st_size
            else:
                path.touch()

            # 不用 with：句柄要长期持有，分片写完才关。
            handle = open(path, "r+b")  # noqa: SIM115
            if not resume:
                # 从头开始：把残留的旧字节清零。
                handle.truncate(0)
            if pre_existing > 0:
                handle.seek(0, os.SEEK_END)
            self._handles[chunk_id] = handle
            self._chunk_locks[chunk_id] = threading.Lock()
            self._partial_paths[chunk_id] = path
            self._chunk_starts[chunk_id] = start_offset
            self._chunk_written[chunk_id] = pre_existing
            if end_offset is not None:
                self._chunk_lengths[chunk_id] = max(0, end_offset - start_offset)

    def append(self, data: bytes, chunk_id: int) -> int:
        """往分片文件追加字节，返回写入量。"""
        return self.append_limited(data, chunk_id, limit=None)[0]

    def append_limited(
        self,
        data: bytes,
        chunk_id: int,
        limit: int | Callable[[], int | None] | None,
    ) -> tuple[int, bool]:
        """追加字节，但**不超过** `limit`（绝对偏移上限）。

        返回 `(写入量, 是否因为到达上限而截断)`。

        **为什么需要上限。** 动态分段（规格 §1.2）会在下载过程中把
        一个在途分片对半拆开——旧分片的区间随之缩短。那一路传输的
        HTTP 请求已经带着旧的 Range 发出去了，服务器还会继续发字节。
        如果照单全收，这些字节就写到了新分片的区间里，两个分片
        的内容重叠，最后合并出来的文件是坏的，而且**大小看着正常**，
        极难发现。

        所以写入前先看上限，超出的部分丢掉。最多丢一个数据块
        （64 KB），换来的是文件一定是对的。

        **`limit` 可以传一个取值函数。** 这是必须的：拆分线程会在
        传输线程读上限的同一瞬间把上限改小。取值函数在**分片锁内**
        调用，于是「读上限」和「写字节」是原子的——拆分线程想改
        上限也得先拿这把锁。

        传函数时的时序：
        1. 传输线程拿锁 → 调 limit() 拿到当前上限 → 写。
        2. 拆分线程等锁 → 拿到锁 → 砍掉超出部分（`shrink_chunk`）。

        两步都拿同一把锁，所以不会出现「上限是旧的、文件被砍过」
        的交叉状态。
        """
        with self._queue_lock:
            lock = self._chunk_locks.get(chunk_id)
            handle = self._handles.get(chunk_id)
        if lock is None or handle is None:
            raise AssemblerError(f"chunk {chunk_id} is not open")

        truncated = False
        with lock:
            current_limit = limit() if callable(limit) else limit
            if current_limit is not None:
                # `handle.tell()` 是**分片内**的偏移（从 0 开始），
                # 而 `limit` 是**文件里的绝对偏移**。两者差一个分片
                # 起点——不换算的话，第二个分片能写进两倍于它应有
                # 的数据。单流下载（服务器无视 Range，把整个文件
                # 发过来）时这个 bug 会让文件凭空多出一截。
                base = self._chunk_starts.get(chunk_id, 0)
                pos = base + handle.tell()
                if pos >= current_limit:
                    return 0, True
                if pos + len(data) > current_limit:
                    data = data[: current_limit - pos]
                    truncated = True
            handle.write(data)
            handle.flush()
        n = len(data)
        with self._queue_lock:
            self._chunk_written[chunk_id] = self._chunk_written.get(chunk_id, 0) + n
            now = time.monotonic()
            # 每个分片自己的最后写入时刻。详情页的「耗时」列用它——
            # 所有分片显示同一个值是没意义的。
            self._chunk_updated[chunk_id] = now
            # **首次写入 = 这个分片开始下载的时刻。**
            # 详情页的「耗时」列要的是「这个分片下了多久」，
            # 不是「距上次写入多久」——后者对已完成的分片会一直涨，
            # 越老的分片数字越大，看着像卡住了。
            self._chunk_started.setdefault(chunk_id, now)
            # **写满 = 这个分片下完的时刻。**
            length = self._chunk_lengths.get(chunk_id)
            if length is not None and self._chunk_written[chunk_id] >= length:
                self._chunk_finished.setdefault(chunk_id, now)
        return n, truncated


    def shrink_chunk(self, chunk_id: int, new_end: int) -> int:
        """把分片的结束位置往前挪，砍掉临时文件里多出来的字节。

        动态分段把在途分片拆开时要调这个：分片的区间从
        `[start, old_end)` 缩成 `[start, new_end)`，但临时文件里可能
        已经写了超过 `new_end` 的字节——那些字节属于新分片的区间，
        留着就会重叠。

        **必须在写分片的同一把锁下调用**，否则会跟正在写的协程
        打架：协程刚判断完「还能写」，这边就把文件截短了。

        返回被砍掉的字节数（0 表示没砍）。
        """
        with self._queue_lock:
            lock = self._chunk_locks.get(chunk_id)
            handle = self._handles.get(chunk_id)
            start = self._chunk_starts.get(chunk_id, 0)
        if lock is None or handle is None:
            # 分片没打开（还没开始下就被拆了）——区间照样变短了，
            # 「应该有多长」也得更新。
            self.set_chunk_length(chunk_id, new_end)
            return 0
        with lock:
            keep = max(0, new_end - start)
            pos = handle.tell()
            if pos <= keep:
                # **没砍掉字节，但区间还是变短了。**
                # 「应该有多长」必须更新——否则合并前拿旧长度去核对
                # 新内容，误判成「分片损坏」，好端端的下载合并失败。
                self.set_chunk_length(chunk_id, new_end)
                return 0
            try:
                handle.truncate(keep)
                handle.seek(keep)
            except OSError:
                self.set_chunk_length(chunk_id, new_end)
                return 0
            removed = pos - keep
        with self._queue_lock:
            self._chunk_written[chunk_id] = keep
            # **区间变短了，「应该有多长」也要跟着变。**
            # 合并前要拿它核对盘上的分片文件——不更新的话，
            # 合并时会拿「旧长度」去核对「已截短的文件」，误判成损坏。
            self._chunk_lengths[chunk_id] = keep
        return removed


    def set_chunk_length(self, chunk_id: int, end_offset: int) -> None:
        """更新「这个分片应该有多长」。

        动态分段把一个**不在途**的分片拆开时用：区间缩短了，
        但盘上的 partial 文件内容仍然有效（那部分区间没变），
        只是「应该有多长」这个记录要跟着改。

        不改的话，合并前核对长度时会拿旧长度去核对新内容，
        误判成「分片损坏」，好端端的下载合并失败。
        """
        with self._queue_lock:
            start = self._chunk_starts.get(chunk_id, 0)
            self._chunk_lengths[chunk_id] = max(0, end_offset - start)

    def chunk_updated_at(self, chunk_id: int) -> float | None:
        """这个分片最后一次写入的时刻（monotonic 秒）。"""
        with self._queue_lock:
            return self._chunk_updated.get(chunk_id)

    def chunk_started_at(self, chunk_id: int) -> float | None:
        """这个分片**首次**写入的时刻（monotonic 秒）。

        没有就是「还没开始写」。详情页的「耗时」列用它算
        「还在下载中的分片已经花了多久」。
        """
        with self._queue_lock:
            return self._chunk_started.get(chunk_id)

    def chunk_finished_at(self, chunk_id: int) -> float | None:
        """这个分片**写满**的时刻（monotonic 秒）。

        没有就是「还没下完」。
        """
        with self._queue_lock:
            return self._chunk_finished.get(chunk_id)

    def chunk_bytes_on_disk(self, chunk_id: int) -> int:
        """这个分片的临时文件在盘上实际有多少字节。

        **跟 `bytes_written()` 的区别**：后者是「我们认为自己写了
        多少」——内存里的计数。这个是「盘上真的有多少」。

        两者对不上说明出了问题（文件被别的东西动了、或者计数漏
        更新）。重试的时候必须用这个核对：重试要从断点继续，
        断点算错就会把已下载的数据**覆盖**成同一段的重放——
        文件大小正常，内容全错。
        """
        try:
            return self.partial_path(chunk_id).stat().st_size
        except OSError:
            return 0

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
        tmp_target = work_dir.merging_path(self.destination)
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
                # **分片内容的长度必须正好等于它的区间长度。**
                #
                # 少了 → 那个区间留一段零（空洞），文件大小看着对，
                #        内容全错。
                # 多了 → `copyfileobj` 会一路拷到 EOF，**溢出覆盖
                #        相邻分片的数据**——下一个分片还没写就被盖掉，
                #        或者反过来它盖掉下一个分片。
                #
                # 两种都必须在合并前拦住。合并完再去查就晚了——
                # 已经被 `os.replace` 换成用户的最终文件了。
                expected = self._chunk_lengths.get(chunk_id, 0)
                partial = partial_paths.get(chunk_id) or self.partial_path(chunk_id)
                if not partial.exists():
                    # **分片文件不见了 → 失败，不是跳过。**
                    #
                    # 跳过的话，这一段在最终文件里就是一段零——文件
                    # 大小看着完全正常，内容却缺了一块。跟「分片
                    # 长度不对」是同一类损坏，只是更难发现。
                    raise AssemblerError(
                        f"分片 {chunk_id} 的临时文件不见了：{partial}"
                    )
                try:
                    on_disk = partial.stat().st_size
                except OSError as err:
                    raise AssemblerError(
                        f"分片 {chunk_id} 读不了：{partial}（{err}）"
                    ) from err
                if not expected:
                    # **没记录过长度 → 没法核对 → 跳过。**
                    #
                    # `end_offset` 是可选参数。生产路径（`download_flow`
                    # 和 `http_downloader`）一定会传，所以核对照常生效。
                    #
                    # 但直接用 assembler 的调用方（测试、将来的别的
                    # 使用者）可能不传——那时候拿「分片应该有多长」
                    # 去核对就是瞎猜，会把好好的下载判成失败。
                    #
                    # 宁可少一道核对，也不要把正常的东西判成坏的。
                    # 分片文件不在的情况仍然报错——那个是真的丢了数据。
                    pass
                elif on_disk != expected:
                    raise AssemblerError(
                        f"分片 {chunk_id} 的长度不对："
                        f"应为 {expected} 字节，盘上是 {on_disk} 字节"
                    )
                out.seek(start)
                with open(partial, "rb") as src:
                    # **限长拷贝。** 分片文件的长度已经核对过，
                    # 这里再加一道保险：最多拷 expected 字节，
                    # 溢出不了下一个分片。
                    #
                    # 没记录长度时（`end_offset` 没传）就按盘上的实际
                    # 大小拷——那种情况下没有「应该有多长」可比。
                    remaining = expected if expected else on_disk
                    while remaining > 0:
                        block = src.read(min(256 * 1024, remaining))
                        if not block:
                            break
                        out.write(block)
                        remaining -= len(block)

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

    def close_all(self) -> None:
        """关掉所有分片文件的句柄。

        Windows 上文件被打开时删不掉。取消任务要删分片，得先
        把句柄都关掉——POSIX 上删打开的文件只是延迟释放 inode，
        Windows 上直接 PermissionError。
        """
        with self._queue_lock:
            handles = list(self._handles.values())
            self._handles.clear()
            self._chunk_locks.clear()
        for handle in handles:
            try:
                handle.close()
            except OSError:
                pass

    def cleanup_partials(self) -> None:
        """删掉所有分片临时文件（取消任务时用）。

        Windows 上正在被写的文件删不掉——unlink 抛 PermissionError。
        所以要重试：分片任务收到取消信号后会停下，句柄随之释放，
        删就能成功。

        **按工作目录里的文件名模式扫，不只看已登记的路径。**
        动态分段切出来的新分片可能还没被 open 过（worker 还没
        抢到它），它的残留文件就不在 `_partial_paths` 里——
        只清登记过的会漏掉它们。

        漏掉的后果很严重：**脏分片留在盘上**，下次续传会拿它
        当「已经下好的字节」接着下，产出的是一个大小正常、
        内容全错的文件。实测（2026-10-30）：用户下的 4 个文件
        全部损坏，每个分片的前 64KB 都等于文件开头。
        """
        with self._queue_lock:
            for handle in self._handles.values():
                try:
                    handle.close()
                except OSError:
                    pass
            self._handles.clear()
            self._chunk_locks.clear()
            to_delete = list(self._partial_paths.values())
            self._partial_paths.clear()
        # **扫工作目录，把这个任务的分片文件全找出来。**
        # 文件名形如 `<key>.sdm.partial-<id>`，key 由目标路径哈希
        # 算出来——所以只匹配这个任务自己的文件。
        #
        # 局部变量不叫 `paths`——`sdm.paths` 是本项目的一个模块，
        # 静态检查器会把 `paths.xxx` 当成跨模块引用来查。
        try:
            key = work_dir.task_key(self.destination)
            root = work_dir.work_root()
            if root.is_dir():
                for found in root.glob(f"{key}{work_dir.PARTIAL_INFIX}*"):
                    if found not in to_delete:
                        to_delete.append(found)
        except OSError:
            pass
        for path in to_delete:
            _unlink_with_retry(path)
