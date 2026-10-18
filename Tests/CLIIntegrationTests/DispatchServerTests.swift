import XCTest

/// DispatchServer smoke tests — P5 wire-up verification.
///
/// These tests probe the live HTTP server at 127.0.0.1:7955
/// (P5 dispatch endpoint). The server only runs while the
/// LaunchAgent-driven `SuperDownloadManager` binary is alive
/// with `SDM_DAEMON=1`; in CI we skip these unless that daemon
/// is reachable.
///
/// Local dev workflow:
///   1. Open SuperDownloadManager.app once (auto-installs
///      LaunchAgent), or run scripts/install.sh.
///   2. `launchctl kickstart -k gui/$(id -u)/com.skyc8266.superdownloadmanager.daemon`
///   3. `xcodebuild test ... -only-testing:SuperDownloadManagerTests/DispatchServerTests`

final class DispatchServerTests: XCTestCase {
    private var daemonReachable: Bool {
        // Synchronous probe using POSIX connect so we don't have to
        // await Task inside a non-async test method.
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = UInt16(7955).bigEndian
        addr.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let fd = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        let connectRes = withUnsafePointer(to: &addr) { ptr -> Int32 in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { p in
                Darwin.connect(fd, p, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return connectRes == 0
    }

    func testPingOK() async throws {
        try XCTSkipIf(!daemonReachable,
                     "skipping: dispatch server not running on 127.0.0.1:7955")
        let sentURL = URL(string: "http://127.0.0.1:7955/ping")!
        let (data, _) = try await URLSession.shared.data(from: sentURL)
        let body = String(decoding: data, as: UTF8.self)
        XCTAssertTrue(body.contains("pong"))
    }

    func testResponseShapeIsJSON() async throws {
        try XCTSkipIf(!daemonReachable,
                     "skipping: dispatch server not running on 127.0.0.1:7955")
        let url = URL(string: "http://127.0.0.1:7955/ping")!
        let req = URLRequest(url: url)
        let (data, response) = try await URLSession.shared.data(for: req)
        let http = response as? HTTPURLResponse
        XCTAssertEqual(http?.statusCode, 200)
        let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        XCTAssertEqual(obj?["ok"] as? Bool, true)
    }
}
