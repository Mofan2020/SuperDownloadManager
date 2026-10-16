import Foundation

/// SDM v1.0 - P0 stub for the headless daemon entrypoint.
///
/// P0 only needs this to compile; the actual sdmd binary gets a
/// dedicated target in P4 once XcodeGen-project.yml grows the
/// `sdmd` target. For now this file lives inside the app target
/// so it MUST NOT carry `@main` (the app target already has
/// `@main` on `SDMApp`). Only the real P4 target will own `@main`.
enum SDMDaemonMain {
    static func run() {
        // P0 stub. P4 replaces this with Unix socket listener.
        FileHandle.standardOutput.write(Data("sdmd stub (P0)\n".utf8))
    }
}
