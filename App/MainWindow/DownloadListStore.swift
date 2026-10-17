import Foundation
import AppKit
import Combine
import CryptoKit
import SwiftUI
import UserNotifications
import SuperDownloadManagerCore

/// DownloadListStore — observable bridge between `SDMEngine` and
/// SwiftUI's `Table`.
///
/// v1.0.4: extended to populate `TaskSummary.urlString/urlHost/
/// category`, expose clipboard auto-capture (P0-3), per-task
/// speed-limit (P1-2), checksum (P1-4), file delete (P1-4),
/// auto-classified destination dir (P1-1), and the dispatch
/// service used by browser extensions (P0-4).
@MainActor
public final class DownloadListStore: ObservableObject {
    @Published public private(set) var tasks: [TaskSummary] = []
    /// v1.0.7: candidate surfaced from the clipboard watcher.
    /// `nil` means no pending candidate. The view binds a
    /// sheet to this; the user confirms (with optional
    /// category / save-dir / max-threads overrides) or
    /// dismisses. v1.0.6 auto-added every http(s) URL, which
    /// meant the app would start downloading random text the
    /// user just copied from a chat window.
    @Published public var pendingClipboard: ClipboardCandidate? = nil
    @AppStorage("sdm.capture.clipboard") public var clipboardCapture: Bool = true
    @AppStorage("sdm.notify.onComplete") public var notifyOnComplete: Bool = true
    @AppStorage("sdm.classify.enabled") public var classifyEnabled: Bool = true
    @AppStorage("sdm.threads.default") public var defaultThreads: Int = 32

    public let engine: SDMEngine
    private var pollTask: Task<Void, Never>?
    private var clipboardTask: Task<Void, Never>?
    private var lastClipboardChange: Int = -1
    private var lastSeenURLs: [String: Date] = [:]
    private var dispatchServer: DispatchServer?
    private var notifiedCompletionIDs: Set<UUID> = []
    private var notifiedFailureIDs: Set<UUID> = []
    // v1.0.5: EWMA-smoothed speed per task. The raw sample is
    // a tick-to-tick delta; the smoothed value is what the UI
    // shows. Keyed by task id.
    private var smoothedSpeed: [UUID: Double] = [:]

    public init(engine: SDMEngine) {
        self.engine = engine
    }

    public func start() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.tick()
                try? await Task.sleep(nanoseconds: 200_000_000)
            }
        }
        startClipboardMonitor()
        startDispatchServer()
        requestNotificationPermission()
        cleanupOrphanedPartFiles()
        applyPersistedBandwidth()
        observeBandwidth()
    }

    /// v1.0.6: push the persisted global bandwidth limit into
    /// the engine at startup. v1.0.x stored the value in
    /// AppStorage but never wired it to the live limiter, so
    /// the Settings → Bandwidth slider was cosmetic.
    private func applyPersistedBandwidth() {
        let kbps = UserDefaults.standard.integer(forKey: "sdm.bandwidth.bytesPerSec")
        let bytesPerSec = Double(kbps) * 1024.0
        Task { [engine] in await engine.setSpeedLimit(bytesPerSec) }
    }

    /// v1.0.6: react to the user editing Settings → Bandwidth
    /// at runtime. UserDefaults posts `didChangeNotification`
    /// synchronously inside the binding's setter, so we hop off
    /// the MainActor and push the new rate to the engine.
    private var bandwidthObserver: NSObjectProtocol?
    public func observeBandwidth() {
        if bandwidthObserver != nil { return }
        bandwidthObserver = NotificationCenter.default.addObserver(
            forName: UserDefaults.didChangeNotification,
            object: UserDefaults.standard,
            queue: .main
        ) { [weak self] _ in
            // didChangeNotification fires on the posting queue.
            // We need to hop to MainActor to touch applyPersistedBandwidth
            // (which is @MainActor on a class annotated with it).
            Task { @MainActor [weak self] in
                self?.applyPersistedBandwidth()
            }
        }
    }
    deinit {
        if let obs = bandwidthObserver {
            NotificationCenter.default.removeObserver(obs)
        }
    }

    public func stop() {
        pollTask?.cancel()
        clipboardTask?.cancel()
        dispatchServer?.stop()
    }

    private func tick() async {
        let raw = await engine.snapshot()
        var lastByID: [UUID: Int64] = [:]
        for t in tasks { lastByID[t.id] = t.bytesDone }
        let now = Date()
        let mapped = raw
            .sorted { $0.id.uuidString < $1.id.uuidString }
            .map { r in
                let prev = lastByID[r.id] ?? 0
                let deltaBytes = max(0, r.bytesDone - prev)
                let rawSpeed = r.speedBytesPerSec > 0
                    ? r.speedBytesPerSec
                    : Double(deltaBytes) / 0.2  // 200ms tick
                let prevE = smoothedSpeed[r.id] ?? 0
                let alpha = 0.4
                let smoothed = (prevE == 0) ? rawSpeed
                    : (alpha * rawSpeed + (1 - alpha) * prevE)
                smoothedSpeed[r.id] = smoothed
                _ = now
                return TaskSummary(
                    id: r.id,
                    filename: r.destination.lastPathComponent,
                    urlString: r.url.absoluteString,
                    urlHost: r.url.host,
                    state: r.state,
                    bytesDone: r.bytesDone,
                    totalBytes: r.totalBytes,
                    speedBytesPerSec: smoothed,
                    destinationPath: r.destination.path,
                    errorMessage: r.errorMessage,
                    category: classifyEnabled
                        ? DownloadCategory.from(url: r.url) : .other,
                    speedLimitBytesPerSec: r.speedLimitBytesPerSec
                )
            }
        // Drop EWMA for tasks that no longer exist.
        let liveIDs = Set(raw.map { $0.id })
        for id in smoothedSpeed.keys where !liveIDs.contains(id) {
            smoothedSpeed.removeValue(forKey: id)
        }
        if mapped != tasks {
            if notifyOnComplete {
                for t in mapped {
                    if t.state == "completed" && !notifiedCompletionIDs.contains(t.id) {
                        notifiedCompletionIDs.insert(t.id)
                        postCompleteNotification(t)
                    } else if t.state == "failed" && !notifiedFailureIDs.contains(t.id) {
                        notifiedFailureIDs.insert(t.id)
                        postFailureNotification(t)
                    }
                }
            }
            tasks = mapped
        }
    }

    // MARK: - Controls

    public func add(url: URL, filenameOverride: String? = nil) {
        let dir = downloadDirectory(for: url)
        Task { [engine] in
            _ = await engine.start([.init(
                url: url,
                destinationDirectory: dir,
                filenameOverride: filenameOverride)])
        }
    }

    /// v1.0.7: full-config add used by the clipboard
    /// confirmation sheet and (in the future) the per-row
    /// "Re-add with different options" menu. The category
    /// override maps to a destination directory; an
    /// override of `nil` falls back to the URL-derived
    /// classification. `maxThreads` of `nil` uses the
    /// engine's global cap.
    public func addWithConfig(
        url: URL,
        category: DownloadCategory? = nil,
        saveDirectory: URL? = nil,
        filenameOverride: String? = nil,
        maxThreads: Int? = nil
    ) {
        let dir: URL
        if let saveDirectory {
            dir = saveDirectory
        } else {
            let resolvedCategory = category ?? DownloadCategory.from(url: url)
            let downloads = FileManager.default
                .urls(for: .downloadsDirectory, in: .userDomainMask).first
                ?? URL(fileURLWithPath: NSHomeDirectory())
                    .appendingPathComponent("Downloads")
            dir = downloads.appendingPathComponent(resolvedCategory.folderName)
            try? FileManager.default.createDirectory(
                at: dir, withIntermediateDirectories: true)
        }
        Task { [engine] in
            _ = await engine.start([.init(
                url: url,
                destinationDirectory: dir,
                filenameOverride: filenameOverride,
                maxThreadsOverride: maxThreads)])
        }
    }

    public func pause(id: UUID) {
        Task { [engine] in await engine.pause(id: id) }
    }
    public func resume(id: UUID) {
        Task { [engine] in await engine.resume(id: id) }
    }
    public func cancel(id: UUID) {
        Task { [engine] in
            await engine.cancel(id: id)
            await MainActor.run {
                if let t = self.tasks.first(where: { $0.id == id }) {
                    // Immediately unlink the partial file so the
                    // next start() doesn't see stale bytes and so
                    // the user doesn't have to wait 7 days for the
                    // orphan-cleanup pass.
                    try? FileManager.default.removeItem(
                        atPath: t.destinationPath + ".part")
                }
            }
        }
    }
    public func remove(id: UUID) {
        Task { [engine] in await engine.remove(id: id) }
    }
    public func clearCompleted() {
        Task { [engine] in await engine.clearCompleted() }
    }

    public func openFile(id: UUID) {
        if let t = tasks.first(where: { $0.id == id }),
           FileManager.default.fileExists(atPath: t.destinationPath) {
            NSWorkspace.shared.open(URL(fileURLWithPath: t.destinationPath))
        }
    }

    public func revealInFinder(id: UUID) {
        if let t = tasks.first(where: { $0.id == id }) {
            NSWorkspace.shared.activateFileViewerSelecting(
                [URL(fileURLWithPath: t.destinationPath)])
        }
    }

    /// v1.0.5: walk all the configured download directories
    /// and unlink `.part` files older than 7 days that aren't
    /// referenced by any active task. Cleans up crashes, force-
    /// quits, and failed downloads that left orphans behind.
    private func cleanupOrphanedPartFiles() {
        let fm = FileManager.default
        let cutoff = Date().addingTimeInterval(-7 * 24 * 3600)
        let activePaths = Set(tasks.map { $0.destinationPath + ".part" })
        for dir in searchRoots() {
            guard let enumerator = fm.enumerator(
                at: dir, includingPropertiesForKeys: [.contentModificationDateKey],
                options: [.skipsHiddenFiles]) else { continue }
            for case let url as URL in enumerator
            where url.pathExtension == "part" {
                if activePaths.contains(url.path) { continue }
                let mtime = (try? url.resourceValues(
                    forKeys: [.contentModificationDateKey])
                    .contentModificationDate) ?? Date()
                if mtime < cutoff {
                    try? fm.removeItem(at: url)
                }
            }
        }
    }

    private func searchRoots() -> [URL] {
        let downloads = FileManager.default
            .urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory())
                .appendingPathComponent("Downloads")
        return [downloads,
                downloads.appendingPathComponent("Video"),
                downloads.appendingPathComponent("Music"),
                downloads.appendingPathComponent("Pictures"),
                downloads.appendingPathComponent("Archives"),
                downloads.appendingPathComponent("Documents"),
                downloads.appendingPathComponent("Software"),
                downloads.appendingPathComponent("Other")]
    }

    public func deleteFile(id: UUID) {
        guard let t = tasks.first(where: { $0.id == id }) else { return }
        try? FileManager.default.trashItem(
            at: URL(fileURLWithPath: t.destinationPath),
            resultingItemURL: nil)
        remove(id: id)
    }

    /// P1-2: per-task speed limit. v1.0.6: real sheet. The user
    /// types a KB/s value (0 = clear override, use global).
    public func setSpeedLimit(id: UUID) {
        // Snapshot the current value so the sheet pre-fills.
        let current = tasks.first { $0.id == id }?.speedLimitBytesPerSec ?? 0
        let alert = NSAlert()
        alert.messageText = "Limit Speed"
        alert.informativeText = "Per-task limit in KB/s. 0 = use global. " +
            "Global is in Settings → Bandwidth (⌘,)."
        alert.alertStyle = .informational
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 200, height: 22))
        field.placeholderString = "0 = unlimited"
        // Convert bytes/sec → KB/s for display, rounded.
        field.stringValue = current > 0
            ? String(Int((current / 1024.0).rounded()))
            : ""
        field.formatter = NumberFormatter()
        (field.formatter as? NumberFormatter)?.numberStyle = .decimal
        alert.accessoryView = field
        alert.addButton(withTitle: "Apply")
        alert.addButton(withTitle: "Cancel")
        if alert.runModal() == .alertFirstButtonReturn {
            let kbps = Int(field.stringValue) ?? 0
            let bytesPerSec = Double(kbps) * 1024.0
            Task { [engine] in await engine.setTaskSpeedLimit(id: id, bytesPerSec: bytesPerSec) }
        }
    }

    /// P1-4: compute SHA-256 of a finished file.
    public func computeChecksum(id: UUID) {
        guard let t = tasks.first(where: { $0.id == id }),
              t.state == "completed" else { return }
        let path = t.destinationPath
        Task.detached(priority: .utility) {
            guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)) else {
                return
            }
            let hash = SHA256.hash(data: data)
                .map { String(format: "%02x", $0) }
                .joined()
            await MainActor.run {
                let alert = NSAlert()
                alert.messageText = "SHA-256"
                alert.informativeText = hash
                alert.addButton(withTitle: "Copy")
                alert.addButton(withTitle: "Close")
                if alert.runModal() == .alertFirstButtonReturn {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(hash, forType: .string)
                }
            }
        }
    }

    // MARK: - P1-1: Auto-classification

    /// Destination directory for a URL, applying category rules.
    /// Default: `~/Downloads/<Category>/`. If the user has
    /// disabled classification, just `~/Downloads/`.
    public func downloadDirectory(for url: URL) -> URL {
        let downloads = FileManager.default
            .urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory())
                .appendingPathComponent("Downloads")
        if !classifyEnabled { return downloads }
        let cat = DownloadCategory.from(url: url)
        let target = downloads.appendingPathComponent(cat.folderName)
        try? FileManager.default.createDirectory(
            at: target, withIntermediateDirectories: true)
        return target
    }

    // MARK: - P0-3: Clipboard auto-capture

    private func startClipboardMonitor() {
        clipboardTask?.cancel()
        clipboardTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.checkClipboard()
                try? await Task.sleep(nanoseconds: 500_000_000)
            }
        }
    }

    private func checkClipboard() async {
        guard clipboardCapture else { return }
        let pb = NSPasteboard.general
        let change = pb.changeCount
        guard change != lastClipboardChange else { return }
        lastClipboardChange = change

        guard let s = pb.string(forType: .string) else { return }
        let trimmed = s.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed),
              let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https" else { return }

        // v1.0.7: only surface http(s) URLs that look like
        // downloadable files. Bare hostname URLs and HTML
        // pages (no extension) are ignored — auto-downloading
        // them on every clipboard change was annoying.
        guard DownloadCategory.isLikelyFileURL(url) else { return }

        // Dedupe within 5 minutes.
        if let last = lastSeenURLs[url.absoluteString],
           Date().timeIntervalSince(last) < 300 {
            return
        }
        lastSeenURLs[url.absoluteString] = Date()

        // Don't surface a candidate that's already an active
        // download. This avoids the sheet popping up for a
        // URL we already started.
        if tasks.contains(where: {
            $0.urlString == url.absoluteString &&
            ($0.state == "queued" || $0.state == "downloading" || $0.state == "paused")
        }) {
            return
        }

        // Hand the URL to the view via the published
        // pendingClipboard. The user confirms, edits, or
        // dismisses.
        let candidate = ClipboardCandidate(
            url: url,
            suggestedCategory: DownloadCategory.from(url: url),
            defaultMaxThreads: defaultThreads)
        pendingClipboard = candidate
    }

    // MARK: - P0-4: Dispatch server (browser extensions)

    private func startDispatchServer() {
        dispatchServer = DispatchServer(
            port: 7955,
            onAdd: { [weak self] url, filename, _ in
                Task { @MainActor in
                    self?.add(url: url, filenameOverride: filename)
                }
            })
        do {
            try dispatchServer?.start()
        } catch {
            NSLog("SDM: DispatchServer failed to start: \(error). " +
                  "Browser extension integration disabled.")
        }
    }

    // MARK: - P0-5: Notifications

    private func requestNotificationPermission() {
        UNUserNotificationCenter.current().requestAuthorization(
            options: [.alert, .sound]
        ) { _, _ in
            // We don't care about the outcome; if denied, we
            // just don't show notifications. The store's
            // notifyOnComplete flag is the user's kill switch.
        }
    }

    private func postCompleteNotification(_ task: TaskSummary) {
        let content = UNMutableNotificationContent()
        content.title = "Download complete"
        content.subtitle = task.filename
        content.body = ByteCountFormatter.string(
            fromByteCount: task.totalBytes ?? 0, countStyle: .file)
        content.sound = .default
        content.userInfo = ["taskID": task.id.uuidString]
        let req = UNNotificationRequest(
            identifier: "complete-\(task.id.uuidString)",
            content: content,
            trigger: nil)
        UNUserNotificationCenter.current().add(req) { _ in }
    }

    private func postFailureNotification(_ task: TaskSummary) {
        let content = UNMutableNotificationContent()
        content.title = "Download failed"
        content.subtitle = task.filename
        content.body = task.errorMessage ?? "Unknown error"
        content.sound = .default
        let req = UNNotificationRequest(
            identifier: "failure-\(task.id.uuidString)",
            content: content,
            trigger: nil)
        UNUserNotificationCenter.current().add(req) { _ in }
    }
}
