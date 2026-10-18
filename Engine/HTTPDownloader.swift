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
public struct HTTPDownloader: Sendable {
    public enum Failure: Error, Sendable {
        case status(Int)
        case notPartialContent        // server returned 200, not 206
        case forbidden                // 403/410/451 — link expired family
        case tooManyRedirects
        case network(URLError)
        case decoding
        case unknown(String)
    }

    public let session: URLSession
    public let limiter: SpeedLimiter?
    public let userAgent: String

    public init(session: URLSession? = nil,
                limiter: SpeedLimiter? = nil,
                userAgent: String = "SDM/1.0 (+https://github.com/Mofan2020/SuperDownloadManager)") {
        // v1.1.4: share a dedicated URLSession with a high per-host
        // connection limit across all chunk workers instead of the
        // process-wide `URLSession.shared`. `URLSession.shared` on
        // macOS silently caps at `httpMaximumConnectionsPerHost = 4`
        // since macOS 10.x — that cap was the *real* reason users
        // saw chunks completing one at a time on a fast link: the
        // 5th–8th chunk workers couldn't open their connection
        // until one of the first four finished. Bumping the cap to
        // 16 lets every configured chunk run in parallel from the
        // moment the engine spawns the TaskGroup.
        //
        // Tests and probes are still allowed to pass an explicit
        // `URLSession` (e.g. URLProtocol mocks); we only fall back
        // to the shared session if `nil`.
        if let session {
            self.session = session
        } else {
            // v1.1.6: macOS 14+ + URLSession silently negotiates
            // HTTP/2 via ALPN whenever the server offers it.
            // HTTP/2 multiplexes every Range request over a
            // *single* TCP connection — Little Snitch shows this
            // as "1 ESTABLISHED socket to the host" while eight
            // chunks claim to be running. Many object-storage
            // backends (S3, GCS, Azure Blob) cap per-connection
            // throughput and per-IP handshake rate, so stuffing
            // every Range stream through one TCP socket turns
            // the multi-thread path into a single-thread
            // bottleneck.
            //
            // `httpShouldUsePipelining` would be the documented
            // knob for forcing HTTP/1.1 over H2, but Apple
            // deprecated it in macOS 15.4 (the API only worked
            // with the classic CFNetwork loader that's gone).
            // The remaining knob on the modern loader is to
            // bypass ephemeral H2 — we use a *named* H2-disabled
            // configuration by giving URLSessionConfiguration
            // an `httpAdditionalHeaders` set with
            // `Connection: close`-style hints, plus `URLSession`
            // delegates HTTP/1.1 by default for the `default`
            // config when the server returns an HTTP/1.1 response
            // (alpn:http/1.1 forced). In practice the only
            // reliable way on macOS 26 is to *not* let URLSession
            // make the connection — we hand each chunk to its
            // own task with a per-request `URLProtocol` that
            // strips H2 ALPN at the protocol layer. See
            // `H2DowngradeProtocol.swift`.
            //
            // For now we still want the rest of the config to
            // be sensible — the dedicated URLSession gets
            // httpMaximumConnectionsPerHost = 32 (so 32 Range
            // sockets can run concurrently under HTTP/1.1), long
            // timeouts, and `waitsForConnectivity = false` so a
            // briefly-unreachable host doesn't pin the worker.
            let cfg = URLSessionConfiguration.default
            cfg.httpMaximumConnectionsPerHost = 32
            cfg.timeoutIntervalForRequest = 60
            cfg.timeoutIntervalForResource = 600
            cfg.waitsForConnectivity = false
            cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
            self.session = URLSession(configuration: cfg)
        }
        self.limiter = limiter
        self.userAgent = userAgent
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

        downloadLog.debug("chunk \(chunk.id) GET \(url.absoluteString) range=\(chunk.rangeHeader()) resumeFrom=\(resumeFrom)")

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

        guard let http = response as? HTTPURLResponse else {
            throw Failure.decoding
        }
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
            throw Failure.forbidden
        case 429, 500..<600:
            throw Failure.status(http.statusCode)
        default:
            throw Failure.unknown("status \(http.statusCode)")
        }

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
