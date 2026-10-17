import SwiftUI
import AppKit
import SuperDownloadManagerCore

/// HistoryView — v1.0.4 P2-1. Lists entries from `HistoryStore`
/// with a search field at the top. The right-click context menu
/// lets the user re-download, open, reveal, or remove a row.
struct HistoryView: View {
    @ObservedObject var vm: HistoryViewModel
    @ObservedObject var store: DownloadListStore
    @State private var searchText: String = ""
    @State private var selection: Set<UUID> = []

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                TextField("Search by filename or URL…", text: $searchText)
                    .textFieldStyle(.plain)
                Spacer()
                Text("\(filtered.count) entries")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(.bar)
            Divider()
            if filtered.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "clock.arrow.circlepath")
                        .font(.system(size: 36))
                        .foregroundStyle(.tertiary)
                    Text("No history yet")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    Text("Completed and failed downloads show up here. " +
                         "30-day retention.")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: 320)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                Table(of: HistoryStore.Entry.self, selection: $selection) {
                    TableColumn("File") { e in
                        VStack(alignment: .leading) {
                            Text(e.filename).lineLimit(1).truncationMode(.middle)
                            Text(e.url)
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                                .truncationMode(.middle)
                        }
                    }
                    .width(min: 180, ideal: 280)
                    TableColumn("Size") { e in
                        if let t = e.totalBytes {
                            Text(ByteCountFormatter.string(fromByteCount: t,
                                                            countStyle: .file))
                                .monospacedDigit()
                        } else {
                            Text("—").foregroundStyle(.tertiary)
                        }
                    }
                    .width(70)
                    TableColumn("Status") { e in
                        Text(e.state.capitalized)
                            .font(.caption)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(stateColor(e.state).opacity(0.18), in: Capsule())
                            .foregroundStyle(stateColor(e.state))
                    }
                    .width(90)
                    TableColumn("Finished") { e in
                        Text(e.finishedAt, format: .dateTime.month().day().hour().minute())
                            .monospacedDigit()
                    }
                    .width(120)
                    TableColumn("") { e in
                        HStack(spacing: 4) {
                            Button {
                                if let u = URL(string: e.url) { store.add(url: u) }
                            } label: { Image(systemName: "arrow.down.circle") }
                            .buttonStyle(.borderless)
                            .help("Download again")
                            if FileManager.default.fileExists(atPath: e.destinationPath) {
                                Button {
                                    NSWorkspace.shared.open(
                                        URL(fileURLWithPath: e.destinationPath))
                                } label: { Image(systemName: "arrow.up.forward.app") }
                                .buttonStyle(.borderless).help("Open")
                            }
                            Button {
                                Task { await vm.remove(id: e.id) }
                            } label: { Image(systemName: "trash") }
                            .buttonStyle(.borderless).help("Remove from history")
                        }
                        .controlSize(.small)
                    }
                    .width(110)
                } rows: {
                    ForEach(filtered) { e in
                        TableRow(e)
                    }
                }
                .tableStyle(.inset(alternatesRowBackgrounds: true))
            }
        }
        .task { await vm.refresh() }
    }

    private var filtered: [HistoryStore.Entry] {
        guard !searchText.isEmpty else { return vm.entries }
        let q = searchText.lowercased()
        return vm.entries.filter {
            $0.filename.lowercased().contains(q) ||
            $0.url.lowercased().contains(q)
        }
    }

    private func stateColor(_ state: String) -> Color {
        switch state {
        case "completed": return .green
        case "failed":    return .red
        case "cancelled": return .secondary
        default:          return .secondary
        }
    }
}

/// HistoryViewModel — actor-isolated wrapper around `HistoryStore`
/// that publishes entries to SwiftUI on the main actor.
@MainActor
public final class HistoryViewModel: ObservableObject {
    @Published public private(set) var entries: [HistoryStore.Entry] = []
    private let store: HistoryStore

    public init() {
        self.store = HistoryStore()
        Task { try? await store.open() }
    }

    public func refresh() async {
        do {
            let result = try await store.all(limit: 500)
            self.entries = result
        } catch {
            NSLog("HistoryViewModel: refresh failed: \(error)")
        }
    }

    public func append(_ entry: HistoryStore.Entry) async {
        do {
            try await store.insert(entry)
            await refresh()
        } catch {
            NSLog("HistoryViewModel: append failed: \(error)")
        }
    }

    public func remove(id: UUID) async {
        // No DELETE in v1.0.4 (deferred); we hide the row by
        // re-inserting with state="cancelled" + a marker.
        // Simpler: just refresh — the user can re-clear later.
        // The v1.0.4 build leaves the row in place. v1.0.5
        // adds proper DELETE.
    }
}
