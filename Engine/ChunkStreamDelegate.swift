import Foundation

/// ChunkStreamDelegate — bridges `URLSessionDataDelegate` callbacks
/// to async callers so a chunk's bytes can be written to disk while
/// the transfer is still running.
///
/// Why this exists (v1.1.10):
///
/// The engine used `URLSession.data(for:)` to pull a chunk. That API
/// **buffers the entire response body in memory** and only returns
/// once the last byte has arrived. Two consequences, both observed
/// by the user as "downloads sit at 0 KB/s":
///
/// 1. Nothing reaches the disk (and nothing is reported to the
///    progress channel) until the chunk is *completely* downloaded.
///    Measured on the user's link (~109 KB/s against GitHub's
///    release CDN): a 2.19 MB chunk takes ~20 s, during which the
///    row shows a frozen 0-byte / 0 KB/s state.
/// 2. The transfer was capped by a wall-clock budget, so a slow but
///    perfectly healthy link was aborted with
///    `NSURLErrorDomain -1001` and the whole download failed —
///    there was no retry and no single-stream fallback.
///
/// The delegate's `didReceive data:` callback fires per network
/// packet, so bytes land on disk while the transfer runs, and the
/// engine can decide — per chunk — whether the response is
/// acceptable *before* any body byte is written.
///
/// Benchmarked against a local 64 MB server with 8 concurrent 1 MB
/// range requests (see `docs/notes.md`):
///
/// | transport                      | throughput |
/// |--------------------------------|------------|
/// | `data(for:)` (v1.1.9)          | 433 MB/s   |
/// | `bytes(for:)` + coalescing     | 13.5 MB/s  |
/// | delegate (`didReceive data:`)  | 4169 MB/s  |
///
/// The delegate shape is both the fastest and the only one that
/// hands bytes to the caller as they arrive.
///
/// This type is deliberately internal: the public surface stays
/// `HTTPDownloader.downloadChunk`.
final class ChunkStreamDelegate: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    /// Receives each body chunk as it arrives.
    typealias DataSink = @Sendable (Data) -> Void
    /// Completes when the transfer finishes.
    typealias Completion = @Sendable (Result<HTTPURLResponse, Error>) -> Void
    /// Inspects a response *before* its body is delivered. Return
    /// `nil` to accept the response, or an error to reject it — a
    /// rejected response is cancelled before a single body byte
    /// reaches the disk, so the partial file is never polluted, and
    /// the *rejection reason* (not the follow-up cancellation) is
    /// what the caller sees.
    typealias ResponseValidator = @Sendable (HTTPURLResponse) -> Error?

    private let lock = NSLock()
    private var entries: [Int: Entry] = [:]
    /// Validators installed before the task was registered. The
    /// engine installs the validator before it wires the sink, and
    /// the two can therefore arrive in either order.
    private var pendingValidators: [Int: ResponseValidator] = [:]

    /// The TLS-bypass helper. When present, server-trust challenges
    /// are answered by it (see `IgnoreSSLAuthChallengeDelegate`).
    private let trust: IgnoreSSLAuthChallengeDelegate?

    private struct Entry {
        var sink: DataSink
        var completion: Completion
        var response: HTTPURLResponse?
        var validator: ResponseValidator?
        /// Set when the response was rejected. The completion is
        /// deferred until `didCompleteWithError` so the caller
        /// always sees exactly one terminal event — and it reports
        /// the *rejection* rather than the cancellation error.
        var rejection: Error?
    }

    init(trust: IgnoreSSLAuthChallengeDelegate?) {
        self.trust = trust
        super.init()
    }

    /// Register a task's sink + completion. Must be called before
    /// the task is resumed.
    ///
    /// v1.1.10: `register` must not clear a validator that was
    /// already installed for this task — the engine installs the
    /// validator *before* it registers the sink, and wiping it here
    /// let a rejected response (403) write its body to the partial
    /// file.
    func register(task: URLSessionTask,
                  sink: @escaping DataSink,
                  completion: @escaping Completion) {
        lock.lock()
        let validator = pendingValidators.removeValue(forKey: task.taskIdentifier)
        if var existing = entries[task.taskIdentifier] {
            existing.sink = sink
            existing.completion = completion
            existing.validator = existing.validator ?? validator
            entries[task.taskIdentifier] = existing
        } else {
            entries[task.taskIdentifier] = Entry(sink: sink,
                                                 completion: completion,
                                                 response: nil,
                                                 validator: validator,
                                                 rejection: nil)
        }
        lock.unlock()
    }

    /// Attach a per-task response validator. Called before the task
    /// is resumed.
    func setValidator(_ validator: @escaping ResponseValidator,
                      for task: URLSessionTask) {
        lock.lock()
        if entries[task.taskIdentifier] != nil {
            entries[task.taskIdentifier]?.validator = validator
        } else {
            pendingValidators[task.taskIdentifier] = validator
        }
        lock.unlock()
    }

    // MARK: - URLSessionDataDelegate

    func urlSession(_ session: URLSession,
                    dataTask: URLSessionDataTask,
                    didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        guard let http = response as? HTTPURLResponse else {
            completionHandler(.cancel)
            return
        }
        lock.lock()
        entries[dataTask.taskIdentifier]?.response = http
        let validator = entries[dataTask.taskIdentifier]?.validator
        lock.unlock()

        // Let the caller's validator decide. A rejected response is
        // cancelled here so no body byte reaches the disk; the
        // rejection reason is kept so the caller sees *why* the
        // response was refused rather than a generic cancellation.
        if let validator, let rejection = validator(http) {
            lock.lock()
            entries[dataTask.taskIdentifier]?.rejection = rejection
            lock.unlock()
            completionHandler(.cancel)
            return
        }
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession,
                    dataTask: URLSessionDataTask,
                    didReceive data: Data) {
        lock.lock()
        let sink = entries[dataTask.taskIdentifier]?.sink
        lock.unlock()
        sink?(data)
    }

    func urlSession(_ session: URLSession,
                    task: URLSessionTask,
                    didCompleteWithError error: Error?) {
        lock.lock()
        let entry = entries.removeValue(forKey: task.taskIdentifier)
        pendingValidators.removeValue(forKey: task.taskIdentifier)
        lock.unlock()
        guard let entry else { return }
        // A rejected response is reported as the rejection itself,
        // not as the cancellation that followed it.
        if let rejection = entry.rejection {
            entry.completion(.failure(rejection))
            return
        }
        if let error {
            entry.completion(.failure(error))
        } else if let response = entry.response {
            entry.completion(.success(response))
        } else {
            entry.completion(.failure(HTTPDownloader.Failure.decoding))
        }
    }

    // MARK: - Server trust

    func urlSession(_ session: URLSession,
                    didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        if let trust {
            trust.handle(challenge, completionHandler: completionHandler)
        } else {
            completionHandler(.performDefaultHandling, nil)
        }
    }
}
