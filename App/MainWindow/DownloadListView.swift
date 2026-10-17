import SwiftUI
import AppKit
import SuperDownloadManagerCore

/// DownloadListView — main window's primary pane (v1.0.3 rewrite).
///
/// Adds a top toolbar with the basic IDM-style controls:
///  - **+ Add URL…** opens a sheet for entering a URL.
///  - **Pause All** / **Resume All** bulk operations.
///  - **Clear Completed** removes finished rows.
///
/// Each row has its own per-state controls: Pause/Resume toggle,
/// Cancel while downloading, Open / Reveal in Finder on
/// completion, Retry on failure, Remove on terminal states.
struct DownloadListView: View {
    @ObservedObject var store: DownloadListStore
    @State private var showAddSheet = false
    @State private var addURL: String = ""
    @State private var addFilename: String = ""
    @State private var addError: String? = nil

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
                List {
                    ForEach(store.tasks) { task in
                        TaskRow(task: task, store: store)
                    }
                }
                .listStyle(.inset)
            }
        }
        .sheet(isPresented: $showAddSheet) { addSheet }
    }

    // MARK: - Toolbar

    private var toolbar: some View {
        HStack(spacing: 8) {
            Button {
                addError = nil
                addFilename = ""
                showAddSheet = true
            } label: {
                Label("Add URL…", systemImage: "plus")
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
                Label("Pause All", systemImage: "pause.circle")
            }
            .disabled(activeCount == 0)
            .help(activeCount == 0
                  ? "No active downloads"
                  : "Pause \(activeCount) active download\(activeCount == 1 ? "" : "s")")

            Button {
                store.tasks.filter { $0.state == "paused" }
                    .forEach { store.resume(id: $0.id) }
            } label: {
                Label("Resume All", systemImage: "play.circle")
            }
            .disabled(pausedCount == 0)
            .help(pausedCount == 0
                  ? "No paused downloads"
                  : "Resume \(pausedCount) paused download\(pausedCount == 1 ? "" : "s")")

            Button {
                store.clearCompleted()
            } label: {
                Label("Clear Completed", systemImage: "trash")
            }
            .disabled(!store.tasks.contains { $0.state == "completed" })
            .help("Remove completed downloads from the list")
        }
    }

    // MARK: - Add URL sheet

    private var addSheet: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Add Download")
                .font(.title3.weight(.semibold))
            Text("Enter a URL. SDM will start a multi-threaded download immediately.")
                .font(.callout)
                .foregroundStyle(.secondary)

            VStack(alignment: .leading, spacing: 4) {
                Text("URL")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                TextField("https://example.com/file.zip", text: $addURL)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(commitAdd)
            }

            VStack(alignment: .leading, spacing: 4) {
                Text("Save as (optional)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                TextField("Leave blank to use the server's filename",
                          text: $addFilename)
                    .textFieldStyle(.roundedBorder)
            }

            if let err = addError {
                Text(err)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            HStack {
                Spacer()
                Button("Cancel") {
                    showAddSheet = false
                }
                .keyboardShortcut(.cancelAction)
                Button("Add") { commitAdd() }
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
            addError = "Enter a valid http(s) URL."
            return
        }
        let name = addFilename.trimmingCharacters(in: .whitespaces)
        store.add(url: url, filenameOverride: name.isEmpty ? nil : name)
        addURL = ""
        addFilename = ""
        addError = nil
        showAddSheet = false
    }
}

private struct TaskRow: View {
    let task: TaskSummary
    @ObservedObject var store: DownloadListStore

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                Image(systemName: iconName(for: task.state))
                    .foregroundStyle(.tint)
                    .frame(width: 22)
                Text(task.filename)
                    .font(.body.weight(.medium))
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer()
                rowActions
            }
            ProgressView(value: task.fractionDone)
                .progressViewStyle(.linear)
            HStack(spacing: 8) {
                Text(byteCount(task.bytesDone) +
                     (task.totalBytes.map { " / \(byteCount($0))" } ?? ""))
                if task.state == "downloading" {
                    Text("·")
                    Text(task.speedDescription)
                    if let eta = task.etaDescription {
                        Text("·")
                        Text("ETA \(eta)")
                    }
                } else if let err = task.errorMessage, !err.isEmpty {
                    Text("·")
                    Text(err)
                        .lineLimit(1)
                        .truncationMode(.tail)
                        .foregroundStyle(.red)
                }
                Spacer()
                Text(localizedState(task.state))
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(stateBadgeColor(task.state).opacity(0.18),
                                in: Capsule())
                    .foregroundStyle(stateBadgeColor(task.state))
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .padding(.vertical, 6)
        .contextMenu {
            rowContextMenu
        }
    }

    // MARK: - Per-row actions

    @ViewBuilder
    private var rowActions: some View {
        switch task.state {
        case "downloading", "queued":
            Button {
                store.pause(id: task.id)
            } label: {
                Image(systemName: "pause.fill")
            }
            .buttonStyle(.borderless)
            .help("Pause")
            Button {
                store.cancel(id: task.id)
            } label: {
                Image(systemName: "xmark.circle.fill")
            }
            .buttonStyle(.borderless)
            .help("Cancel")
        case "paused":
            Button {
                store.resume(id: task.id)
            } label: {
                Image(systemName: "play.fill")
            }
            .buttonStyle(.borderless)
            .help("Resume (v1.0.3 restarts from byte 0)")
            Button {
                store.cancel(id: task.id)
            } label: {
                Image(systemName: "xmark.circle.fill")
            }
            .buttonStyle(.borderless)
            .help("Cancel")
        case "failed":
            Button {
                store.resume(id: task.id)
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .help("Retry")
            Button {
                store.remove(id: task.id)
            } label: {
                Image(systemName: "trash")
            }
            .buttonStyle(.borderless)
            .help("Remove from list")
        case "completed":
            Button {
                store.openFile(id: task.id)
            } label: {
                Image(systemName: "arrow.up.forward.app")
            }
            .buttonStyle(.borderless)
            .help("Open")
            Button {
                store.revealInFinder(id: task.id)
            } label: {
                Image(systemName: "folder")
            }
            .buttonStyle(.borderless)
            .help("Reveal in Finder")
            Button {
                store.remove(id: task.id)
            } label: {
                Image(systemName: "trash")
            }
            .buttonStyle(.borderless)
            .help("Remove from list")
        case "cancelled":
            Button {
                store.resume(id: task.id)
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .help("Re-add")
            Button {
                store.remove(id: task.id)
            } label: {
                Image(systemName: "trash")
            }
            .buttonStyle(.borderless)
            .help("Remove from list")
        default:
            EmptyView()
        }
    }

    @ViewBuilder
    private var rowContextMenu: some View {
        if task.state == "completed" {
            Button("Open") { store.openFile(id: task.id) }
            Button("Reveal in Finder") { store.revealInFinder(id: task.id) }
        }
        if task.state == "downloading" || task.state == "queued" {
            Button("Pause") { store.pause(id: task.id) }
            Button("Cancel") { store.cancel(id: task.id) }
        }
        if task.state == "paused" || task.state == "failed" || task.state == "cancelled" {
            Button("Retry") { store.resume(id: task.id) }
        }
        if task.isTerminal || task.state == "paused" {
            Divider()
            Button("Remove from list") { store.remove(id: task.id) }
        }
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

    private func localizedState(_ state: String) -> LocalizedStringKey {
        switch state {
        case "downloading":       return L10n.stateDownloading
        case "completed":         return L10n.stateCompleted
        case "awaiting_new_link": return L10n.stateAwaitingLink
        case "failed":            return L10n.stateFailed
        case "paused":            return "Paused"
        case "cancelled":         return "Cancelled"
        case "queued":            return "Queued"
        default:                  return L10n.stateUnknown
        }
    }

    private func stateBadgeColor(_ state: String) -> Color {
        switch state {
        case "downloading", "queued": return .accentColor
        case "completed": return .green
        case "paused":    return .orange
        case "failed":    return .red
        case "cancelled": return .secondary
        default:          return .secondary
        }
    }

    private func byteCount(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }
}

/// Empty-state placeholder. Shown when there are no rows in the
/// list — gives the user a clear "click + to add a download"
/// affordance instead of staring at a blank window.
struct EmptyStateView: View {
    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: "arrow.down.circle")
                .font(.system(size: 56, weight: .light))
                .foregroundStyle(.tint)
            Text("Super Download Manager")
                .font(.title2.weight(.semibold))
            Text("No downloads yet")
                .foregroundStyle(.secondary)
            Text("Click + or press ⌘N to add a URL. Drop a download in Firefox/Safari to send it to SDM. From a terminal: `sdmdm add <url>`.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 380)
            Text("Settings → Language to switch to 中文.")
                .font(.caption)
                .foregroundStyle(.tertiary)
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
