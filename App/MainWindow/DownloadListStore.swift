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
    @AppStorage("sdm.capture.clipboard") public var clipboardCapture: Bool = true
    @AppStorage("sdm.notify.onComplete") public var notifyOnComplete: Bool = true
    @AppStorage("sdm.classify.enabled") public var classifyEnabled: Bool = true

    public let engine: SDMEngine
    private var pollTask: Task<Void, Never>?
    private var clipboardTask: Task<Void, Never>?
    private var lastClipboardChange: Int = -1
    private var lastSeenURLs: [String: Date] = [:]
    private var dispatchServer: DispatchServer?
    private var notifiedCompletionIDs: Set<UUID> = []
    private var notifiedFailureIDs: Set<UUID> = []

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
    }

    public func stop() {
        pollTask?.cancel()
        clipboardTask?.cancel()
        dispatchServer?.stop()
    }

    private func tick() async {
        let raw = await engine.snapshot()
        let mapped = raw
            .sorted { $0.id.uuidString < $1.id.uuidString }
            .map { r in
                TaskSummary(
                    id: r.id,
                    filename: r.destination.lastPathComponent,
                    urlString: r.url.absoluteString,
                    urlHost: r.url.host,
                    state: r.state,
                    bytesDone: r.bytesDone,
                    totalBytes: r.totalBytes,
                    speedBytesPerSec: r.speedBytesPerSec,
                    destinationPath: r.destination.path,
                    errorMessage: r.errorMessage,
                    category: classifyEnabled
                        ? DownloadCategory.from(url: r.url) : .other
                )
            }
        if mapped != tasks {
            // Fire notifications for newly-completed / failed tasks.
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

    public func pause(id: UUID) {
        Task { [engine] in await engine.pause(id: id) }
    }
    public func resume(id: UUID) {
        Task { [engine] in await engine.resume(id: id) }
    }
    public func cancel(id: UUID) {
        Task { [engine] in await engine.cancel(id: id) }
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

    public func deleteFile(id: UUID) {
        guard let t = tasks.first(where: { $0.id == id }) else { return }
        try? FileManager.default.trashItem(
            at: URL(fileURLWithPath: t.destinationPath),
            resultingItemURL: nil)
        remove(id: id)
    }

    /// P1-4: per-task speed limit. v1.0.4 wires a stub alert;
    /// the real per-task limiter ships in v1.0.5 once the
    /// engine refactor lands.
    public func setSpeedLimit(id: UUID) {
        let alert = NSAlert()
        alert.messageText = "Limit Speed"
        alert.informativeText = "Per-task speed limit lands in v1.0.5. " +
            "Global speed limit is in Settings → Bandwidth (cmd-,)."
        alert.alertStyle = .informational
        alert.addButton(withTitle: "OK")
        alert.runModal()
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

        // Dedupe within 5 minutes.
        if let last = lastSeenURLs[url.absoluteString],
           Date().timeIntervalSince(last) < 300 {
            return
        }
        lastSeenURLs[url.absoluteString] = Date()

        // Don't add if the user is editing the AddSheet. The
        // simplest way: if a download with this URL is already
        // queued/active, skip.
        if tasks.contains(where: {
            $0.urlString == url.absoluteString &&
            ($0.state == "queued" || $0.state == "downloading" || $0.state == "paused")
        }) {
            return
        }

        add(url: url)
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
