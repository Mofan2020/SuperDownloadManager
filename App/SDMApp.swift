import SwiftUI

/// SDM v1.0 - P0 stub
///
/// Real app body lives in P2 (MainWindow, SettingsView, etc.).
/// For P0 we only prove the MenuBarExtra + WindowGroup + openWindow pattern
/// from the macos-native-app-development skill's menubar-and-window-lifecycle pitfall.
@main
struct SDMApp: App {
    @State private var menuOpen: Bool = false

    var body: some Scene {
        // MenuBarExtra with default `.menu` style; reach the main window
        // through `openWindow(id:)`. Never set `.window` style unless we
        // want a popover (we don't — SDM needs a multi-purpose window).
        MenuBarExtra("SDM", systemImage: "arrow.down.circle") {
            Button("Open Main Window") {
                NSApplication.shared.activate(ignoringOtherApps: true)
                // The actual openWindow call is wired in MainWindowController
                // during P2; P0 leaves the menu stub so the build is real.
            }
            .keyboardShortcut("0", modifiers: [.command])

            Divider()

            Button("Downloads") {
                NSApplication.shared.activate(ignoringOtherApps: true)
            }
            Button("Settings...") {
                NSApplication.shared.activate(ignoringOtherApps: true)
            }

            Divider()

            Button("Quit SDM") {
                NSApplication.shared.terminate(nil)
            }
            .keyboardShortcut("q", modifiers: [.command])
        }

        // WindowGroup(id: "main") is required for openWindow(id:) routing
        // from MenuBarExtra buttons (per the skill pitfall).
        WindowGroup("Super Download Manager", id: "main") {
            MainWindowPlaceholder()
                .frame(minWidth: 720, minHeight: 480)
        }
        .defaultSize(width: 960, height: 600)

        Settings {
            SettingsPlaceholder()
        }
    }
}

private struct MainWindowPlaceholder: View {
    var body: some View {
        ContentUnavailableView(
            "Downloads",
            systemImage: "arrow.down.circle",
            description: Text("P0 scaffold — main UI lands in P2.")
        )
    }
}

private struct SettingsPlaceholder: View {
    var body: some View {
        ContentUnavailableView(
            "Settings",
            systemImage: "gearshape",
            description: Text("P0 scaffold — settings UI lands in P2.")
        )
        .frame(minWidth: 480, minHeight: 320)
    }
}
