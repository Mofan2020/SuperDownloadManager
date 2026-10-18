import SwiftUI
import AppKit
import CryptoKit
import SuperDownloadManagerCore

/// TaskDetailView — v1.0.6, rewritten in v1.0.7 to be live.
/// Sheet shown on row double-click (or "Show details"
/// context-menu item). v1.0.7 takes `taskID + store` and
/// re-resolves the live `TaskSummary` from the store on every
/// render, so bytesDone / speed / ETA / progress bar update
/// in real time while the sheet is open. v1.0.6 took a frozen
/// `TaskSummary` snapshot at sheet open, so the displayed
/// values never changed.
///
/// v1.1.2: the chunk breakdown in the "Show chunks" section
/// (here `ChunkStatus` rows) also lives as a *computed*
/// property keyed off the live `task`. v1.0.7 kept `chunks` as
/// `@State` populated only on the showChunks toggle, which is
/// why the chunk bars never moved after the section was first
/// opened. Computing 32 chunks per render is well under a
/// millisecond; the trade-off is worth the live accuracy.
struct TaskDetailView: View {
    let taskID: UUID
    @ObservedObject var store: DownloadListStore
    @Environment(\.strings) private var S
    @State private var computingChecksum = false
    @State private var checksumResult: String? = nil
    @State private var showChunks = false

    private var task: TaskSummary? {
        store.tasks.first(where: { $0.id == taskID })
    }

    /// Live chunk breakdown. Synthesised from the live task's
    /// `totalBytes` and `bytesDone` (the engine exposes the real
    /// chunk state through `RangePlanner.snapshot`, but no
    /// instance is reachable from the GUI today; this
    /// approximation is what the user can already infer from
    /// the row's progress bar and refreshes on every render).
    /// Capped at 32 chunks so v1.0.7's small-file UI
    /// (≤ 2 MB chunks) stays readable.
    private var chunks: [ChunkStatus] {
        guard let t = task else { return [] }
        let total = t.totalBytes ?? 0
        guard total > 0 else { return [] }
        let n = max(1, min(32, Int(total / (2 * 1024 * 1024)) + 1))
        let chunkSize = total / Int64(n)
        var out: [ChunkStatus] = []
        out.reserveCapacity(n)
        for i in 0..<n {
            let start = Int64(i) * chunkSize
            let end = (i == n - 1) ? total : (Int64(i + 1) * chunkSize)
            let size = end - start
            let done = max(0, min(size, t.bytesDone - start))
            let fraction = size > 0 ? Double(done) / Double(size) : 1
            let state: ChunkStatus.State = (done >= size) ? .done
                : (fraction > 0) ? .active
                : .pending
            out.append(ChunkStatus(
                id: i, start: start, end: end, bytesDone: done,
                fraction: fraction, state: state))
        }
        return out
    }

    var body: some View {
        Group {
            if let t = task {
                VStack(alignment: .leading, spacing: 14) {
                    header(t)
                    Divider()
                    metadata(t)
                    Divider()
                    HStack {
                        Button(showChunks
                               ? S.detailHideChunks
                               : S.detailShowChunks(count: chunks.count)) {
                            showChunks.toggle()
                        }
                        if computingChecksum {
                            ProgressView().controlSize(.small)
                        }
                        if let h = checksumResult {
                            Text(S.listSha256(String(h.prefix(16))))
                                .font(.caption.monospaced())
                                .textSelection(.enabled)
                        }
                        Spacer()
                        Button(S.detailCopyJSON) { copyJSON(t) }
                    }
                    if showChunks { chunksList }
                }
            } else {
                VStack(spacing: 12) {
                    Text(S.detailTaskGone)
                        .foregroundStyle(.secondary)
                    Button(S.detailClose) {
                        NSApp.keyWindow?.sheetParent?
                            .endSheet(NSApp.keyWindow!)
                    }
                }
                .padding(40)
            }
        }
        .padding(20)
        .frame(minWidth: 540, idealWidth: 620, minHeight: 360)
    }

    // MARK: - Header

    private func header(_ task: TaskSummary) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: iconName(for: task.state))
                .font(.system(size: 28))
                .foregroundStyle(stateColor(task.state))
            VStack(alignment: .leading, spacing: 4) {
                Text(task.filename).font(.title3.weight(.semibold))
                    .lineLimit(1).truncationMode(.middle)
                Text(task.urlString)
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer()
            stateBadge(task)
        }
    }

    private func stateBadge(_ task: TaskSummary) -> some View {
        Text(task.state.capitalized)
            .font(.caption.weight(.semibold))
            .padding(.horizontal, 10)
            .padding(.vertical, 4)
            .background(stateColor(task.state).opacity(0.18), in: Capsule())
            .foregroundStyle(stateColor(task.state))
    }

    // MARK: - Metadata table

    private func metadata(_ task: TaskSummary) -> some View {
        Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 6) {
            GridRow {
                Text(S.detailDestination).foregroundStyle(.secondary)
                Text(task.destinationPath).font(.callout.monospaced())
                    .textSelection(.enabled)
            }
            GridRow {
                Text(S.detailSize).foregroundStyle(.secondary)
                Text(sizeString(for: task)).monospacedDigit()
            }
            GridRow {
                Text(S.detailSpeed).foregroundStyle(.secondary)
                Text(task.speedDescription).monospacedDigit()
            }
            if let eta = task.etaDescription {
                GridRow {
                    Text(S.detailETA).foregroundStyle(.secondary)
                    Text(eta).monospacedDigit()
                }
            }
            if task.speedLimitBytesPerSec > 0 {
                GridRow {
                    Text(S.detailPerTaskLimit).foregroundStyle(.secondary)
                    Text("\(Int((task.speedLimitBytesPerSec / 1024.0).rounded())) KB/s")
                        .monospacedDigit()
                }
            }
            if let err = task.errorMessage, !err.isEmpty {
                GridRow {
                    Text(S.detailError).foregroundStyle(.secondary)
                    Text(err).foregroundStyle(.red).lineLimit(3)
                }
            }
        }
        .font(.callout)
    }

    // MARK: - Chunk list (P4)

    private var chunksList: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(chunks.enumerated()), id: \.offset) { (_, c) in
                    HStack(spacing: 8) {
                        Image(systemName: chunkIcon(c.state))
                            .foregroundStyle(chunkColor(c.state))
                            .frame(width: 16)
                        Text(S.listChunkPrefix(c.id))
                            .font(.caption.monospaced())
                            .frame(width: 70, alignment: .leading)
                        Text(byteRange(c))
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                        ProgressView(value: c.fraction)
                            .frame(maxWidth: 200)
                        Text("\(Int(c.fraction * 100))%")
                            .font(.caption.monospacedDigit())
                            .frame(width: 40, alignment: .trailing)
                        Spacer()
                    }
                }
            }
            .padding(.vertical, 4)
        }
        .frame(maxHeight: 220)
    }

    // MARK: - Helpers

    private func sizeString(for task: TaskSummary) -> String {
        if let total = task.totalBytes {
            let done = ByteCountFormatter.string(fromByteCount: task.bytesDone,
                                                  countStyle: .file)
            let all = ByteCountFormatter.string(fromByteCount: total,
                                                 countStyle: .file)
            return "\(done) / \(all)"
        }
        return ByteCountFormatter.string(fromByteCount: task.bytesDone,
                                          countStyle: .file)
    }

    private func copyJSON(_ task: TaskSummary) {
        let json: [String: Any] = [
            "id": task.id.uuidString,
            "filename": task.filename,
            "url": task.urlString,
            "state": task.state,
            "bytesDone": task.bytesDone,
            "totalBytes": task.totalBytes as Any,
            "speedBytesPerSec": task.speedBytesPerSec,
            "speedLimitBytesPerSec": task.speedLimitBytesPerSec,
            "destination": task.destinationPath,
            "error": task.errorMessage as Any
        ]
        let data = try? JSONSerialization.data(
            withJSONObject: json, options: [.prettyPrinted, .sortedKeys])
        if let s = data.flatMap({ String(data: $0, encoding: .utf8) }) {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(s, forType: .string)
        }
    }

    private func byteRange(_ c: ChunkStatus) -> String {
        let s = ByteCountFormatter.string(fromByteCount: c.start, countStyle: .file)
        let e = ByteCountFormatter.string(fromByteCount: c.end, countStyle: .file)
        return "\(s) – \(e)"
    }

    private func chunkIcon(_ s: ChunkStatus.State) -> String {
        switch s {
        case .pending: return "circle"
        case .active:  return "arrow.down.circle"
        case .done:    return "checkmark.circle.fill"
        case .failed:  return "xmark.octagon.fill"
        }
    }
    private func chunkColor(_ s: ChunkStatus.State) -> Color {
        switch s {
        case .pending: return .secondary
        case .active:  return .accentColor
        case .done:    return .green
        case .failed:  return .red
        }
    }

    private func iconName(for state: String) -> String {
        switch state {
        case "downloading": return "arrow.down.circle.fill"
        case "completed":   return "checkmark.circle.fill"
        case "failed":      return "xmark.octagon.fill"
        case "paused":      return "pause.circle.fill"
        case "cancelled":   return "minus.circle"
        case "queued":      return "ellipsis.circle"
        default:            return "circle.dashed"
        }
    }
    private func stateColor(_ state: String) -> Color {
        switch state {
        case "downloading", "queued": return .accentColor
        case "completed": return .green
        case "paused":    return .orange
        case "failed":    return .red
        case "cancelled": return .secondary
        default:          return .secondary
        }
    }
}

/// Per-chunk view-model used by `TaskDetailView`. The engine
/// exposes real chunk state through `RangePlanner`; for the
/// v1.0.6 GUI we derive an approximation from total bytes +
/// bytesDone so the user can see how the segments are laid
/// out. v1.0.7 refreshes the breakdown from the live task on
/// every showChunks toggle.
struct ChunkStatus: Identifiable {
    enum State: String { case pending, active, done, failed }
    let id: Int
    let start: Int64
    let end: Int64
    let bytesDone: Int64
    let fraction: Double
    let state: State
}
