import Foundation
import SwiftUI

/// L10n — bilingual EN / zh-Hans string catalog.
///
/// v1.0.1 introduces the bilingual switch. Strings are exposed
/// via `LocalizedStringKey` constants consumed by SwiftUI (which
/// looks them up through `Bundle.main.localizedString(forKey:value:table:)`);
/// the corresponding `Localizable.strings` files live under
/// `App/Resources/<lang>.lproj/Localizable.strings` and are bundled
/// via XcodeGen's `type: folder` resources entry.
///
/// Adding a new language:
///   1. Create `App/Resources/<new>.lproj/Localizable.strings`
///   2. Translate each key from the English .strings file
///   3. Add the new locale option to `SettingsView`'s language picker
///
/// macOS 26 picks the bundle language from the user's preferred
/// languages; the user can override per-app via the
/// `@AppStorage("sdm.locale")` setting in Settings → General.
public enum L10n {
    // Menu bar (MenuBarExtra) items
    public static var menuOpenWindow: LocalizedStringKey { "menu.openMainWindow" }
    public static var menuDownloads:   LocalizedStringKey { "menu.downloads" }
    public static var menuSettings:    LocalizedStringKey { "menu.settings" }
    public static var menuQuit:        LocalizedStringKey { "menu.quit" }

    // Main window
    public static var mainDownloads:   LocalizedStringKey { "window.main.title" }

    // Settings window sections
    public static var settingsSectionDefaults: LocalizedStringKey { "settings.section.defaults" }
    public static var settingsDefaultThreads:  LocalizedStringKey { "settings.defaultThreads" }
    public static var settingsBandwidth:       LocalizedStringKey { "settings.bandwidth" }
    public static var settingsBandwidthHint:   LocalizedStringKey { "settings.bandwidth.hint" }
    public static var settingsKbps:            LocalizedStringKey { "settings.kbps" }
    public static var settingsSectionCategories: LocalizedStringKey { "settings.section.categories" }
    public static var settingsCategoriesLink:    LocalizedStringKey { "settings.categories.link" }
    public static var settingsSectionStartup:    LocalizedStringKey { "settings.section.startup" }
    public static var settingsOpenAtLogin:        LocalizedStringKey { "settings.openAtLogin" }
    public static var settingsUpdateChannel:     LocalizedStringKey { "settings.updateChannel" }
    public static var settingsUpdateStable:      LocalizedStringKey { "settings.update.stable" }
    public static var settingsUpdatePrerelease:  LocalizedStringKey { "settings.update.prerelease" }
    public static var settingsSectionGeneral:     LocalizedStringKey { "settings.section.general" }
    public static var settingsLanguage:          LocalizedStringKey { "settings.language" }
    public static var settingsLanguageSystem:    LocalizedStringKey { "settings.language.system" }
    public static var settingsLanguageEn:        LocalizedStringKey { "settings.language.en" }
    public static var settingsLanguageZh:        LocalizedStringKey { "settings.language.zh-Hans" }

    // Categories placeholder
    public static var settingsCategoriesPlaceholder: LocalizedStringKey { "settings.categories.placeholder" }

    // Expired-link banner
    public static var bannerRefreshLink: LocalizedStringKey { "banner.refreshLink" }

    // States
    public static var stateDownloading:  LocalizedStringKey { "state.downloading" }
    public static var stateCompleted:    LocalizedStringKey { "state.completed" }
    public static var stateAwaitingLink: LocalizedStringKey { "state.awaiting_link" }
    public static var stateFailed:       LocalizedStringKey { "state.failed" }
    public static var statePaused:       LocalizedStringKey { "state.paused" }
    public static var stateUnknown:      LocalizedStringKey { "state.unknown" }
}

/// `String` variant for code paths where SwiftUI's
/// `LocalizedStringKey` doesn't fit (logging, alerts, dynamic
/// interpolation). SwiftUI's `LocalizedStringResource` covers both;
/// for now we provide a parallel string-based catalog so callers
/// without a SwiftUI `View` context can localize too.
public enum L10nStr {
    public static func menuOpenWindow(_ locale: String? = nil) -> String {
        loc("menu.openMainWindow", locale)
    }
    public static func bannerRefreshLink(_ locale: String? = nil) -> String {
        loc("banner.refreshLink", locale)
    }
    public static func state(_ key: String, _ locale: String? = nil) -> String {
        loc("state.\(key)", locale)
    }

    private static func loc(_ key: String, _ locale: String?) -> String {
        // The lookup target depends on the host process:
        // - From the app, `Bundle.main` is the .app bundle.
        // - From a unit test, `Bundle.main` is the .xctest bundle.
        // - `Bundle.main` does NOT walk linked frameworks
        //   automatically for NSLocalizedString by default in
        //   test contexts.
        // We always inspect every bundle that ships the requested
        // .lproj via `Bundle.allFrameworks` + `Bundle.main`. The
        // first match wins.
        if let tag = locale, tag != "system" {
            let candidates: [Bundle] = [Bundle.main] + Bundle.allFrameworks
            for bundle in candidates {
                if let p = bundle.path(forResource: tag, ofType: "lproj"),
                   let b = Bundle(path: p) {
                    let s = NSLocalizedString(key, bundle: b, comment: "")
                    if s != key { return s }
                }
            }
        }
        // Fallback path: walk ALL bundled .lproj files and look for
        // any English catalog. `Bundle.main.localizedString` would
        // also walk frameworks, but only if the framework's
        // resource bundle is reachable from `Bundle.main`; we
        // don't rely on that.
        let candidates: [Bundle] = [Bundle.main] + Bundle.allFrameworks
        for bundle in candidates {
            let s = bundle.localizedString(forKey: key, value: key, table: nil)
            if s != key { return s }
        }
        return key
    }
}
