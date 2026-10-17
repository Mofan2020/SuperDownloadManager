import SwiftUI
import AppKit
import UniformTypeIdentifiers
import SuperDownloadManagerCore

/// Tiny Identifiable wrapper around UUID so SwiftUI's
/// `sheet(item:)` can be driven by a `UUID?` state. v1.0.7
/// keeps only the task ID in `@State`; the live `TaskSummary`
/// is resolved by `TaskDetailView` from the `DownloadListStore`
/// on every render. v1.0.6 stored the full snapshot, which
/// froze the displayed bytesDone / speed / ETA.
private struct Identified: Identifiable {
    let id: UUID
}

/// DownloadListView — main window's primary pane (v1.0.4 rewrite).
///
/// v1.0.4: 7-column `Table` with sortable headers, multi-select,
/// drag-URL-into-window, and the per-row context menu that P1-4
/// asked for. Clipboard auto-capture lives in `DownloadListStore`;
/// per-row action helpers and helpers are kept local.
struct DownloadListView: View {
    @ObservedObject var store: DownloadListStore
    @State private var showAddSheet = false
    @State private var addURL: String = ""
    @State private var addFilename: String = ""
    @State private var addError: String? = nil
    @State private var selection: Set<UUID> = []
    @State private var sortOrder: [KeyPathComparator<TaskSummary>] = [
        .init(\.state, order: .forward)
    ]
    @State private var detailTaskID: UUID? = nil
    @Environment(\.strings) private var S

    var body: some View {
        VStack(spacing: 0) {
            toolbar
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(.bar)
            Divider()
            if store.tasks.isEmpty {
                EmptyStateView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                table
            }
        }
        .onDrop(of: [.url, .text, .fileURL],
                isTargeted: nil) { providers in
            handleDrop(providers: providers)
        }
        .sheet(isPresented: $showAddSheet) { addSheet }
        .environment(\.strings, S)
    }

    // MARK: - Table

    private var table: some View {
        Table(of: TaskSummary.self,
              selection: $selection,
              sortOrder: $sortOrder) {
            TableColumn("") { task in
                Image(systemName: iconName(for: task.state))
                    .foregroundStyle(stateColor(task.state))
                    .help(task.state)
            }
            .width(20)

            TableColumn("File", value: \.filename) { task in
                VStack(alignment: .leading, spacing: 2) {
                    Text(task.filename)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    if let host = task.urlHost {
                        Text(host)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .width(min: 160, ideal: 280)

            TableColumn("Size", value: \.totalBytesOrZero) { task in
                Text(byteCount(task.totalBytes))
                    .monospacedDigit()
            }
            .width(70)

            TableColumn("Progress") { task in
                if task.totalBytes ?? 0 > 0 {
                    HStack(spacing: 6) {
                        ProgressView(value: task.fractionDone)
                            .frame(maxWidth: 100)
                        Text("\(Int(task.fractionDone * 100))%")
                            .monospacedDigit()
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                } else {
                    Text("—").foregroundStyle(.secondary)
                }
            }
            .width(150)

            TableColumn("Speed", value: \.speedBytesPerSec) { task in
                Text(task.speedDescription)
                    .monospacedDigit()
                    .foregroundStyle(task.state == "downloading"
                                     ? .primary : .secondary)
            }
            .width(80)

            TableColumn("ETA") { task in
                if let eta = task.etaDescription {
                    Text(eta).monospacedDigit().foregroundStyle(.secondary)
                } else {
                    Text("—").foregroundStyle(.tertiary)
                }
            }
            .width(60)

            TableColumn("Status", value: \.state) { task in
                HStack(spacing: 4) {
                    if let cat = task.category {
                        Image(systemName: cat.iconName)
                            .font(.caption)
                            .foregroundStyle(cat.tint)
                    }
                    Text(stateLabel(task.state))
                        .font(.caption)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(stateColor(task.state).opacity(0.18),
                                    in: Capsule())
                        .foregroundStyle(stateColor(task.state))
                    if task.speedLimitBytesPerSec > 0 {
                        Image(systemName: "speedometer")
                            .font(.caption2)
                            .foregroundStyle(.orange)
                            .help("Limited to \(Int((task.speedLimitBytesPerSec / 1024.0).rounded())) KB/s")
                    }
                }
            }
            .width(100)

            TableColumn("") { task in
                rowActions(task)
            }
            .width(80)
        } rows: {
            ForEach(sortedTasks) { task in
                TableRow(task)
                    .contextMenu { rowContextMenu(task) }
            }
        }
        .tableStyle(.inset(alternatesRowBackgrounds: true))
        .contextMenu(forSelectionType: TaskSummary.ID.self) { ids in
            // Show details for the right-clicked row. macOS
            // delivers the selection via SwiftUI's table
            // context-menu API; we open the detail sheet for
            // the first selected id (or the right-clicked one).
            if let id = ids.first {
                Button("Show details") { detailTaskID = id }
            }
        } primaryAction: { ids in
            // Double-click on a row.
            if let id = ids.first {
                detailTaskID = id
            }
        }
        .sheet(item: Binding(
            get: { detailTaskID.map(Identified.init) },
            set: { detailTaskID = $0?.id }
        )) { identified in
            TaskDetailView(taskID: identified.id, store: store)
        }
    }

    private var sortedTasks: [TaskSummary] {
        store.tasks.sorted(using: sortOrder)
    }

    // MARK: - Per-row inline actions

    @ViewBuilder
    private func rowActions(_ task: TaskSummary) -> some View {
        HStack(spacing: 4) {
            switch task.state {
            case "downloading", "queued":
                Button { store.pause(id: task.id) } label: {
                    Image(systemName: "pause.fill")
                }.buttonStyle(.borderless).help(S.rowPause)
                Button { store.cancel(id: task.id) } label: {
                    Image(systemName: "xmark.circle.fill")
                }.buttonStyle(.borderless).help(S.rowCancel)
            case "paused":
                Button { store.resume(id: task.id) } label: {
                    Image(systemName: "play.fill")
                }.buttonStyle(.borderless).help(S.rowResume)
                Button { store.cancel(id: task.id) } label: {
                    Image(systemName: "xmark.circle.fill")
                }.buttonStyle(.borderless).help(S.rowCancel)
            case "failed", "cancelled":
                Button { store.resume(id: task.id) } label: {
                    Image(systemName: "arrow.clockwise")
                }.buttonStyle(.borderless).help(S.rowRetry)
            case "completed":
                Button { store.openFile(id: task.id) } label: {
                    Image(systemName: "arrow.up.forward.app")
                }.buttonStyle(.borderless).help(S.rowOpen)
                Button { store.revealInFinder(id: task.id) } label: {
                    Image(systemName: "folder")
                }.buttonStyle(.borderless).help(S.rowReveal)
            default:
                EmptyView()
            }
        }
        .controlSize(.small)
    }

    // MARK: - Right-click context menu (P1-4)

    @ViewBuilder
    private func rowContextMenu(_ task: TaskSummary) -> some View {
        Button("Show details") { detailTaskID = task.id }
        if task.state == "completed" {
            Button(S.rowOpen) { store.openFile(id: task.id) }
            Button(S.rowReveal) { store.revealInFinder(id: task.id) }
        }
        if task.state == "downloading" || task.state == "queued" {
            Button(S.rowPause) { store.pause(id: task.id) }
            Button(S.rowCancel) { store.cancel(id: task.id) }
        }
        if task.state == "paused" || task.state == "failed" || task.state == "cancelled" {
            Button(S.rowResume) { store.resume(id: task.id) }
        }
        Divider()
        Button(S.rowCopyURL) {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(task.urlString, forType: .string)
        }
        if let host = task.urlHost, let url = URL(string: "https://\(host)") {
            Button(S.rowOpenSource) { NSWorkspace.shared.open(url) }
        }
        Divider()
        Button(S.rowLimitSpeed) { store.setSpeedLimit(id: task.id) }
        if task.state == "completed" {
            Button(S.rowChecksum) { store.computeChecksum(id: task.id) }
        }
        Divider()
        Button(S.rowRemove) { store.remove(id: task.id) }
        Button(S.rowDelete) { store.deleteFile(id: task.id) }
    }

    // MARK: - Toolbar

    private var toolbar: some View {
        HStack(spacing: 8) {
            Button {
                addError = nil
                addFilename = ""
                showAddSheet = true
            } label: {
                Label(S.toolbarAddURL, systemImage: "plus")
            }
            .keyboardShortcut("n", modifiers: [.command])
            .help("Add a new download URL (⌘N)")

            Spacer()

            let activeCount = store.tasks.filter {
                $0.state == "downloading" || $0.state == "queued"
            }.count
            let pausedCount = store.tasks.filter { $0.state == "paused" }.count

            Button {
                store.tasks.filter {
                    $0.state == "downloading" || $0.state == "queued"
                }.forEach { store.pause(id: $0.id) }
            } label: {
                Label(S.toolbarPauseAll, systemImage: "pause.circle")
            }
            .disabled(activeCount == 0)

            Button {
                store.tasks.filter { $0.state == "paused" }
                    .forEach { store.resume(id: $0.id) }
            } label: {
                Label(S.toolbarResumeAll, systemImage: "play.circle")
            }
            .disabled(pausedCount == 0)

            Button {
                store.clearCompleted()
            } label: {
                Label(S.toolbarClearCompleted, systemImage: "trash")
            }
            .disabled(!store.tasks.contains { $0.state == "completed" })
        }
    }

    // MARK: - Add URL sheet

    private var addSheet: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(S.addSheetTitle)
                .font(.title3.weight(.semibold))
            Text(S.addSheetSubtitle)
                .font(.callout)
                .foregroundStyle(.secondary)

            VStack(alignment: .leading, spacing: 4) {
                Text(S.addSheetURLField)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                TextField("https://example.com/file.zip", text: $addURL)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(commitAdd)
            }

            VStack(alignment: .leading, spacing: 4) {
                Text(S.addSheetFilenameField)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                TextField(S.addSheetFilenameHint, text: $addFilename)
                    .textFieldStyle(.roundedBorder)
            }

            if let err = addError {
                Text(err)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            HStack {
                Spacer()
                Button(S.addSheetCancel) { showAddSheet = false }
                    .keyboardShortcut(.cancelAction)
                Button(S.addSheetAdd) { commitAdd() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(addURL.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding(20)
        .frame(width: 460)
    }

    private func commitAdd() {
        let trimmed = addURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed),
              let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https" else {
            addError = S.addSheetErrorBadURL
            return
        }
        let name = addFilename.trimmingCharacters(in: .whitespaces)
        store.add(url: url, filenameOverride: name.isEmpty ? nil : name)
        addURL = ""
        addFilename = ""
        addError = nil
        showAddSheet = false
    }

    // MARK: - Drag & drop (P0-2)

    private func handleDrop(providers: [NSItemProvider]) -> Bool {
        var accepted = false
        for provider in providers {
            if provider.canLoadObject(ofClass: URL.self) {
                _ = provider.loadObject(ofClass: URL.self) { url, _ in
                    if let url = url, let scheme = url.scheme?.lowercased(),
                       scheme == "http" || scheme == "https" {
                        Task { @MainActor in store.add(url: url) }
                        accepted = true
                    }
                }
            } else if provider.canLoadObject(ofClass: String.self) {
                _ = provider.loadObject(ofClass: String.self) { text, _ in
                    if let s = text, let url = URL(string: s),
                       let scheme = url.scheme?.lowercased(),
                       scheme == "http" || scheme == "https" {
                        Task { @MainActor in store.add(url: url) }
                        accepted = true
                    }
                }
            }
        }
        return accepted
    }

    // MARK: - Helpers

    private func iconName(for state: String) -> String {
        switch state {
        case "downloading":      return "arrow.down.circle.fill"
        case "completed":        return "checkmark.circle.fill"
        case "awaiting_new_link": return "link.badge.plus"
        case "failed":           return "xmark.octagon.fill"
        case "paused":           return "pause.circle.fill"
        case "cancelled":        return "minus.circle"
        case "queued":           return "ellipsis.circle"
        default:                 return "circle.dashed"
        }
    }

    private func stateLabel(_ state: String) -> String {
        switch state {
        case "downloading":       return S.stateDownloading
        case "completed":         return S.stateCompleted
        case "awaiting_new_link": return S.stateAwaitingLink
        case "failed":            return S.stateFailed
        case "paused":            return S.statePaused
        case "cancelled":         return S.stateCancelled
        case "queued":            return S.stateQueued
        default:                  return S.stateUnknown
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

    private func byteCount(_ bytes: Int64?) -> String {
        guard let bytes, bytes > 0 else { return "—" }
        return ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }
}

/// Empty-state placeholder.
struct EmptyStateView: View {
    @Environment(\.strings) private var S

    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: "arrow.down.circle")
                .font(.system(size: 56, weight: .light))
                .foregroundStyle(.tint)
            Text("Super Download Manager")
                .font(.title2.weight(.semibold))
            Text(S.emptyTitle)
                .foregroundStyle(.secondary)
            Text(S.emptyHint)
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
            Text(S.emptyTipLang)
                .font(.caption)
                .foregroundStyle(.tertiary)
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
