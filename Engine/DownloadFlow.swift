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
        // v1.1.3: scan pre-existing per-chunk partials left on
        // disk by a previous (paused / cancelled / crashed)
        // session. Those files are named
        // `<dest>.sdm.partial-<chunkID>` and carry the bytes the
        // worker had already persisted before interruption.
        // Pre-loading `chunk.bytesDone` from disk is what makes
        // resume work — without this step, a paused download
        // would re-download every byte from scratch.
        var chunks = await planner.allChunks()
        var preExistingBytes: Int64 = 0
        for i in chunks.indices {
            let partialPath = destination
                .appendingPathExtension("sdm.partial-\(chunks[i].id)")
            let size: Int64 = (try? FileManager.default
                .attributesOfItem(atPath: partialPath.path)[.size] as? Int64) ?? 0
            if size > 0 {
                let cap = chunks[i].end - chunks[i].start
                let clamped = min(size, cap)
                if clamped > 0 {
                    chunks[i].bytesDone = clamped
                    preExistingBytes += clamped
                }
            }
        }
        await planner.replaceChunks(chunks)

        let n = max(1, min(chunks.count, parallelismCap))
        let activeChunks: [Chunk] = chunks
            .prefix(n)
            .filter { $0.bytesDone < $0.size }
        // Always re-decode so the progress channel can include
        // pre-existing bytes from the very first tick.
        if preExistingBytes > 0 {
            progressChannel.report(preExistingBytes)
        }

        let assembler = FileAssembler(destination: destination)
        for chunk in activeChunks {
            // Pass the per-chunk's *planned* startOffset — the
            // absolute byte position this chunk occupies in the
            // final destination file. `chunk.bytesDone` is how
            // far into the chunk range the worker has already
            // persisted on a resume; that's tracked separately
            // by `chunk.bytesDone` and by `chunkWritten` in the
            // assembler itself.
            _ = try? await assembler.openChunk(
                chunkID: chunk.id,
                startOffset: chunk.start)
        }

        // 3. Chunk workers in parallel — each one downloads its
        //    range and feeds bytes to the partial file. We use
        //    a TaskGroup so cancellation propagates cleanly.
        //    v1.0.7: progress is reported via ChunkProgress
        //    (aggregating per-flush bytes across all chunks),
        //    so the UI sees monotonic file-level progress, not
        //    ping-pong between chunk completion percentages.
        let chunkProgress = ChunkProgress(channel: progressChannel)
        do {
            try await withThrowingTaskGroup(of: Void.self) { group in
                for chunk in activeChunks {
                    let url = self.url
                    let chunkCopy = chunk
                    let cp = chunkProgress
                    group.addTask {
                        do {
                            let n = try await self.downloader.downloadChunk(
                                url: url,
                                chunk: chunkCopy,
                                assembler: assembler,
                                progress: { perChunkBytes in
                                    cp.report(perChunkBytesDone: perChunkBytes)
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
                for try await _ in group {
                    // v1.0.7: flush the channel's pending
                    // value so the engine sees the final
                    // aggregate before the stream ends.
                    progressChannel.flush()
                }
            }
        } catch let e as FlowError {
            throw e
        } catch {
            throw FlowError.partial(error)
        }

        // 4. Merge + sidecar.
        // v1.1.3: the assembler exposes both sync (finalize) and
        // async (finalizeAsync) entry points. The flow is itself
        // an async function, but the merge IO is short and the
        // sync version is what tests cover; dispatch off the
        // actor via `finalizeAsync` keeps the engine's task
        // scheduler happy.
        do {
            try await assembler.finalizeAsync(
                chunksInOrder: chunks.map { $0.id })
        } catch {
            throw FlowError.mergeFailed(error)
        }
        let state = ResumeState.from(probe: probe,
                                     filename: destination.lastPathComponent,
                                     chunks: chunks)
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
///
/// v1.0.7: the value reported is the **aggregate bytesDone
/// across every chunk in the file**, not a single chunk's
/// per-chunk offset. v1.0.5/v1.0.6 reported per-chunk cumulative
/// (`chunkStart + byteCount`), which made the row's progress bar
/// jump between chunk completion percentages as different chunks
/// finished, instead of rising monotonically with the file's
/// true total.
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
        // v1.0.7 fix: the channel always forwards the
        // **most recent** value seen during a coalesce
        // window, not the first one. v1.0.5/v1.0.6 sent the
        // first call's value and then dropped every later
        // call on the floor — the consumer saw a stale,
        // low number and ping-ponged as soon as the window
        // closed. We now keep the most recent `pending` and
        // always send the latest on the next non-coalesced
        // tick.
        if now.timeIntervalSince(lastReportedAt) < 0.05 {
            pending = bytes
            lock.unlock()
            return
        }
        lastReportedAt = now
        // If we coalesced anything in the prior window,
        // send the latest now (not the first one we saw).
        let toSend = pending ?? bytes
        pending = nil
        lock.unlock()
        continuation.yield(toSend)
    }

    /// Flush whatever is in `pending` right now, ignoring the
    /// 50 ms window. The flow's chunk workers call this on
    /// completion so the final aggregate reaches the engine
    /// before the stream finishes, instead of being dropped
    /// if every report in the last window went into
    /// `pending`.
    public func flush() {
        lock.lock()
        guard let p = pending else {
            lock.unlock()
            return
        }
        pending = nil
        lastReportedAt = Date()
        lock.unlock()
        continuation.yield(p)
    }

    public func finish() {
        continuation.finish()
    }
}

/// Per-chunk reporter the DownloadFlow hands to each worker.
/// The worker calls `report(perChunkBytesDone:)` after every
/// successful write; the progress channel receives the SUM
/// across all chunks so the UI shows a monotonic file-level
/// progress, not a ping-pong between chunk percentages.
public final class ChunkProgress: @unchecked Sendable {
    private let channel: ProgressChannel
    private let lock = NSLock()
    private var aggregate: Int64 = 0

    public init(channel: ProgressChannel) {
        self.channel = channel
    }

    /// Report that this chunk has written `perChunkBytesDone`
    /// bytes (relative to its own range, not absolute file
    /// offset). The progress channel sees the new file-level
    /// total.
    public func report(perChunkBytesDone: Int64) {
        lock.lock()
        aggregate += perChunkBytesDone
        let total = aggregate
        lock.unlock()
        channel.report(total)
    }
}
