import XCTest
@testable import SuperDownloadManagerCore

/// StreamingTransportTests — lock the v1.1.10 fix for the
/// "download sits at 0 KB/s" bug.
///
/// The bug: v1.1.9 pulled each chunk with `URLSession.data(for:)`,
/// which **buffers the whole response body** and returns only after
/// the last byte arrives. Until then *nothing* reached the disk and
/// nothing was reported to the progress channel, so the GUI row
/// stayed at 0 bytes / 0 KB/s for the entire chunk. On the user's
/// link (~109 KB/s against GitHub's release CDN) a chunk took tens of
/// seconds of apparent freeze, and a wall-clock request budget could
/// abort the transfer outright with `NSURLErrorDomain -1001`.
///
/// The fix: the chunk transport streams through a
/// `URLSessionDataDelegate`, so bytes land on disk while the
/// transfer is running, and a dead connection is detected by an
/// *idle* watchdog rather than by a cap on the total transfer time.
///
/// These tests use a local Python server that trickles bytes out
/// slowly, which is the deterministic version of "slow but healthy
/// link".
final class StreamingTransportTests: XCTestCase {

    private var serverPID: Int32?

    override func tearDownWithError() throws {
        if let pid = serverPID { kill(pid, SIGTERM) }
        serverPID = nil
        try super.tearDownWithError()
    }

    /// The core regression test: while a slow chunk is still
    /// transferring, bytes must already be on disk.
    ///
    /// Before the fix this assertion failed with `disk == 0` for the
    /// whole transfer — the literal "0 KB/s" the user reported.
    ///
    /// The sample is taken in the middle of the server's pause, so
    /// the transfer is provably still in flight and the reading
    /// cannot be a race with the end of the download.
    func testBytesReachDiskWhileTransferIsStillRunning() async throws {
        let port = UInt16.random(in: 40000..<50000)
        let total = 256 * 1024
        let url = try await startPausingServer(port: port,
                                              totalBytes: total,
                                              pauseAfterBytes: 64 * 1024,
                                              pauseSeconds: 3.0)
        defer { stopServer() }

        let dest = FileManager.default.temporaryDirectory
            .appendingPathComponent("sdm-stream-\(UUID().uuidString).bin")
        defer { try? FileManager.default.removeItem(at: dest) }
        let partial = dest.appendingPathExtension("sdm.partial-0")

        let downloader = HTTPDownloader()
        let assembler = FileAssembler(destination: dest)
        let chunk = Chunk(id: 0, start: 0, end: Int64(total))

        let download = Task {
            try await downloader.downloadChunk(
                url: url, chunk: chunk, assembler: assembler)
        }

        // Sample in the middle of the server's 3 s pause.
        try await Task.sleep(nanoseconds: 1_500_000_000)
        let midTransfer = (try? FileManager.default
            .attributesOfItem(atPath: partial.path)[.size] as? Int64) ?? 0

        let written = try await download.value

        XCTAssertEqual(written, Int64(total),
            "chunk should report every byte it persisted")
        XCTAssertGreaterThan(midTransfer, 0,
            "bytes must reach the disk while the transfer is still running "
            + "(sampled \(midTransfer) bytes mid-transfer) — the pre-fix "
            + "transport buffered the whole body and the GUI showed "
            + "0 KB/s until the chunk finished")
        let finalSize = (try? FileManager.default
            .attributesOfItem(atPath: partial.path)[.size] as? Int64) ?? 0
        XCTAssertEqual(finalSize, Int64(total))
    }

    /// The progress callback must fire *during* the transfer, not
    /// once at the end — that is what the GUI's speed column reads.
    func testProgressIsReportedDuringTransfer() async throws {
        let port = UInt16.random(in: 40000..<50000)
        let total = 256 * 1024
        let url = try await startTrickleServer(port: port,
                                              totalBytes: total,
                                              chunkBytes: 8 * 1024,
                                              gapSeconds: 0.05)
        defer { stopServer() }

        let dest = FileManager.default.temporaryDirectory
            .appendingPathComponent("sdm-progress-\(UUID().uuidString).bin")
        defer { try? FileManager.default.removeItem(at: dest) }

        let downloader = HTTPDownloader()
        let assembler = FileAssembler(destination: dest)
        let chunk = Chunk(id: 0, start: 0, end: Int64(total))

        let counter = ProgressCounter()
        let written = try await downloader.downloadChunk(
            url: url, chunk: chunk, assembler: assembler,
            progress: { n in counter.add(n) })

        XCTAssertEqual(written, Int64(total))
        let ticks = counter.count
        XCTAssertGreaterThan(ticks, 1,
            "progress must be reported incrementally during the transfer; "
            + "got \(ticks) report(s)")
        XCTAssertEqual(counter.total, Int64(total))
    }

    /// A server that ignores `Range` and answers 200 must still
    /// produce a usable file (the engine's "not segmentable"
    /// fallback), and the chunk must not be silently accepted as a
    /// partial-content response.
    func testServerIgnoringRangeStillCompletes() async throws {
        let port = UInt16.random(in: 40000..<50000)
        let total = 64 * 1024
        let url = try await startTrickleServer(port: port,
                                              totalBytes: total,
                                              chunkBytes: 16 * 1024,
                                              gapSeconds: 0.0,
                                              honourRange: false)
        defer { stopServer() }

        let dest = FileManager.default.temporaryDirectory
            .appendingPathComponent("sdm-norange-\(UUID().uuidString).bin")
        defer { try? FileManager.default.removeItem(at: dest) }

        let downloader = HTTPDownloader()
        let assembler = FileAssembler(destination: dest)
        let chunk = Chunk(id: 0, start: 0, end: Int64(total))

        let written = try await downloader.downloadChunk(
            url: url, chunk: chunk, assembler: assembler)
        XCTAssertGreaterThan(written, 0,
            "a 200 response (server ignored Range) must still deliver bytes")
    }

    /// A 403 must be rejected *before* any body byte is written, so
    /// the partial file is never polluted with an error page.
    func testForbiddenResponseWritesNoBytes() async throws {
        let port = UInt16.random(in: 40000..<50000)
        let url = try await startForbiddenServer(port: port)
        defer { stopServer() }

        let dest = FileManager.default.temporaryDirectory
            .appendingPathComponent("sdm-forbidden-\(UUID().uuidString).bin")
        defer { try? FileManager.default.removeItem(at: dest) }
        let partial = dest.appendingPathExtension("sdm.partial-0")

        let downloader = HTTPDownloader()
        let assembler = FileAssembler(destination: dest)
        let chunk = Chunk(id: 0, start: 0, end: 1024)

        do {
            _ = try await downloader.downloadChunk(
                url: url, chunk: chunk, assembler: assembler)
            XCTFail("a 403 response must not be treated as success")
        } catch {
            // Expected.
        }
        let size = (try? FileManager.default
            .attributesOfItem(atPath: partial.path)[.size] as? Int64) ?? 0
        XCTAssertEqual(size, 0,
            "a rejected response must not write any body bytes to the partial")
    }

    /// A dead connection (server accepts, then goes silent forever)
    /// must be detected by the idle watchdog instead of hanging
    /// forever.
    func testIdleConnectionIsDetected() async throws {
        let port = UInt16.random(in: 40000..<50000)
        let url = try await startSilentServer(port: port)
        defer { stopServer() }

        let dest = FileManager.default.temporaryDirectory
            .appendingPathComponent("sdm-idle-\(UUID().uuidString).bin")
        defer { try? FileManager.default.removeItem(at: dest) }

        let downloader = HTTPDownloader()
        // A short idle window so the test does not take a minute.
        downloader.idleTimeout = 2
        downloader.maxRetries = 0
        let assembler = FileAssembler(destination: dest)
        let chunk = Chunk(id: 0, start: 0, end: 1024)

        let started = Date()
        do {
            _ = try await downloader.downloadChunk(
                url: url, chunk: chunk, assembler: assembler)
            XCTFail("a silent connection must not be reported as success")
        } catch {
            // Expected: the idle watchdog fires.
        }
        let elapsed = Date().timeIntervalSince(started)
        XCTAssertLessThan(elapsed, 30,
            "the idle watchdog must fire promptly; took \(elapsed)s")
    }

    /// Negative test: the same scenario as
    /// `testBytesReachDiskWhileTransferIsStillRunning`, but with the
    /// transport forced back to the pre-v1.1.10 buffering behaviour.
    ///
    /// This proves the regression test above is not passing
    /// vacuously: with the old transport, no byte reaches the disk
    /// until the transfer finishes.
    ///
    /// The server pauses for `pauseSeconds` in the middle of the
    /// body, so the "is anything on disk?" question is asked during
    /// a window where the answer cannot be a race: the transfer is
    /// provably still in flight.
    func testBufferingTransportKeepsDiskEmptyUntilTheEnd() async throws {
        let port = UInt16.random(in: 40000..<50000)
        let total = 256 * 1024
        let url = try await startPausingServer(port: port,
                                              totalBytes: total,
                                              pauseAfterBytes: 64 * 1024,
                                              pauseSeconds: 3.0)
        defer { stopServer() }

        let dest = FileManager.default.temporaryDirectory
            .appendingPathComponent("sdm-buffered-\(UUID().uuidString).bin")
        defer { try? FileManager.default.removeItem(at: dest) }
        let partial = dest.appendingPathExtension("sdm.partial-0")

        let downloader = HTTPDownloader()
        // The old, buffering transport.
        downloader.forceBufferingTransport = true
        let assembler = FileAssembler(destination: dest)
        let chunk = Chunk(id: 0, start: 0, end: Int64(total))

        let download = Task {
            try await downloader.downloadChunk(
                url: url, chunk: chunk, assembler: assembler)
        }

        // Sample in the middle of the server's 3 s pause — the same
        // moment the positive test samples.
        try await Task.sleep(nanoseconds: 1_500_000_000)
        let midTransfer = (try? FileManager.default
            .attributesOfItem(atPath: partial.path)[.size] as? Int64) ?? 0

        let written = try await download.value

        XCTAssertEqual(written, Int64(total))
        XCTAssertEqual(midTransfer, 0,
            "the buffering transport must keep the partial file empty "
            + "until the transfer completes (sampled \(midTransfer) bytes "
            + "mid-transfer) — this is the old bug the streaming transport "
            + "fixes")
    }

    // MARK: - Helpers

    /// Counts progress reports.
    private final class ProgressCounter: @unchecked Sendable {
        private let lock = NSLock()
        private var n = 0
        private var bytes: Int64 = 0
        func add(_ k: Int64) {
            lock.lock(); n += 1; bytes += k; lock.unlock()
        }
        var count: Int { lock.lock(); defer { lock.unlock() }; return n }
        var total: Int64 { lock.lock(); defer { lock.unlock() }; return bytes }
    }

    private func startTrickleServer(port: UInt16,
                                    totalBytes: Int,
                                    chunkBytes: Int,
                                    gapSeconds: Double,
                                    honourRange: Bool = true) async throws -> URL {
        // v1.1.10: the flag must be rendered as Python's `True` /
        // `False` — Swift's `true`/`false` are not Python literals
        // and the script dies with a NameError before it binds.
        let honourLiteral = honourRange ? "True" : "False"
        let script = """
        import http.server, socketserver, time
        PORT = \(port)
        TOTAL = \(totalBytes)
        BLOCK = \(chunkBytes)
        GAP = \(gapSeconds)
        HONOUR = \(honourLiteral)

        class S(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def do_GET(self):
                rng = self.headers.get("Range")
                start, end = 0, TOTAL - 1
                if HONOUR and rng and rng.startswith("bytes="):
                    spec = rng.split("=", 1)[1]
                    a, _, b = spec.partition("-")
                    start = int(a)
                    end = int(b) if b else TOTAL - 1
                    end = min(end, TOTAL - 1)
                length = end - start + 1
                self.send_response(206 if (HONOUR and rng) else 200)
                self.send_header("Content-Length", str(length))
                if HONOUR and rng:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{TOTAL}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                sent = 0
                try:
                    while sent < length:
                        n = min(BLOCK, length - sent)
                        self.wfile.write(b"a" * n)
                        self.wfile.flush()
                        sent += n
                        if sent < length and GAP > 0:
                            time.sleep(GAP)
                except Exception:
                    pass
            def log_message(self, *a): pass

        with S(("127.0.0.1", PORT), H) as httpd:
            httpd.serve_forever()
        """
        return try await startServerWithScript(script, port: port)
    }

    /// Sends `pauseAfterBytes`, then pauses for `pauseSeconds`
    /// before sending the rest. The pause gives the test a
    /// guaranteed window in which the transfer is in flight but the
    /// body is not complete.
    private func startPausingServer(port: UInt16,
                                    totalBytes: Int,
                                    pauseAfterBytes: Int,
                                    pauseSeconds: Double) async throws -> URL {
        let script = """
        import http.server, socketserver, time
        PORT = \(port)
        TOTAL = \(totalBytes)
        PAUSE_AFTER = \(pauseAfterBytes)
        PAUSE_SECONDS = \(pauseSeconds)

        class S(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def do_GET(self):
                rng = self.headers.get("Range")
                start, end = 0, TOTAL - 1
                if rng and rng.startswith("bytes="):
                    spec = rng.split("=", 1)[1]
                    a, _, b = spec.partition("-")
                    start = int(a)
                    end = int(b) if b else TOTAL - 1
                    end = min(end, TOTAL - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Range", f"bytes {start}-{end}/{TOTAL}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                sent = 0
                paused = False
                try:
                    while sent < length:
                        n = min(8192, length - sent)
                        self.wfile.write(b"a" * n)
                        self.wfile.flush()
                        sent += n
                        if not paused and sent >= PAUSE_AFTER and sent < length:
                            paused = True
                            time.sleep(PAUSE_SECONDS)
                except Exception:
                    pass
            def log_message(self, *a): pass

        with S(("127.0.0.1", PORT), H) as httpd:
            httpd.serve_forever()
        """
        return try await startServerWithScript(script, port: port)
    }

    private func startForbiddenServer(port: UInt16) async throws -> URL {
        let script = """
        import http.server, socketserver
        PORT = \(port)
        class S(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True
        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def do_GET(self):
                body = b"forbidden" * 100
                self.send_response(403)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *a): pass
        with S(("127.0.0.1", PORT), H) as httpd:
            httpd.serve_forever()
        """
        return try await startServerWithScript(script, port: port)
    }

    /// Accepts the connection, sends headers, then never sends a
    /// body byte — the "dead connection" case.
    private func startSilentServer(port: UInt16) async throws -> URL {
        let script = """
        import socket, threading, time
        PORT = \(port)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", PORT))
        s.listen(8)
        def handle(conn):
            try:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 206 Partial Content\\r\\n"
                             b"Content-Length: 1024\\r\\n"
                             b"Content-Range: bytes 0-1023/1024\\r\\n"
                             b"\\r\\n")
                time.sleep(300)
            except Exception:
                pass
        while True:
            c, _ = s.accept()
            threading.Thread(target=handle, args=(c,), daemon=True).start()
        """
        return try await startServerWithScript(script, port: port)
    }

    private func startServerWithScript(_ py: String, port: UInt16) async throws -> URL {
        let scriptPath = FileManager.default.temporaryDirectory
            .appendingPathComponent("sdm-stream-server-\(UUID().uuidString).py")
        try py.write(to: scriptPath, atomically: true, encoding: .utf8)
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [scriptPath.path]
        let outPipe = Pipe()
        let errPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = errPipe
        try process.run()
        serverPID = Int32(process.processIdentifier)
        for _ in 0..<50 {
            try await Task.sleep(nanoseconds: 100_000_000)
            if await probeLocalhost(port: port) {
                return URL(string: "http://127.0.0.1:\(port)/file.bin")!
            }
        }
        // Surface the server's own error so a broken script does
        // not show up as an unexplained skip.
        let outData = outPipe.fileHandleForReading.readDataToEndOfFile()
        let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
        let out = String(data: outData, encoding: .utf8) ?? ""
        let err = String(data: errData, encoding: .utf8) ?? ""
        // Keep the script on disk — it is the only way to see the
        // Python traceback that explains the failure.
        NSLog("[SDM test] server script kept at \(scriptPath.path) port=\(port)")
        throw XCTSkip("server did not bind on port \(port). stdout=\(out) stderr=\(err) script=\(scriptPath.path)")
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
