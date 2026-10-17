import SwiftUI

/// DownloadListView — P2 main window's primary pane.
///
/// Renders the engine's task list. Each row is a `TaskRow` with
/// filename, progress bar, current speed, ETA, and status badge.
/// The view subscribes to a `DownloadListStore` (publishing live
/// snapshots from `SDMEngine`) so a running download refreshes in
/// place without manual re-renders.
///
/// v1.0 deliberately keeps the view dumb — all state flows through
/// the store; the row view does not own timers, URLSessions, or any
/// side effects.
struct DownloadListView: View {
    @ObservedObject var store: DownloadListStore

    var body: some View {
        List(store.tasks, id: \.id) { task in
            TaskRow(task: task)
        }
        .listStyle(.inset)
    }
}

private struct TaskRow: View {
    let task: TaskSummary

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: iconName(for: task.state))
                .foregroundStyle(.secondary)
                .frame(width: 22)

            VStack(alignment: .leading, spacing: 4) {
                Text(task.filename)
                    .font(.body)
                    .lineLimit(1)
                ProgressView(value: task.fractionDone)
                    .frame(width: 220)
                HStack(spacing: 8) {
                    Text(task.speedDescription)
                    if let eta = task.etaDescription {
                        Text("·")
                        Text(eta)
                    }
                    Text("·")
                    Text(task.state)
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 4)
    }

    private func iconName(for state: String) -> String {
        switch state {
        case "downloading":   return "arrow.down.circle.fill"
        case "completed":     return "checkmark.circle.fill"
        case "awaiting_new_link": return "link.badge.plus"
        case "failed":        return "xmark.octagon.fill"
        case "paused":        return "pause.circle.fill"
        default:              return "circle.dashed"
        }
    }
}

/// Compact denormalised summary the view consumes. The store hands
/// these out already-formatted for display so SwiftUI doesn't have
/// to recompute strings on every tick.
public struct TaskSummary: Identifiable, Equatable {
    public let id: UUID
    public var filename: String
    public var state: String
    public var bytesDone: Int64
    public var totalBytes: Int64?
    public var speedBytesPerSec: Double

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
}

/// Localized formatters — kept separate so they're testable.
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
