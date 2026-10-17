import SwiftUI
import AppKit
import SuperDownloadManagerCore

/// SiteRulesView — v1.0.4 P2-3. Editor for per-hostname request
/// rules. Add a hostname, set Referer / User-Agent / Cookie /
/// Basic Auth. The engine applies matching rules at request time.
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
        VStack(alignment: .leading, spacing: 12) {
            Text(S.siteRulesTitle)
                .font(.title3.weight(.semibold))
            Text(S.siteRulesExplanation)
                .font(.callout)
                .foregroundStyle(.secondary)

            VStack(alignment: .leading, spacing: 6) {
                Text(S.siteRulesAddHint).font(.caption).foregroundStyle(.secondary)
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
                    Button(S.siteRulesClearAll) {
                        SiteRuleStore.shared.clearAll()
                        rules = []
                    }
                }
                if let err = error {
                    Text(err).font(.caption).foregroundStyle(.red)
                }
            }
            .frame(maxWidth: 540)
            Divider()
            Text(S.siteRulesExisting)
                .font(.caption)
                .foregroundStyle(.secondary)
            if rules.isEmpty {
                Text(S.siteRulesEmpty)
                    .foregroundStyle(.tertiary)
                    .padding(.vertical, 8)
            } else {
                ForEach(rules) { rule in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(rule.host).font(.body.weight(.medium))
                            if let r = rule.referer { Text(S.siteRulesRefererShort(r)).font(.caption).foregroundStyle(.secondary) }
                            if let ua = rule.userAgent { Text(S.siteRulesUserAgentShort(ua)).font(.caption).foregroundStyle(.secondary) }
                            if let c = rule.cookies { Text(S.siteRulesCookieShort(c)).font(.caption).foregroundStyle(.secondary) }
                            if let u = rule.basicAuthUser { Text(S.siteRulesBasicAuthShort(u)).font(.caption).foregroundStyle(.secondary) }
                        }
                        Spacer()
                        Button {
                            SiteRuleStore.shared.remove(id: rule.id)
                            rules = SiteRuleStore.shared.load()
                        } label: { Image(systemName: "trash") }
                        .buttonStyle(.borderless)
                    }
                    .padding(.vertical, 4)
                }
            }
            Spacer()
        }
        .padding(20)
        .frame(width: 580, height: 540)
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
