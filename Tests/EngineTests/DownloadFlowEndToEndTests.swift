import XCTest
@testable import SuperDownloadManagerCore

/// DownloadFlowEndToEnd tests — drive the full pipeline through
/// real `URLSession` bytes streams.
///
/// v1.0 has two coverage modes:
/// - **Local**: opt-in via `SDM_E2E=1`. Spawns a Python range-aware
///   server and runs actual end-to-end probes. Skipped by default
///   to keep `swift test` cheap.
/// - **CI (GitHub Actions)**: runs by default since the runner image
///   ships with `/usr/bin/python3`. Pass `SDM_E2E=0` to disable.
///
/// These tests are the **honest integration check** that the wiring
/// from `URLSession.bytes` → `FileAssembler` → `finalize()` is
/// alive. Without them, "42 tests passed" only proves the parts
/// work — the whole pipeline can still be unwired, which is the
/// shape of the bug the project hit at v1.0 hand-off.
final class DownloadFlowEndToEndTests: XCTestCase {
    override func setUpWithError() throws {
        try super.setUpWithError()
        let optIn = ProcessInfo.processInfo.environment["SDM_E2E"]
        let isCI = ProcessInfo.processInfo.environment["CI"] == "true"
        // v1.1.4: for local debugging, also run when running under
        // `xcodebuild test` and the test was *explicitly* selected
        // — i.e. `xcodebuild -only-testing` — without the env vars
        // needed by CI. Otherwise a developer has to remember to
        // pass `SDM_E2E=1` through `xcodebuild`, which strips env
        // vars by default.
        let onlyTesting = ProcessInfo.processInfo.arguments.contains("-only-testing")
            || (ProcessInfo.processInfo.environment["ONLY_TESTING"] != nil)
        let runLocally = !isCI && optIn != "1" && !onlyTesting
        if runLocally {
            throw XCTSkip("DownloadFlow e2e opts in via SDM_E2E=1 (Python range server required).")
        }
        if isCI && optIn == "0" {
            throw XCTSkip("DownloadFlow e2e skipped in CI (SDM_E2E=0).")
        }
        guard FileManager.default.isExecutableFile(atPath: "/usr/bin/python3") else {
            throw XCTSkip("python3 not available; skipping e2e")
        }
    }

    private var serverPID: Int32?

    override func tearDownWithError() throws {
        if let pid = serverPID { kill(pid, SIGTERM) }
        serverPID = nil
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

    // MARK: - Concurrency regression test (v1.1.4)
    //
    // The user reported that chunks complete one-at-a-time even
    // when 8 of them are queued. The throttle was each chunk's
    // Range response taking ~1s to start, so 8 chunks in serial
    // take ~8s while 8 chunks *in parallel* should take ~1s. The
    // RangeStrategy here forces 8 chunks of ~64 KB each.
    func testChunksRunInParallelNotSequentially() async throws {
        let port = UInt16.random(in: 40000..<50000)
        let script = """
        import http.server, socketserver, time, os
        from socketserver import ThreadingMixIn
        PORT = \(port)
        TOTAL = \(8 * 64 * 1024)

        class ThreadingHTTPServer(ThreadingMixIn, socketserver.TCPServer):
            daemon_threads = True
            pass

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
                    # Force each chunk to take ~1 s so a serial
                    # bottleneck is observable.
                    self.end_headers()
                    block = 4096
                    sent = 0
                    while sent < length:
                        chunk = b"a" * min(block, length - sent)
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        except Exception:
                            return
                        time.sleep(0.05)
                        sent += len(chunk)
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", str(TOTAL))
                    self.end_headers()
                    self.wfile.write(b"a" * TOTAL)
            def log_message(self, *args): pass

        with ThreadingHTTPServer(("127.0.0.1", PORT), H) as httpd:
            httpd.serve_forever()
        """
        let url = try await startServerWithScript(script, port: port)
        defer { stopServer() }
        let dest = FileManager.default.temporaryDirectory
            .appendingPathComponent("sdm-e2e-parallel-\(UUID().uuidString).bin")
        defer { try? FileManager.default.removeItem(at: dest) }

        let flow = DownloadFlow(
            url: url,
            destination: dest,
            strategy: RangeStrategy(
                initialChunkSize: 64 * 1024,
                maxChunks: 16,
                defaultChunks: 8,
                firstRearrangeAfter: 999
            ),
            parallelismCap: 8
        )

        let startedAt = Date()
        try await flow.run()
        let elapsed = Date().timeIntervalSince(startedAt)

        // 8 chunks × 0.8 s each serial = ~6.4 s.
        // 8 chunks in parallel with the same latency = ~0.8 s.
        // Allow plenty of head-room for CI jitter.
        XCTAssertLessThan(elapsed, 4.0,
            "Chunks appear to be running serially — 8 × 0.8 s = ~6.4 s; " +
            "expected ~0.8 s if parallelismCap actually fans out. " +
            "Actual: \(String(format: "%.2f", elapsed))s")
    }

    // MARK: - Server helpers

    private func startServer(port: UInt16, totalBytes: Int) async throws -> URL {
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
