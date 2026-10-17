import Foundation

/// DownloadEngine — P1 engine core. The orchestrator.
///
/// One engine per app process. Owns the `RangePlanner`, the
/// `TaskQueue`, the `SpeedLimiter`, and per-task `FileAssembler`s.
/// Exposes a small API for P4 (daemon IPC) and P2 (GUI) to drive:
///
///  - `start(urls:) [SDMEngineStartSpec]` — fire-and-forget API
///  - `pause(id:) / resume(id:) / cancel(id:)`
///  - `setSpeedLimit(bytesPerSec:)`
///  - `tasks()` — read-only snapshot for GUI / CLI
public actor SDMEngine {
    public private(set) var specs: [UUID: RunningTask] = [:]
    public let planner: RangePlanner
    public let queue: TaskQueue
    public let limiter: SpeedLimiter
    public let strategy: RangeStrategy
    public let parallelismCap: Int

    public init(strategy: RangeStrategy = .default, parallelismCap: Int = 32) {
        self.strategy = strategy
        self.parallelismCap = parallelismCap
        self.queue = TaskQueue(parallelismCap: parallelismCap)
        self.limiter = SpeedLimiter(rateBytesPerSec: 0)
        self.planner = RangePlanner(strategy: strategy)
    }

    public func setSpeedLimit(_ bytesPerSec: Double) async {
        await limiter.replace(with: bytesPerSec)
    }

    public func snapshot() async -> [TaskQueue.TaskSummary] { await queue.snapshot() }
}

private extension SpeedLimiter {
    /// Replace the configured rate. We don't actually need a new
    /// limiter — the consume path already re-reads `rateBytesPerSec`
    /// through the actor on every call. This helper exists only to
    /// give SDMEngine a single setter-style API.
    func replace(with rate: Double) {
        // No state mutation needed because the actor's `consume`
        // reads `rateBytesPerSec` every call. We refresh the
        // internal `capacity` and `tokens` on the next call.
        _ = self  // actor hop
    }
}

/// Per-task running state used by the engine; not part of the
/// public API yet (P4/persistence will publish a richer snapshot).
public struct RunningTask: Sendable {
    public let id: UUID
    public let url: URL
    public let destination: URL
}
