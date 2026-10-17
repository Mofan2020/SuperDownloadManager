import SwiftUI

import SuperDownloadManagerCore

/// SettingsView — P2 settings window with bilingual EN/中文.
///
/// All user-visible strings go through `LocalizedStringKey`
/// constants in `L10n.swift`, looked up against
/// `App/Resources/{en,zh-Hans}.lproj/Localizable.strings`.
///
/// v1.0.1 added the language picker (Section "General"): users
/// can either follow the system locale (default) or pin to
/// English / 简体中文. The choice is persisted in `@AppStorage`
/// under `sdm.locale`; the App re-reads it on next launch.
struct SettingsView: View {
    let engine: SDMEngine?
    @AppStorage("sdm.threads.default") private var defaultThreads: Int = 32
    @AppStorage("sdm.bandwidth.bytesPerSec") private var bandwidth: Int = 0
    @AppStorage("sdm.openAtLogin") private var openAtLogin: Bool = false
    @AppStorage("sdm.updateChannel") private var updateChannel: String = "stable"
    @AppStorage("sdm.locale") private var localeTag: String = "system"

    init(engine: SDMEngine? = nil) {
        self.engine = engine
    }

    var body: some View {
        Form {
            Section {
                Stepper(L10n.settingsDefaultThreads,
                        value: $defaultThreads, in: 1...128, step: 1)
                BandwidthField(value: $bandwidth)
            } header: {
                Text(L10n.settingsSectionDefaults)
            }

            Section {
                NavigationLink(L10n.settingsCategoriesLink) {
                    CategoryRulesEditor()
                }
            } header: {
                Text(L10n.settingsSectionCategories)
            }

            Section {
                Toggle(L10n.settingsOpenAtLogin, isOn: $openAtLogin)
                Picker(L10n.settingsUpdateChannel, selection: $updateChannel) {
                    Text(L10n.settingsUpdateStable).tag("stable")
                    Text(L10n.settingsUpdatePrerelease).tag("pre")
                }
            } header: {
                Text(L10n.settingsSectionStartup)
            }

            Section {
                Picker(L10n.settingsLanguage, selection: $localeTag) {
                    Text(L10n.settingsLanguageSystem).tag("system")
                    Text(L10n.settingsLanguageEn).tag("en")
                    Text(L10n.settingsLanguageZh).tag("zh-Hans")
                }
            } header: {
                Text(L10n.settingsSectionGeneral)
            } footer: {
                // macOS 14+ footer text; will show beneath the picker.
                // We keep the message here so the user knows
                // restarting the app is required for the language
                // switch to fully apply (system-level resource
                // bundle hot-reload is not supported by SwiftUI).
                Text("Restart the app for changes to take effect.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .frame(width: 540, height: 460)
    }
}

private struct BandwidthField: View {
    @Binding var value: Int

    var body: some View {
        HStack {
            Text(L10n.settingsBandwidth)
            TextField("", value: $value, format: .number, prompt: Text(L10n.settingsBandwidthHint))
                .frame(width: 100)
                .multilineTextAlignment(.trailing)
            Text(L10n.settingsKbps)
        }
    }
}

/// Categories' default mapping surface — v1.0 keeps the JSON-driven
/// table in `UserDefaults` for the GUI's sake. Live editing of the
/// rule table lives in `CategoryRulesEditor`; the actual download
/// classification happens in P3.
struct CategoryRulesEditor: View {
    var body: some View {
        Text(L10n.settingsCategoriesPlaceholder)
            .foregroundStyle(.secondary)
            .frame(width: 480, height: 320)
    }
}
