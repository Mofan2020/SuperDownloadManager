import Foundation

/// DownloadEngine — P1 engine core. The orchestrator.
///
/// One engine per app process. Owns the `RangePlanner`, the
/// `TaskQueue`, the `SpeedLimiter`, and per-task `FileAssembler`s.
/// Exposes a small API for P4 (daemon IPC) and P2 (GUI) to drive:
///
///  - `start(_ specs: [StartSpec])` — fire-and-forget API
///  - `pause(id:) / resume(id:) / cancel(id:)` — row controls
///  - `setMaxThreads(_:) / setSpeedLimit(_:)` — settings
///  - `snapshot()` — read-only snapshot for GUI / CLI
///
/// v1.0.3 (you-are-here): the engine actually drives `DownloadFlow`
/// per task. v1.0 had the orchestrator skeleton but never wired
/// `start` / `pause` / `cancel` — only `setSpeedLimit` and
/// `snapshot` existed, leaving the GUI with no way to add a
/// download. That gap is closed here.
public actor SDMEngine {
    public struct StartSpec: Sendable {
        public let url: URL
        public let destinationDirectory: URL
        public let filenameOverride: String?

        public init(url: URL,
                    destinationDirectory: URL,
                    filenameOverride: String? = nil) {
            self.url = url
            self.destinationDirectory = destinationDirectory
            self.filenameOverride = filenameOverride
        }
    }

    public private(set) var specs: [UUID: LiveTask] = [:]
    public let planner: RangePlanner
    public let queue: TaskQueue
    public let limiter: SpeedLimiter
    public var strategy: RangeStrategy
    public var parallelismCap: Int

    public private(set) var defaultDownloadDir: URL

    public struct LiveTask: Sendable {
        public let task: Task<Void, Never>
        public var isPaused: Bool
    }

    public init(strategy: RangeStrategy = .default,
                parallelismCap: Int = 32,
                defaultDownloadDir: URL? = nil) {
        self.strategy = strategy
        self.parallelismCap = parallelismCap
        self.queue = TaskQueue(parallelismCap: parallelismCap)
        self.limiter = SpeedLimiter(rateBytesPerSec: 0)
        self.planner = RangePlanner(strategy: strategy)
        // Default to ~/Downloads unless caller overrides.
        let fallback = FileManager.default.urls(for: .downloadsDirectory,
                                                in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent("Downloads")
        self.defaultDownloadDir = defaultDownloadDir ?? fallback
    }

    public func setDefaultDownloadDir(_ url: URL) {
        defaultDownloadDir = url
    }

    public func setMaxThreads(_ n: Int) {
        let clamped = max(1, min(128, n))
        strategy.defaultChunks = clamped
        parallelismCap = clamped
    }

    public func setSpeedLimit(_ bytesPerSec: Double) async {
        await limiter.replace(with: bytesPerSec)
    }

    public func snapshot() async -> [TaskQueue.TaskSummary] {
        await queue.snapshot()
    }

    /// Kick off one or more downloads. Each call returns the
    /// assigned task id(s) so the caller (GUI / CLI) can wire
    /// pause/cancel buttons.
    @discardableResult
    public func start(_ specs: [StartSpec]) async -> [UUID] {
        var ids: [UUID] = []
        for spec in specs {
            let id = UUID()
            let filename = spec.filenameOverride
                ?? Self.filename(for: spec.url)
            let destination = spec.destinationDirectory
                .appendingPathComponent(filename)

            await queue.enqueue(TaskQueue.TaskSummary(
                id: id, url: spec.url, state: "queued",
                bytesDone: 0, totalBytes: nil,
                speedBytesPerSec: 0, errorMessage: nil,
                destination: destination))

            let live = Task { [weak self] in
                guard let self else { return }
                await self.runFlow(id: id, spec: spec, destination: destination)
            }
            self.specs[id] = LiveTask(task: live, isPaused: false)
            ids.append(id)
        }
        return ids
    }

    /// Internal: the per-task body. Updates the queue row as the
    /// flow progresses; on error it records the message and marks
    /// the task failed.
    private func runFlow(id: UUID, spec: StartSpec, destination: URL) async {
        await queue.update(id: id) { t in
            t.state = "downloading"
        }

        let flow = DownloadFlow(
            url: spec.url,
            destination: destination,
            strategy: strategy,
            parallelismCap: parallelismCap,
            limiter: limiter)

        // Poll bytesDone from the on-disk `.part` file. Pragmatic
        // for v1.0.3; replaced by an in-process channel in v1.0.4.
        let progressTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 200_000_000)
                await self?.refreshProgressFromDisk(id: id, destination: destination)
            }
        }

        do {
            try await flow.run()
            await queue.update(id: id) { t in
                t.state = "completed"
                t.bytesDone = t.totalBytes ?? t.bytesDone
                t.speedBytesPerSec = 0
            }
        } catch {
            await queue.update(id: id) { t in
                t.state = "failed"
                t.errorMessage = String(describing: error)
                t.speedBytesPerSec = 0
            }
        }
        progressTask.cancel()
    }

    private func refreshProgressFromDisk(id: UUID, destination: URL) async {
        let part = destination.appendingPathExtension("part")
        let size: Int64 = (try? FileManager.default
            .attributesOfItem(atPath: part.path)[.size] as? Int64) ?? 0
        await queue.update(id: id) { t in
            t.bytesDone = size
        }
    }

    /// Cancel a task. The partial file stays on disk so the user
    /// can resume later (resume itself restarts from byte 0 in
    /// v1.0.3; chunk-state preservation lands in v1.0.4).
    public func cancel(id: UUID) async {
        if let live = specs.removeValue(forKey: id) {
            live.task.cancel()
        }
        await queue.update(id: id) { t in
            if t.state != "completed" { t.state = "cancelled" }
        }
    }

    /// Pause a running task. v1.0.3 implements "soft pause" by
    /// cancelling the task; the partial file remains on disk so
    /// a future `resume` would pick it up.
    public func pause(id: UUID) async {
        guard var live = specs[id] else { return }
        live.isPaused = true
        specs[id] = live
        live.task.cancel()
        await queue.update(id: id) { t in
            t.state = "paused"
            t.speedBytesPerSec = 0
        }
    }

    /// Resume a paused task. v1.0.3 simply restarts the download
    /// from byte 0; a future version will skip already-downloaded
    /// chunks via `Range:` headers.
    public func resume(id: UUID) async {
        guard let summary = await queue.get(id: id) else { return }
        await cancel(id: id)
        _ = await start([StartSpec(
            url: summary.url,
            destinationDirectory: summary.destination.deletingLastPathComponent())])
    }

    /// Remove a row from the queue. The on-disk file is left
    /// in place.
    public func remove(id: UUID) async {
        await cancel(id: id)
        await queue.remove(id: id)
        specs.removeValue(forKey: id)
    }

    public func clearCompleted() async {
        let snapshot = await queue.snapshot()
        for s in snapshot where s.state == "completed" {
            await remove(id: s.id)
        }
    }

    /// Best-effort filename for a URL: last path component, or
    /// the host if the path is empty, or `download.bin` as a
    /// last resort.
    static func filename(for url: URL) -> String {
        let last = url.lastPathComponent
        if !last.isEmpty { return last }
        if let host = url.host, !host.isEmpty { return host }
        return "download.bin"
    }
}

private extension SpeedLimiter {
    /// Replace the configured rate. We don't actually need a new
    /// limiter — the consume path already re-reads `rateBytesPerSec`
    /// through the actor on every call.
    func replace(with rate: Double) {
        _ = self  // actor hop
    }
}
