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
        "Quit Super Download Manager", "退出 Super Download Manager")

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
    static let settingsOpenAtLoginHint: (String, String) = (
        "Registers the app with macOS as a login item.",
        "向 macOS 注册为登录项。")
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
        "Super Download Manager", "Super Download Manager")
    static let settingsAboutVersion: (String, String) = (
        "Version", "版本")
    static let settingsAboutLicense: (String, String) = (
        "MIT License", "MIT 协议")
    static let settingsAboutCopyright: (String, String) = (
        "© 2026 Skyc8266", "© 2026 Skyc8266")

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
        "输入 URL，SDM 将立即开始多线程下载。")
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
        "点击 + 或按 ⌘N 添加 URL。把 Firefox/Safari 的下载发到 SDM。终端命令：`sdmdm add <url>`。直接把 URL 拖进窗口。")
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

    // Task detail sheet
    static let detailShowChunks: (String, String) = (
        "Show chunks", "显示分段")
    static let detailHideChunks: (String, String) = (
        "Hide chunks", "隐藏分段")
    static let detailCopyJSON: (String, String) = (
        "Copy all as JSON", "复制全部为 JSON")
    static let detailTaskGone: (String, String) = (
        "Task no longer exists", "任务已不存在")
    static let detailClose: (String, String) = (
        "Close", "关闭")
    static let detailDestination: (String, String) = (
        "Destination", "保存路径")
    static let detailSize: (String, String) = (
        "Size", "大小")
    static let detailSpeed: (String, String) = (
        "Speed", "速度")
    static let detailETA: (String, String) = (
        "ETA", "剩余时间")
    static let detailPerTaskLimit: (String, String) = (
        "Per-task limit", "单任务限速")
    static let detailError: (String, String) = (
        "Error", "错误")

    // v1.0.7: Settings window title (was reusing
    // settingsAboutTitle, which is the app name, not the
    // window title).
    static let settingsWindowTitle: (String, String) = (
        "SDM Settings", "SDM 设置")
    static let settingsBehaviour: (String, String) = (
        "Behaviour", "行为")
    static let settingsBehaviourHint: (String, String) = (
        "These take effect immediately.", "更改立即生效。")
    static let settingsCategoryFolderHint: (String, String) = (
        "Downloads land in ~/Downloads/<Category>/ when classification is on.",
        "启用分类后，下载文件会落到 ~/Downloads/<类别>/ 目录。")
    static let settingsCategoriesHint: (String, String) = (
        "Add / remove extensions in v1.0.5+. The v1.0 list lives in the code; an in-app editor arrives later.",
        "在 v1.0.5+ 中可增删扩展名。v1.0 列表在代码里，应用内编辑器后续版本加入。")

    // v1.0.7: missing History labels
    static let historyNoEntries: (String, String) = (
        "No history yet", "暂无历史")
    static let historyExplanation: (String, String) = (
        "Completed and failed downloads show up here. Right-click for actions.",
        "已完成和失败的下载会显示在这里。右键可执行操作。")
    static let historyEntriesCount: (String, String) = (
        "\(0) entries", "\(0) 条记录")
    static let historyFilter: (String, String) = (
        "Filter", "筛选")
    static let historyClearAll: (String, String) = (
        "Clear all", "清空")
    static let historyColumnDate: (String, String) = (
        "Date", "时间")
    static let historyColumnFile: (String, String) = (
        "File", "文件")
    static let historyColumnSize: (String, String) = (
        "Size", "大小")
    static let historyColumnState: (String, String) = (
        "State", "状态")
    static let historyActionRedownload: (String, String) = (
        "Re-download", "重新下载")
    static let historyActionOpenSource: (String, String) = (
        "Open source page", "打开来源页")
    static let historyActionReveal: (String, String) = (
        "Reveal in Finder", "在访达中显示")
    static let historyActionRemove: (String, String) = (
        "Remove", "移除")

    // v1.0.7: Site rules
    static let siteRulesTitle: (String, String) = (
        "Site Rules", "站点规则")
    static let siteRulesExplanation: (String, String) = (
        "Match downloads by hostname (suffix match). Applied rules add Referer / User-Agent / Cookie / Basic Auth headers.",
        "按主机名匹配下载（后缀匹配）。命中的规则会附加 Referer / User-Agent / Cookie / Basic Auth 头。")
    static let siteRulesAddHint: (String, String) = (
        "Add a rule", "添加规则")
    static let siteRulesAdd: (String, String) = (
        "Add", "添加")
    static let siteRulesClearAll: (String, String) = (
        "Clear All", "全部清除")
    static let siteRulesExisting: (String, String) = (
        "Existing rules", "已有规则")
    static let siteRulesEmpty: (String, String) = (
        "No rules yet.", "暂无规则。")
    static let siteRulesHost: (String, String) = (
        "Host (e.g. example.com)", "主机名（如 example.com）")
    static let siteRulesReferer: (String, String) = (
        "Referer", "Referer")
    static let siteRulesUserAgent: (String, String) = (
        "User-Agent", "User-Agent")
    static let siteRulesCookie: (String, String) = (
        "Cookie", "Cookie")
    static let siteRulesBasicAuthUser: (String, String) = (
        "Basic Auth user", "Basic Auth 用户名")
    static let siteRulesBasicAuthPassword: (String, String) = (
        "Basic Auth password", "Basic Auth 密码")
    static let siteRulesEnabled: (String, String) = (
        "Enabled", "已启用")
    static let siteRulesRefererShort: (String, String) = (
        "Referer: \(0)", "Referer：\(0)")
    static let siteRulesUserAgentShort: (String, String) = (
        "UA: \(0)", "UA：\(0)")
    static let siteRulesCookieShort: (String, String) = (
        "Cookie: \(0)", "Cookie：\(0)")
    static let siteRulesBasicAuthShort: (String, String) = (
        "Basic: \(0)", "Basic：\(0)")

    // v1.0.7: Schedule
    static let scheduleTitle: (String, String) = (
        "Schedule", "计划")
    static let scheduleAdd: (String, String) = (
        "Schedule", "添加")
    static let scheduleEmpty: (String, String) = (
        "No scheduled downloads", "暂无计划任务")
    static let scheduleHint: (String, String) = (
        "Pick a URL and a future time above.", "在上方填入 URL 和未来时间。")
    static let scheduleURL: (String, String) = (
        "URL", "URL")
    static let scheduleFireAt: (String, String) = (
        "Fire at", "触发时间")
    static let scheduleNote: (String, String) = (
        "Note (optional)", "备注（可选）")
    static let scheduleColumnFireAt: (String, String) = (
        "Fire at", "触发时间")
    static let scheduleColumnURL: (String, String) = (
        "URL", "URL")
    static let scheduleColumnNote: (String, String) = (
        "Note", "备注")
    static let scheduleRemove: (String, String) = (
        "Remove", "移除")

    // v1.0.7: Expired link banner
    static let expiredRefresh: (String, String) = (
        "Refresh link", "刷新链接")

    // v1.0.7: List view short labels
    static let listShowDetails: (String, String) = (
        "Show details", "查看详情")
    static let listEmDash: (String, String) = (
        "—", "—")
    static let listAppName: (String, String) = (
        "Super Download Manager", "Super Download Manager")
    static let listProgressPercent: (String, String) = (
        "\(0)%", "\(0)%")
    static let listKBPerSec: (String, String) = (
        "\(0) KB/s", "\(0) KB/s")
    static let listChunkPrefix: (String, String) = (
        "chunk #\(0)", "分段 #\(0)")
    static let listSha256: (String, String) = (
        "SHA-256: \(0)", "SHA-256：\(0)")

    // v1.0.7: Clipboard confirm sheet
    static let clipboardSheetTitle: (String, String) = (
        "Download from clipboard?", "从剪贴板下载？")
    static let clipboardSheetSubtitle: (String, String) = (
        "This URL looks like a downloadable file. Confirm or skip.",
        "该链接像是可下载文件。确认或跳过。")
    static let clipboardCategory: (String, String) = (
        "Save to category", "保存到分类")
    static let clipboardSavePath: (String, String) = (
        "Save path", "保存路径")
    static let clipboardChoose: (String, String) = (
        "Choose…", "选择…")
    static let clipboardMaxThreads: (String, String) = (
        "Threads for this download", "本下载的线程数")
    static let clipboardMaxThreadsHint: (String, String) = (
        "1 – 128", "1 – 128")
    static let clipboardDisableToggle: (String, String) = (
        "Stop monitoring clipboard", "停止监听剪贴板")
    static let clipboardSkip: (String, String) = (
        "Skip", "跳过")
    static let clipboardDownload: (String, String) = (
        "Download", "下载")

    // v1.0.7: Settings toggle labels (Behaviour section)
    static let settingsToggleAutoCategorize: (String, String) = (
        "Auto-categorize downloads", "下载自动分类")
    static let settingsToggleClipboardCapture: (String, String) = (
        "Clipboard URL auto-capture", "剪贴板链接自动识别")
    static let settingsToggleSystemNotifications: (String, String) = (
        "System notifications on complete", "完成时系统通知")
    static let settingsCategorySection: (String, String) = (
        "Categories", "分类")

    // v1.1.2: Dock visibility toggle.
    static let settingsToggleHideDockWhenWindowClosed: (String, String) = (
        "Hide dock icon when window is closed",
        "关闭窗口后隐藏 Dock 图标")
    static let settingsHideDockHint: (String, String) = (
        "Keep a menu-bar icon only. The dock icon reappears while a window is open.",
        "仅保留菜单栏图标。打开窗口期间，Dock 图标会重新显示。")
    static let settingsDockHeader: (String, String) = (
        "Window & Dock", "窗口与 Dock")
}

/// Resolve the current locale tag to a `Strings` locale. v1.1.2:
/// `system` is now resolved from the OS preferred language list
/// instead of always returning English (the v1.0.7 bug — picking
/// "Follow system" gave English unless the user manually changed
/// languages; switching languages via Settings → General → Region
/// didn't update the UI either).
private func resolve(_ tag: LocaleTag) -> Catalog.Pair {
    switch tag {
    case .en:     return .en
    case .zhHans: return .zh
    case .system: return resolveSystemLocale()
    }
}

/// Decide whether the OS default locale should render as 中文 or
/// English. Checks `Bundle.main.preferredLocalizations` first
/// (Apple-recommended API — accounts for the user's actual app
/// language preferences), then falls back to `Locale.preferredLanguages`.
private func resolveSystemLocale() -> Catalog.Pair {
    let preferred = Bundle.main.preferredLocalizations
        .isEmpty
        ? Locale.preferredLanguages
        : Bundle.main.preferredLocalizations
    for tag in preferred {
        let lower = tag.lowercased()
        // Match Chinese (Simplified / Traditional / regional
        // variants) — any 简中 user is most likely expecting
        // 简体中文. Traditional / HK / TW users would normally
        // pick 中文 explicitly; the UI only ships 简中, so route
        // everything `zh*` here.
        if lower.hasPrefix("zh") { return .zh }
        if lower.hasPrefix("en") { return .en }
    }
    // No positive match — default to English (matches the
    // developmentLanguage of the bundle).
    return .en
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
    public var settingsOpenAtLoginHint: String { pick(Catalog.settingsOpenAtLoginHint) }
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

    // Task detail sheet
    public var detailShowChunks:   String { pick(Catalog.detailShowChunks) }
    public var detailHideChunks:   String { pick(Catalog.detailHideChunks) }
    public var detailCopyJSON:     String { pick(Catalog.detailCopyJSON) }
    public var detailTaskGone:     String { pick(Catalog.detailTaskGone) }
    public var detailClose:        String { pick(Catalog.detailClose) }
    public var detailDestination:  String { pick(Catalog.detailDestination) }
    public var detailSize:         String { pick(Catalog.detailSize) }
    public var detailSpeed:        String { pick(Catalog.detailSpeed) }
    public var detailETA:          String { pick(Catalog.detailETA) }
    public var detailPerTaskLimit: String { pick(Catalog.detailPerTaskLimit) }
    public var detailError:        String { pick(Catalog.detailError) }

    public func detailShowChunks(count: Int) -> String {
        switch pair {
        case .en: return "Show chunks (\(count))"
        case .zh: return "显示分段 (\(count))"
        }
    }

    // v1.0.7: Settings window title
    public var settingsWindowTitle:        String { pick(Catalog.settingsWindowTitle) }
    public var settingsBehaviour:          String { pick(Catalog.settingsBehaviour) }
    public var settingsBehaviourHint:      String { pick(Catalog.settingsBehaviourHint) }
    public var settingsCategoryFolderHint: String { pick(Catalog.settingsCategoryFolderHint) }
    public var settingsCategoriesHint:     String { pick(Catalog.settingsCategoriesHint) }

    // v1.0.7: History
    public var historyNoEntries:        String { pick(Catalog.historyNoEntries) }
    public var historyExplanation:      String { pick(Catalog.historyExplanation) }
    public var historyFilter:           String { pick(Catalog.historyFilter) }
    public var historyClearAll:         String { pick(Catalog.historyClearAll) }
    public var historyColumnDate:       String { pick(Catalog.historyColumnDate) }
    public var historyColumnFile:       String { pick(Catalog.historyColumnFile) }
    public var historyColumnSize:       String { pick(Catalog.historyColumnSize) }
    public var historyColumnState:      String { pick(Catalog.historyColumnState) }
    public var historyActionRedownload: String { pick(Catalog.historyActionRedownload) }
    public var historyActionOpenSource: String { pick(Catalog.historyActionOpenSource) }
    public var historyActionReveal:     String { pick(Catalog.historyActionReveal) }
    public var historyActionRemove:     String { pick(Catalog.historyActionRemove) }
    public func historyEntriesCount(_ n: Int) -> String {
        switch pair {
        case .en: return "\(n) entries"
        case .zh: return "\(n) 条记录"
        }
    }

    // v1.0.7: Site rules
    public var siteRulesTitle:           String { pick(Catalog.siteRulesTitle) }
    public var siteRulesExplanation:     String { pick(Catalog.siteRulesExplanation) }
    public var siteRulesAddHint:         String { pick(Catalog.siteRulesAddHint) }
    public var siteRulesAdd:             String { pick(Catalog.siteRulesAdd) }
    public var siteRulesClearAll:        String { pick(Catalog.siteRulesClearAll) }
    public var siteRulesExisting:        String { pick(Catalog.siteRulesExisting) }
    public var siteRulesEmpty:           String { pick(Catalog.siteRulesEmpty) }
    public var siteRulesHost:            String { pick(Catalog.siteRulesHost) }
    public var siteRulesReferer:         String { pick(Catalog.siteRulesReferer) }
    public var siteRulesUserAgent:       String { pick(Catalog.siteRulesUserAgent) }
    public var siteRulesCookie:          String { pick(Catalog.siteRulesCookie) }
    public var siteRulesBasicAuthUser:   String { pick(Catalog.siteRulesBasicAuthUser) }
    public var siteRulesBasicAuthPassword: String { pick(Catalog.siteRulesBasicAuthPassword) }
    public var siteRulesEnabled:         String { pick(Catalog.siteRulesEnabled) }
    public func siteRulesRefererShort(_ v: String) -> String {
        switch pair {
        case .en: return "Referer: \(v)"
        case .zh: return "Referer：\(v)"
        }
    }
    public func siteRulesUserAgentShort(_ v: String) -> String {
        switch pair {
        case .en: return "UA: \(v.prefix(40))…"
        case .zh: return "UA：\(v.prefix(40))…"
        }
    }
    public func siteRulesCookieShort(_ v: String) -> String {
        switch pair {
        case .en: return "Cookie: \(v.prefix(40))…"
        case .zh: return "Cookie：\(v.prefix(40))…"
        }
    }
    public func siteRulesBasicAuthShort(_ v: String) -> String {
        switch pair {
        case .en: return "Basic: \(v)"
        case .zh: return "Basic：\(v)"
        }
    }

    // v1.0.7: Schedule
    public var scheduleTitle:    String { pick(Catalog.scheduleTitle) }
    public var scheduleAdd:      String { pick(Catalog.scheduleAdd) }
    public var scheduleEmpty:    String { pick(Catalog.scheduleEmpty) }
    public var scheduleHint:     String { pick(Catalog.scheduleHint) }
    public var scheduleURL:      String { pick(Catalog.scheduleURL) }
    public var scheduleFireAt:   String { pick(Catalog.scheduleFireAt) }
    public var scheduleNote:     String { pick(Catalog.scheduleNote) }
    public var scheduleColumnFireAt: String { pick(Catalog.scheduleColumnFireAt) }
    public var scheduleColumnURL:    String { pick(Catalog.scheduleColumnURL) }
    public var scheduleColumnNote:  String { pick(Catalog.scheduleColumnNote) }
    public var scheduleRemove:   String { pick(Catalog.scheduleRemove) }

    // v1.0.7: Expired link banner
    public var expiredRefresh:   String { pick(Catalog.expiredRefresh) }

    // v1.0.7: List view short labels
    public var listShowDetails:  String { pick(Catalog.listShowDetails) }
    public var listEmDash:       String { pick(Catalog.listEmDash) }
    public var listAppName:      String { pick(Catalog.listAppName) }
    public func listProgressPercent(_ n: Int) -> String {
        "\(n)%"
    }
    public func listKBPerSec(_ n: Int) -> String {
        switch pair {
        case .en: return "\(n) KB/s"
        case .zh: return "\(n) KB/s"
        }
    }
    public func listChunkPrefix(_ n: Int) -> String {
        switch pair {
        case .en: return "chunk #\(n)"
        case .zh: return "分段 #\(n)"
        }
    }
    public func listSha256(_ prefix: String) -> String {
        switch pair {
        case .en: return "SHA-256: \(prefix)…"
        case .zh: return "SHA-256：\(prefix)…"
        }
    }

    // v1.0.7: Clipboard confirm sheet
    public var clipboardSheetTitle:        String { pick(Catalog.clipboardSheetTitle) }
    public var clipboardSheetSubtitle:     String { pick(Catalog.clipboardSheetSubtitle) }
    public var clipboardCategory:          String { pick(Catalog.clipboardCategory) }
    public var clipboardSavePath:          String { pick(Catalog.clipboardSavePath) }
    public var clipboardChoose:            String { pick(Catalog.clipboardChoose) }
    public var clipboardMaxThreads:        String { pick(Catalog.clipboardMaxThreads) }
    public var clipboardMaxThreadsHint:    String { pick(Catalog.clipboardMaxThreadsHint) }
    public var clipboardDisableToggle:     String { pick(Catalog.clipboardDisableToggle) }
    public var clipboardSkip:              String { pick(Catalog.clipboardSkip) }
    public var clipboardDownload:          String { pick(Catalog.clipboardDownload) }

    // v1.0.7: Settings toggle labels
    public var settingsToggleAutoCategorize:    String { pick(Catalog.settingsToggleAutoCategorize) }
    public var settingsToggleClipboardCapture: String { pick(Catalog.settingsToggleClipboardCapture) }
    public var settingsToggleSystemNotifications: String { pick(Catalog.settingsToggleSystemNotifications) }
    public var settingsCategorySection:         String { pick(Catalog.settingsCategorySection) }

    // v1.1.2: Dock visibility
    public var settingsToggleHideDockWhenWindowClosed: String { pick(Catalog.settingsToggleHideDockWhenWindowClosed) }
    public var settingsHideDockHint:         String { pick(Catalog.settingsHideDockHint) }
    public var settingsDockHeader:           String { pick(Catalog.settingsDockHeader) }

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
