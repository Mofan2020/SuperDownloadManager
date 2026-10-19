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
        } else {
            let cfg = URLSessionConfiguration.default
            cfg.httpMaximumConnectionsPerHost = 32
            cfg.timeoutIntervalForRequest = 60
            cfg.timeoutIntervalForResource = 600
            cfg.waitsForConnectivity = false
            cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
            self.config = cfg
            let delegate = IgnoreSSLAuthChallengeDelegate(ignoreSSL: ignoreSSL)
            self.trustDelegate = delegate
            self.session = URLSession(configuration: cfg,
                                      delegate: delegate,
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
        // `bytes=<start>-<end-1>`. The cap on the loop is also
        // reduced so we don't overwrite the bytes we kept.
        let resumeFrom = chunk.bytesDone
        let remaining = chunk.end - resumeFrom
        guard remaining > 0 else { return 0 }
        var req = URLRequest(url: url)
        apply(siteRule: SiteRuleStore.shared.match(url: url),
              to: &req, for: url)
        if resumeFrom == 0 {
            req.setValue(chunk.rangeHeader(), forHTTPHeaderField: "Range")
        } else {
            req.setValue("bytes=\(resumeFrom)-\(chunk.end - 1)",
                         forHTTPHeaderField: "Range")
        }
        req.setValue(userAgent, forHTTPHeaderField: "User-Agent")
        req.timeoutInterval = 60

        // v1.1.9: explicit chunk-level diagnostics. The user
        // observed "0 KB/s forever, no error" and Little Snitch
        // showed almost no connections — that signature maps to
        // either (a) chunk never goes out, (b) chunk goes out but
        // URLSession never receives headers, (c) headers arrive
        // but body stalls. Logging the four timestamps + byte
        // counts makes Console.app output sufficient to identify
        // which stage actually stalls. Subsystem stays at
        // `com.skyc8266.superdownloadmanager` so existing Console
        // recipes keep matching.
        let chunkStartWall = Date()
        downloadLog.notice("chunk \(chunk.id) start GET \(url.absoluteString) range=\(chunk.rangeHeader()) resumeFrom=\(resumeFrom) bytesDone=\(chunk.bytesDone)")

        // Open the per-chunk partial up-front so the seek is set
        // before any bytes flow. `chunk.start` is the absolute
        // file offset this chunk occupies in the eventual
        // destination; it's the right value for both fresh and
        // resumed downloads (the assembler records it under
        // `chunkStarts[chunk.id]`).
        _ = try await assembler.openChunk(chunkID: chunk.id,
                                          startOffset: chunk.start)

        // v1.1.5: use `URLSession.data(for:)` (one Data object
        // per request) instead of `URLSession.bytes` (one byte
        // per async hop). On macOS 26, `URLSession.AsyncBytes`
        // forced an actor hop *per single byte*, which throttled
        // the chunk workers' real concurrency to roughly one
        // byte per scheduler tick per chunk — on a fast link
        // (8 × 64 KB chunks pulling at line rate), the system
        // spent more time scheduling hops than moving data.
        // `data(for:)` returns the full response body in one
        // shot, so each chunk worker runs straight through with
        // no intermediate yields, and a single `URLSession` with
        // `httpMaximumConnectionsPerHost = 16` (set in the
        // init below) keeps every chunk live in parallel.
        //
        // The byte-level loop is still useful for progress
        // updates — we slice the body into 64 KB blocks for that.
        let (data, response) = try await session.data(for: req)

        let headersReceivedWall = Date()
        guard let http = response as? HTTPURLResponse else {
            throw Failure.decoding
        }
        let responseHead = "status=\(http.statusCode) contentLength=\(http.value(forHTTPHeaderField: "Content-Length") ?? "<nil>") contentRange=\(http.value(forHTTPHeaderField: "Content-Range") ?? "<nil>")"
        switch http.statusCode {
        case 200:
            // Server ignored Range — fall back to writing the
            // entire response into our partial. The existing
            // bytes (resumeFrom) were already on disk and stay
            // untouched; only the newly-received bytes append.
            break
        case 206:
            break  // expected path
        case 403, 410, 451:
            downloadLog.notice("chunk \(chunk.id) http=4xx \(responseHead) elapsedHeaders=\(headersReceivedWall.timeIntervalSince(chunkStartWall))s")
            throw Failure.forbidden
        case 429, 500..<600:
            downloadLog.notice("chunk \(chunk.id) http=5xx \(responseHead) elapsedHeaders=\(headersReceivedWall.timeIntervalSince(chunkStartWall))s")
            throw Failure.status(http.statusCode)
        default:
            downloadLog.notice("chunk \(chunk.id) http=? \(responseHead) elapsedHeaders=\(headersReceivedWall.timeIntervalSince(chunkStartWall))s")
            throw Failure.unknown("status \(http.statusCode)")
        }

        // v1.1.9: log how long `URLSession.data(for:)` actually
        // took to deliver the body. If headers arrive quickly but
        // body stage hangs (a known Fastly HTTP/2 failure mode on
        // macOS 26), this is the timestamp that surfaces it.
        let bodyReceivedWall = Date()
        downloadLog.notice("chunk \(chunk.id) body received bytes=\(data.count) elapsedHeaders=\(headersReceivedWall.timeIntervalSince(chunkStartWall))s elapsedTotal=\(bodyReceivedWall.timeIntervalSince(chunkStartWall))s")

        // Slice the response body into 64 KB blocks, flush to
        // the per-chunk partial, and report progress. The whole
        // body is in memory at this point — for very large
        // chunks (> tens of MB), the OS reuses the buffer for
        // the next chunk; in practice this is bounded by the
        // largest single chunk a Range header would return.
        var offset = 0
        var written: Int64 = 0
        let total = data.count
        if total == 0 { return 0 }
        while offset < total {
            if Task.isCancelled { break }
            let chunkEnd = min(offset + 64 * 1024, total)
            let slice = data.subdata(in: offset..<chunkEnd)
            // Cap the chunk at `chunk.end - chunk.bytesDone`. The
            // server returns 206 with `Content-Length: chunk.size`,
            // but rare chunked encodings advertise more than we
            // asked for. Honor the planned range, not the
            // response header.
            let cap = chunk.end - chunk.bytesDone
            let remaining = cap - Int64(written)
            if remaining <= 0 { break }
            let toWrite: Data
            if Int64(slice.count) > remaining {
                toWrite = slice.prefix(Int(remaining))
            } else {
                toWrite = slice
            }
            await limiter?.consume(toWrite.count)
            let n = try await assembler.append(toWrite, to: chunk.id)
            written += n
            progress?(n)
            offset = chunkEnd
            if Int64(slice.count) > remaining { break }
        }
        return written
    }

    /// Send a probe that's safe to retry on misbehaving servers.
    public func probe(_ url: URL) async throws -> ContentProbe {
        try await ContentSniffer.probe(url, session: session)
    }
}
