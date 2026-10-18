import Foundation
import Security

/// SiteRule — v1.0.4 P2-3. Per-hostname rule: extra HTTP headers
/// (Referer, User-Agent, Cookie) and optional Basic Auth
/// credentials. Stored as JSON in UserDefaults; sensitive
/// credentials live in Keychain keyed by hostname.
public struct SiteRule: Codable, Identifiable, Equatable, Sendable {
    public let id: UUID
    public var host: String
    public var referer: String?
    public var userAgent: String?
    public var cookies: String?
    public var basicAuthUser: String?
    public var basicAuthPass: String?   // stored in Keychain, not in JSON

    public init(id: UUID = UUID(),
                host: String,
                referer: String? = nil,
                userAgent: String? = nil,
                cookies: String? = nil,
                basicAuthUser: String? = nil,
                basicAuthPass: String? = nil) {
        self.id = id
        self.host = host
        self.referer = referer
        self.userAgent = userAgent
        self.cookies = cookies
        self.basicAuthUser = basicAuthUser
        self.basicAuthPass = basicAuthPass
    }
}

/// SiteRuleStore — JSON-backed rule list. Passwords go through
/// Keychain so they don't leak in plain JSON.
public final class SiteRuleStore: @unchecked Sendable {
    public static let shared = SiteRuleStore()
    private let defaultsKey = "sdm.siteRules"
    private let keychainService = "com.skyc8266.superdownloadmanager.siteRules"

    public func load() -> [SiteRule] {
        guard let data = UserDefaults.standard.data(forKey: defaultsKey) else {
            return []
        }
        return (try? JSONDecoder().decode([SiteRule].self, from: data)) ?? []
    }

    public func save(_ rules: [SiteRule]) {
        // Strip password fields before JSON-encoding; we look
        // them up from Keychain by id when applying.
        let stripped = rules.map { r in
            SiteRule(id: r.id, host: r.host,
                     referer: r.referer, userAgent: r.userAgent,
                     cookies: r.cookies, basicAuthUser: r.basicAuthUser,
                     basicAuthPass: nil)
        }
        if let data = try? JSONEncoder().encode(stripped) {
            UserDefaults.standard.set(data, forKey: defaultsKey)
        }
    }

    public func add(_ rule: SiteRule) {
        var all = load()
        all.append(rule)
        save(all)
        if let p = rule.basicAuthPass, let u = rule.basicAuthUser {
            setPassword(p, account: u, host: rule.host)
        }
    }

    public func remove(id: UUID) {
        var all = load()
        all.removeAll { $0.id == id }
        save(all)
    }

    /// Find a rule whose host matches the URL's host (suffix
    /// match: `cdn.example.com` matches `x.cdn.example.com`).
    public func match(url: URL) -> SiteRule? {
        guard let host = url.host else { return nil }
        let all = load()
        return all.first(where: { host == $0.host || host.hasSuffix(".\($0.host)") })
    }

    // MARK: - Keychain

    @discardableResult
    public func setPassword(_ password: String, account: String, host: String) -> Bool {
        let data = Data(password.utf8)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: keychainService,
            kSecAttrAccount as String: "\(account)@\(host)"
        ]
        SecItemDelete(query as CFDictionary)
        var add = query
        add[kSecValueData as String] = data
        add[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlocked
        return SecItemAdd(add as CFDictionary, nil) == errSecSuccess
    }

    public func getPassword(account: String, host: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: keychainService,
            kSecAttrAccount as String: "\(account)@\(host)",
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    public func clearAll() {
        for rule in load() {
            if let _ = rule.basicAuthPass, let u = rule.basicAuthUser {
                let q: [String: Any] = [
                    kSecClass as String: kSecClassGenericPassword,
                    kSecAttrService as String: keychainService,
                    kSecAttrAccount as String: "\(u)@\(rule.host)"
                ]
                SecItemDelete(q as CFDictionary)
            }
        }
        UserDefaults.standard.removeObject(forKey: defaultsKey)
    }
}
