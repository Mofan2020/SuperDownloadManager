import SwiftUI

/// SettingsView — P2 settings window. Lets the user change:
///
/// - Default thread count (1..128, default 32)
/// - Global bandwidth limit (0 = unlimited; min 100 KB/s)
/// - Default category rules (P3 wires these to `ContentSniffer`)
/// - "Open at login" toggle (P4 wire-up reads `LaunchAgent` state)
/// - Update channel (P7 wires to Sparkle)
struct SettingsView: View {
    @AppStorage("sdm.threads.default") private var defaultThreads: Int = 32
    @AppStorage("sdm.bandwidth.bytesPerSec") private var bandwidth: Int = 0
    @AppStorage("sdm.openAtLogin") private var openAtLogin: Bool = false
    @AppStorage("sdm.updateChannel") private var updateChannel: String = "stable"

    var body: some View {
        Form {
            Section("默认下载") {
                Stepper("默认线程数: \(defaultThreads)",
                        value: $defaultThreads, in: 1...128, step: 1)
                BandwidthField(value: $bandwidth)
            }
            Section("分类") {
                NavigationLink("文件分类规则") {
                    CategoryRulesEditor()
                }
            }
            Section("启动") {
                Toggle("开机自启动", isOn: $openAtLogin)
                Picker("更新通道", selection: $updateChannel) {
                    Text("Stable").tag("stable")
                    Text("Pre-release").tag("pre")
                }
            }
        }
        .frame(width: 520, height: 420)
    }
}

private struct BandwidthField: View {
    @Binding var value: Int

    var body: some View {
        HStack {
            Text("带宽限制:")
            TextField("0 = 不限速", value: $value, format: .number)
                .frame(width: 100)
                .multilineTextAlignment(.trailing)
            Text("KB/s")
        }
    }
}

/// Categories' default mapping surface — v1.0 keeps the JSON-driven
/// table in `UserDefaults` for the GUI's sake. Live editing of the
/// rule table lives in `CategoryRulesEditor`; the actual download
/// classification happens in P3.
struct CategoryRulesEditor: View {
    var body: some View {
        Text("分类规则编辑器（P3 接入）")
            .foregroundStyle(.secondary)
            .frame(width: 480, height: 320)
    }
}
