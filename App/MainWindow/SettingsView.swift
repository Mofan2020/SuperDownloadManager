import SwiftUI
import AppKit
import SuperDownloadManagerCore

/// SettingsView — v1.0.5 rewrite. NavigationStack left-rail
/// + detail-pane layout, bilingual in-code catalog (`Strings`),
/// and live locale switcher (no app restart).
///
/// v1.0.4 had a single Form with three problems:
///   1. `Text(L10n.settingsDefaultThreads)` rendered the literal
///      `%lld` because SwiftUI's `LocalizedStringKey` doesn't
///      substitute printf-style args.
///   2. `NavigationLink` inside a Form shows up greyed out in
///      macOS 14 — the row is read-only when the destination
///      uses the same data scope, and the affordance for
///      "click to drill in" was unclear.
///   3. Switching language required an app restart.
///
/// v1.0.5 fixes all three: bilingual dictionary in `Strings`
/// (no `%lld`), explicit `NavigationSplitView` with a left rail
/// (so the "drill in" affordance is the whole row), and a
/// `@AppStorage` tag read at every body re-evaluation so
/// the picker takes effect on the next frame.
struct SettingsView: View {
    let engine: SDMEngine?
    @AppStorage("sdm.threads.default") private var defaultThreads: Int = 32
    @AppStorage("sdm.bandwidth.bytesPerSec") private var bandwidth: Int = 0
    @AppStorage("sdm.openAtLogin") private var openAtLogin: Bool = false
    @AppStorage("sdm.updateChannel") private var updateChannel: String = "stable"
    @AppStorage("sdm.locale") private var localeTag: String = LocaleTag.system.rawValue
    @AppStorage("sdm.classify.enabled") private var classifyEnabled: Bool = true
    @AppStorage("sdm.capture.clipboard") private var clipboardCapture: Bool = true
    @AppStorage("sdm.notify.onComplete") private var notifyOnComplete: Bool = true
    @AppStorage("sdm.hideDockWhenWindowClosed") private var hideDockWhenWindowClosed: Bool = false
    @AppStorage("sdm.toolbar.showLabels") private var showToolbarLabels: Bool = true

    init(engine: SDMEngine? = nil) {
        self.engine = engine
    }

    private var strings: Strings { Strings(locale: LocaleTag(rawValue: localeTag) ?? .system) }
    private var locale: Locale {
        switch LocaleTag(rawValue: localeTag) ?? .system {
        case .zhHans: return Locale(identifier: "zh-Hans")
        case .en:     return Locale(identifier: "en")
        case .system: return .autoupdatingCurrent
        }
    }

    var body: some View {
        NavigationSplitView {
            List(SettingsSection.allCases, selection: $section) { sec in
                Label(label(for: sec), systemImage: icon(for: sec))
                    .tag(sec)
            }
            .navigationSplitViewColumnWidth(min: 160, ideal: 180, max: 220)
        } detail: {
            detail
                .frame(minWidth: 480, minHeight: 380)
        }
        .environment(\.locale, locale)
        .environment(\.strings, strings)
        .navigationTitle(strings.settingsWindowTitle)
        .frame(width: 720, height: 480)
    }

    @State private var section: SettingsSection = .general

    enum SettingsSection: String, CaseIterable, Identifiable {
        case general, defaults, categories, siteRules, startup, about
        var id: String { rawValue }
    }

    @ViewBuilder
    private var detail: some View {
        switch section {
        case .general:    generalSection
        case .defaults:   defaultsSection
        case .categories: CategoryRulesEditor()
        case .siteRules:  SiteRulesView()
        case .startup:    startupSection
        case .about:      aboutSection
        }
    }

    private func label(for s: SettingsSection) -> String {
        let S = strings
        switch s {
        case .general:    return S.settingsSidebarGeneral
        case .defaults:   return S.settingsSidebarDefaults
        case .categories: return S.settingsSidebarCategories
        case .siteRules:  return S.settingsSidebarSiteRules
        case .startup:    return S.settingsSidebarStartup
        case .about:      return S.settingsSidebarAbout
        }
    }

    private func icon(for s: SettingsSection) -> String {
        switch s {
        case .general:    return "gearshape"
        case .defaults:   return "slider.horizontal.3"
        case .categories: return "folder.badge.gearshape"
        case .siteRules:  return "globe"
        case .startup:    return "power"
        case .about:      return "info.circle"
        }
    }

    // MARK: - Sections

    private var generalSection: some View {
        let S = strings
        return Form {
            Section {
                Picker(S.settingsLanguage, selection: $localeTag) {
                    Text(S.settingsLanguageSystem).tag(LocaleTag.system.rawValue)
                    Text(S.settingsLanguageEn).tag(LocaleTag.en.rawValue)
                    Text(S.settingsLanguageZh).tag(LocaleTag.zhHans.rawValue)
                }
                .pickerStyle(.inline)
                .labelsHidden()
            } header: {
                Text(S.settingsLanguage)
            } footer: {
                Text(S.settingsLanguageHint)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section {
                Toggle(S.settingsToggleAutoCategorize, isOn: $classifyEnabled)
                Toggle(S.settingsToggleClipboardCapture, isOn: $clipboardCapture)
                Toggle(S.settingsToggleSystemNotifications, isOn: $notifyOnComplete)
            } header: {
                Text(S.settingsBehaviour)
            } footer: {
                Text(S.settingsBehaviourHint)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section {
                Toggle(S.settingsToggleHideDockWhenWindowClosed,
                       isOn: $hideDockWhenWindowClosed)
            } header: {
                Text(S.settingsDockHeader)
            } footer: {
                Text(S.settingsHideDockHint)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section {
                Toggle(S.settingsToggleShowToolbarLabels,
                       isOn: $showToolbarLabels)
            } header: {
                Text(S.settingsToolbarHeader)
            } footer: {
                Text(S.settingsShowToolbarLabelsHint)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .padding(20)
    }

    private var defaultsSection: some View {
        let S = strings
        return Form {
            Section {
                DefaultThreadsField(value: $defaultThreads)
                    .environment(\.strings, S)
            } header: {
                Text(S.settingsSectionDefaults)
            } footer: {
                Text(S.settingsDefaultThreadsHint)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section {
                BandwidthField(value: $bandwidth)
                    .environment(\.strings, S)
            } header: {
                Text(S.settingsBandwidth)
            } footer: {
                Text(S.settingsBandwidthHint)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .padding(20)
    }

    private var startupSection: some View {
        let S = strings
        return Form {
            Section {
                Toggle(S.settingsOpenAtLogin, isOn: openAtLoginBinding)
            } header: {
                Text(S.settingsSectionStartup)
            } footer: {
                Text(S.settingsOpenAtLoginHint)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section {
                Picker(S.settingsUpdateChannel, selection: $updateChannel) {
                    Text(S.settingsUpdateStable).tag("stable")
                    Text(S.settingsUpdatePrerelease).tag("pre")
                }
                .pickerStyle(.inline)
                .labelsHidden()
            }
        }
        .formStyle(.grouped)
        .padding(20)
    }

    /// v1.1.2: wraps the `openAtLogin` AppStorage so flipping the
    /// Toggle immediately registers or un-registers the app with
    /// SMAppService. v1.0.4 only wrote the Bool — the helper was
    /// never called, so the toggle did nothing.
    private var openAtLoginBinding: Binding<Bool> {
        Binding(
            get: { openAtLogin },
            set: { newValue in
                openAtLogin = newValue
                LaunchAtLogin.setEnabled(newValue)
            }
        )
    }

    private var aboutSection: some View {
        let S = strings
        return VStack(spacing: 12) {
            Image(nsImage: NSImage(named: "AppIcon") ?? NSImage())
                .resizable()
                .frame(width: 96, height: 96)
            Text(S.settingsAboutTitle).font(.title2.weight(.semibold))
            HStack {
                Text(S.settingsAboutVersion)
                Text(Bundle.main.infoDictionary?["CFBundleShortVersionString"]
                     as? String ?? "?")
            }
            Text(S.settingsAboutLicense).foregroundStyle(.secondary)
            Text(S.settingsAboutCopyright).foregroundStyle(.tertiary).font(.caption)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(20)
    }
}

/// Default-threads field. v1.0.4 used a Stepper whose label
/// rendered the literal `%lld`. v1.0.5 uses Slider + TextField
/// so the value shows as a real number regardless of locale.
private struct DefaultThreadsField: View {
    @Binding var value: Int
    @Environment(\.strings) private var S

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(S.settingsDefaultThreadsLabel)
                Spacer()
                HStack(spacing: 4) {
                    Text(S.settingsDefaultThreadsValue(value))
                        .font(.body.monospacedDigit().weight(.semibold))
                    Text(S.threadUnit(value))
                        .foregroundStyle(.secondary)
                }
            }
            HStack(spacing: 12) {
                Slider(value: Binding(
                    get: { Double(value) },
                    set: { value = Int($0.rounded()) }),
                    in: 1...128, step: 1)
                TextField("", value: $value, format: .number)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 64)
                    .multilineTextAlignment(.trailing)
            }
        }
    }
}

private struct BandwidthField: View {
    @Binding var value: Int
    @Environment(\.strings) private var S

    var body: some View {
        HStack {
            Text(S.settingsBandwidth)
            Spacer()
            TextField("", value: $value, format: .number,
                      prompt: Text(S.settingsBandwidthHint))
                .textFieldStyle(.roundedBorder)
                .frame(width: 100)
                .multilineTextAlignment(.trailing)
            Text(S.settingsKbps)
        }
    }
}

/// Categories' default mapping surface — v1.0 keeps the JSON-driven
/// table in `UserDefaults` for the GUI's sake. Live editing of the
/// rule table lives in `CategoryRulesEditor`; the actual download
/// classification happens in P3.
struct CategoryRulesEditor: View {
    @Environment(\.strings) private var S
    @AppStorage("sdm.classify.enabled") private var classifyEnabled: Bool = true

    var body: some View {
        let S = self.S
        let cats: [(DownloadCategory, [String])] = [
            (.video,     ["mp4", "mov", "mkv", "webm", "avi", "flv", "m4v", "wmv", "ts"]),
            (.music,     ["mp3", "flac", "wav", "aac", "ogg", "m4a", "opus"]),
            (.pictures,  ["jpg", "jpeg", "png", "gif", "webp", "heic", "svg", "bmp", "tiff"]),
            (.archives,  ["zip", "7z", "rar", "tar", "gz", "bz2", "xz", "iso", "tgz"]),
            (.documents, ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
                          "txt", "md", "epub", "mobi", "rtf", "csv"]),
            (.software,  ["exe", "msi", "deb", "rpm", "apk", "app", "dmg", "pkg"]),
            (.other,     ["…"])
        ]
        return Form {
            Section {
                Toggle(S.settingsToggleAutoCategorize, isOn: $classifyEnabled)
            } header: {
                Text(S.settingsBehaviour)
            } footer: {
                Text(S.settingsCategoryFolderHint)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section {
                ForEach(cats, id: \.0) { (cat, exts) in
                    HStack(alignment: .top) {
                        Label(cat.folderName, systemImage: cat.iconName)
                            .frame(width: 130, alignment: .leading)
                            .foregroundStyle(cat.tint)
                        Text(exts.joined(separator: " "))
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            } header: {
                Text(S.settingsCategorySection)
            } footer: {
                Text(S.settingsCategoriesHint)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .padding(20)
    }
}
