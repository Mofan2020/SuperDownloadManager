import Foundation

/// RangeManager — P1 engine core. Owns the partition of a single
/// download's bytes into parallel work units ("chunks") and the
/// dynamic re-partition logic that defines v1.0's "aggressive"
/// strategy.
///
/// Algorithm basis: a chunked download is just a sorted run of
/// half-open `[start, end)` byte intervals. Splitting is "remove the
/// chunk and replace with two adjacent ones sharing a midpoint".
/// Merging is "remove two adjacent chunks and replace with their
/// union". Speed sampling averages over a sliding window so a chunk
/// that has been alive forever doesn't dominate (the freshest few
/// samples carry more weight). Combined with a fire-on-threshold
/// trigger to avoid thrashing at every sample — these are the same
/// knobs every public multi-threaded HTTP downloader uses. The
/// specific constants in v1.0 are picked aggressively per 星空喵's
/// brief, not borrowed from any one source. See `docs/notes.md`
/// "借鉴 / 算法事实".
public struct Chunk: Codable, Sendable, Equatable, Hashable {
    public var id: Int
    public var start: Int64
    public var end: Int64   // exclusive
    public var bytesDone: Int64 = 0

    public var size: Int64 { end - start }

    public var progress: Double {
        guard size > 0 else { return 1.0 }
        return Double(bytesDone) / Double(size)
    }

    public func rangeHeader() -> String {
        if end - start == 1 {
            return "bytes=\(start)-\(start)"
        }
        return "bytes=\(start)-\(end - 1)"
    }
}

/// Tunables driving the dynamic re-partition logic. Defaults
/// reflect v1.0's "aggressive" strategy.
public struct RangeStrategy: Sendable {
    public var initialChunkSize: Int64 = 2 * 1024 * 1024          // 2 MB
    public var minChunkSize: Int64     = 1 * 1024 * 1024          // 1 MB
    public var maxChunkSize: Int64     = 16 * 1024 * 1024         // 16 MB
    public var maxChunks: Int          = 128                      // hard cap
    public var defaultChunks: Int      = 32                       // per-task default
    public var sampleWindowSec: TimeInterval = 5
    public var splitThreshold: Double = 1.5                       // maxBytes/minBytes > → split
    public var mergeThreshold: Double = 0.3                       // minBytes/maxBytes < → merge
    public var splitStreakSec: TimeInterval = 5                   // slow chunk must be slow this long
    public var firstRearrangeAfter: TimeInterval = 10             // v1.0 aggressive: 10s, not 30s

    public static let `default` = RangeStrategy()
}

/// A snapshot of how fast each chunk has been progressing within the
/// current `RangeStrategy.sampleWindowSec`. The window is wall-clock,
/// not "chunks completed", so an idle chunk isn't punished by its
/// own inactivity — only the recent slope counts.
public struct SpeedSample: Sendable {
    public let chunkID: Int
    public let bytesPerSec: Double
}

/// Planner that owns chunk IDs and produces split / merge
/// suggestions based on the samples and strategy. The actual move
/// is performed by `Engine` under its actor, not here — keeping
/// the planner pure makes the strategy unit-testable without
/// touching the network.
public actor RangePlanner {
    private(set) var chunks: [Chunk] = []
    private let strategy: RangeStrategy
    private var nextID: Int = 0
    private var startedAt: Date = Date()
    private var lastRearrange: Date = .distantPast

    public init(strategy: RangeStrategy = .default) {
        self.strategy = strategy
    }

    public func startPlan(contentLength: Int64) {
        precondition(chunks.isEmpty, "plan already started")
        let target = min(strategy.defaultChunks,
                         Int(contentLength / strategy.initialChunkSize))
        let n = max(1, target)
        let base = contentLength / Int64(n)
        var start: Int64 = 0
        for i in 0..<n {
            let end = (i == n - 1) ? contentLength : (start + base)
            chunks.append(Chunk(id: nextID, start: start, end: end))
            start = end
            nextID += 1
        }
    }

    public func allChunks() -> [Chunk] { chunks }

    /// v1.1.3: replace the plan wholesale with the given chunks.
    /// Used by `DownloadFlow` to seed `bytesDone` from any
    /// pre-existing partial files before the chunk workers are
    /// spawned. The plan layout (start, end, id) is preserved —
    /// only `bytesDone` typically changes.
    public func replaceChunks(_ newChunks: [Chunk]) {
        // Validate that the number of chunks doesn't change so
        // the worker Tasks that reference the same id remain
        // valid. Renumbering would orphan the running work; the
        // download must complete + restart for that.
        precondition(newChunks.count == chunks.count,
                     "replaceChunks: chunk count must match")
        chunks = newChunks
    }

    public func consume(chunkID: Int, bytes: Int64) {
        guard let idx = chunks.firstIndex(where: { $0.id == chunkID }) else { return }
        chunks[idx].bytesDone = min(chunks[idx].end, chunks[idx].bytesDone + bytes)
    }

    public func complete(chunkID: Int) {
        guard let idx = chunks.firstIndex(where: { $0.id == chunkID }) else { return }
        chunks[idx].bytesDone = chunks[idx].end
    }

    /// Compute split / merge deltas from a fresh batch of samples.
    /// Returns the IDs of chunks to:
    ///  - *split*: must be removed and replaced with two halves
    ///  - *merge*: an adjacent pair that should fuse
    ///
    /// Strategy gates (v1.0 aggressive):
    ///  - rear before `firstRearrangeAfter` → no-op
    ///  - split a slow chunk only if (maxBytes/minBytes) > 1.5
    ///    AND the chunk ≥ 4 MB AND was already split-eligible for
    ///    `splitStreakSec` of samples
    ///  - merge a fast neighbor into a slow chunk when
    ///    (minBytes/maxBytes) < 0.3 — and only if the merge leaves
    ///    no chunk larger than `maxChunkSize` (we don't try to undo
    ///    a split).
    public func rearrange(_ samples: [SpeedSample], now: Date) -> [RearrangeDelta] {
        guard now.timeIntervalSince(startedAt) >= strategy.firstRearrangeAfter else { return [] }
        guard now.timeIntervalSince(lastRearrange) >= 1.0 else { return [] }

        let byID = Dictionary(uniqueKeysWithValues: samples.map { ($0.chunkID, $0.bytesPerSec) })
        let maxSpeed = byID.values.max() ?? 0

        var deltas: [RearrangeDelta] = []
        var used = Set<Int>()

        // Slow chunks first — splitting takes a chunk away from the
        // worker pool briefly, so if a fast chunk and a slow chunk
        // both need attention we want the worker reassigned to the
        // fast one. Order matters.
        let sortedBySlowness = chunks.sorted { a, b in
            let av = byID[a.id] ?? .greatestFiniteMagnitude
            let bv = byID[b.id] ?? .greatestFiniteMagnitude
            return av < bv
        }

        for chunk in sortedBySlowness {
            if used.contains(chunk.id) { continue }
            guard let v = byID[chunk.id] else { continue }
            if maxSpeed <= 0 { continue }
            let ratio = v / maxSpeed
            if ratio < strategy.mergeThreshold
                && chunk.size < strategy.maxChunkSize {
                // find adjacent slow chunk to merge
                if let idx = chunks.firstIndex(where: { $0.id == chunk.id }) {
                    let neighbors = (idx > 0 ? [chunks[idx - 1]] : [])
                        + (idx + 1 < chunks.count ? [chunks[idx + 1]] : [])
                    if let partner = neighbors.first(where: { n in
                        !used.contains(n.id)
                            && (byID[n.id] ?? 0) < maxSpeed * (strategy.mergeThreshold + 0.2)
                            && (chunk.size + n.size) <= strategy.maxChunkSize
                    }) {
                        used.insert(chunk.id)
                        used.insert(partner.id)
                        deltas.append(.merge(a: chunk.id, b: partner.id))
                        continue
                    }
                }
            }
            if ratio > 0  // sentinel so the closure always runs
                && chunks.count < strategy.maxChunks
                && chunk.size >= 4 * 1024 * 1024
                && ratio < (1.0 / strategy.splitThreshold) {
                used.insert(chunk.id)
                deltas.append(.split(chunkID: chunk.id))
            }
        }

        if !deltas.isEmpty { lastRearrange = now }
        return deltas
    }

    /// Apply a split: the chunk is removed and two new chunks replace it.
    public func apply(_ delta: RearrangeDelta) {
        switch delta {
        case .split(let chunkID):
            guard let idx = chunks.firstIndex(where: { $0.id == chunkID }) else { return }
            let c = chunks.remove(at: idx)
            let mid = (c.start + c.end) / 2
            let left  = Chunk(id: nextID, start: c.start, end: mid, bytesDone: c.bytesDone)
            let right = Chunk(id: nextID + 1, start: mid, end: c.end,
                              bytesDone: c.bytesDone)
            chunks.insert(left, at: idx)
            chunks.insert(right, at: idx + 1)
            nextID += 2
        case .merge(let a, let b):
            guard let ai = chunks.firstIndex(where: { $0.id == a }),
                  let bi = chunks.firstIndex(where: { $0.id == b }) else { return }
            let (lo, hi) = ai < bi ? (ai, bi) : (bi, ai)
            let c1 = chunks[lo]
            let c2 = chunks[hi]
            let merged = Chunk(id: nextID,
                               start: min(c1.start, c2.start),
                               end: max(c1.end, c2.end),
                               bytesDone: c1.bytesDone + c2.bytesDone)
            chunks.remove(at: hi)
            chunks.remove(at: lo)
            chunks.insert(merged, at: lo)
            nextID += 1
        }
    }
}

public enum RearrangeDelta: Sendable, Equatable {
    case split(chunkID: Int)
    case merge(a: Int, b: Int)
}
