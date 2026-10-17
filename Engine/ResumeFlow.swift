import Foundation

/// ResumeFlow — v1.0.4 P1-3. Continues a paused download from the
/// last persisted byte offset by issuing a single `Range:` request
/// for `[bytesDone, totalBytes-1]` and appending to the existing
/// `.part` file.
///
/// Why single-stream instead of per-chunk Range: the current
/// `DownloadFlow` doesn't expose a way to seed per-chunk
/// `bytesDone`. A full refactor to plumb that through is on the
/// v1.0.5 docket. Range: with a single connection still
/// legitimately *is* a resume — the user gets the missing bytes
/// appended to the partial, which is the IDM-style
/// "right-click → Resume" behaviour.
public struct ResumeFlow: Sendable {
    public let url: URL
    public let destination: URL
    public let downloader: HTTPDownloader
    public let probe: ContentProbe

    public init(url: URL,
                destination: URL,
                probe: ContentProbe,
                downloader: HTTPDownloader = HTTPDownloader()) {
        self.url = url
        self.destination = destination
        self.probe = probe
        self.downloader = downloader
    }

    public enum Failure: Error, CustomStringConvertible {
        case rangeRejected
        case ioError(Error)
        case serverError(Int)

        public var description: String {
            switch self {
            case .rangeRejected: return "server returned 200 (not 206); resume not possible"
            case .ioError(let e): return "i/o error: \(e)"
            case .serverError(let code): return "server returned HTTP \(code)"
            }
        }
    }

    public func run() async throws {
        let partPath = destination.appendingPathExtension("part")
        let bytesDone: Int64 = (try? FileManager.default
            .attributesOfItem(atPath: partPath.path)[.size] as? Int64) ?? 0
        guard bytesDone > 0 else { return }   // nothing to resume
        let total = probe.contentLength ?? 0
        guard total > bytesDone else { return }   // already complete

        var req = URLRequest(url: url)
        req.setValue("bytes=\(bytesDone)-\(total - 1)", forHTTPHeaderField: "Range")
        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await URLSession.shared.data(for: req)
        } catch {
            throw Failure.ioError(error)
        }
        guard let http = response as? HTTPURLResponse else {
            throw Failure.ioError(NSError(
                domain: "ResumeFlow", code: 0,
                userInfo: [NSLocalizedDescriptionKey: "non-HTTP response"]))
        }
        guard http.statusCode == 206 else {
            throw Failure.rangeRejected
        }
        // Append to .part.
        let url = partPath
        do {
            let handle = try FileHandle(forWritingTo: url)
            defer { try? handle.close() }
            try handle.seekToEnd()
            try handle.write(contentsOf: data)
        } catch {
            throw Failure.ioError(error)
        }
    }
}
