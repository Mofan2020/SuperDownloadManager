// v1.1.8: opt-in TLS validation bypass.
//
// The default `URLSession` trust chain on macOS does NOT
// accept certificates that aren't in the system keychain.
// Some reverse proxies (the GitHub release-asset CDN being
// the common one) terminate TLS with a leaf signed by an
// intermediate that ships with the proxy's edge appliance
// rather than with the system CA bundles. Browsers like
// Safari get away with this because Apple pre-installs
// certain edge-issuer intermediates in their keychain
// through a separate path; ad-hoc signed third-party apps
// like SDM do not. Until now the only workaround was for
// the user to manually install the leaf into their
// Keychain — painful and not stable across sessions.
//
// The user's Settings → General → "允许忽略 SSL 验证"
// switch flips this gate on. When it's on, every
// URLSession we own accepts the server's presented
// `SecTrust` without re-validating against the chain.
//
// This delegate is `Sendable` only insofar as the engine
// already runs URLSession callbacks on a serial queue.
// The `ignoreSSL` flag is mutated from the Settings
// toggle via `setIgnoreSSL(_:)`, called from the main
// thread; the queue URLSession uses is also serial, so
// the reads from `didReceive challenge` see the latest
// value once the write has happened. That's a relaxed
// synchronization but it's the same shape AppKit-style
// settings toggles use, and the worst case is one extra
// request going through with the wrong flag — never a
// crash, never an unauthorized change to *cert* trust.

import Foundation
import os.log

public final class IgnoreSSLAuthChallengeDelegate: NSObject, URLSessionDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private var _ignoreSSL: Bool

    public init(ignoreSSL: Bool) {
        self._ignoreSSL = ignoreSSL
        super.init()
    }

    /// Toggle from the Settings UI. URLSession requires a
    /// fixed delegate per session, so when the user flips
    /// the switch we ask `HTTPDownloader` to rebuild the
    /// session with a fresh delegate; this setter is only
    /// called from the new session's init path.
    public func setIgnoreSSL(_ value: Bool) {
        lock.lock()
        defer { lock.unlock() }
        _ignoreSSL = value
    }

    private var ignoreSSL: Bool {
        lock.lock()
        defer { lock.unlock() }
        return _ignoreSSL
    }

    public func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        let allowed = ignoreSSL
        guard allowed else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust else {
            completionHandler(.performDefaultHandling, nil)
            return
        }

        // Surface the bypass in the engine log so the user
        // can see what's happening when downloads quietly
        // succeed despite a broken chain. This is the only
        // place we log a deliberately-broken TLS connection
        // in the engine layer. v1.1.9 dropped the OSLog
        // `privacy: .public` annotation: the proxy logger
        // forwards plain `String` to `SDMLogger`, which
        // records the value unredacted into the file log
        // anyway. The host/port/IP is diagnostic info the
        // user wants to see.
        downloadLog.warning("SSL bypass active for host=\(challenge.protectionSpace.host) port=\(challenge.protectionSpace.port)")

        completionHandler(.useCredential, URLCredential(trust: trust))
    }
}
