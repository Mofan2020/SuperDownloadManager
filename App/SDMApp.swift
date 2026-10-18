import SwiftUI
import AppKit
import SuperDownloadManagerCore

// v1.1.2: `GlassEffectContainer` is iOS/macOS 26+; the project
// deployment target is 26, but compiling on an older SDK still
// needs an `#available` fallback so the file builds in CI when
// Xcode hasn't been updated.
@available(macOS 26.0, *)
private struct TabBarGlassContainer<Content: View>: View {
    let tint: Color
    let content: () -> Content
    init(tint: Color = .accentColor, @ViewBuilder content: @escaping () -> Content) {
        self.tint = tint
        self.content = content
    }
    var body: some View {
        content()
            .padding(6)
            .background {
                Capsule().fill(.clear)
            }
            .glassEffect(.regular.interactive(), in: Capsule())
            .overlay {
                Capsule().strokeBorder(
                    Color.primary.opacity(0.06),
                    lineWidth: 0.5)
            }
    }
}

/// Tab-bar fallback for macOS < 26 (not used in practice — the
/// project deployment target is 26.0 — but the build references
/// it so older CI toolchains still link).
private struct TabBarPlainContainer<Content: View>: View {
    let content: () -> Content
    init(@ViewBuilder content: @escaping () -> Content) {
        self.content = content
    }
    var body: some View {
        content()
            .padding(6)
            .background {
                Capsule().fill(.regularMaterial)
            }
            .overlay {
                Capsule().strokeBorder(
                    Color.primary.opacity(0.08),
                    lineWidth: 0.5)
            }
    }
}

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

    // v1.1.2: Track how many "real" app windows are currently
    // open. The SwiftUI `WindowGroup` keeps detached window
    // instances around after `cmd-W`, so we count on the live
    // key/front visibility rather than `NSApp.windows.count`.
    // When the count drops to zero and the user has opted in
    // ("Hide dock icon when window is closed"), we switch to
    // `.accessory` so the dock icon disappears. As soon as any
    // window becomes key again, we restore `.regular`.
    private var visibleMainWindowCount: Int = 0
    private var observeTokens: [NSObjectProtocol] = []

    func applicationDidFinishLaunching(_ notification: Notification) {
        installStatusItem()
        registerURLEventHandler()
        installDockVisibilityObservers()
        // v1.1.2: reconcile the "Open at login" AppStorage value
        // with the system's login-item registration. v1.0.4 stored
        // the Bool but never called SMAppService, so toggling in
        // Settings had no effect. We do this idempotently here so
        // any pre-v1.1.2 user who flipped the toggle and then quit
        // gets registered (or un-registered) on next launch.
        LaunchAtLogin.syncFromStorage()
        // Apply the user's dock preference on launch, but only
        // once SwiftUI has actually opened the main window (the
        // WindowGroup default) — otherwise we'd dock-hide the
        // app at startup just because the user toggled it last
        // session.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
            [weak self] in
            self?.updateDockVisibilityForCurrentState(force: true)
        }
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
        // openWindow may have just created a new NSWindow — give
        // SwiftUI a tick to actually attach it before we make any
        // activation policy decisions based on visibility.
        DispatchQueue.main.async { [weak self] in
            self?.updateDockVisibilityForCurrentState()
        }
    }

    // MARK: - Dock visibility (v1.1.2)

    /// Hook the two NSWindow lifecycle notifications and watch the
    /// toggle in AppStorage. `NotificationCenter.addObserver`
    /// returns tokens so we don't leak handlers when the
    /// AppDelegate is replaced (it isn't in practice, but the
    /// safe pattern keeps memory clean across relaunches).
    private func installDockVisibilityObservers() {
        let nc = NotificationCenter.default
        let didBecomeKey = nc.addObserver(
            forName: NSWindow.didBecomeKeyNotification,
            object: nil, queue: .main) { [weak self] _ in
                // `queue: .main` already pins delivery to the main
                // thread, so we can re-enter the MainActor without
                // an async hop. `assumeIsolated` is the runtime
                // assertion the Swift 5 / minimal-concurrency
                // mode wants here.
                MainActor.assumeIsolated {
                    self?.handleWindowBecameKey()
                }
            }
        let willClose = nc.addObserver(
            forName: NSWindow.willCloseNotification,
            object: nil, queue: .main) { [weak self] note in
                MainActor.assumeIsolated {
                    // Ignore close notifications for transient panels
                    // (NSAlert, NSOpenPanel, NSSavePanel, …) — they
                    // would otherwise force a flip to .accessory
                    // after every Add URL sheet. NSWindow exposes
                    // `styleMask` (a bitmask of NSWindow.StyleMask),
                    // NOT a `classification` field — check the bits.
                    if let win = note.object as? NSWindow {
                        let mask = win.styleMask
                        if mask.contains(.nonactivatingPanel)
                            || win.isSheet {
                            return
                        }
                    }
                    self?.handleWindowWillClose()
                }
            }
        observeTokens = [didBecomeKey, willClose]

        // The toggle is a @AppStorage Bool, but SwiftUI writes
        // through `UserDefaults.standard.set`, so listening to
        // the global `UserDefaults.didChangeNotification` and
        // checking the key catches every edit (the user could
        // also flip it via `defaults write` from terminal).
        let defaultsChanged = nc.addObserver(
            forName: UserDefaults.didChangeNotification,
            object: UserDefaults.standard,
            queue: .main) { [weak self] _ in
                MainActor.assumeIsolated {
                    self?.updateDockVisibilityForCurrentState(force: true)
                }
            }
        observeTokens.append(defaultsChanged)
    }

    /// Bumped whenever any NSWindow becomes key. The `didBecomeKey`
    /// notification can fire for sheets too — but sheets can't
    /// become key over a closed parent, so incrementing here on
    /// any key window is good enough for the visibility model
    /// (we only ever switch to `.regular` on a count > 0).
    private func handleWindowBecameKey() {
        visibleMainWindowCount = max(visibleMainWindowCount, 1)
        updateDockVisibilityForCurrentState()
    }

    /// Decremented when any window closes. We clamp at zero; even
    /// if notifications fire out of order (a stale willClose
    /// from an already-closed window), the dock check stays
    /// correct because we use the clamped value.
    private func handleWindowWillClose() {
        // Use a small grace period so that a window-close
        // immediately followed by an openWindow (cmd-W then
        // click tray icon) doesn't transiently flip to
        // .accessory — macOS animates the dock re-add back in
        // otherwise.
        let captured = max(0, visibleMainWindowCount - 1)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
            [weak self] in
            guard let self = self else { return }
            self.visibleMainWindowCount = captured
            self.updateDockVisibilityForCurrentState()
        }
    }

    /// Apply the user's preference against the current state.
    /// `force: true` ignores the "don't change policy mid-launch"
    /// guard and is used right after `applicationDidFinishLaunching`
    /// to settle the initial value.
    private func updateDockVisibilityForCurrentState(force: Bool = false) {
        let hide = UserDefaults.standard.bool(forKey: "sdm.hideDockWhenWindowClosed")
        let hasVisibleWindow = visibleMainWindowCount > 0
            || (AppDelegate.sharedMainWindow?.isVisible ?? false)

        if hide && !hasVisibleWindow {
            // Menu-bar-only mode — dock is hidden.
            NSApp.setActivationPolicy(.accessory)
        } else {
            // Default for any app that owns a SwiftUI WindowGroup.
            NSApp.setActivationPolicy(.regular)
        }
    }

    deinit {
        let nc = NotificationCenter.default
        for token in observeTokens { nc.removeObserver(token) }
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
            TabHeader(selection: $selectedTab)
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
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

/// v1.1.2: tab-bar header. Replaces the bare `Picker(.segmented)`
/// from v1.0.5 — the segmented control was the right *control*,
/// but it sat on top of `.background(.bar)` with no container,
/// which read as a flat chrome strip. Wrapping it in a Liquid
/// Glass capsule on macOS 26 lifts the bar off the content and
/// tracks the selection lens like the system segmented controls
/// do. Falls back to a plain `.regularMaterial` capsule on
/// older SDKs (CI without Xcode 26).
private struct TabHeader: View {
    @Binding var selection: MainWindow.Tab
    @Environment(\.strings) private var S

    var body: some View {
        let S = self.S
        let picker = Picker("", selection: $selection) {
            Text(S.tabActive).tag(MainWindow.Tab.active)
            Text(S.tabHistory).tag(MainWindow.Tab.history)
            Text(S.tabSchedule).tag(MainWindow.Tab.schedule)
        }
        .pickerStyle(.segmented)
        .labelsHidden()

        Group {
            if #available(macOS 26.0, *) {
                picker
                    .padding(6)
                    .background {
                        Capsule().fill(.clear)
                    }
                    .glassEffect(.regular.interactive(), in: Capsule())
                    .overlay {
                        Capsule().strokeBorder(
                            Color.primary.opacity(0.06),
                            lineWidth: 0.5)
                    }
            } else {
                picker
                    .padding(6)
                    .background {
                        Capsule().fill(.regularMaterial)
                    }
                    .overlay {
                        Capsule().strokeBorder(
                            Color.primary.opacity(0.08),
                            lineWidth: 0.5)
                    }
            }
        }
        .frame(maxWidth: 420)
        .frame(maxWidth: .infinity, alignment: .center)
    }
}
