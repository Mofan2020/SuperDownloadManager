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

    public init(session: URLSession = .shared,
                limiter: SpeedLimiter? = nil,
                userAgent: String = "SDM/1.0 (+https://github.com/Mofan2020/SuperDownloadManager)") {
        self.session = session
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
        var req = URLRequest(url: url)
        apply(siteRule: SiteRuleStore.shared.match(url: url),
              to: &req, for: url)
        req.setValue(chunk.rangeHeader(), forHTTPHeaderField: "Range")
        req.setValue(userAgent, forHTTPHeaderField: "User-Agent")
        req.timeoutInterval = 60

        let (bytesStream, response) = try await session.bytes(for: req)

        guard let http = response as? HTTPURLResponse else {
            throw Failure.decoding
        }
        switch http.statusCode {
        case 200:
            throw Failure.notPartialContent
        case 206:
            break  // expected path
        case 403, 410, 451:
            throw Failure.forbidden
        case 429, 500..<600:
            throw Failure.status(http.statusCode)
        default:
            throw Failure.unknown("status \(http.statusCode)")
        }

        // Open the per-chunk partial up-front so the seek is set
        // before any bytes flow.
        _ = try await assembler.openChunk(chunkID: chunk.id,
                                          startOffset: chunk.bytesDone)

        // v1.0 streams bytes via `URLSession.bytes`. The `URLSession.AsyncBytes`
        // type on macOS 26 SDK only exposes byte-level iteration, which
        // forces an actor hop on every byte when combined with the
        // limiter. We work around that by:
        //   1. reading a moderate buffer per iteration
        //   2. flush to disk only when the buffer crosses 64 KB
        // The actor hop still happens once per underlying socket
        // read (not once per byte) because `URLSession.AsyncBytes`
        // already coalesces server-side buffer blocks; the per-byte
        // `for await` loop's hop overhead is paid only on the
        // actor side, not the network side. This is acceptable
        // for the v1.0 single-task model.
        var buf = Data()
        buf.reserveCapacity(64 * 1024)
        var written: Int64 = 0

        for try await byte in bytesStream {
            if Task.isCancelled { break }
            // Cap the chunk at `chunk.end - chunk.bytesDone`. The
            // server returns 206 with `Content-Length: chunk.size`,
            // but rare chunked encodings advertise more than we asked
            // for. Honor the planned range, not the response header.
            let cap = chunk.end - chunk.bytesDone
            let remaining: Int64 = cap - written
            if remaining <= 0 { break }
            buf.append(byte)
            // Flush when buf is large enough OR we've hit the cap.
            let flushNow = buf.count >= 64 * 1024
                || (Int64(buf.count) >= remaining)
            if flushNow {
                let toWrite: Data
                if Int64(buf.count) > remaining {
                    toWrite = Data(buf.prefix(Int(remaining)))
                } else {
                    toWrite = buf
                }
                await limiter?.consume(toWrite.count)
                let n = try await assembler.append(toWrite, to: chunk.id)
                written += n
                progress?(n)
                if Int64(buf.count) > remaining {
                    buf.removeAll(keepingCapacity: true)
                    break
                }
                buf.removeAll(keepingCapacity: true)
            }
        }
        if !buf.isEmpty {
            let cap = chunk.end - chunk.bytesDone
            let remaining: Int64 = max(0, cap - written)
            let toWrite: Data = (Int64(buf.count) > remaining && remaining > 0)
                ? Data(buf.prefix(Int(remaining)))
                : buf
            if !toWrite.isEmpty {
                await limiter?.consume(toWrite.count)
                let n = try await assembler.append(toWrite, to: chunk.id)
                written += n
                progress?(n)
            }
        }

        return written
    }

    /// Send a probe that's safe to retry on misbehaving servers.
    public func probe(_ url: URL) async throws -> ContentProbe {
        try await ContentSniffer.probe(url, session: session)
    }
}
