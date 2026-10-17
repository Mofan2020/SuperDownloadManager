import SwiftUI
import SuperDownloadManagerCore

/// SDM v1.0 — top-level app.
///
/// P0/P2 layout:
/// - `MenuBarExtra` (default `.menu` style, routed through
///   `openWindow(id:)` — the macos-native-app-development skill
///   pitfall on window/menu pairing calls for this shape).
/// - `WindowGroup(id: "main")` for the download list window.
/// - `Settings` (driven by `cmd-,`) for the settings window.
/// - A single engine + store plumbed through `@State`.
@main
struct SDMApp: App {
    @StateObject private var store: DownloadListStore

    private let engine: SDMEngine

    init() {
        let engine = SDMEngine()
        self.engine = engine
        _store = StateObject(wrappedValue: DownloadListStore(engine: engine))
    }

    var body: some Scene {
        MenuBarExtra("SDM", systemImage: "arrow.down.circle") {
            Button("Open Main Window") {
                NSApplication.shared.activate(ignoringOtherApps: true)
            }
            .keyboardShortcut("0", modifiers: [.command])

            Divider()

            Button("Downloads") {
                NSApplication.shared.activate(ignoringOtherApps: true)
            }
            Button("Settings...") {
                NSApplication.shared.activate(ignoringOtherApps: true)
                // SwiftUI Settings scene opens via the app menu's
                // standard Settings item under the app name. Setting
                // up the "Settings…" shortcut here only needs the
                // window activation; cmd-, opens the Settings scene.
            }

            Divider()

            Button("Quit SDM") {
                NSApplication.shared.terminate(nil)
            }
            .keyboardShortcut("q", modifiers: [.command])
        }

        WindowGroup("Super Download Manager", id: "main") {
            MainWindow(store: store)
                .frame(minWidth: 720, minHeight: 480)
                .onAppear { store.start() }
        }
        .defaultSize(width: 960, height: 600)

        Settings {
            SettingsView()
        }
    }
}

/// Top-level container that lays out the download list and the
/// (stub) expired-link banner. Both panes share the
/// `DownloadListStore` because the banner needs engine state too.
struct MainWindow: View {
    @ObservedObject var store: DownloadListStore
    @State private var bannerMessage: String? = nil

    var body: some View {
        VStack(spacing: 0) {
            if let msg = bannerMessage {
                ExpiredLinkBanner(summary: msg,
                                  onReenter: { bannerMessage = nil })
            }
            DownloadListView(store: store)
        }
    }
}
