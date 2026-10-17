import Foundation

/// UpdateService — auto-update driver (placeholder).
///
/// v1.0 ships without Sparkle because the Sparkle 2.6+ Swift
/// Package's binary archive is unreachable from this CI's SPM
/// cache (the binary zip returns "invalid archive" from the
/// GitHub release URL). The class remains in place so the GUI
/// can be wired in v1.1.0 without an API shape change.
///
/// When Sparkle is added back:
///
///   packages:
///     Sparkle:
///       url: https://github.com/sparkle-project/Sparkle
///       exactVersion: "2.6.4"
///
/// then replace this stub with
/// `SPUStandardUpdaterController` driving an appcast channel.
public final class UpdateService {
    public static let shared = UpdateService()

    private init() {}

    /// Forward menu-bar trigger to "no-op" until Sparkle lands.
    public func checkForUpdates() {
        FileHandle.standardError.write(Data(
            "[UpdateService] auto-update is not enabled in v1.0; Sparkle wiring lands in v1.1.0.\n".utf8))
    }
}
