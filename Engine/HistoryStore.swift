import Foundation
import SQLite3

/// HistoryStore — v1.0.4 P2-1. Persistent history of every
/// download SDM has ever completed/failed/cancelled. Backed by
/// sqlite3 directly (no GRDB, no SwiftData) so the daemon and
/// the GUI can share the same file via POSIX file locking.
///
/// The store lives at:
/// `~/Library/Application Support/SuperDownloadManager/history.sqlite`
///
/// Schema:
/// ```
/// CREATE TABLE history (
///   id TEXT PRIMARY KEY,           -- UUID
///   url TEXT NOT NULL,
///   filename TEXT NOT NULL,
///   destination_path TEXT NOT NULL,
///   total_bytes INTEGER,
///   bytes_done INTEGER,
///   state TEXT NOT NULL,           -- completed/failed/cancelled
///   error_message TEXT,
///   started_at REAL NOT NULL,
///   finished_at REAL NOT NULL,
///   sha256 TEXT                    -- optional, computed on demand
/// );
/// ```
///
/// 30-day retention is enforced on every insert. The history is
/// the queue's "old" state — completed rows are moved here when
/// `clearCompleted()` is invoked.
public actor HistoryStore {
    public struct Entry: Sendable, Identifiable, Equatable {
        public let id: UUID
        public let url: String
        public let filename: String
        public let destinationPath: String
        public let totalBytes: Int64?
        public let bytesDone: Int64
        public let state: String
        public let errorMessage: String?
        public let startedAt: Date
        public let finishedAt: Date
        public let sha256: String?

        public init(id: UUID,
                    url: String,
                    filename: String,
                    destinationPath: String,
                    totalBytes: Int64?,
                    bytesDone: Int64,
                    state: String,
                    errorMessage: String?,
                    startedAt: Date,
                    finishedAt: Date,
                    sha256: String?) {
            self.id = id
            self.url = url
            self.filename = filename
            self.destinationPath = destinationPath
            self.totalBytes = totalBytes
            self.bytesDone = bytesDone
            self.state = state
            self.errorMessage = errorMessage
            self.startedAt = startedAt
            self.finishedAt = finishedAt
            self.sha256 = sha256
        }
    }

    private var db: OpaquePointer?
    private let path: URL

    public init(path: URL? = nil) {
        let defaultPath = FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            .map { $0.appendingPathComponent("SuperDownloadManager") }
            .map { $0.appendingPathComponent("history.sqlite") }
            ?? URL(fileURLWithPath: NSHomeDirectory())
                .appendingPathComponent(".sdm-history.sqlite")
        self.path = path ?? defaultPath
    }

    public func open() throws {
        try FileManager.default.createDirectory(
            at: path.deletingLastPathComponent(),
            withIntermediateDirectories: true)
        var handle: OpaquePointer?
        let flags = SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX
        if sqlite3_open_v2(path.path, &handle, flags, nil) != SQLITE_OK {
            let msg = String(cString: sqlite3_errmsg(handle))
            sqlite3_close_v2(handle)
            throw NSError(domain: "HistoryStore", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "open: \(msg)"])
        }
        self.db = handle
        try createSchema()
        try pruneOldEntries()
    }

    public func close() {
        if let db = db { sqlite3_close_v2(db) }
        db = nil
    }

    private func createSchema() throws {
        let sql = """
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
        if sqlite3_exec(db, sql, nil, nil, nil) != SQLITE_OK {
            let msg = String(cString: sqlite3_errmsg(db))
            throw NSError(domain: "HistoryStore", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "create: \(msg)"])
        }
    }

    public func insert(_ entry: Entry) throws {
        let sql = """
        INSERT OR REPLACE INTO history
        (id, url, filename, destination_path, total_bytes, bytes_done, state,
         error_message, started_at, finished_at, sha256)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
            throw NSError(domain: "HistoryStore", code: 3,
                          userInfo: [NSLocalizedDescriptionKey:
                            "prepare: \(String(cString: sqlite3_errmsg(db)))"])
        }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_text(stmt, 1, entry.id.uuidString, -1,
                          SQLITE_TRANSIENT)
        sqlite3_bind_text(stmt, 2, entry.url, -1, SQLITE_TRANSIENT)
        sqlite3_bind_text(stmt, 3, entry.filename, -1, SQLITE_TRANSIENT)
        sqlite3_bind_text(stmt, 4, entry.destinationPath, -1, SQLITE_TRANSIENT)
        if let total = entry.totalBytes {
            sqlite3_bind_int64(stmt, 5, total)
        } else {
            sqlite3_bind_null(stmt, 5)
        }
        sqlite3_bind_int64(stmt, 6, entry.bytesDone)
        sqlite3_bind_text(stmt, 7, entry.state, -1, SQLITE_TRANSIENT)
        if let err = entry.errorMessage {
            sqlite3_bind_text(stmt, 8, err, -1, SQLITE_TRANSIENT)
        } else {
            sqlite3_bind_null(stmt, 8)
        }
        sqlite3_bind_double(stmt, 9, entry.startedAt.timeIntervalSince1970)
        sqlite3_bind_double(stmt, 10, entry.finishedAt.timeIntervalSince1970)
        if let h = entry.sha256 {
            sqlite3_bind_text(stmt, 11, h, -1, SQLITE_TRANSIENT)
        } else {
            sqlite3_bind_null(stmt, 11)
        }
        if sqlite3_step(stmt) != SQLITE_DONE {
            throw NSError(domain: "HistoryStore", code: 4,
                          userInfo: [NSLocalizedDescriptionKey:
                            "step: \(String(cString: sqlite3_errmsg(db)))"])
        }
        try pruneOldEntries()
    }

    public func all(limit: Int = 500) throws -> [Entry] {
        let sql = """
        SELECT id, url, filename, destination_path, total_bytes, bytes_done,
               state, error_message, started_at, finished_at, sha256
        FROM history
        ORDER BY finished_at DESC
        LIMIT ?;
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
            throw NSError(domain: "HistoryStore", code: 5,
                          userInfo: [NSLocalizedDescriptionKey:
                            "prepare: \(String(cString: sqlite3_errmsg(db)))"])
        }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_int(stmt, 1, Int32(limit))
        var out: [Entry] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            let id = UUID(uuidString: String(cString: sqlite3_column_text(stmt, 0))) ?? UUID()
            let url = String(cString: sqlite3_column_text(stmt, 1))
            let filename = String(cString: sqlite3_column_text(stmt, 2))
            let path = String(cString: sqlite3_column_text(stmt, 3))
            let total: Int64? = sqlite3_column_type(stmt, 4) == SQLITE_NULL
                ? nil : sqlite3_column_int64(stmt, 4)
            let done = sqlite3_column_int64(stmt, 5)
            let state = String(cString: sqlite3_column_text(stmt, 6))
            let err: String? = sqlite3_column_type(stmt, 7) == SQLITE_NULL
                ? nil : String(cString: sqlite3_column_text(stmt, 7))
            let started = Date(timeIntervalSince1970: sqlite3_column_double(stmt, 8))
            let finished = Date(timeIntervalSince1970: sqlite3_column_double(stmt, 9))
            let sha: String? = sqlite3_column_type(stmt, 10) == SQLITE_NULL
                ? nil : String(cString: sqlite3_column_text(stmt, 10))
            out.append(Entry(
                id: id, url: url, filename: filename, destinationPath: path,
                totalBytes: total, bytesDone: done, state: state,
                errorMessage: err, startedAt: started, finishedAt: finished,
                sha256: sha))
        }
        return out
    }

    private func pruneOldEntries() throws {
        // 30 days
        let cutoff = Date().addingTimeInterval(-30 * 24 * 3600)
        let sql = "DELETE FROM history WHERE finished_at < ?;"
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
            return
        }
        defer { sqlite3_finalize(stmt) }
        sqlite3_bind_double(stmt, 1, cutoff.timeIntervalSince1970)
        sqlite3_step(stmt)
    }
}

private let SQLITE_TRANSIENT = unsafeBitCast(
    OpaquePointer(bitPattern: -1), to: sqlite3_destructor_type.self)
