import SwiftUI

import Combine
import SuperDownloadManagerCore

/// DownloadListStore — observable bridge between `SDMEngine` and
/// SwiftUI's `List`. Polls the engine's snapshot at ~5 Hz and
/// republishes as `TaskSummary`.
///
/// v1.0.3: the store also exposes the engine control API
/// (`add`, `pause`, `resume`, `cancel`, `clearCompleted`) so the
/// view layer can wire buttons without re-typing actor-bound
/// async calls everywhere.
@MainActor
public final class DownloadListStore: ObservableObject {
    @Published public private(set) var tasks: [TaskSummary] = []
    public let engine: SDMEngine
    private var pollTask: Task<Void, Never>?
    private var pollGenerations: [UUID: Int] = [:]

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
    }

    public func stop() {
        pollTask?.cancel()
    }

    private func tick() async {
        let raw = await engine.snapshot()
        let mapped = raw
            .sorted { $0.id.uuidString < $1.id.uuidString }
            .map { r in
                TaskSummary(
                    id: r.id,
                    filename: r.destination.lastPathComponent,
                    state: r.state,
                    bytesDone: r.bytesDone,
                    totalBytes: r.totalBytes,
                    speedBytesPerSec: r.speedBytesPerSec,
                    destinationPath: r.destination.path,
                    errorMessage: r.errorMessage
                )
            }
        if mapped != tasks {
            tasks = mapped
        }
    }

    // MARK: - Controls (v1.0.3)

    public func add(url: URL, filenameOverride: String? = nil) {
        let dir = downloadDirectory()
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

    public func downloadDirectory() -> URL {
        // v1.0.3: hard-coded to ~/Downloads for now. v1.0.4
        // wires the per-task category rules (Pictures / Music /
        // Archives / ...) the user configures in Settings.
        let home = FileManager.default.urls(for: .downloadsDirectory,
                                            in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent("Downloads")
        try? FileManager.default.createDirectory(
            at: home, withIntermediateDirectories: true)
        return home
    }
}
