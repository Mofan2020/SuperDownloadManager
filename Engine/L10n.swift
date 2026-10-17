import Foundation
import SwiftUI

/// Strings — v1.0.5 bilingual catalog (replaces Localizable.strings).
///
/// Why not Localizable.strings?
/// 1. `LocalizedStringKey` constants don't allow printf-style
///    format arguments, so the old `%lld` placeholder in
///    `settings.defaultThreads` rendered literally in v1.0.4
///    instead of being substituted.
/// 2. `Bundle.main.localizedString` reads the active locale once
///    at process start; re-picking a different locale in Settings
///    didn't take effect until the next launch (the "language
///    switch requires restart" footgun).
/// 3. Localizable.strings files can't ship rich plurals without
///    a separate .stringsdict per locale.
///
/// v1.0.5 in-code dictionary fixes all three: every string is a
/// `(en, zh-Hans)` pair; `current` is a computed property driven
/// by `@AppStorage("sdm.locale")`; format strings like
/// `defaultThreads` take the int as a parameter via `defaultThreads(_:)`.
public enum LocaleTag: String, CaseIterable, Identifiable, Equatable, Sendable {
    case system = "system"
    case en     = "en"
    case zhHans = "zh-Hans"
    public var id: String { rawValue }
    public var displayName: String {
        switch self {
        case .system: return "Follow system"
        case .en:     return "English"
        case .zhHans: return "简体中文"
        }
    }
}

/// Per-locale dictionary. Every UI string lives here as a
/// `Pair`. Add a new key by adding two `case` entries and
/// handling it in `localized(_:)`.
private enum Catalog {
    enum Pair { case en, zh }

    static let menuOpenWindow: (String, String) = (
        "Open Main Window", "打开主窗口")
    static let menuDownloads: (String, String) = (
        "Downloads", "下载")
    static let menuSettings: (String, String) = (
        "Settings…", "设置…")
    static let menuQuit: (String, String) = (
        "Quit Super Download Manager", "退出 隙间下载")

    static let mainDownloads: (String, String) = (
        "Downloads", "下载")

    // Settings → Defaults section
    static let settingsSectionDefaults: (String, String) = (
        "Defaults", "默认")
    static let settingsDefaultThreadsLabel: (String, String) = (
        "Default threads per download", "每个下载的默认线程数")
    static let settingsDefaultThreadsHint: (String, String) = (
        "1 – 128", "1 – 128")
    static let settingsBandwidth: (String, String) = (
        "Bandwidth limit", "带宽限制")
    static let settingsBandwidthHint: (String, String) = (
        "0 = unlimited", "0 = 不限速")
    static let settingsKbps: (String, String) = (
        "KB/s", "KB/s")

    // Settings → Categories
    static let settingsSectionCategories: (String, String) = (
        "Categories & Rules", "分类与规则")
    static let settingsCategoriesLink: (String, String) = (
        "File categories…", "文件分类…")
    static let settingsCategoriesPlaceholder: (String, String) = (
        "Categories are auto-detected from file extension. " +
        "Toggle in Active tab toolbar.",
        "根据文件扩展名自动分类。在主窗口工具栏切换。")
    static let settingsSiteRulesLink: (String, String) = (
        "Site rules…", "站点规则…")

    // Settings → Startup
    static let settingsSectionStartup: (String, String) = (
        "Startup & Updates", "启动与更新")
    static let settingsOpenAtLogin: (String, String) = (
        "Open at login", "开机自启")
    static let settingsUpdateChannel: (String, String) = (
        "Update channel", "更新通道")
    static let settingsUpdateStable: (String, String) = (
        "Stable only", "仅正式版")
    static let settingsUpdatePrerelease: (String, String) = (
        "Include pre-releases", "包含预发布")

    // Settings → General
    static let settingsSectionGeneral: (String, String) = (
        "General", "通用")
    static let settingsLanguage: (String, String) = (
        "Language", "语言")
    static let settingsLanguageHint: (String, String) = (
        "Changes apply immediately.", "更改立即生效。")
    static let settingsLanguageSystem: (String, String) = (
        "Follow system", "跟随系统")
    static let settingsLanguageEn: (String, String) = (
        "English", "English")
    static let settingsLanguageZh: (String, String) = (
        "简体中文", "简体中文")

    // Settings → Tabs (left column)
    static let settingsSidebarGeneral: (String, String) = (
        "General", "通用")
    static let settingsSidebarDefaults: (String, String) = (
        "Defaults", "默认")
    static let settingsSidebarCategories: (String, String) = (
        "Categories", "分类")
    static let settingsSidebarSiteRules: (String, String) = (
        "Site rules", "站点规则")
    static let settingsSidebarStartup: (String, String) = (
        "Startup", "启动")
    static let settingsSidebarAbout: (String, String) = (
        "About", "关于")
    static let settingsAboutTitle: (String, String) = (
        "Super Download Manager", "隙间下载")
    static let settingsAboutVersion: (String, String) = (
        "Version", "版本")
    static let settingsAboutLicense: (String, String) = (
        "MIT License", "MIT 协议")
    static let settingsAboutCopyright: (String, String) = (
        "© 2026 Skyc8266", "© 2026 星空喵")

    // Toolbar / Add URL
    static let toolbarAddURL: (String, String) = (
        "Add URL…", "添加 URL…")
    static let toolbarPauseAll: (String, String) = (
        "Pause All", "全部暂停")
    static let toolbarResumeAll: (String, String) = (
        "Resume All", "全部继续")
    static let toolbarClearCompleted: (String, String) = (
        "Clear Completed", "清除已完成")
    static let addSheetTitle: (String, String) = (
        "Add Download", "添加下载")
    static let addSheetSubtitle: (String, String) = (
        "Enter a URL. SDM will start a multi-threaded download immediately.",
        "输入 URL，隙间下载将立即开始多线程下载。")
    static let addSheetURLField: (String, String) = (
        "URL", "URL")
    static let addSheetFilenameField: (String, String) = (
        "Save as (optional)", "另存为（可选）")
    static let addSheetFilenameHint: (String, String) = (
        "Leave blank to use the server's filename",
        "留空使用服务器的文件名")
    static let addSheetAdd: (String, String) = (
        "Add", "添加")
    static let addSheetCancel: (String, String) = (
        "Cancel", "取消")
    static let addSheetErrorBadURL: (String, String) = (
        "Enter a valid http(s) URL.", "请输入有效的 http(s) URL。")

    // Empty state
    static let emptyTitle: (String, String) = (
        "No downloads yet", "还没有下载")
    static let emptyHint: (String, String) = (
        "Click + or press ⌘N to add a URL. Drop a download in Firefox/Safari to send it to SDM. From a terminal: `sdmdm add <url>`. Drag a URL onto this window.",
        "点击 + 或按 ⌘N 添加 URL。把 Firefox/Safari 的下载发到隙间下载。终端命令：`sdmdm add <url>`。直接把 URL 拖进窗口。")
    static let emptyTipLang: (String, String) = (
        "Settings → Language to switch to 中文.", "设置 → 语言 切换到 English。")

    // Tabs
    static let tabActive: (String, String) = (
        "Active", "下载中")
    static let tabHistory: (String, String) = (
        "History", "历史")
    static let tabSchedule: (String, String) = (
        "Schedule", "计划")

    // States
    static let stateDownloading: (String, String) = (
        "Downloading", "下载中")
    static let stateCompleted: (String, String) = (
        "Completed", "已完成")
    static let statePaused: (String, String) = (
        "Paused", "已暂停")
    static let stateFailed: (String, String) = (
        "Failed", "失败")
    static let stateCancelled: (String, String) = (
        "Cancelled", "已取消")
    static let stateQueued: (String, String) = (
        "Queued", "等待中")
    static let stateAwaitingLink: (String, String) = (
        "Awaiting link", "等待新链接")
    static let stateUnknown: (String, String) = (
        "Unknown", "未知")

    // Per-row actions
    static let rowPause: (String, String) = (
        "Pause", "暂停")
    static let rowResume: (String, String) = (
        "Resume", "继续")
    static let rowCancel: (String, String) = (
        "Cancel", "取消")
    static let rowRetry: (String, String) = (
        "Retry", "重试")
    static let rowOpen: (String, String) = (
        "Open", "打开")
    static let rowReveal: (String, String) = (
        "Reveal in Finder", "在访达中显示")
    static let rowRemove: (String, String) = (
        "Remove from list", "从列表移除")
    static let rowDelete: (String, String) = (
        "Delete file", "删除文件")
    static let rowCopyURL: (String, String) = (
        "Copy URL", "复制 URL")
    static let rowOpenSource: (String, String) = (
        "Open source page", "打开来源页")
    static let rowLimitSpeed: (String, String) = (
        "Limit speed…", "限速…")
    static let rowChecksum: (String, String) = (
        "Compute checksum", "计算校验和")
}

/// Resolve the current locale tag to a `Strings` locale.
private func resolve(_ tag: LocaleTag) -> Catalog.Pair {
    switch tag {
    case .en:     return .en
    case .zhHans: return .zh
    case .system: return .en   // default until overridden
    }
}

/// Per-key accessor. Read from `Strings.current.<key>` once you've
/// taken `let S = Strings.current` in `body`.
public struct Strings {
    public let locale: LocaleTag
    private let pair: Catalog.Pair
    public init(locale: LocaleTag) {
        self.locale = locale
        self.pair = resolve(locale)
    }

    fileprivate func pick(_ t: (String, String)) -> String {
        switch pair {
        case .en: return t.0
        case .zh: return t.1
        }
    }

    // MARK: - Menu
    public var menuOpenWindow: String { pick(Catalog.menuOpenWindow) }
    public var menuDownloads:   String { pick(Catalog.menuDownloads) }
    public var menuSettings:    String { pick(Catalog.menuSettings) }
    public var menuQuit:        String { pick(Catalog.menuQuit) }

    // Main window
    public var mainDownloads: String { pick(Catalog.mainDownloads) }

    // Settings → Defaults
    public var settingsSectionDefaults: String { pick(Catalog.settingsSectionDefaults) }
    public var settingsDefaultThreadsLabel: String { pick(Catalog.settingsDefaultThreadsLabel) }
    public var settingsDefaultThreadsHint: String { pick(Catalog.settingsDefaultThreadsHint) }
    public var settingsBandwidth: String { pick(Catalog.settingsBandwidth) }
    public var settingsBandwidthHint: String { pick(Catalog.settingsBandwidthHint) }
    public var settingsKbps: String { pick(Catalog.settingsKbps) }

    // Settings → Categories
    public var settingsSectionCategories: String { pick(Catalog.settingsSectionCategories) }
    public var settingsCategoriesLink: String { pick(Catalog.settingsCategoriesLink) }
    public var settingsCategoriesPlaceholder: String { pick(Catalog.settingsCategoriesPlaceholder) }
    public var settingsSiteRulesLink: String { pick(Catalog.settingsSiteRulesLink) }

    // Settings → Startup
    public var settingsSectionStartup: String { pick(Catalog.settingsSectionStartup) }
    public var settingsOpenAtLogin: String { pick(Catalog.settingsOpenAtLogin) }
    public var settingsUpdateChannel: String { pick(Catalog.settingsUpdateChannel) }
    public var settingsUpdateStable: String { pick(Catalog.settingsUpdateStable) }
    public var settingsUpdatePrerelease: String { pick(Catalog.settingsUpdatePrerelease) }

    // Settings → General
    public var settingsSectionGeneral: String { pick(Catalog.settingsSectionGeneral) }
    public var settingsLanguage: String { pick(Catalog.settingsLanguage) }
    public var settingsLanguageHint: String { pick(Catalog.settingsLanguageHint) }
    public var settingsLanguageSystem: String { pick(Catalog.settingsLanguageSystem) }
    public var settingsLanguageEn: String { pick(Catalog.settingsLanguageEn) }
    public var settingsLanguageZh: String { pick(Catalog.settingsLanguageZh) }

    // Settings → Sidebar
    public var settingsSidebarGeneral: String { pick(Catalog.settingsSidebarGeneral) }
    public var settingsSidebarDefaults: String { pick(Catalog.settingsSidebarDefaults) }
    public var settingsSidebarCategories: String { pick(Catalog.settingsSidebarCategories) }
    public var settingsSidebarSiteRules: String { pick(Catalog.settingsSidebarSiteRules) }
    public var settingsSidebarStartup: String { pick(Catalog.settingsSidebarStartup) }
    public var settingsSidebarAbout: String { pick(Catalog.settingsSidebarAbout) }
    public var settingsAboutTitle: String { pick(Catalog.settingsAboutTitle) }
    public var settingsAboutVersion: String { pick(Catalog.settingsAboutVersion) }
    public var settingsAboutLicense: String { pick(Catalog.settingsAboutLicense) }
    public var settingsAboutCopyright: String { pick(Catalog.settingsAboutCopyright) }

    // Toolbar
    public var toolbarAddURL: String { pick(Catalog.toolbarAddURL) }
    public var toolbarPauseAll: String { pick(Catalog.toolbarPauseAll) }
    public var toolbarResumeAll: String { pick(Catalog.toolbarResumeAll) }
    public var toolbarClearCompleted: String { pick(Catalog.toolbarClearCompleted) }

    // Add sheet
    public var addSheetTitle: String { pick(Catalog.addSheetTitle) }
    public var addSheetSubtitle: String { pick(Catalog.addSheetSubtitle) }
    public var addSheetURLField: String { pick(Catalog.addSheetURLField) }
    public var addSheetFilenameField: String { pick(Catalog.addSheetFilenameField) }
    public var addSheetFilenameHint: String { pick(Catalog.addSheetFilenameHint) }
    public var addSheetAdd: String { pick(Catalog.addSheetAdd) }
    public var addSheetCancel: String { pick(Catalog.addSheetCancel) }
    public var addSheetErrorBadURL: String { pick(Catalog.addSheetErrorBadURL) }

    // Empty
    public var emptyTitle: String { pick(Catalog.emptyTitle) }
    public var emptyHint: String { pick(Catalog.emptyHint) }
    public var emptyTipLang: String { pick(Catalog.emptyTipLang) }

    // Tabs
    public var tabActive: String { pick(Catalog.tabActive) }
    public var tabHistory: String { pick(Catalog.tabHistory) }
    public var tabSchedule: String { pick(Catalog.tabSchedule) }

    // States
    public var stateDownloading:  String { pick(Catalog.stateDownloading) }
    public var stateCompleted:    String { pick(Catalog.stateCompleted) }
    public var statePaused:       String { pick(Catalog.statePaused) }
    public var stateFailed:       String { pick(Catalog.stateFailed) }
    public var stateCancelled:    String { pick(Catalog.stateCancelled) }
    public var stateQueued:       String { pick(Catalog.stateQueued) }
    public var stateAwaitingLink: String { pick(Catalog.stateAwaitingLink) }
    public var stateUnknown:      String { pick(Catalog.stateUnknown) }

    // Per-row
    public var rowPause:      String { pick(Catalog.rowPause) }
    public var rowResume:     String { pick(Catalog.rowResume) }
    public var rowCancel:     String { pick(Catalog.rowCancel) }
    public var rowRetry:      String { pick(Catalog.rowRetry) }
    public var rowOpen:       String { pick(Catalog.rowOpen) }
    public var rowReveal:     String { pick(Catalog.rowReveal) }
    public var rowRemove:     String { pick(Catalog.rowRemove) }
    public var rowDelete:     String { pick(Catalog.rowDelete) }
    public var rowCopyURL:    String { pick(Catalog.rowCopyURL) }
    public var rowOpenSource: String { pick(Catalog.rowOpenSource) }
    public var rowLimitSpeed: String { pick(Catalog.rowLimitSpeed) }
    public var rowChecksum:   String { pick(Catalog.rowChecksum) }

    // MARK: - Format helpers
    public func settingsDefaultThreadsValue(_ n: Int) -> String {
        String(format: "%d", n)
    }
    public func threadUnit(_ n: Int) -> String {
        switch pair {
        case .en: return n == 1 ? "thread" : "threads"
        case .zh: return "个线程"
        }
    }
}

/// Provide a SwiftUI `EnvironmentKey` so any view can read the
/// current `Strings` from the environment.
public struct StringsKey: EnvironmentKey {
    public static let defaultValue: Strings = Strings(locale: .system)
}
public extension EnvironmentValues {
    var strings: Strings {
        get { self[StringsKey.self] }
        set { self[StringsKey.self] = newValue }
    }
}
