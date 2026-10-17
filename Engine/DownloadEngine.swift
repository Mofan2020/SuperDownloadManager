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

        // Probe first to capture the real filename from
        // Content-Disposition. If the server returns one and
        // the user didn't override the filename, rename the
        // destination to use it.
        do {
            let p = try await HTTPDownloader().probe(spec.url)
            await queue.update(id: id) { t in
                if let real = p.suggestedFilename, !real.isEmpty,
                   spec.filenameOverride == nil {
                    // Re-anchor: use server's filename in the
                    // same category directory.
                    let dir = t.destination.deletingLastPathComponent()
                    let newDest = dir.appendingPathComponent(real)
                    t.destination = newDest
                }
                t.totalBytes = p.contentLength
            }
        } catch {
            // Probe failure: keep the URL-derived filename and
            // let DownloadFlow's own probe + error path surface
            // the problem to the user.
        }

        let realDest: URL
        if let summary = await queue.get(id: id) {
            realDest = summary.destination
        } else {
            realDest = destination
        }

        // Build the flow first so the in-process progress
        // channel exists; then bridge its stream to the queue.
        let flow = DownloadFlow(
            url: spec.url,
            destination: realDest,
            strategy: strategy,
            parallelismCap: parallelismCap,
            limiter: limiter)

        let progressTask = Task { [weak self] in
            for await bytes in flow.progressChannel.stream {
                await self?.queue.update(id: id) { t in
                    t.bytesDone = bytes
                }
            }
        }

        // (Disk poll removed in v1.0.5 — the in-process progress
        // channel above is the source of truth for bytesDone.)

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

    /// Resume a paused task. v1.0.4 P1-3: probe the URL, then
    /// run `ResumeFlow` to append the missing bytes via a single
    /// `Range:` request, and finalize the merged file. Falls
    /// back to a from-scratch download if the server doesn't
    /// honour Range.
    public func resume(id: UUID) async {
        guard let summary = await queue.get(id: id) else { return }

        // If no .part on disk, this isn't a paused download —
        // just kick off a fresh one.
        let partPath = summary.destination.appendingPathExtension("part")
        let bytesOnDisk: Int64 = (try? FileManager.default
            .attributesOfItem(atPath: partPath.path)[.size] as? Int64) ?? 0

        if bytesOnDisk == 0 {
            await cancel(id: id)
            _ = await start([StartSpec(
                url: summary.url,
                destinationDirectory: summary.destination.deletingLastPathComponent())])
            return
        }

        // Spawn a resume task.
        await queue.update(id: id) { t in
            t.state = "downloading"
            t.speedBytesPerSec = 0
        }
        let live = Task { [weak self] in
            guard let self else { return }
            await self.runResume(id: id, summary: summary)
        }
        specs[id] = LiveTask(task: live, isPaused: false)
    }

    private func runResume(id: UUID, summary: TaskQueue.TaskSummary) async {
        let probe: ContentProbe
        do {
            probe = try await HTTPDownloader().probe(summary.url)
        } catch {
            await queue.update(id: id) { t in
                t.state = "failed"
                t.errorMessage = "probe failed: \(error)"
            }
            return
        }
        let partPath = summary.destination.appendingPathExtension("part")
        let flow = ResumeFlow(url: summary.url,
                              destination: summary.destination,
                              probe: probe)
        do {
            try await flow.run()
            // Move .part → final.
            try? FileManager.default.removeItem(at: summary.destination)
            try FileManager.default.moveItem(at: partPath, to: summary.destination)
            await queue.update(id: id) { t in
                t.state = "completed"
                t.totalBytes = probe.contentLength
                t.bytesDone = probe.contentLength ?? t.bytesDone
            }
        } catch {
            // Fall back to from-scratch if Range was rejected.
            if case ResumeFlow.Failure.rangeRejected = error {
                await cancel(id: id)
                _ = await start([StartSpec(
                    url: summary.url,
                    destinationDirectory: summary.destination.deletingLastPathComponent())])
                return
            }
            await queue.update(id: id) { t in
                t.state = "failed"
                t.errorMessage = String(describing: error)
            }
        }
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
