import Foundation

/// DownloadFlow — the real download worker.
///
/// This is the missing link between the IPC `Dispatcher.add` and
/// the actual `HTTPDownloader`. v1.0 wire-up:
///
///   1. `probe(url)` via `ContentSniffer` — fail fast if the
///      server doesn't return Accept-Ranges (we refuse segmented
///      download in that case; P5 will add a single-stream
///      fallback once that's needed).
///   2. `FileAssembler.initialize(destination:)` and open partial
///      files for every chunk.
///   3. Spawn `n = min(strategy.defaultChunks, ...)` chunk
///      workers via `HTTPDownloader.downloadChunk` — each one
///      feeds bytes through `assembler.append` and
///      `planner.consume` / `planner.complete`.
///   4. When every chunk reports `bytesDone == chunk.end`, the
///      assembler merges the partials into the destination and
///      writes the `ResumeState` sidecar.
///
/// The flow is *intentionally* a struct, not an actor: it owns
/// mutable state on a single task and is only ever called from
/// the engine's actor boundary. Tests can construct one and
/// `await run()` directly without an actor hop.
///
/// Algorithm basis (chunked HTTP download): RFC 7233 (Range
/// requests), the standard "split into N byte ranges, GET each
/// in parallel, seek-write to a shared file" pattern that IDM /
/// aria2 / FreeDownloadManager all implement. No source was
/// copied. See `docs/notes.md` "借鉴 / 算法事实".
public struct DownloadFlow: Sendable {
    public let url: URL
    public let destination: URL
    public let strategy: RangeStrategy
    public let parallelismCap: Int
    public let limiter: SpeedLimiter
    public let downloader: HTTPDownloader
    public let progressChannel: ProgressChannel

    public init(url: URL,
                destination: URL,
                strategy: RangeStrategy = .default,
                parallelismCap: Int = 32,
                limiter: SpeedLimiter? = nil,
                downloader: HTTPDownloader = HTTPDownloader()) {
        self.url = url
        self.destination = destination
        self.strategy = strategy
        self.parallelismCap = parallelismCap
        self.limiter = limiter ?? SpeedLimiter(rateBytesPerSec: 0)
        self.downloader = downloader
        self.progressChannel = ProgressChannel()
    }

    public enum FlowError: Error, CustomStringConvertible {
        case probeFailed(Error)
        case noContentLength
        case notSegementable       // server ignored Range (200 not 206)
        case partial(Error)
        case mergeFailed(Error)
        case sidecarFailed(Error)

        public var description: String {
            switch self {
            case .probeFailed(let e):    return "probe failed: \(e)"
            case .noContentLength:       return "no Content-Length"
            case .notSegementable:       return "server returned 200 (not 206); multi-segment disabled"
            case .partial(let e):        return "segment failed: \(e)"
            case .mergeFailed(let e):    return "merge failed: \(e)"
            case .sidecarFailed(let e):  return "sidecar write failed: \(e)"
            }
        }
    }

    /// Run the full download. Returns when bytes are flushed to
    /// disk and the sidecar is written.
    public func run() async throws {
        // 1. Probe.
        let probe: ContentProbe
        do {
            probe = try await downloader.probe(url)
        } catch {
            throw FlowError.probeFailed(error)
        }
        guard let contentLength = probe.contentLength, contentLength > 0 else {
            throw FlowError.noContentLength
        }

        // 2. Plan + assembler.
        let planner = RangePlanner(strategy: strategy)
        await planner.startPlan(contentLength: contentLength)
        let chunks = await planner.allChunks()
        let n = max(1, min(chunks.count, parallelismCap))
        let activeChunks = Array(chunks.prefix(n))

        let assembler = FileAssembler(destination: destination)
        for chunk in activeChunks {
            _ = try? await assembler.openChunk(
                chunkID: chunk.id,
                startOffset: chunk.start)
        }

        // 3. Chunk workers in parallel — each one downloads its
        //    range and feeds bytes to the partial file. We use
        //    a TaskGroup so cancellation propagates cleanly.
        //    v1.0.5: each worker reports its cumulative byte
        //    total to the progress channel as it goes.
        do {
            try await withThrowingTaskGroup(of: Void.self) { group in
                for chunk in activeChunks {
                    let url = self.url
                    let chunkCopy = chunk
                    group.addTask {
                        do {
                            let n = try await self.downloader.downloadChunk(
                                url: url,
                                chunk: chunkCopy,
                                assembler: assembler,
                                progress: { byteCount in
                                    let cumulative = chunkCopy.start + byteCount
                                    self.progressChannel.report(cumulative)
                                })
                            await planner.consume(
                                chunkID: chunkCopy.id,
                                bytes: n)
                            await planner.complete(chunkID: chunkCopy.id)
                        } catch is HTTPDownloader.Failure { throw FlowError.notSegementable
                        } catch {
                            throw FlowError.partial(error)
                        }
                    }
                }
                for try await _ in group { progressChannel.finish() }
            }
        } catch let e as FlowError {
            throw e
        } catch {
            throw FlowError.partial(error)
        }

        // 4. Merge + sidecar.
        do {
            try await assembler.finalize(chunksInOrder: activeChunks.map { $0.id })
        } catch {
            throw FlowError.mergeFailed(error)
        }
        let state = ResumeState.from(probe: probe,
                                     filename: destination.lastPathComponent,
                                     chunks: activeChunks)
        do {
            try state.write(to: destination)
        } catch {
            throw FlowError.sidecarFailed(error)
        }
        progressChannel.finish()
    }
}

/// ProgressChannel — in-process progress streaming for the
/// DownloadFlow. v1.0.5 replaces the v1.0.4 disk poll (200ms
/// `attributesOfItem(.size)`) with a real-time stream that the
/// engine consumes to update the queue row. The flow's chunk
/// workers call `report(_:)` after each successful write to the
/// chunk's partial file; the stream coalesces events so the
/// consumer sees at most one update per 50 ms.
public final class ProgressChannel: @unchecked Sendable {
    private let continuation: AsyncStream<Int64>.Continuation
    public let stream: AsyncStream<Int64>
    private var lastReportedAt: Date = .distantPast
    private var pending: Int64?
    private let lock = NSLock()

    public init() {
        var cont: AsyncStream<Int64>.Continuation!
        self.stream = AsyncStream<Int64>(bufferingPolicy: .bufferingNewest(16)) { c in
            cont = c
        }
        self.continuation = cont
    }

    public func report(_ bytes: Int64) {
        lock.lock()
        let now = Date()
        if now.timeIntervalSince(lastReportedAt) < 0.05 {
            pending = bytes
            lock.unlock()
            return
        }
        lastReportedAt = now
        let toSend = pending ?? bytes
        pending = nil
        lock.unlock()
        continuation.yield(toSend)
    }

    public func finish() {
        continuation.finish()
    }
}
