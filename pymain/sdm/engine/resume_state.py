"""续传状态：落盘的 sidecar JSON。

对应 Swift 版 Engine/ResumeState.swift。

文件格式（`<目标名>.sdm.json`）：
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
from datetime import datetime, timezone
from pathlib import Path

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
    ) -> "ResumeState":
        return cls(
            url=probe.url,
            content_length=probe.content_length or 0,
            etag=probe.etag,
            last_modified=probe.last_modified,
            accepts_ranges=probe.accepts_ranges,
            filename=filename,
            chunks=[asdict(c) for c in chunks],
            saved_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def sidecar_path(final_path: Path) -> Path:
        """目标文件旁边的 sidecar：`x.dmg` → `x.dmg.sdm.json`。"""
        return final_path.with_name(final_path.name + ".sdm.json")

    def write(self, final_path: Path) -> None:
        dst = self.sidecar_path(final_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(asdict(self), indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(dst)

    @staticmethod
    def read(sidecar: Path) -> "ResumeState":
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return cls(**data)
