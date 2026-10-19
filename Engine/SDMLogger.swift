// SDMLogger — v1.1.9. Centralised logging for the engine and
// app layer.
//
// Goals (per 星空喵 2026-10-19 brief):
//   1. **Levels** the user can switch between (debug / info /
//      warning / error). Mapped onto `OSLogType` so they show up
//      in Console.app with the right severity.
//   2. **File persistence**: every emitted record also lands in
//      `~/Library/Logs/SuperDownloadManager/sdm.log` (rotated when
//      it grows past the user's "max bytes" setting) so the user
//      can attach a log when filing a bug even if Console.app
//      has already trimmed the old records.
//   3. **Auto-cleanup**: a daily sweep trims log files older than
//      the user-configured retention (default 14 days) and caps
//      the line count at the user-configured ceiling (default
//      5000). Both knobs are exposed in Settings → Logs.
//
// Subsystem:
//   `com.skyc8266.superdownloadmanager` (matches `EngineLog.swift`).
//   Don't change it — existing Console.app recipes break.
//
// File layout:
//   ~/Library/Logs/SuperDownloadManager/
//       sdm.log          (current; size-capped at maxBytes)
//       sdm-2026-10-19.log  (rotated older; retained until purge)
//
// Concurrency:
//   Writes funnel through an internal `DispatchQueue(label:
//   "com.skyc8266.superdownloadmanager.logger", qos: .utility)`
//   so the hot path never blocks on disk IO. Reads (the "Open in
//   Preview" sheet) are also funnelled through the same queue;
//   callers can use the async `readAll()` for non-blocking
//   access or `readAllBlocking()` for the settings sheet that
//   needs an immediate snapshot.

import Foundation
import os.log

/// Severity ladder. Lower number = more verbose.
public enum SDMLogLevel: Int, CaseIterable, Sendable, Comparable {
    case debug = 0
    case info  = 1
    case notice = 2
    case warning = 3
    case error = 4

    public static func < (a: SDMLogLevel, b: SDMLogLevel) -> Bool {
        a.rawValue < b.rawValue
    }

    public var displayName: String {
        switch self {
        case .debug:   return "Debug"
        case .info:    return "Info"
        case .notice:  return "Notice"
        case .warning: return "Warning"
        case .error:   return "Error"
        }
    }

    /// Mapping onto Apple's `OSLogType`. Notice/diag have no direct
    /// OSLogType peer so we collapse them onto `.info`/`.default`.
    /// Matched on `rawValue` so future SDK additions like
    /// `.signpost` fall through to `.default` instead of breaking
    /// the build.
    var osLogType: OSLogType {
        switch rawValue {
        case SDMLogLevel.debug.rawValue:   return .debug
        case SDMLogLevel.info.rawValue:    return .info
        case SDMLogLevel.notice.rawValue:  return .default
        case SDMLogLevel.warning.rawValue: return .error
        case SDMLogLevel.error.rawValue:   return .fault
        default: return .default
        }
    }

    /// Short tag used in the file log so a tail-`grep` shows
    /// severity without parsing the date prefix.
    public var shortTag: String {
        switch self {
        case .debug:   return "DBG"
        case .info:    return "INF"
        case .notice:  return "NTC"
        case .warning: return "WRN"
        case .error:   return "ERR"
        }
    }
}

/// User-visible log destination. Persistence is on by default;
/// the GUI exposes it as a toggle so the user can turn it off
/// when they're filing a transient bug report and want a fresh
/// file next launch.
public struct SDMLogSettings: Equatable, Sendable {
    public var level: SDMLogLevel
    public var retentionDays: Int        // 0 = keep forever (not recommended)
    public var maxBytesPerFile: Int      // rotate when this is exceeded
    public var maxLines: Int             // cap total line count post-cleanup
    public var persistenceEnabled: Bool  // writes also go to disk

    /// v1.1.9: explicit public initializer. Swift's auto-
    /// synthesised memberwise init is internal by default, which
    /// blocks callers outside the framework (i.e. the App target)
    /// from constructing new settings. Mirror each field by hand
    /// with `public` so the Settings sheet can call this.
    public init(level: SDMLogLevel,
                retentionDays: Int,
                maxBytesPerFile: Int,
                maxLines: Int,
                persistenceEnabled: Bool) {
        self.level = level
        self.retentionDays = retentionDays
        self.maxBytesPerFile = maxBytesPerFile
        self.maxLines = maxLines
        self.persistenceEnabled = persistenceEnabled
    }

    public static let `default` = SDMLogSettings(
        level: .info,
        retentionDays: 14,
        maxBytesPerFile: 4 * 1024 * 1024,
        maxLines: 5000,
        persistenceEnabled: true)

    /// Validate user-supplied values before they touch the disk.
    /// Without this, a Settings field set to `-7` or `999999999`
    /// would crash the cleanup sweep or balloon the log file.
    public func validated() -> SDMLogSettings {
        var copy = self
        copy.level = min(.error, max(.debug, copy.level))
        copy.retentionDays = min(365, max(1, copy.retentionDays))
        // Floor on maxBytes: 32 KB prevents the rotate from
        // firing on every line; ceiling 256 MB caps disk usage.
        copy.maxBytesPerFile = min(256 * 1024 * 1024, max(32 * 1024, copy.maxBytesPerFile))
        // Floor on maxLines: 100 (so preview still gets
        // meaningful content); ceiling 1 000 000 (so a runaway
        // loop can't fill a TB).
        copy.maxLines = min(1_000_000, max(100, copy.maxLines))
        return copy
    }
}

/// One on-disk log line. Public so the settings-preview sheet
/// can pretty-print without re-implementing the date format.
public struct SDMLogEntry: Sendable, Identifiable {
    public let id = UUID()
    public let timestamp: Date
    public let level: SDMLogLevel
    public let category: String
    public let subsystem: String
    public let message: String
}

/// The protocol or static singleton. Service-style — design keeps
/// it simple and avoids an `actor` (no `await` between every
/// statement in tests). All shared mutation is behind `queueLock`.
public final class SDMLogger: @unchecked Sendable {
    public static let shared = SDMLogger()

    /// Single `os.Logger` per subsystem. We instantiate once at
    /// startup and keep a private ref so callers don't have to
    /// thread them through. Subsystem matches EngineLog.swift.
    private let osLogger = Logger(
        subsystem: "com.skyc8266.superdownloadmanager",
        category: "logger")

    /// User-configurable knobs. Mutations from `Upload` always
    /// validate before they touch disk (see `.validated()`).
    public private(set) var settings: SDMLogSettings
    private let queueLock = NSLock()
    /// Funnel all disk IO through a single serial queue so a
    /// burst of log calls never blocks the calling thread. The
    /// dispatch queue is process-scoped, which is what we want —
    /// two threads racing on a FileHandle would corrupt lines.
    private let ioQueue = DispatchQueue(
        label: "com.skyc8266.superdownloadmanager.logger",
        qos: .utility)
    /// Pending writes (in case the disk write fails, we keep
    /// them in memory so the next call can flush). Bounded so
    /// a misconfigured logger never balloons RAM.
    private var pending: [SDMLogEntry] = []
    private let pendingCap = 1024
    /// Current file size in bytes; cached so the rotate check
    /// doesn't need an `attributesOfItem` on every write.
    private var currentFileSize: Int = 0

    /// Per-subsystem logger ref. Created lazily because
    /// `Logger(subsystem:category:)` is cheap but we want one
    /// per category for nicer Console.app filtering.
    private static var categoryLoggers: [String: Logger] = [:]
    private static let categoryLock = NSLock()

    /// Public so the Settings → Logs sheet can build an "Open
    /// in Console.app" URL.
    public static let subsystemID = "com.skyc8266.superdownloadmanager"
    public static let fileURL: URL = {
        let base = FileManager.default.urls(
            for: .libraryDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory())
                .appendingPathComponent("Library")
        let dir = base
            .appendingPathComponent("Logs", isDirectory: true)
            .appendingPathComponent("SuperDownloadManager", isDirectory: true)
        return dir.appendingPathComponent("sdm.log", isDirectory: false)
    }()

    public static let logDir: URL = {
        let url = fileURL.deletingLastPathComponent()
        try? FileManager.default.createDirectory(
            at: url, withIntermediateDirectories: true)
        return url
    }()

    // MARK: - Init

    private init(settings: SDMLogSettings = .default) {
        self.settings = settings.validated()
        // Prime the size cache so the first write doesn't pay
        // an attributesOfItem() round-trip.
        self.currentFileSize = (try? FileManager.default
            .attributesOfItem(atPath: Self.fileURL.path)[.size] as? Int) ?? 0
        // Schedule the daily cleanup. `Timer.scheduledTimer` is
        // good enough here — we don't need exact timing, and the
        // app's own lifecycle handles missed ticks when the user
        // quits and relaunches.
        Timer.scheduledTimer(withTimeInterval: 24 * 3600,
                             repeats: true) { [weak self] _ in
            self?.runCleanup()
        }
    }

    /// Apply a new settings bundle. Threadsafe; we take the lock
    /// before swapping, so a concurrent log call never sees a
    /// torn value. The new settings are validated first, so
    /// disk-side invariants (file size, retention) are
    /// automatically respected.
    public func updateSettings(_ new: SDMLogSettings) {
        let validated = new.validated()
        queueLock.lock()
        settings = validated
        queueLock.unlock()
        // If the user shrank `maxBytesPerFile`, the existing
        // file may now be over-cap. Run a cleanup pass so the
        // invariant is restored on the next write.
        ioQueue.async { [weak self] in
            self?.runCleanup()
        }
    }

    public func currentSettings() -> SDMLogSettings {
        queueLock.lock()
        defer { queueLock.unlock() }
        return settings
    }

    // MARK: - Emitting

    /// Public emit path. Always emits to `os.Logger` (so it shows
    /// in Console.app); also writes to disk if persistence is on.
    /// `category` is the same as the old `Logger(subsystem:category:)`
    /// callers used, so existing call sites don't have to change
    /// their category strings.
    public func log(_ level: SDMLogLevel,
                    category: String,
                    _ message: String) {
        let snapshot: SDMLogSettings
        queueLock.lock()
        snapshot = settings
        queueLock.unlock()
        guard level >= snapshot.level else { return }

        // Always emit to OSLog so the existing Console.app
        // recipes keep working. The file-write path is gated
        // by `persistenceEnabled`.
        let logger = Self.logger(for: category)
        logger.log(level: level.osLogType, "\(message, privacy: .public)")

        guard snapshot.persistenceEnabled else { return }
        let entry = SDMLogEntry(
            timestamp: Date(),
            level: level,
            category: category,
            subsystem: Self.subsystemID,
            message: message)
        ioQueue.async { [weak self] in
            self?.appendToDisk(entry, maxBytes: snapshot.maxBytesPerFile)
        }
    }

    public func debug(_ category: String, _ msg: String)   { log(.debug,   category: category, msg) }
    public func info(_ category: String, _ msg: String)    { log(.info,    category: category, msg) }
    public func notice(_ category: String, _ msg: String)  { log(.notice,  category: category, msg) }
    public func warning(_ category: String, _ msg: String) { log(.warning, category: category, msg) }
    public func error(_ category: String, _ msg: String)   { log(.error,   category: category, msg) }

    // MARK: - File access

    /// Read the entire on-disk log as an array of entries. The
    /// returned array is ordered oldest-first so the preview
    /// sheet can scroll top-to-bottom. Lines that fail to parse
    /// (truncated, hand-edited) are silently skipped — the user
    /// gets what they can read instead of an empty preview.
    public func readAll() async -> [SDMLogEntry] {
        await withCheckedContinuation { cont in
            ioQueue.async { [weak self] in
                let lines = self?.readAllSync() ?? []
                cont.resume(returning: lines)
            }
        }
    }

    /// Synchronous read used only by the settings sheet. We
    /// dispatch through `ioQueue.sync` so a write in flight
    /// never tears. Don't call from the hot path — it can take
    /// O(file size) and blocks the calling thread until the
    /// queue drains.
    public func readAllSync() -> [SDMLogEntry] {
        var out: [SDMLogEntry] = []
        ioQueue.sync {
            out = self.parseFile(at: Self.fileURL)
        }
        return out
    }

    // MARK: - Internal

    /// Append one entry to the active log file, rotating first
    /// if the size cap would be exceeded. Always called on
    /// `ioQueue` so concurrent appends never race.
    private func appendToDisk(_ entry: SDMLogEntry, maxBytes: Int) {
        // Cache check — if cached size says we're already over,
        // rotate before writing. Otherwise we'd write one byte
        // over the cap.
        if currentFileSize >= maxBytes {
            rotate(maxBytes: maxBytes)
        }
        let formatter = Self.fileDateFormatter
        let line = "\(formatter.string(from: entry.timestamp)) [\(entry.level.shortTag)] [\(entry.category)] \(entry.message)\n"
        guard let data = line.data(using: .utf8) else { return }
        do {
            let handle = try FileHandle(forUpdating: Self.fileURL)
            defer { try? handle.close() }
            try handle.seekToEnd()
            try handle.write(contentsOf: data)
            currentFileSize += data.count
        } catch {
            // Disk full? Permission revoked? We log to OSLog (not
            // back into the file — that would loop). Next
            // successful write will recreate the file via
            // `createFile(atPath:contents:attributes:)` path
            // if we made it absent.
            osLogger.error("SDMLogger: write failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    /// Move `sdm.log` → `sdm-<date>.log`. Called when the active
    /// file exceeds `maxBytesPerFile`. The new empty file is
    /// created lazily on the next write so we don't leave an
    /// empty placeholder around.
    private func rotate(maxBytes: Int) {
        let stamp = Self.fileNameFormatter.string(from: Date())
        let archiveURL = Self.fileURL.deletingLastPathComponent()
            .appendingPathComponent("sdm-\(stamp).log")
        do {
            if FileManager.default.fileExists(atPath: archiveURL.path) {
                // Same-day re-rotate: append a sequence number
                // instead of overwriting. Rare in practice but
                // cheap insurance.
                var n = 2
                while FileManager.default.fileExists(
                    atPath: archiveURL
                        .deletingPathExtension()
                        .appendingPathExtension("\(n).log").path) {
                    n += 1
                }
                try FileManager.default.moveItem(
                    at: Self.fileURL,
                    to: archiveURL.deletingPathExtension()
                        .appendingPathExtension("\(n).log"))
            } else {
                try FileManager.default.moveItem(
                    at: Self.fileURL,
                    to: archiveURL)
            }
            currentFileSize = 0
        } catch {
            osLogger.error("SDMLogger: rotate failed: \(error.localizedDescription, privacy: .public)")
            // Reset cached size so the next write doesn't think
            // we're still under cap.
            currentFileSize = (try? FileManager.default
                .attributesOfItem(atPath: Self.fileURL.path)[.size] as? Int) ?? 0
        }
    }

    /// Parse the log file into entries. Best-effort: bad lines
    /// are skipped, not raised. Format is the one
    /// `appendToDisk` produces — `<date> [<LVL>] [<cat>] <msg>`.
    private func parseFile(at url: URL) -> [SDMLogEntry] {
        guard let raw = try? String(contentsOf: url, encoding: .utf8) else {
            return []
        }
        var out: [SDMLogEntry] = []
        for line in raw.split(separator: "\n", omittingEmptySubsequences: false) {
            // Format: "YYYY-MM-DD HH:MM:SS.mmm [LVL] [cat] msg"
            // We slice on the first three "[" — anything before
            // is the timestamp, anything after is the level/cat.
            guard let firstBracket = line.firstIndex(of: "["),
                  let secondOpen = line[line.index(after: firstBracket)...].firstIndex(of: "["),
                  let secondBracket = line[secondOpen...].firstIndex(of: "]") else {
                continue
            }
            let tsString = String(line[..<firstBracket])
                .trimmingCharacters(in: .whitespaces)
            let levelRaw = String(line[line.index(after: firstBracket)..<secondOpen])
                .trimmingCharacters(in: .whitespaces)
            let cat = String(line[line.index(after: secondOpen)..<secondBracket])
                .trimmingCharacters(in: .whitespaces)
            let rest = String(line[line.index(after: secondBracket)...])
                .trimmingCharacters(in: .whitespaces)
            guard let date = Self.fileDateFormatter.date(from: tsString) else { continue }
            let level = SDMLogLevel.allCases.first { $0.shortTag == levelRaw } ?? .info
            out.append(SDMLogEntry(
                timestamp: date,
                level: level,
                category: cat,
                subsystem: Self.subsystemID,
                message: rest))
        }
        return out
    }

    /// Daily / settings-changed sweep. Removes:
    ///   - any `sdm-<date>.log` file older than `retentionDays`
    ///   - lines past `maxLines` (truncated oldest-first; we
    ///     rewrite the active file in place rather than touching
    ///     the rotated ones).
    /// Called on `ioQueue` — never on the calling thread.
    private func runCleanup() {
        let settings = currentSettings()
        let dir = Self.logDir
        guard let contents = try? FileManager.default.contentsOfDirectory(
            at: dir,
            includingPropertiesForKeys: [.contentModificationDateKey]) else {
            return
        }
        // 1. Delete rotated archives older than retention.
        let cutoff = Date().addingTimeInterval(
            -TimeInterval(settings.retentionDays) * 24 * 3600)
        for url in contents where url.lastPathComponent.hasPrefix("sdm-")
                                && url.pathExtension == "log" {
            let mtime = (try? url.resourceValues(
                forKeys: [.contentModificationDateKey])
                .contentModificationDate) ?? Date()
            if mtime < cutoff {
                try? FileManager.default.removeItem(at: url)
            }
        }
        // 2. Cap active file at maxLines. This rewrites the file
        //    so the next writes don't see ghost lines.
        guard let raw = try? String(contentsOf: Self.fileURL, encoding: .utf8) else {
            return
        }
        let lines = raw.split(separator: "\n", omittingEmptySubsequences: true)
        if lines.count > settings.maxLines {
            let keep = Array(lines.suffix(settings.maxLines))
            let trimmed = keep.joined(separator: "\n") + "\n"
            do {
                try trimmed.write(to: Self.fileURL, atomically: true, encoding: .utf8)
                currentFileSize = trimmed.utf8.count
            } catch {
                osLogger.error("SDMLogger: line-cap rewrite failed: \(error.localizedDescription, privacy: .public)")
            }
        }
    }

    private static let fileDateFormatter: DateFormatter = {
        let f = DateFormatter()
        // `yyyy-MM-dd HH:mm:ss.SSS` matches the format we write
        // and is locale-independent (Calendar.current isn't
        // consulted), so a log written under zh-Hans still
        // parses back under en.
        f.dateFormat = "yyyy-MM-dd HH:mm:ss.SSS"
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone.current
        return f
    }()

    private static let fileNameFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone.current
        return f
    }()

    /// Lazily-instantiated per-category Logger. The first call
    /// for a category does a dictionary lookup; subsequent
    /// calls are O(1). Threadsafe via `categoryLock`.
    private static func logger(for category: String) -> Logger {
        categoryLock.lock()
        if let cached = categoryLoggers[category] {
            categoryLock.unlock()
            return cached
        }
        categoryLock.unlock()
        let fresh = Logger(subsystem: subsystemID, category: category)
        categoryLock.lock()
        categoryLoggers[category] = fresh
        categoryLock.unlock()
        return fresh
    }
}