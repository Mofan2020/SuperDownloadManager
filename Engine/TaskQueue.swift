import Foundation

/// TaskQueue — P1 engine core. Coordinates which task gets the
/// engine's attention.
///
/// SDM v1.0 is multi-task but the user's intent is usually "download
/// my one big file as fast as possible". Therefore the queue
/// fairness policy is **dedicated-bandwidth**: while a single
/// active task is below the system bandwidth ceiling, it owns the
/// parallelism budget; a second task only gets a slot if the
/// first task has saturated the link OR has been paused.
///
/// `availableSlots()` returns the dynamic parallelism budget for
/// the active task. The engine asks for `min(taskWants,
/// availableSlots())` workers.
public actor TaskQueue {
    public struct TaskSummary: Sendable, Identifiable {
        public let id: UUID
        public var url: URL
        public var state: String          // "queued" | "downloading" | "paused" | "completed" | "failed" | "cancelled"
        public var bytesDone: Int64
        public var totalBytes: Int64?
        public var speedBytesPerSec: Double
        public var errorMessage: String?
        public var destination: URL
    }

    public private(set) var tasks: [UUID: TaskSummary] = [:]
    public let parallelismCap: Int  // hard ceiling on the whole engine

    public init(parallelismCap: Int) {
        self.parallelismCap = parallelismCap
    }

    public func enqueue(_ summary: TaskSummary) {
        tasks[summary.id] = summary
    }

    public func update(id: UUID, _ block: (inout TaskSummary) -> Void) {
        guard var t = tasks[id] else { return }
        block(&t)
        tasks[id] = t
    }

    public func remove(id: UUID) {
        tasks.removeValue(forKey: id)
    }

    public func snapshot() -> [TaskSummary] { Array(tasks.values) }

    public func get(id: UUID) -> TaskSummary? { tasks[id] }

    /// Slots the engine may grant the active task right now.
    /// v1.0: every active task gets the full cap when alone; we
    /// reserve at least 4 slots for any second concurrent task so
    /// the UI doesn't feel frozen when a user starts a second
    /// download while the first is mid-flight.
    public func availableSlots(for taskID: UUID) -> Int {
        let otherActive = tasks.values.filter {
            $0.id != taskID && $0.state == "downloading"
        }.count
        if otherActive == 0 { return parallelismCap }
        return max(4, parallelismCap / (otherActive + 1))
    }
}
