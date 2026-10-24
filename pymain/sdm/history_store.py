"""历史记录：SQLite 存储。

对应 Swift 版 Engine/HistoryStore.swift。

表结构与 Swift 版一致，所以两边的历史文件可以互相读。
30 天保留期——每次写入时清理过期行。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .logging_setup import CAT_STORE, get_logger

log = get_logger()

RETENTION_DAYS = 30


@dataclass
class HistoryEntry:
    id: str
    url: str
    filename: str
    destination_path: str
    total_bytes: int | None
    bytes_done: int
    state: str
    error_message: str | None
    started_at: float
    finished_at: float
    sha256: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "filename": self.filename,
            "destination_path": self.destination_path,
            "total_bytes": self.total_bytes,
            "bytes_done": self.bytes_done,
            "state": self.state,
            "error_message": self.error_message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "sha256": self.sha256,
        }


class HistoryStore:
    """历史记录读写。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or paths.history_db_path()
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        with self._lock:
            if self._conn is not None:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._create_schema()
            self._prune_old_entries()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _create_schema(self) -> None:
        assert self._conn is not None
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS history (
              id TEXT PRIMARY KEY,
              url TEXT NOT NULL,
              filename TEXT NOT NULL,
              destination_path TEXT NOT NULL,
              total_bytes INTEGER,
              bytes_done INTEGER,
              state TEXT NOT NULL,
              error_message TEXT,
              started_at REAL NOT NULL,
              finished_at REAL NOT NULL,
              sha256 TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_history_finished_at ON history(finished_at DESC);
            CREATE INDEX IF NOT EXISTS idx_history_url ON history(url);
            """
        )
        self._conn.commit()

    def insert(self, entry: HistoryEntry) -> None:
        self.open()
        with self._lock:
            assert self._conn is not None
            self._conn.execute(
                """
                INSERT OR REPLACE INTO history
                (id, url, filename, destination_path, total_bytes, bytes_done,
                 state, error_message, started_at, finished_at, sha256)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.id,
                    entry.url,
                    entry.filename,
                    entry.destination_path,
                    entry.total_bytes,
                    entry.bytes_done,
                    entry.state,
                    entry.error_message,
                    entry.started_at,
                    entry.finished_at,
                    entry.sha256,
                ),
            )
            self._conn.commit()
        self._prune_old_entries()

    def all(self, limit: int = 500) -> list[HistoryEntry]:
        self.open()
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(
                """
                SELECT id, url, filename, destination_path, total_bytes, bytes_done,
                       state, error_message, started_at, finished_at, sha256
                FROM history
                ORDER BY finished_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            HistoryEntry(
                id=row["id"],
                url=row["url"],
                filename=row["filename"],
                destination_path=row["destination_path"],
                total_bytes=row["total_bytes"],
                bytes_done=row["bytes_done"],
                state=row["state"],
                error_message=row["error_message"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                sha256=row["sha256"],
            )
            for row in rows
        ]

    def remove(self, entry_id: str) -> None:
        self.open()
        with self._lock:
            assert self._conn is not None
            self._conn.execute("DELETE FROM history WHERE id = ?", (entry_id,))
            self._conn.commit()

    def clear_all(self) -> None:
        self.open()
        with self._lock:
            assert self._conn is not None
            self._conn.execute("DELETE FROM history")
            self._conn.commit()

    def _prune_old_entries(self) -> None:
        cutoff = time.time() - RETENTION_DAYS * 24 * 3600
        try:
            with self._lock:
                assert self._conn is not None
                self._conn.execute("DELETE FROM history WHERE finished_at < ?", (cutoff,))
                self._conn.commit()
        except sqlite3.Error as err:
            log.warning(CAT_STORE, f"历史清理失败: {err}")
