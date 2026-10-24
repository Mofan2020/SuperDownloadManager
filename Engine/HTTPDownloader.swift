import Foundation

/// HTTPDownloader — P1 engine core. Owns the per-chunk network IO.
///
/// Strategy:
/// - One `Task` per chunk, all running concurrently up to
///   `TaskQueue`'s limit.
/// - `Range: bytes=<start>-<end-1>` for every chunk; `206 Partial
///   Content` expected. A 200 response means the server ignored
///   Range; the engine treats this as "you can't segment this
///   resource" and falls back to a single chunk.
/// - Errors classified into "retryable" (5xx, timeout, connection
///   reset) and "fatal for this link" (403, 410, 451). The latter
///   signal P3's expired-link handler.
/// - Per-chunk retries with exponential backoff capped at 5 — after
///   that the chunk is reported to the engine for re-planning.
///
/// v1.1.10 — the chunk transport streams instead of buffering.
///
/// v1.1.9 pulled a chunk with `URLSession.data(for:)`, which
/// buffers the **entire** response body in memory and returns only
/// after the last byte arrives. Measured consequences on the user's
/// link (~109 KB/s against GitHub's release CDN):
///   * the row sat at 0 bytes / 0 KB/s for the whole chunk — the
///     literal symptom the user reported;
///   * the transfer was killed by a wall-clock budget with
///     `NSURLErrorDomain -1001`, failing the whole download even
///     though the link was healthy and still moving.
///
/// The chunk now runs through a `URLSessionDataDelegate`
/// (`ChunkStreamDelegate`): body bytes are handed to the assembler
/// as they arrive, so progress is real-time, and a dead connection
/// is detected by an *idle* watchdog (no bytes for N seconds)
/// rather than by a cap on the total transfer time.
public final class HTTPDownloader: @unchecked Sendable {
    public enum Failure: Error, Sendable {
        case status(Int)
        case notPartialContent        // server returned 200, not 206
        case forbidden                // 403/410/451 — link expired family
        case tooManyRedirects
        case network(URLError)
        case decoding
        case unknown(String)
    }

    public private(set) var session: URLSession
    public let limiter: SpeedLimiter?
    public let userAgent: String
    private let lock = NSLock()
    /// Mutable so the Settings toggle can rebuild the session
    /// in place. Reads from `downloadChunk` happen on URLSession's
    /// queue; writes come from the Settings UI on the main thread;
    /// the lock guarantees the swap is atomic.
    private var trustDelegate: IgnoreSSLAuthChallengeDelegate?
    private var config: URLSessionConfiguration

    /// v1.1.10: the streaming delegate that owns the chunk
    /// transport. It is also the session's delegate, so it must
    /// answer server-trust challenges — it forwards those to
    /// `trustDelegate` when the TLS bypass is enabled.
    private let streamDelegate: ChunkStreamDelegate

    /// v1.1.10: how long a chunk may go without delivering a byte
    /// before the transfer is considered dead. This is an *idle*
    /// timeout, not a cap on the total transfer time — a slow link
    /// that keeps delivering bytes is never killed.
    ///
    /// 60 s is far above the largest observed inter-byte gap on the
    /// user's link (0.34 s measured against the release CDN) and far
    /// below the time it takes to notice a genuinely dead socket.
    public var idleTimeout: TimeInterval = 60

    /// v1.1.10: base delay for retrying a chunk that died with a
    /// retryable error (timeout, connection reset). Doubles per
    /// attempt.
    public var retryBackoff: TimeInterval = 1

    /// v1.1.10: how many times a chunk is retried before the flow
    /// gives up on it.
    public var maxRetries: Int = 3

    /// v1.1.10: test-only switch that forces the chunk transport
    /// back to the pre-v1.1.10 buffering behaviour
    /// (`URLSession.data(for:)`). It exists so the regression test
    /// can prove it actually catches the "0 KB/s" bug instead of
    /// passing vacuously. Production never sets it.
    public var forceBufferingTransport: Bool = false

    public init(session: URLSession? = nil,
                limiter: SpeedLimiter? = nil,
                userAgent: String = "SDM/1.0 (+https://github.com/Mofan2020/SuperDownloadManager)",
                ignoreSSL: Bool = false) {
        // v1.1.4: share a dedicated URLSession with a high per-host
        // connection limit across all chunk workers instead of the
        // process-wide `URLSession.shared`. `URLSession.shared` on
        // macOS silently caps at `httpMaximumConnectionsPerHost = 4`
        // since macOS 10.x — that cap was the *real* reason users
        // saw chunks completing one at a time on a fast link: the
        // 5th–8th chunk workers couldn't open their connection
        // until one of the first four finished. Bumping the cap to
        // 32 lets every configured chunk run in parallel from the
        // moment the engine spawns the TaskGroup.
        //
        // Tests and probes are still allowed to pass an explicit
        // `URLSession` (e.g. URLProtocol mocks); we only fall back
        // to building one ourselves if `nil`.
        if let session {
            self.session = session
            self.config = session.configuration
            // The user-controlled URLSession probably does not
            // honour our trust delegate; that's fine — the
            // engine's default behaviour (verify SSL) is what a
            // test author would want anyway.
            self.trustDelegate = nil
            self.streamDelegate = ChunkStreamDelegate(trust: nil)
        } else {
            let cfg = URLSessionConfiguration.default
            cfg.httpMaximumConnectionsPerHost = 32
            // v1.1.10: `timeoutIntervalForRequest` is an *idle*
            // timeout for data tasks — it resets whenever a byte
            // arrives — so a slow link is not killed by it. The
            // wall-clock budget that used to break downloads was
            // the combination of the buffered transport with a
            // finite resource timeout; both are now generous, and
            // dead connections are caught by `idleTimeout` per
            // chunk instead.
            cfg.timeoutIntervalForRequest = 60
            cfg.timeoutIntervalForResource = 3600
            cfg.waitsForConnectivity = false
            cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
            self.config = cfg
            let delegate = IgnoreSSLAuthChallengeDelegate(ignoreSSL: ignoreSSL)
            self.trustDelegate = delegate
            let stream = ChunkStreamDelegate(trust: delegate)
            self.streamDelegate = stream
            self.session = URLSession(configuration: cfg,
                                      delegate: stream,
                                      delegateQueue: nil)
        }
        self.limiter = limiter
        self.userAgent = userAgent
    }

    /// Flip the SSL bypass on/off. URLSession is configured
    /// with a fixed delegate per session, so flipping
    /// requires a fresh session. This is called from the
    /// Settings UI on the main thread; it briefly
    /// invalidates the in-flight session, which means
    /// in-progress chunk workers will see connection-loss
    /// errors that the engine converts to user-visible
    /// reattempts on the next request.
    public func setIgnoreSSL(_ value: Bool) {
        lock.lock()
        let delegate = trustDelegate
        let cfg = config
        lock.unlock()
        guard let delegate else { return }
        delegate.setIgnoreSSL(value)
        if value {
            downloadLog.warning("SSL bypass enabled for downloader session")
        }
        // The session still uses the same delegate, so a true
        // flip from off→on or on→off only requires updating
        // the flag on the existing delegate. No need to
        // rebuild the session.
        _ = cfg  // silence unused if we ever swap to rebuild
    }

    /// Apply a v1.0.4 P2-3 `SiteRule` (if any matches `url`) to
    /// the request. Headers + cookies + basic auth are added
    /// inline; the URLSession itself is shared.
    public func apply(siteRule: SiteRule?, to req: inout URLRequest, for url: URL) {
        req.setValue(userAgent, forHTTPHeaderField: "User-Agent")
        guard let r = siteRule else { return }
        if let referer = r.referer, !referer.isEmpty {
            req.setValue(referer, forHTTPHeaderField: "Referer")
        }
        if let ua = r.userAgent, !ua.isEmpty {
            req.setValue(ua, forHTTPHeaderField: "User-Agent")
        }
        if let cookies = r.cookies, !cookies.isEmpty {
            req.setValue(cookies, forHTTPHeaderField: "Cookie")
        }
        if let u = r.basicAuthUser, let p = SiteRuleStore.shared.getPassword(
            account: u, host: r.host) {
            let raw = "\(u):\(p)"
            if let token = raw.data(using: .utf8)?.base64EncodedString() {
                req.setValue("Basic \(token)", forHTTPHeaderField: "Authorization")
            }
        }
    }

    /// Convenience: build a request for `url` with the matching
    /// `SiteRule` applied.
    public func request(for url: URL) -> URLRequest {
        var req = URLRequest(url: url)
        apply(siteRule: SiteRuleStore.shared.match(url: url),
              to: &req, for: url)
        return req
    }

    /// Stream a single chunk to the supplied `FileAssembler`.
    /// Returns the total bytes successfully persisted.
    ///
    /// v1.1.10: bytes are written to the chunk's partial file *as
    /// they arrive*, so the progress channel and the on-disk partial
    /// grow together with the transfer. The transport is retried on
    /// a retryable failure, resuming from whatever was already
    /// persisted (the chunk's `Range` header moves forward), so a
    /// dropped connection costs a reconnect rather than the whole
    /// download.
    public func downloadChunk(
        url: URL,
        chunk: Chunk,
        assembler: FileAssembler,
        maxBytes: Int64? = nil,
        progress: (@Sendable (Int64) -> Void)? = nil
    ) async throws -> Int64 {
        // v1.1.3: when resuming from a previously-persisted partial,
        // skip the bytes already on disk by asking the server for
        // `bytes=<start+bytesDone>-<end-1>` instead of
        // `bytes=<start>-<end-1>`.
        let resumeFrom = chunk.bytesDone
        let remaining = chunk.end - resumeFrom
        guard remaining > 0 else { return 0 }

        var lastError: Error?
        for attempt in 0...maxRetries {
            if Task.isCancelled { throw CancellationError() }
            // Each attempt resumes from what is already on disk.
            let alreadyOnDisk = try assembler.bytesWritten(for: chunk.id)
            let start = chunk.start + alreadyOnDisk
            if start >= chunk.end { return alreadyOnDisk }
            do {
                return try await streamChunk(
                    url: url,
                    chunk: chunk,
                    assembler: assembler,
                    from: start,
                    progress: progress)
            } catch let e as Failure {
                // A rejected response is a property of the *link*,
                // not of the moment — retrying it just burns time.
                switch e {
                case .forbidden, .notPartialContent, .decoding:
                    throw e
                default:
                    lastError = e
                }
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                lastError = error
            }
            let backoff = retryBackoff * pow(2.0, Double(attempt))
            downloadLog.notice("chunk \(chunk.id) attempt \(attempt + 1) failed (\(lastError.map { String(describing: $0) } ?? "unknown")) — retrying in \(String(format: "%.1f", backoff))s")
            try? await Task.sleep(nanoseconds: UInt64(backoff * 1_000_000_000))
        }
        throw lastError ?? Failure.unknown("chunk \(chunk.id) exhausted retries")
    }

    /// One streaming attempt at a chunk. The bytes already
    /// persisted for this chunk are kept; the `Range` header asks
    /// the server for the remainder.
    private func streamChunk(
        url: URL,
        chunk: Chunk,
        assembler: FileAssembler,
        from startOffset: Int64,
        progress: (@Sendable (Int64) -> Void)?
    ) async throws -> Int64 {
        var req = URLRequest(url: url)
        apply(siteRule: SiteRuleStore.shared.match(url: url),
              to: &req, for: url)
        // v1.1.10: the request no longer carries a wall-clock cap —
        // the chunk may legitimately take many minutes on a slow
        // link. The *idle* watchdog below is what detects a dead
        // connection.
        req.timeoutInterval = 3600
        req.setValue("bytes=\(startOffset)-\(chunk.end - 1)",
                     forHTTPHeaderField: "Range")

        // Open the per-chunk partial up-front so the seek is set
        // before any bytes flow. `chunk.start` is the absolute
        // file offset this chunk occupies in the eventual
        // destination; it's the right value for both fresh and
        // resumed downloads (the assembler records it under
        // `chunkStarts[chunk.id]`).
        _ = try await assembler.openChunk(chunkID: chunk.id,
                                          startOffset: chunk.start)

        let chunkStartWall = Date()
        downloadLog.notice("chunk \(chunk.id) start GET \(url.absoluteString) range=bytes=\(startOffset)-\(chunk.end - 1)")

        // v1.1.10: the transport can be forced back to the old
        // buffering behaviour (`URLSession.data(for:)`) so the
        // regression test can prove it actually catches the bug.
        // Production never sets this.
        if forceBufferingTransport {
            let (bufferedData, bufferedResponse) = try await session.data(for: req)
            guard bufferedResponse is HTTPURLResponse else {
                throw Failure.decoding
            }
            let bufferedWritten = try await assembler.append(bufferedData, to: chunk.id)
            progress?(bufferedWritten)
            return bufferedWritten
        }

        // The stream's own bookkeeping.
        let state = ChunkStreamState()
        let writer = ChunkWriter(assembler: assembler, chunk: chunk, progress: progress)
        let task = session.dataTask(with: req)

        // Reject a bad response *before* any body byte is written.
        // `dataTask(with:)` returns a task whose delegate is the
        // session's delegate — our `ChunkStreamDelegate` — so the
        // validator below is consulted in `didReceive response:`.
        // The validator returns the *reason* for the rejection, so
        // the caller sees a 403 rather than a generic cancellation.
        streamDelegate.setValidator({ http in
            switch http.statusCode {
            case 200, 206:
                return nil
            case 403, 410, 451:
                return HTTPDownloader.Failure.forbidden
            case 429, 500..<600:
                return HTTPDownloader.Failure.status(http.statusCode)
            default:
                return HTTPDownloader.Failure.unknown("status \(http.statusCode)")
            }
        }, for: task)

        // The sink writes to disk as bytes arrive. The delegate
        // queue must not block, so the write is handed to a
        // serialised writer task.
        streamDelegate.register(task: task, sink: { data in
            state.deliver(data.count)
            writer.submit(data)
        }, completion: { result in
            state.finish(result)
        })

        task.resume()

        // Wait for the transfer to complete, the idle watchdog to
        // fire, or the task to be cancelled.
        let outcome = await state.wait(timeout: idleTimeout)
        switch outcome {
        case .completed(let result):
            // Flush any bytes the writer has not persisted yet.
            await writer.drain()
            switch result {
            case .success(let response):
                let headersReceivedWall = Date()
                let responseHead = "status=\(response.statusCode) contentLength=\(response.value(forHTTPHeaderField: "Content-Length") ?? "<nil>") contentRange=\(response.value(forHTTPHeaderField: "Content-Range") ?? "<nil>")"
                switch response.statusCode {
                case 200, 206:
                    break
                case 403, 410, 451:
                    downloadLog.notice("chunk \(chunk.id) http=4xx \(responseHead) elapsedHeaders=\(headersReceivedWall.timeIntervalSince(chunkStartWall))s")
                    throw Failure.forbidden
                case 429, 500..<600:
                    downloadLog.notice("chunk \(chunk.id) http=5xx \(responseHead) elapsedHeaders=\(headersReceivedWall.timeIntervalSince(chunkStartWall))s")
                    throw Failure.status(response.statusCode)
                default:
                    downloadLog.notice("chunk \(chunk.id) http=? \(responseHead) elapsedHeaders=\(headersReceivedWall.timeIntervalSince(chunkStartWall))s")
                    throw Failure.unknown("status \(response.statusCode)")
                }
                let written = await writer.total()
                downloadLog.notice("chunk \(chunk.id) done bytes=\(written) elapsed=\(headersReceivedWall.timeIntervalSince(chunkStartWall))s")
                return written
            case .failure(let error):
                let written = await writer.total()
                if let urlError = error as? URLError {
                    downloadLog.notice("chunk \(chunk.id) network failure code=\(urlError.code) written=\(written) elapsed=\(Date().timeIntervalSince(chunkStartWall))s")
                    throw Failure.network(urlError)
                }
                downloadLog.notice("chunk \(chunk.id) failure written=\(written) elapsed=\(Date().timeIntervalSince(chunkStartWall))s error=\(error)")
                throw error
            }
        case .idleTimeout:
            task.cancel()
            await writer.drain()
            let written = await writer.total()
            downloadLog.notice("chunk \(chunk.id) idle timeout after \(idleTimeout)s with no data — written=\(written)")
            throw Failure.network(URLError(.timedOut))
        case .cancelled:
            task.cancel()
            throw CancellationError()
        }
    }

    /// Send a probe that's safe to retry on misbehaving servers.
    public func probe(_ url: URL) async throws -> ContentProbe {
        try await ContentSniffer.probe(url, session: session)
    }
}

// MARK: - Streaming plumbing

/// Tracks a chunk's transfer state: completion, the last time a
/// byte arrived, and whether bytes are still flowing.
///
/// The idle watchdog lives here: `wait(timeout:)` returns
/// `.idleTimeout` when no byte has arrived for that long, which is
/// what distinguishes "slow but alive" from "dead".
///
/// The lock is only ever taken from the synchronous helpers, never
/// from an async context — Swift 6 forbids `NSLock` in an async
/// function.
final class ChunkStreamState: @unchecked Sendable {
    enum Outcome {
        case completed(Result<HTTPURLResponse, Error>)
        case idleTimeout
        case cancelled
    }

    private let lock = NSLock()
    private var result: Result<HTTPURLResponse, Error>?
    private var lastByteAt: Date
    private var finished = false

    init() { lastByteAt = Date() }

    /// Called from the delegate's data sink — records that bytes
    /// are still flowing.
    func deliver(_ count: Int) {
        guard count > 0 else { return }
        lock.lock()
        lastByteAt = Date()
        lock.unlock()
    }

    /// Called when the transfer finishes.
    func finish(_ result: Result<HTTPURLResponse, Error>) {
        lock.lock()
        if !finished {
            finished = true
            self.result = result
        }
        lock.unlock()
    }

    /// Snapshot of the transfer state, taken under the lock.
    private func snapshot() -> (result: Result<HTTPURLResponse, Error>?, lastByteAt: Date) {
        lock.lock()
        defer { lock.unlock() }
        return (result, lastByteAt)
    }

    /// Wait for the transfer to finish, the idle watchdog to fire,
    /// or the caller to be cancelled.
    func wait(timeout: TimeInterval) async -> Outcome {
        while true {
            if Task.isCancelled { return .cancelled }
            let snap = snapshot()
            if let r = snap.result {
                return .completed(r)
            }
            let idleFor = Date().timeIntervalSince(snap.lastByteAt)
            if idleFor >= timeout {
                // Re-check under the lock: the transfer may have
                // completed in the window between the snapshot and
                // the timeout decision.
                if let r = snapshot().result {
                    return .completed(r)
                }
                return .idleTimeout
            }
            // Sleep in short slices so cancellation is noticed
            // promptly and the idle check stays accurate.
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
    }
}

/// Persists a chunk's bytes to disk in order, off the delegate
/// queue.
///
/// The delegate's `didReceive data:` callback runs on URLSession's
/// delegate queue; writing there would block the whole session
/// (and every other chunk sharing it). Instead the bytes are
/// appended to a queue and a single writer task drains it.
///
/// The writer is *serialised per chunk*, so the bytes land in the
/// partial file in the order the server sent them.
actor ChunkWriter {
    private let assembler: FileAssembler
    private let chunk: Chunk
    private let progress: (@Sendable (Int64) -> Void)?

    private var pending: [Data] = []
    private var pendingBytes = 0
    private var written: Int64 = 0
    private var drainTask: Task<Void, Never>?

    init(assembler: FileAssembler,
         chunk: Chunk,
         progress: (@Sendable (Int64) -> Void)?) {
        self.assembler = assembler
        self.chunk = chunk
        self.progress = progress
    }

    /// Hand a freshly-arrived body chunk to the writer.
    nonisolated func submit(_ data: Data) {
        // `submit` is called from the delegate queue; hop onto the
        // actor to append to the queue.
        Task { await self.enqueue(data) }
    }

    private func enqueue(_ data: Data) {
        pending.append(data)
        pendingBytes += data.count
        if drainTask == nil {
            drainTask = Task { await self.drainLoop() }
        }
    }

    /// Write everything that is queued, then wait for the writer to
    /// be idle. Called when the transfer ends so no bytes are lost.
    func drain() async {
        // Wait for the current drain loop to finish.
        while let t = drainTask {
            await t.value
            if drainTask == nil { break }
            drainTask = nil
            break
        }
        await flush()
    }

    private func drainLoop() async {
        while !pending.isEmpty {
            await flush()
        }
        drainTask = nil
    }

    private func flush() async {
        while !pending.isEmpty {
            let batch = pending
            pending.removeAll(keepingCapacity: true)
            pendingBytes = 0
            for data in batch {
                do {
                    let n = try await assembler.append(data, to: chunk.id)
                    written += n
                    progress?(n)
                } catch {
                    downloadLog.error("chunk \(chunk.id) write failed: \(error)")
                }
            }
        }
    }

    func total() -> Int64 { written }
}
