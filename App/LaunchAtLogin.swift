import Foundation
import ServiceManagement

/// LaunchAtLogin — v1.1.2. Wires the "Open at login" toggle in
/// `Settings → Startup` to the system login-items API.
///
/// v1.0.4 added the @AppStorage Bool and the Toggle row, but never
/// actually registered the helper — toggling it did nothing. This
/// hooks the value into `SMAppService.mainApp`, the macOS 13+
/// replacement for the deprecated `LaunchServices`-style APIs.
/// `mainApp` is the recommended path for an app that wants to run
/// itself (not a privileged helper) at user login; the system
/// reads the bundle id from `Info.plist` and registers the user's
/// own app under their user account, so no UI prompt is shown and
/// no extra entitlements are needed for an ad-hoc / non-sandboxed
/// build.
///
/// Behaviour
/// ---------
/// - `enabled = true`  : register `SMAppService.mainApp`
/// - `enabled = false` : unregister `SMAppService.mainApp`
/// - Registering an already-registered service is a no-op
///   (`register()` returns false; we treat that as "already on");
///   unregistering an un-registered service is also a no-op.
///
/// We deliberately do NOT touch `SMAppService.status` while the
/// app is running — `register()` already calls into launchd,
/// which writes the helper entry under
/// `~/Library/Application Support/com.apple.backgroundtaskmanagementagent/`
/// on macOS 13+.
@MainActor
enum LaunchAtLogin {
    private static let service = SMAppService.mainApp

    /// Whether the app is currently registered to run at login.
    static var isEnabled: Bool {
        service.status == .enabled
    }

    /// Best-effort toggle. Returns whether the post-state matches
    /// `target`. Errors from `register()` are swallowed on purpose
    /// — the typical cause is "already registered" which is fine.
    @discardableResult
    static func setEnabled(_ target: Bool) -> Bool {
        if target {
            do {
                try service.register()
            } catch {
                // Already enabled? `register()` throws when the
                // service is already in `.enabled`. Treat that as
                // a success; surface anything else via NSLog.
                NSLog("LaunchAtLogin.register threw: \(error)")
                if !isEnabled { return false }
            }
        } else {
            do {
                try service.unregister()
            } catch {
                NSLog("LaunchAtLogin.unregister threw: \(error)")
                if isEnabled { return false }
            }
        }
        return isEnabled == target
    }

    /// Sync the system state from the AppStorage Bool. Safe to
    /// call every launch — calling `register()` / `unregister()`
    /// when already in the desired state is a no-op.
    static func syncFromStorage() {
        let target = UserDefaults.standard.bool(forKey: "sdm.openAtLogin")
        if target != isEnabled {
            setEnabled(target)
        }
    }
}
