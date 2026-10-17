import SwiftUI
import AppKit
import SuperDownloadManagerCore

/// SDM v1.0.3 — top-level app.
///
/// v1.0.1: bilingual EN/中文 + locale override; "Open Main
///         Window" routes through `\.openWindow`.
/// v1.0.2: empty-state placeholder so the window doesn't look
///         blank before the first download lands.
/// v1.0.3: real GUI controls (Add URL sheet, Pause/Resume/Cancel
///         per row, Clear Completed, Open/Reveal in Finder, etc.)
///         + an AppDelegate that owns a manual `NSStatusItem` and
///         NSMenu — the SwiftUI `MenuBarExtra` drop-down doesn't
///         reliably receive `\.openWindow` from its child views on
///         macOS 26. The status item now activates the running app
///         and asks `openWindow(id: "main")` via direct NSWindow
///         handle or a URL-scheme round trip.
@main
struct SDMApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var store: DownloadListStore
    @AppStorage("sdm.locale") private var locale: String = "system"

    private let engine: SDMEngine

    init() {
        let engine = SDMEngine()
        self.engine = engine
        _store = StateObject(wrappedValue: DownloadListStore(engine: engine))
        AppDelegate.engine = engine
    }

    private var resolvedLocale: Locale {
        switch LocaleTag(rawValue: locale) ?? .system {
        case .zhHans: return Locale(identifier: "zh-Hans")
        case .en:     return Locale(identifier: "en")
        case .system: return .autoupdatingCurrent
        }
    }

    private var strings: Strings { Strings(locale: LocaleTag(rawValue: locale) ?? .system) }

    var body: some Scene {
        // We do NOT use SwiftUI's `MenuBarExtra` — its popover
        // doesn't reliably inherit `\.openWindow` on macOS 26.
        // AppDelegate owns the real NSStatusItem and NSMenu.
        WindowGroup("Super Download Manager", id: "main") {
            MainWindow(store: store, engine: engine)
                .frame(minWidth: 720, minHeight: 480)
                .onAppear { store.start() }
                .environment(\.strings, strings)
                .environment(\.locale, resolvedLocale)
                .background(WindowAccessor())
        }
        .defaultSize(width: 960, height: 600)
        .commands {
            CommandGroup(replacing: .newItem) {
                OpenMainWindowMenuItem(
                    title: strings.menuOpenWindow,
                    shortcut: KeyEquivalent("0"),
                    modifiers: [.command]
                )
                AddURLMenuItem(title: strings.toolbarAddURL)
            }
        }

        Settings {
            SettingsView(engine: engine)
                .environment(\.strings, strings)
                .environment(\.locale, resolvedLocale)
        }
    }
}

/// Bridges an AppKit NSWindow to SwiftUI so AppDelegate can
/// bring the latest-known window forward when the user clicks
/// the status item.
private struct WindowAccessor: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView {
        let v = NSView()
        DispatchQueue.main.async { [weak v] in
            guard let win = v?.window else { return }
            AppDelegate.sharedMainWindow = win
        }
        return v
    }
    func updateNSView(_ nsView: NSView, context: Context) {}
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    static var engine: SDMEngine?
    static var sharedMainWindow: NSWindow?
    private var statusItem: NSStatusItem?

    func applicationDidFinishLaunching(_ notification: Notification) {
        installStatusItem()
        registerURLEventHandler()
        NSLog("SDM AppDelegate: launched; statusItem=\(String(describing: statusItem))")
    }

    func applicationShouldTerminateAfterLastWindowClosed(
        _ sender: NSApplication
    ) -> Bool {
        // Stay alive after the user closes the window. The app
        // lives in the menu bar.
        return false
    }

    private func installStatusItem() {
        let item = NSStatusBar.system.statusItem(
            withLength: NSStatusItem.variableLength)
        if let button = item.button {
            button.image = NSImage(
                systemSymbolName: "arrow.down.circle",
                accessibilityDescription: "Super Download Manager")
            button.image?.isTemplate = true
            button.toolTip = "Super Download Manager"
        }
        item.menu = makeMenu()
        statusItem = item
    }

    private func makeMenu() -> NSMenu {
        let menu = NSMenu()
        let open = NSMenuItem(title: "Open Main Window",
                              action: #selector(openMainWindow),
                              keyEquivalent: "0")
        open.keyEquivalentModifierMask = [.command]
        menu.addItem(open)

        let add = NSMenuItem(title: "Add URL…",
                             action: #selector(addURLClicked),
                             keyEquivalent: "n")
        add.keyEquivalentModifierMask = [.command]
        menu.addItem(add)

        menu.addItem(NSMenuItem.separator())

        let reveal = NSMenuItem(title: "Show Downloads in Finder",
                                action: #selector(revealInFinder),
                                keyEquivalent: "")
        menu.addItem(reveal)

        menu.addItem(NSMenuItem.separator())

        let about = NSMenuItem(title: "About Super Download Manager",
                               action: #selector(about),
                               keyEquivalent: "")
        menu.addItem(about)

        let settings = NSMenuItem(title: "Settings…",
                                  action: #selector(openSettings),
                                  keyEquivalent: ",")
        settings.keyEquivalentModifierMask = [.command]
        menu.addItem(settings)

        menu.addItem(NSMenuItem.separator())

        let quit = NSMenuItem(title: "Quit Super Download Manager",
                              action: #selector(quit),
                              keyEquivalent: "q")
        quit.keyEquivalentModifierMask = [.command]
        menu.addItem(quit)

        for it in menu.items {
            it.target = self
        }
        return menu
    }

    // MARK: - Menu actions

    @objc func openMainWindow() {
        activateAndShow()
    }

    @objc func addURLClicked() {
        activateAndShow()
        NotificationCenter.default.post(name: .sdmShowAddSheet, object: nil)
    }

    @objc func revealInFinder() {
        let url = FileManager.default
            .urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory())
                .appendingPathComponent("Downloads")
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    @objc func about() {
        NSApp.orderFrontStandardAboutPanel(nil)
    }

    @objc func openSettings() {
        activateAndShow()
        NotificationCenter.default.post(name: .sdmShowSettings, object: nil)
    }

    @objc func quit() {
        NSApp.terminate(nil)
    }

    // MARK: - URL scheme

    private func registerURLEventHandler() {
        NSAppleEventManager.shared().setEventHandler(
            self,
            andSelector: #selector(handleURLEvent(_:withReply:)),
            forEventClass: AEEventClass(kInternetEventClass),
            andEventID: AEEventID(kAEGetURL))
    }

    @objc func handleURLEvent(_ event: NSAppleEventDescriptor,
                              withReply reply: NSAppleEventDescriptor) {
        guard let urlString = event
            .paramDescriptor(forKeyword: AEKeyword(keyDirectObject))?
            .stringValue,
              let url = URL(string: urlString),
              let host = url.host?.lowercased() else { return }
        switch host {
        case "show", "open":
            activateAndShow()
        case "add":
            let qs = URLComponents(url: url, resolvingAgainstBaseURL: false)?
                .queryItems ?? []
            if let urlItem = qs.first(where: { $0.name == "url" })?.value,
               let target = URL(string: urlItem),
               let engine = AppDelegate.engine {
                let dir = FileManager.default
                    .urls(for: .downloadsDirectory, in: .userDomainMask).first
                    ?? URL(fileURLWithPath: NSHomeDirectory())
                        .appendingPathComponent("Downloads")
                Task { _ = await engine.start([.init(
                    url: target, destinationDirectory: dir)]) }
                activateAndShow()
            }
        default:
            break
        }
    }

    private func activateAndShow() {
        NSApp.activate(ignoringOtherApps: true)
        if let win = AppDelegate.sharedMainWindow {
            win.makeKeyAndOrderFront(nil)
        }
    }
}

extension Notification.Name {
    static let sdmShowAddSheet = Notification.Name("sdm.showAddSheet")
    static let sdmShowSettings = Notification.Name("sdm.showSettings")
}

// MARK: - Standard menu items

private struct OpenMainWindowMenuItem: View {
    let title: String
    let shortcut: KeyEquivalent
    let modifiers: SwiftUI.EventModifiers
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Button(title) {
            openWindow(id: "main")
            NSApp.activate(ignoringOtherApps: true)
        }
        .keyboardShortcut(shortcut, modifiers: modifiers)
    }
}

private struct AddURLMenuItem: View {
    let title: String
    var body: some View {
        Button(title) {
            NotificationCenter.default.post(name: .sdmShowAddSheet, object: nil)
            NSApp.activate(ignoringOtherApps: true)
        }
        .keyboardShortcut("n", modifiers: [.command])
    }
}

// MARK: - MainWindow

/// Top-level container for the main window. v1.0.5: bilingual
/// tabs via `Strings`, `Active` / `History` / `Schedule`.
struct MainWindow: View {
    @ObservedObject var store: DownloadListStore
    let engine: SDMEngine
    @State private var bannerMessage: String? = nil
    @State private var showAddSheet = false
    @State private var selectedTab: Tab = .active
    @State private var historyVM = HistoryViewModel()
    @Environment(\.strings) private var S

    enum Tab: String, CaseIterable, Identifiable {
        case active = "active"
        case history = "history"
        case schedule = "schedule"
        var id: String { rawValue }
    }

    var body: some View {
        VStack(spacing: 0) {
            Picker("", selection: $selectedTab) {
                Text(S.tabActive).tag(Tab.active)
                Text(S.tabHistory).tag(Tab.history)
                Text(S.tabSchedule).tag(Tab.schedule)
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(.bar)
            if let msg = bannerMessage {
                ExpiredLinkBanner(summary: msg,
                                  onReenter: { bannerMessage = nil })
            }
            switch selectedTab {
            case .active:
                DownloadListView(store: store)
            case .history:
                HistoryView(vm: historyVM, store: store)
            case .schedule:
                ScheduleView(store: store)
            }
        }
        .onReceive(NotificationCenter.default.publisher(
            for: .sdmShowAddSheet)) { _ in
            selectedTab = .active
        }
        .onChange(of: store.tasks) { _, new in
            for t in new where t.isTerminal {
                let entry = HistoryStore.Entry(
                    id: t.id,
                    url: t.urlString,
                    filename: t.filename,
                    destinationPath: t.destinationPath,
                    totalBytes: t.totalBytes,
                    bytesDone: t.bytesDone,
                    state: t.state,
                    errorMessage: t.errorMessage,
                    startedAt: Date().addingTimeInterval(-60),
                    finishedAt: Date(),
                    sha256: nil)
                Task { await historyVM.append(entry) }
            }
        }
    }
}
