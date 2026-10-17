import SwiftUI
import AppKit
import UniformTypeIdentifiers
import SuperDownloadManagerCore

/// ClipboardConfirmView — v1.0.7.
///
/// Sheet shown when the clipboard watcher detects a URL
/// that looks like a downloadable file. The user picks
/// the destination category, can override the save
/// directory, and can cap the per-task thread count.
///
/// Buttons:
///   - **Download** — calls `store.addWithConfig(...)`
///     with the chosen overrides.
///   - **Skip**     — dismisses; the URL is recorded as
///     "seen" so the same URL won't re-prompt within 5 min.
///   - **Stop monitoring clipboard** — toggles
///     `sdm.capture.clipboard` off and dismisses. This is
///     the kill switch.
struct ClipboardConfirmView: View {
    @Environment(\.strings) private var S
    @ObservedObject var store: DownloadListStore
    let candidate: ClipboardCandidate
    @State private var category: DownloadCategory
    @State private var maxThreads: Int
    @State private var useCustomDir: Bool = false
    @State private var customDirPath: String = ""
    @Environment(\.dismiss) private var dismiss

    init(store: DownloadListStore, candidate: ClipboardCandidate) {
        self.store = store
        self.candidate = candidate
        self._category = State(initialValue: candidate.suggestedCategory)
        self._maxThreads = State(initialValue: candidate.defaultMaxThreads)
        let downloads = FileManager.default
            .urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory())
                .appendingPathComponent("Downloads")
        self._customDirPath = State(initialValue:
            downloads.appendingPathComponent(
                candidate.suggestedCategory.folderName).path)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Image(systemName: "doc.on.clipboard")
                    .font(.system(size: 28))
                    .foregroundStyle(Color.accentColor)
                VStack(alignment: .leading, spacing: 2) {
                    Text(S.clipboardSheetTitle)
                        .font(.title3.weight(.semibold))
                    Text(S.clipboardSheetSubtitle)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }

            // URL display (read-only)
            VStack(alignment: .leading, spacing: 4) {
                Text(S.detailDestination)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(candidate.url.absoluteString)
                    .font(.callout.monospaced())
                    .textSelection(.enabled)
                    .lineLimit(3)
                    .truncationMode(.middle)
            }

            // Category picker
            VStack(alignment: .leading, spacing: 4) {
                Text(S.clipboardCategory)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Picker("", selection: $category) {
                    ForEach(DownloadCategory.allCases, id: \.self) { c in
                        Label(c.folderName, systemImage: c.iconName)
                            .tag(c)
                    }
                }
                .pickerStyle(.menu)
                .labelsHidden()
                .onChange(of: category) { _, newValue in
                    // When the user switches category, default
                    // the save directory to the matching folder
                    // unless they've explicitly picked a custom
                    // path.
                    if !useCustomDir {
                        customDirPath = defaultDir(for: newValue).path
                    }
                }
            }

            // Save path
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(S.clipboardSavePath)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button(S.clipboardChoose) { chooseDirectory() }
                        .font(.caption)
                }
                Text(customDirPath)
                    .font(.callout.monospaced())
                    .textSelection(.enabled)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .padding(6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.secondary.opacity(0.08),
                                in: RoundedRectangle(cornerRadius: 4))
            }

            // Max threads
            VStack(alignment: .leading, spacing: 4) {
                Text(S.clipboardMaxThreads)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                HStack(spacing: 8) {
                    Stepper(value: $maxThreads, in: 1...128) {
                        Text("\(maxThreads)")
                            .monospacedDigit()
                            .frame(width: 36, alignment: .trailing)
                    }
                    Text(S.clipboardMaxThreadsHint)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            // Kill switch
            HStack(spacing: 6) {
                Toggle(S.clipboardDisableToggle, isOn: Binding(
                    get: { store.clipboardCapture },
                    set: { store.clipboardCapture = $0 }
                ))
                .toggleStyle(.checkbox)
            }

            if let err = downloadError {
                Text(err)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            Divider()

            HStack {
                Spacer()
                Button(S.clipboardSkip) { skip() }
                    .keyboardShortcut(.cancelAction)
                Button(S.clipboardDownload) { confirm() }
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
            }
        }
        .padding(20)
        .frame(width: 520)
    }

    @State private var downloadError: String? = nil

    // MARK: - Actions

    private func confirm() {
        let dir: URL?
        if useCustomDir {
            let url = URL(fileURLWithPath: customDirPath)
            dir = url
        } else {
            dir = defaultDir(for: category)
        }
        store.addWithConfig(
            url: candidate.url,
            category: category,
            saveDirectory: dir,
            maxThreads: maxThreads > 0 ? maxThreads : nil)
        store.pendingClipboard = nil
    }

    private func skip() {
        // The store's `lastSeenURLs` already records this URL
        // (set by checkClipboard); that prevents re-prompting
        // for 5 minutes. v1.0.7 behavior: skip == "no, thanks,
        // not right now", not "never again".
        store.pendingClipboard = nil
    }

    private func chooseDirectory() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = true
        panel.directoryURL = URL(fileURLWithPath: customDirPath)
        if panel.runModal() == .OK, let url = panel.url {
            customDirPath = url.path
            useCustomDir = true
        }
    }

    private func defaultDir(for cat: DownloadCategory) -> URL {
        let downloads = FileManager.default
            .urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory())
                .appendingPathComponent("Downloads")
        return downloads.appendingPathComponent(cat.folderName)
    }
}
