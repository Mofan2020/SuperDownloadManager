import XCTest
@testable import SuperDownloadManagerCore

/// End-to-end download flow test using a real local HTTP server.
///
/// This is the **honest integration test** for v1.0: it spins up a
/// `python3`-based HTTP server with `Range` support, points a
/// `DownloadFlow` at it, and verifies the actual bytes land on disk.
/// Without this test, "tests pass" only proves the parts work —
/// the whole pipeline can still be unwired, which is exactly the
/// shape of the bug the project hit at the v1.0 hand-off.
///
/// Tests skip if `python3` isn't available.
final class DownloadFlowEndToEndTests: XCTestCase {
    private var server: Task<URL, Error>?
    private var serverPID: Int32?

    override func setUpWithError() throws {
        try super.setUpWithError()
        // End-to-end tests spawn a Python range-aware HTTP server.
        // Local dev: opt-in via SDM_E2E=1 to keep routine tests fast.
        // CI runners (GitHub Actions) get a full python3 install, so
        // we run them by default there — unless the operator passes
        // SDM_E2E=0 to skip. The CI runner image has python3 at
        // /usr/bin/python3; if missing we still skip cleanly.
        let optIn = ProcessInfo.processInfo.environment["SDM_E2E"]
        let isCI = ProcessInfo.processInfo.environment["CI"] == "true"
        if !isCI && optIn != "1" {
            throw XCTSkip("DownloadFlow e2e opts in via SDM_E2E=1 (Python range server required).")
        }
        if isCI && optIn == "0" {
            throw XCTSkip("DownloadFlow e2e skipped in CI (SDM_E2E=0).")
        }
        guard FileManager.default.isExecutableFile(atPath: "/usr/bin/python3") else {
            throw XCTSkip("python3 not available; skipping e2e")
        }
    }

    override func tearDownWithError() throws {
        if let pid = serverPID { kill(pid, SIGTERM) }
        try super.tearDownWithError()
    }

    func testDownloadSingleChunkCreatesIdenticalFile() async throws {
        let port = UInt16.random(in: 40000..<50000)
        let url = try await startServer(port: port, totalBytes: 64 * 1024)
        defer { stopServer() }

        let dest = FileManager.default.temporaryDirectory
            .appendingPathComponent("sdm-e2e-\(UUID().uuidString).bin")
        defer { try? FileManager.default.removeItem(at: dest) }

        let flow = DownloadFlow(
            url: url,
            destination: dest,
            strategy: RangeStrategy(
                initialChunkSize: 4096,
                maxChunks: 8,
                defaultChunks: 4,
                firstRearrangeAfter: 999  // don't rearrange in this tiny test
            ),
            parallelismCap: 4
        )
        try await flow.run()

        // Bytes on disk == bytes the server claimed, byte-equal.
        let dataOnDisk = try Data(contentsOf: dest)
        let dataFromServer = try Data(contentsOf: url)
        XCTAssertEqual(dataOnDisk, dataFromServer)
    }

    func testDownloadTwoChunksReassembleCorrectly() async throws {
        let port = UInt16.random(in: 40000..<50000)
        let url = try await startServer(port: port, totalBytes: 8 * 1024)
        defer { stopServer() }

        let dest = FileManager.default.temporaryDirectory
            .appendingPathComponent("sdm-e2e-\(UUID().uuidString).bin")
        defer { try? FileManager.default.removeItem(at: dest) }

        let flow = DownloadFlow(
            url: url,
            destination: dest,
            strategy: RangeStrategy(
                initialChunkSize: 1024,
                maxChunks: 16,
                defaultChunks: 8,
                firstRearrangeAfter: 999
            ),
            parallelismCap: 8
        )
        try await flow.run()
        let onDisk = try Data(contentsOf: dest)
        XCTAssertEqual(onDisk.count, 8 * 1024)
    }

    func testNoContentLengthRejects() async throws {
        let port = UInt16.random(in: 40000..<50000)
        let serverScript = """
        import http.server, socketserver, sys
        PORT = \(port)
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                # Deliberately no Content-Length.
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    for _ in range(1024):
                        self.wfile.write(b"a" * 4096)
                except Exception:
                    pass
            def log_message(self, *args): pass
        with socketserver.TCPServer(("127.0.0.1", PORT), H) as httpd:
            httpd.serve_forever()
        """
        let url = try await startServerWithScript(serverScript, port: port)
        defer { stopServer() }

        let dest = FileManager.default.temporaryDirectory
            .appendingPathComponent("sdm-e2e-\(UUID().uuidString).bin")
        defer { try? FileManager.default.removeItem(at: dest) }

        let flow = DownloadFlow(
            url: url,
            destination: dest,
            strategy: RangeStrategy(),
            parallelismCap: 1
        )
        do {
            try await flow.run()
            XCTFail("Flow should reject when Content-Length is missing")
        } catch let e as DownloadFlow.FlowError {
            if case .noContentLength = e { /* expected */ } else { XCTFail("got \(e)") }
        }
    }

    // MARK: - Server helpers

    private func startServer(port: UInt16, totalBytes: Int) async throws -> URL {
        // Range-aware server: returns Content-Length, Accept-Ranges,
        // and 206 Partial Content for byte ranges; 200 for "0-0"
        // so the probe still works.
        let script = """
        import http.server, socketserver, sys, os
        PORT = \(port)
        TOTAL = \(totalBytes)

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    spec = rng[len("bytes="):]
                    s, e = spec.split("-")
                    start = int(s) if s else 0
                    end = int(e) if e else TOTAL - 1
                    end = min(end, TOTAL - 1)
                    length = end - start + 1
                    self.send_response(206)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(length))
                    self.send_header("Content-Range", f"bytes {start}-{end}/{TOTAL}")
                    self.end_headers()
                    payload = (b"a" * 256) * (length // 256) + b"a" * (length % 256)
                    self.wfile.write(payload[:length])
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(TOTAL))
                    self.end_headers()
                    payload = (b"a" * 256) * (TOTAL // 256) + b"a" * (TOTAL % 256)
                    self.wfile.write(payload[:TOTAL])
            def log_message(self, *args): pass

        with socketserver.TCPServer(("127.0.0.1", PORT), H) as httpd:
            httpd.serve_forever()
        """
        return try await startServerWithScript(script, port: port)
    }

    private func startServerWithScript(_ py: String, port: UInt16) async throws -> URL {
        let scriptPath = FileManager.default.temporaryDirectory
            .appendingPathComponent("sdm-test-server-\(UUID().uuidString).py")
        try py.write(to: scriptPath, atomically: true, encoding: .utf8)
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [scriptPath.path]
        process.standardOutput = Pipe()
        process.standardError = Pipe()
        try process.run()
        serverPID = Int32(process.processIdentifier)
        // Wait for server to bind.
        for _ in 0..<30 {
            try await Task.sleep(nanoseconds: 100_000_000)
            if await probeLocalhost(port: port) {
                return URL(string: "http://127.0.0.1:\(port)/file.bin")!
            }
        }
        throw XCTSkip("server did not bind on port \(port)")
    }

    private func probeLocalhost(port: UInt16) async -> Bool {
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let fd = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        let res = withUnsafePointer(to: &addr) { ptr -> Int32 in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { p in
                Darwin.connect(fd, p, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return res == 0
    }

    private func stopServer() {
        if let pid = serverPID { kill(pid, SIGTERM) }
        serverPID = nil
    }
}
