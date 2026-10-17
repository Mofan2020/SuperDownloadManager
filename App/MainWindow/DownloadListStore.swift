import SwiftUI

import SuperDownloadManagerCore
import Combine
import SuperDownloadManagerCore

/// DownloadListStore — observable bridge between `SDMEngine` and
/// SwiftUI's `List`. Polls the engine's snapshot at ~5 Hz and
/// republishes as `TaskSummary`.
///
/// Why polling rather than letting the actor push changes: the
/// engine is intentionally isolated behind `actor` boundaries; an
/// `AsyncStream` from the actor wouldn't compose with
/// `ObservableObject`. The 200 ms tick is an honest trade between
/// freshness and CPU; the row view's progress bar smooths the
/// visible jumps anyway.
@MainActor
public final class DownloadListStore: ObservableObject {
    @Published public private(set) var tasks: [TaskSummary] = []
    private let engine: SDMEngine
    private var pollTask: Task<Void, Never>?

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
        // Map engine-internal TaskQueue.TaskSummary to the GUI's
        // view-shaped TaskSummary. We re-pull filename from the
        // underlying URL because v1.0's TaskQueue summary uses
        // the URL itself; filename resolution from
        // Content-Disposition is the sniffer's job in P3+.
        let mapped = raw.map { r in
            TaskSummary(
                id: r.id,
                filename: r.url.lastPathComponent,
                state: r.state,
                bytesDone: r.bytesDone,
                totalBytes: r.totalBytes,
                speedBytesPerSec: 0  // added in P4 — engine reports speed per task
            )
        }
        if mapped != tasks {
            tasks = mapped
        }
    }
}
