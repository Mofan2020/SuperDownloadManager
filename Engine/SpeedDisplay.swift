import Foundation

/// Pure speed-display logic for the list view. Extracted
/// from `DownloadListStore.tick()` so the v1.0.8 fix can be
/// unit-tested without spinning up a full engine + store.
///
/// The bug it locks: v1.0.7 used an EMA `α=0.4` to smooth
/// per-flush speeds. When a download hit 100 % the engine
/// zeroed its `speedBytesPerSec`, but the EMA cache kept
/// decaying toward 0 at `0.6·prev` per tick — a row that
/// just completed showed e.g. 500 KB/s for ~1 s before
/// visibly dropping. The user sees "进度条满了100，速度还在"
/// because the EMA was leaking the pre-completion rate
/// into the terminal-state read.
///
/// v1.0.8: when the row is in a terminal state
/// (`completed` / `failed` / `cancelled`) or paused
/// (`paused` / `awaiting_new_link`), the displayed speed is
/// always 0 and the EMA cache is reset, regardless of the
/// engine's reported `speedBytesPerSec`.
public enum SpeedDisplay {
    /// A read-only snapshot of the input the UI needs to
    /// decide what to show. Mirrors the relevant fields of
    /// `TaskQueue.TaskSummary` without creating a circular
    /// dependency on the App target.
    public struct Input: Sendable {
        public let state: String
        public let engineSpeedBytesPerSec: Double
        /// Bytes-done delta from the previous tick. The store
        /// computes this before calling us; we don't have
        /// access to the previous `bytesDone` here.
        public let deltaBytesSinceLastTick: Int64
        /// 0.2 s for a 200 ms tick. The store passes this in
        /// so the math stays here.
        public let tickIntervalSeconds: Double
        public init(state: String,
                    engineSpeedBytesPerSec: Double,
                    deltaBytesSinceLastTick: Int64,
                    tickIntervalSeconds: Double) {
            self.state = state
            self.engineSpeedBytesPerSec = engineSpeedBytesPerSec
            self.deltaBytesSinceLastTick = deltaBytesSinceLastTick
            self.tickIntervalSeconds = tickIntervalSeconds
        }
    }

    public struct Output: Sendable, Equatable {
        public let displayedSpeed: Double
        public let nextEMA: Double
    }

    /// Compute the displayed speed and the next EMA value.
    ///
    /// - parameter input: see `Input`
    /// - parameter previousEMA: the EMA from the previous
    ///   tick (0 if no prior sample)
    /// - parameter alpha: smoothing factor; 0.4 matches the
    ///   v1.0.5-onward UI behaviour
    public static func compute(input: Input,
                               previousEMA: Double,
                               alpha: Double = 0.4) -> Output {
        let isTerminal = input.state == "completed" ||
                         input.state == "failed" ||
                         input.state == "cancelled"
        let isPaused = input.state == "paused" ||
                       input.state == "awaiting_new_link"
        if isTerminal || isPaused {
            return Output(displayedSpeed: 0, nextEMA: 0)
        }
        let rawSpeed: Double
        if input.engineSpeedBytesPerSec > 0 {
            rawSpeed = input.engineSpeedBytesPerSec
        } else if input.tickIntervalSeconds > 0 {
            rawSpeed = Double(input.deltaBytesSinceLastTick) /
                input.tickIntervalSeconds
        } else {
            rawSpeed = 0
        }
        let nextEMA: Double
        if previousEMA == 0 {
            nextEMA = rawSpeed
        } else {
            nextEMA = alpha * rawSpeed + (1 - alpha) * previousEMA
        }
        return Output(displayedSpeed: nextEMA, nextEMA: nextEMA)
    }
}
