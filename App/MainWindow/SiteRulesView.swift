import SwiftUI
import AppKit
import SuperDownloadManagerCore

/// SiteRulesView — v1.0.4 P2-3. Editor for per-hostname request
/// rules. Add a hostname, set Referer / User-Agent / Cookie /
/// Basic Auth. The engine applies matching rules at request time.
struct SiteRulesView: View {
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
            Text("Site Rules")
                .font(.title3.weight(.semibold))
            Text("Match downloads by hostname (suffix match). " +
                 "Set Referer / User-Agent / Cookie / Basic Auth. " +
                 "Passwords go to Keychain.")
                .font(.callout)
                .foregroundStyle(.secondary)

            VStack(alignment: .leading, spacing: 6) {
                Text("Add a rule").font(.caption).foregroundStyle(.secondary)
                TextField("hostname (e.g. cdn.example.com)", text: $newHost)
                    .textFieldStyle(.roundedBorder)
                TextField("Referer", text: $newReferer)
                    .textFieldStyle(.roundedBorder)
                TextField("User-Agent", text: $newUA)
                    .textFieldStyle(.roundedBorder)
                TextField("Cookie header", text: $newCookie)
                    .textFieldStyle(.roundedBorder)
                HStack {
                    TextField("Basic Auth user", text: $newUser)
                        .textFieldStyle(.roundedBorder)
                    SecureField("password", text: $newPass)
                        .textFieldStyle(.roundedBorder)
                }
                HStack {
                    Button("Add") { commit() }
                    Button("Clear All") {
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
            Text("Existing rules")
                .font(.caption)
                .foregroundStyle(.secondary)
            if rules.isEmpty {
                Text("No rules yet.")
                    .foregroundStyle(.tertiary)
                    .padding(.vertical, 8)
            } else {
                ForEach(rules) { rule in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(rule.host).font(.body.weight(.medium))
                            if let r = rule.referer { Text("Referer: \(r)").font(.caption).foregroundStyle(.secondary) }
                            if let ua = rule.userAgent { Text("UA: \(ua.prefix(40))…").font(.caption).foregroundStyle(.secondary) }
                            if let c = rule.cookies { Text("Cookie: \(c.prefix(40))…").font(.caption).foregroundStyle(.secondary) }
                            if let u = rule.basicAuthUser { Text("Basic: \(u)").font(.caption).foregroundStyle(.secondary) }
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
