import Foundation

// sdmd daemon entry point — P4 helper target.
//
// When the SDM.app is launched with the env var `SDM_DAEMON=1`,
// this main.swift takes over and runs headlessly as a server
// bound to a Unix Domain Socket at
// `~/Library/Application Support/SuperDownloadManager/sdmd.sock`.
// The LaunchAgent plist (P0's
// `Launcher/templates/com.superdownloadmanager.daemon.plist`)
// points at this binary and sets that env var so that even the
// .app launch behaves as a server.
//
// CI tests the daemon by launching the built product directly
// with `SDM_DAEMON=1` and using `sdmdm ping`.

let envOk = ProcessInfo.processInfo.environment["SDM_DAEMON"] == "1"
if !envOk {
    FileHandle.standardError.write(Data(
        "[sdmd] this binary is meant to run as a daemon; launch via LaunchAgent or set SDM_DAEMON=1.\n".utf8))
    exit(0)
}

let socketPath = ProcessInfo.processInfo.environment["SDM_SOCKET"]
    ?? SDMDaemonMain.defaultSocketPath()

// Spin up the daemon loop on a background task and block the
// main thread forever.
Task.detached {
    do {
        let dispatcher = Dispatcher()
        let server = SocketServer(path: socketPath) { data in
            await dispatcher.handle(data)
        }
        try await server.start()
        FileHandle.standardError.write(Data(
            "[sdmd] listening at \(socketPath)\n".utf8))
    } catch {
        FileHandle.standardError.write(Data(
            "[sdmd] failed to start: \(error)\n".utf8))
        exit(1)
    }
}

dispatchMain()
