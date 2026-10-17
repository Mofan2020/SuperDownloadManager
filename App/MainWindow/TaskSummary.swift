import Foundation
import SuperDownloadManagerCore

/// Compact denormalised summary the view consumes. The store hands
/// these out already-formatted for display so SwiftUI doesn't have
/// to recompute strings on every tick.
///
/// v1.0.3: `state` is the engine's raw state string. We treat
/// "completed" / "failed" / "cancelled" / "paused" as terminal;
/// "queued" / "downloading" as live. The view layer renders
/// appropriate per-state controls.
public struct TaskSummary: Identifiable, Equatable {
    public let id: UUID
    public var filename: String
    public var state: String
    public var bytesDone: Int64
    public var totalBytes: Int64?
    public var speedBytesPerSec: Double
    public var destinationPath: String
    public var errorMessage: String?

    public var fractionDone: Double {
        guard let total = totalBytes, total > 0 else { return 0 }
        return Double(bytesDone) / Double(total)
    }

    public var speedDescription: String {
        ByteRateFormatter.string(from: speedBytesPerSec)
    }

    public var etaDescription: String? {
        guard let total = totalBytes,
              total > bytesDone,
              speedBytesPerSec > 0 else { return nil }
        let remaining = Double(total - bytesDone)
        return TimeFormatter.string(from: remaining / speedBytesPerSec)
    }

    public var isTerminal: Bool {
        switch state {
        case "completed", "failed", "cancelled": return true
        default: return false
        }
    }
}

public enum ByteRateFormatter {
    public static func string(from bytesPerSec: Double) -> String {
        if bytesPerSec >= 1_048_576 { return String(format: "%.2f MB/s", bytesPerSec / 1_048_576) }
        if bytesPerSec >= 1_024     { return String(format: "%.1f KB/s", bytesPerSec / 1_024) }
        return String(format: "%.0f B/s", bytesPerSec)
    }
}

public enum TimeFormatter {
    public static func string(from seconds: Double) -> String {
        if seconds < 60 { return String(format: "%.0fs", seconds) }
        let totalMin = Int(seconds / 60)
        let secs = Int(seconds.truncatingRemainder(dividingBy: 60))
        if totalMin < 60 { return "\(totalMin)m \(secs)s" }
        let h = totalMin / 60
        let m = totalMin % 60
        return "\(h)h \(m)m"
    }
}
