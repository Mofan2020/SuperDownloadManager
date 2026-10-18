import SwiftUI
import AppKit
import SuperDownloadManagerCore

/// SiteRulesView — v1.1.2 layout pass. v1.0.7 wrapped the editor
/// in a fixed `.frame(width: 580, height: 540)`, which clashed
/// with the `NavigationSplitView` detail column on macOS 26 —
/// the inner field stack could exceed the column on resize, and
/// the trailing button group fell off the right edge on wide
/// detail panes. v1.1.2 switches to a Form-style layout with
/// `.frame(maxWidth: .infinity, minHeight: …)`, full-width
/// `TextField`s, and a scroll view so the editor adapts to the
/// detail column without breaking the rest of Settings.
struct SiteRulesView: View {
    @Environment(\.strings) private var S
    @State private var rules: [SiteRule] = SiteRuleStore.shared.load()
    @State private var newHost: String = ""
    @State private var newReferer: String = ""
    @State private var newUA: String = ""
    @State private var newCookie: String = ""
    @State private var newUser: String = ""
    @State private var newPass: String = ""
    @State private var error: String? = nil

    var body: some View {
        let S = self.S
        return Form {
            Section {
                Text(S.siteRulesExplanation)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } header: {
                Text(S.siteRulesTitle)
            }

            Section {
                TextField(S.siteRulesHost, text: $newHost)
                    .textFieldStyle(.roundedBorder)
                TextField(S.siteRulesReferer, text: $newReferer)
                    .textFieldStyle(.roundedBorder)
                TextField(S.siteRulesUserAgent, text: $newUA)
                    .textFieldStyle(.roundedBorder)
                TextField(S.siteRulesCookie, text: $newCookie)
                    .textFieldStyle(.roundedBorder)
                HStack {
                    TextField(S.siteRulesBasicAuthUser, text: $newUser)
                        .textFieldStyle(.roundedBorder)
                    SecureField(S.siteRulesBasicAuthPassword, text: $newPass)
                        .textFieldStyle(.roundedBorder)
                }
                HStack {
                    Button(S.siteRulesAdd) { commit() }
                        .keyboardShortcut(.defaultAction)
                    Button(S.siteRulesClearAll) {
                        SiteRuleStore.shared.clearAll()
                        rules = []
                    }
                }
                if let err = error {
                    Text(err).font(.caption).foregroundStyle(.red)
                }
            } header: {
                Text(S.siteRulesAddHint)
            }

            Section {
                if rules.isEmpty {
                    Text(S.siteRulesEmpty)
                        .foregroundStyle(.tertiary)
                        .padding(.vertical, 8)
                } else {
                    ForEach(rules) { rule in
                        SiteRuleRow(rule: rule,
                                    S: S,
                                    onDelete: {
                                        SiteRuleStore.shared.remove(id: rule.id)
                                        rules = SiteRuleStore.shared.load()
                                    })
                    }
                }
            } header: {
                Text(S.siteRulesExisting)
            }
        }
        .formStyle(.grouped)
        .padding(20)
        .frame(maxWidth: .infinity, minHeight: 380, alignment: .topLeading)
    }

    private func commit() {
        let host = newHost.trimmingCharacters(in: .whitespaces).lowercased()
        guard !host.isEmpty else {
            error = "Hostname is required."
            return
        }
        let rule = SiteRule(
            host: host,
            referer: newReferer.isEmpty ? nil : newReferer,
            userAgent: newUA.isEmpty ? nil : newUA,
            cookies: newCookie.isEmpty ? nil : newCookie,
            basicAuthUser: newUser.isEmpty ? nil : newUser,
            basicAuthPass: newPass.isEmpty ? nil : newPass)
        SiteRuleStore.shared.add(rule)
        rules = SiteRuleStore.shared.load()
        newHost = ""
        newReferer = ""
        newUA = ""
        newCookie = ""
        newUser = ""
        newPass = ""
        error = nil
    }
}

/// One row in the existing-rules list. Pulled out so the parent
/// `Form` can stay flat and so the password field never sneaks
/// into the summary line.
private struct SiteRuleRow: View {
    let rule: SiteRule
    let S: Strings
    let onDelete: () -> Void

    var body: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 2) {
                Text(rule.host).font(.body.weight(.medium))
                if let r = rule.referer {
                    Text(S.siteRulesRefererShort(r)).font(.caption).foregroundStyle(.secondary)
                }
                if let ua = rule.userAgent {
                    Text(S.siteRulesUserAgentShort(ua)).font(.caption).foregroundStyle(.secondary)
                }
                if let c = rule.cookies {
                    Text(S.siteRulesCookieShort(c)).font(.caption).foregroundStyle(.secondary)
                }
                if let u = rule.basicAuthUser {
                    Text(S.siteRulesBasicAuthShort(u)).font(.caption).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 8)
            Button(role: .destructive, action: onDelete) {
                Image(systemName: "trash")
            }
            .buttonStyle(.borderless)
            .help("Remove")
        }
        .padding(.vertical, 4)
    }
}
