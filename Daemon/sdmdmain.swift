import Foundation

/// sdmd — P4 daemon entry point.
///
/// This file lives inside the framework target today because XcodeGen
/// treats `app + helper` as one project; once `target: tool` lands in
/// P4 wire-up, the helper is built as a separate `sdmd` executable
/// under `Contents/MacOS/sdmd` alongside the main app binary.
///
/// For v1.0 we keep a single-binary daemon: the daemon is launched
/// by `scripts/install.sh` registering the LaunchAgent plist that
/// runs the same `SuperDownloadManager.app/Contents/MacOS/sdmd` path
/// inside the app bundle.
///
/// When the helper target lands in P4, the daemon file moves
/// wholesale — only the LaunchAgent plist and `install.sh` need to
/// change to point at the new path.
public enum SDMDaemonMain {
    public static func defaultSocketPath() -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let dir = home.appendingPathComponent(
            "Library/Application Support/SuperDownloadManager")
        return dir.appendingPathComponent("sdmd.sock").path
    }

    /// P0 stub: prints to stdout.
    /// Real implementation begins in P4 once the helper target
    /// exists. Kept tiny on purpose — anything in this file is
    /// compiled into the app target, so `@main` would conflict
    /// with `SDMApp`.
    public static func run() {
        FileHandle.standardOutput.write(Data(
            "[sdmd] P0 stub — real daemon lands in P4 once helper target is added\n".utf8))
        _ = defaultSocketPath()
    }
}
