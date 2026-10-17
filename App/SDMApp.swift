import SwiftUI
import SuperDownloadManagerCore

/// SDM v1.0 — top-level app.
///
/// v1.0.1 fixes two bugs reported by 星空喵 in pre-release testing:
///   1. "Open Main Window" from the menu bar was a silent no-op —
///      only `NSApp.activate(ignoringOtherApps:)` was called with
///      no `openWindow(id:)`, so the button appeared to do nothing.
///   2. "Settings…" sent a selector that didn't exist on macOS 26 SDK
///      and was wired for nothing. Both buttons are now real
///      `OpenWindow` / `OpenSettings` view-bridges that work through
///      the SwiftUI environment.
///
/// v1.0.1 also adds the bilingual EN/中文 switch (L10n.swift) plus
/// `@AppStorage("sdm.locale")` settings that override the system
/// locale (or follow it when set to "system").
@main
struct SDMApp: App {
    @StateObject private var store: DownloadListStore
    @AppStorage("sdm.locale") private var locale: String = "system"

    private let engine: SDMEngine

    init() {
        let engine = SDMEngine()
        self.engine = engine
        _store = StateObject(wrappedValue: DownloadListStore(engine: engine))
    }

    /// Resolve the user's chosen locale string into a SwiftUI
    /// `Locale` value. Default to system ("system" or empty = auto).
    private var resolvedLocale: Locale {
        if locale == "zh-Hans" { return Locale(identifier: "zh-Hans") }
        if locale == "en"      { return Locale(identifier: "en") }
        return .autoupdatingCurrent
    }

    var body: some Scene {
        MenuBarExtra("SDM", systemImage: "arrow.down.circle") {
            // `MenuBarExtra` doesn't directly carry the
            // `@Environment(\.openWindow)` from the App body, so we
            // wrap the action in a tiny View that does.
            OpenMainWindowMenuItem(
                title: L10n.menuOpenWindow,
                shortcut: "0",
                modifiers: [.command]
            )

            Divider()

            // Show Downloads main window — same bridge as Open Main
            // Window; both route to WindowGroup(id: "main").
            OpenMainWindowMenuItem(
                title: L10n.menuDownloads,
                shortcut: KeyEquivalent("d"),
                modifiers: [.command]
            )

            SettingsMenuItem(title: L10n.menuSettings)

            Divider()

            QuitAppMenuItem(title: L10n.menuQuit,
                            shortcut: KeyEquivalent("q"),
                            modifiers: [.command])
        }

        WindowGroup("Super Download Manager", id: "main") {
            // Wrap in LocalizedRootView so `.environment(\.locale:)` is
            // reachable from MenuBarExtra's child Views as well as the
            // main window. macOS 14+ picks the bundle language from
            // `Info.plist` CFBundleLocalizations when the user has not
            // overridden; the @AppStorage override here wins over the
            // system preference at launch.
            LocalizedRootView(locale: resolvedLocale) {
                MainWindow(store: store)
                    .frame(minWidth: 720, minHeight: 480)
                    .onAppear { store.start() }
            }
        }
        .defaultSize(width: 960, height: 600)

        Settings {
            LocalizedRootView(locale: resolvedLocale) {
                SettingsView()
            }
        }
    }
}

/// View wrapper that injects the user-chosen Locale into SwiftUI's
/// environment. Without this, the system locale wins regardless of
/// the `@AppStorage("sdm.locale")` choice.
private struct LocalizedRootView<Content: View>: View {
    let locale: Locale
    let content: () -> Content

    var body: some View {
        content()
            .environment(\.locale, locale)
    }
}

/// MenuBarExtra helper that wraps a Button which calls
/// `@Environment(\.openWindow)` with id "main". SwiftUI's
/// environment doesn't get inherited through MenuBarExtra, so
/// putting it inside a View (which itself runs in an environment)
/// is the only way to bridge the menu item back to the openWindow
/// action.
private struct OpenMainWindowMenuItem: View {
    let title: LocalizedStringKey
    let shortcut: KeyEquivalent
    let modifiers: SwiftUI.EventModifiers

    @Environment(\.openWindow) private var openWindow

    init(title: LocalizedStringKey,
         shortcut: KeyEquivalent = KeyEquivalent("0"),
         modifiers: SwiftUI.EventModifiers = [.command]) {
        self.title = title
        self.shortcut = shortcut
        self.modifiers = modifiers
    }

    var body: some View {
        Button(title) {
            // `main` matches WindowGroup(id: "main") above.
            openWindow(id: "main")
            NSApp.activate(ignoringOtherApps: true)
        }
        .keyboardShortcut(shortcut, modifiers: modifiers)
    }
}

/// MenuBarExtra helper that opens the SwiftUI `Settings` scene
/// via `@Environment(\.openSettings)`. The previous v1.0 attempt
/// used `NSApp.sendAction(Selector("showSettingsWindow:"))` which
/// doesn't exist and was a silent no-op.
private struct SettingsMenuItem: View {
    let title: LocalizedStringKey
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        Button(title) {
            openSettings()
            NSApp.activate(ignoringOtherApps: true)
        }
        .keyboardShortcut(",", modifiers: [.command])
    }
}

/// MenuBarExtra helper that quits the app.
private struct QuitAppMenuItem: View {
    let title: LocalizedStringKey
    let shortcut: KeyEquivalent
    let modifiers: SwiftUI.EventModifiers

    var body: some View {
        Button(title) {
            NSApp.terminate(nil)
        }
        .keyboardShortcut(shortcut, modifiers: modifiers)
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
