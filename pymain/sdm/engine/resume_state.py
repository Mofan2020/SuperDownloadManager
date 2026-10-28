"""续传状态：落盘的 sidecar JSON。

对应 Swift 版 Engine/ResumeState.swift。

文件格式（`downloads/<key>.sdm.json`）：
    {
      "url": "...",
      "content_length": 12345,
      "etag": "...",
      "last_modified": "...",
      "accepts_ranges": true,
      "filename": "...",
      "chunks": [{"id": 0, "start": 0, "end": 4096, "bytes_done": 4096}],
      "saved_at": "2026-01-01T00:00:00+00:00"
    }

`etag` / `content_length` 用来判断「换链接后还是不是同一个文件」——
不一致时不能把新内容续写进旧的空洞里。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import work_dir
from .content_sniffer import ContentProbe
from .range_planner import Chunk


@dataclass
class ResumeState:
    url: str
    content_length: int = 0
    etag: str | None = None
    last_modified: str | None = None
    accepts_ranges: bool = False
    filename: str = ""
    chunks: list[dict] = field(default_factory=list)
    saved_at: str = ""

    @classmethod
    def from_probe(
        cls,
        probe: ContentProbe,
        filename: str,
        chunks: list[Chunk],
    ) -> ResumeState:
        # **按起始偏移排好序再存。**
        #
        # 规划器返回的是「创建顺序」，不是「文件里的位置顺序」——动态
        # 分段会反复拆在途分片，创建顺序跟字节位置完全对不上。存成
        # 位置顺序，sidecar 才读得懂（也才每次写出来都一样）。
        ordered = sorted(chunks, key=lambda c: c.start)
        return cls(
            url=probe.url,
            content_length=probe.content_length or 0,
            etag=probe.etag,
            last_modified=probe.last_modified,
            accepts_ranges=probe.accepts_ranges,
            filename=filename,
            chunks=[asdict(c) for c in ordered],
            saved_at=datetime.now(UTC).isoformat(),
        )

    @staticmethod
    def sidecar_path(final_path: Path) -> Path:
        """sidecar 的位置：`downloads/<key>.sdm.json`。

        **不在下载目录里。** sidecar 是软件的内部元信息，跟用户
        要的文件混在一起只会污染下载目录（详见 `work_dir` 模块）。
        """
        return work_dir.sidecar_path(final_path)

    def write(self, final_path: Path) -> None:
        dst = self.sidecar_path(final_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(asdict(self), indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(dst)

    @classmethod
    def read(cls, sidecar: Path) -> ResumeState:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return cls(**data)
