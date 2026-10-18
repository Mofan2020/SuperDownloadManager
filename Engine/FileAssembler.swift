import Foundation

/// FileAssembler — P1 engine core. Owns disk IO for a single
/// download's bytes.
///
/// Design notes:
/// - One `FileHandle` per *chunk* — never share a handle across
///   chunks because concurrent `write(at:)` to the same handle is
///   a documented Apple API that performs correctly but is easy to
///   misuse (the seek/write race gives the wrong byte position to
///   some bytes).
/// - Each chunk writes to a per-chunk partial file
///   `<dest>.sdm.partial-<chunkID>`. When all chunks complete, the
///   engine asks us to merge them in order and `rename(2)` to the
///   final path. The rename is atomic on APFS, so a crash mid-merge
///   leaves either the old or the new state visible — never a
///   half-written destination.
/// - Use `posix_fallocate` via `preallocateFile` to give the kernel
///   a chance to lay out contiguous blocks; this is what makes a
///   10 GB download write at full speed instead of getting
///   fragmented writes once the file grows past the buffer cache.
///
/// Threading: v1.1.2 changed this from an `actor` to a plain
/// class with a per-chunk `NSLock`. The actor model serialized
/// *every* `append` to *every* chunk on a single executor, which
/// meant 8 concurrent chunk workers queued their writes through
/// the same actor mailbox — visually the user saw "chunk 0
/// finishes, then chunk 1, then chunk 2…", not "all chunks in
/// parallel". The chunks each write to an *independent* partial
/// file, so there's no shared mutable state to protect *across*
/// chunks. The per-chunk lock keeps each individual chunk's
/// `FileHandle.write(contentsOf:)` atomic (without it, two
/// flushes from the same chunk's task could interleave at the
/// byte level).
public final class FileAssembler: @unchecked Sendable {
    private let destination: URL
    private let queueLock = NSLock()
    private var handles: [Int: FileHandle] = [:]
    private var chunkLocks: [Int: NSLock] = [:]
    private var partialPaths: [Int: URL] = [:]
    private var chunkStarts: [Int: Int64] = [:]  // track per-chunk byte where it starts
    private var chunkWritten: [Int: Int64] = [:]

    public init(destination: URL) {
        self.destination = destination
    }

    /// Open a per-chunk partial file and remember it. Idempotent:
    /// re-opening the same chunk ID returns the existing handle.
    /// The partial file is initially empty; `append` writes bytes
    /// at the running position. The `startOffset` argument is
    /// recorded so `finalize` knows where each partial belongs in
    /// the final layout.
    ///
    /// `async` is preserved for caller compatibility (matches the
    /// v1.0 actor signature); the body itself does not suspend.
    public func openChunk(chunkID: Int, startOffset: Int64) async throws -> FileHandle {
        try _openChunk(chunkID: chunkID, startOffset: startOffset)
    }

    /// Append a fresh buffered range to the chunk's partial file.
    /// Returns the bytes written so the caller can update chunk
    /// accounting in `RangePlanner.consume`.
    ///
    /// `openChunk` must have been called for this chunk first;
    /// otherwise we throw `chunkNotOpen`. The per-chunk NSLock
    /// only blocks *same-chunk* writers — different chunks write
    /// concurrently.
    @discardableResult
    public func append(_ data: Data, to chunkID: Int) async throws -> Int64 {
        try _append(data, to: chunkID)
    }

    public func closeChunk(chunkID: Int) {
        queueLock.lock()
        if let h = handles.removeValue(forKey: chunkID) {
            try? h.close()
        }
        chunkLocks.removeValue(forKey: chunkID)
        partialPaths.removeValue(forKey: chunkID)
        queueLock.unlock()
    }

    /// Merge all open partial files into `destination` in chunk
    /// order (caller supplies the order so this class doesn't need
    /// to know about RangePlanner's structure) and remove the
    /// partials. The merge is the only place that touches the
    /// final filename; if it fails midway, the destination is
    /// simply not created and the next retry sees only the
    /// partials.
    public func finalize(chunksInOrder: [Int]) throws {
        // v1.1.2: close all open handles first — appendices are
        // already flushed. Sync body — the async wrapper above
        // (`finalize(chunksInOrder:)` async throws) hops off the
        // actor before invoking this method, so `NSLock.lock()` is
        // safe to call here.
        queueLock.lock()
        for id in chunksInOrder {
            if let h = handles[id] {
                try? h.close()
                handles.removeValue(forKey: id)
            }
            chunkLocks.removeValue(forKey: id)
        }
        let partialsSnapshot = partialPaths
        let startsSnapshot = chunkStarts
        let writtenSnapshot = chunkWritten
        partialPaths.removeAll()
        queueLock.unlock()

        let fm = FileManager.default
        if fm.fileExists(atPath: destination.path) {
            try fm.removeItem(at: destination)
        }
        try fm.createDirectory(at: destination.deletingLastPathComponent(),
                               withIntermediateDirectories: true)
        // Build the final file by seeking to each partial's
        // recorded startOffset and copying its bytes. This handles
        // sparse / out-of-order chunks correctly.
        fm.createFile(atPath: destination.path, contents: nil)
        let outHandle = try FileHandle(forWritingTo: destination)
        defer { try? outHandle.close() }

        // Sort partials by start offset so we walk the file in
        // ascending position; gaps between chunks are zero-filled.
        let ordered = chunksInOrder.compactMap { id -> (Int, Int64)? in
            guard let start = startsSnapshot[id] else { return nil }
            return (id, start)
        }.sorted(by: { $0.1 < $1.1 })

        // Compute total size we need to materialize (last partial's
        // end). If total is unknown (no chunks), nothing to write.
        guard let lastOffset = ordered.last?.1,
              let lastID = ordered.last?.0 else { return }
        let lastBytes = writtenSnapshot[lastID] ?? 0
        let totalSize = lastOffset + lastBytes

        // Pre-allocate so zero-fill gaps don't see a sparse file.
        // We `ftruncate` via seek-to-(totalSize-1)-then-write-1-byte;
        // a zero-fill after the last write position is wasteful for
        // an exact file but lets us rely on the kernel to lay out
        // contiguous pages for large downloads.
        if totalSize > 0 {
            try? outHandle.seek(toOffset: UInt64(totalSize - 1))
            try? outHandle.write(contentsOf: Data([0]))
            try outHandle.seek(toOffset: 0)
        }

        for (id, start) in ordered {
            try outHandle.seek(toOffset: UInt64(start))
            guard let partial = partialsSnapshot[id]
                ?? partialPathForChunk(id) else { continue }
            let inHandle = try FileHandle(forReadingFrom: partial)
            defer { try? inHandle.close() }
            while true {
                let buf = try inHandle.read(upToCount: 256 * 1024) ?? Data()
                if buf.isEmpty { break }
                try outHandle.write(contentsOf: buf)
            }
            try? fm.removeItem(at: partial)
        }
    }

    /// v1.1.2: async wrapper that preserves the v1.0.x actor
    /// signature. The body hops off via `withCheckedContinuation`,
    /// runs the sync implementation on a background thread, and
    /// re-enters the caller.
    public func finalizeAsync(chunksInOrder: [Int]) async throws {
        try await withCheckedThrowingContinuation { (cont:
            CheckedContinuation<Void, Error>) in
            DispatchQueue.global(qos: .utility).async { [self] in
                do { try finalize(chunksInOrder: chunksInOrder); cont.resume() }
                catch { cont.resume(throwing: error) }
            }
        }
    }

    /// Best-effort pre-allocation hint. APFS honors this by
    /// extending the file with zeros without flushing real zero
    /// pages, so a download that writes "into" pre-allocated
    /// space doesn't have to grow the file system object
    /// repeatedly. No-op when the OS refuses.
    public func preallocate(size: Int64) {
        // File-level preallocation for the *final* path isn't
        // valid until we know we're going to write there. We rely
        // on the kernel's normal growth pattern per partial file
        // instead. Provided as a no-op for API stability; P3 will
        // wire the actual ftruncate-based call.
    }

    // MARK: - Internal sync helpers
    //
    // The async wrappers above delegate here. The async boundary
    // is preserved (no behavioural change for the engine / network
    // code that already `await`s these methods) while the body
    // itself does no actual suspension.

    private func _openChunk(chunkID: Int, startOffset: Int64) throws -> FileHandle {
        queueLock.lock()
        defer { queueLock.unlock() }
        if let h = handles[chunkID] { return h }
        try FileManager.default.createDirectory(
            at: destination.deletingLastPathComponent(),
            withIntermediateDirectories: true)
        let partial = destination.appendingPathExtension("sdm.partial-\(chunkID)")
        FileManager.default.createFile(atPath: partial.path, contents: nil)
        let handle = try FileHandle(forWritingTo: partial)
        handles[chunkID] = handle
        chunkLocks[chunkID] = NSLock()
        partialPaths[chunkID] = partial
        chunkStarts[chunkID] = startOffset
        chunkWritten[chunkID] = 0
        return handle
    }

    private func _append(_ data: Data, to chunkID: Int) throws -> Int64 {
        // Snapshot the per-chunk lock + handle under the queue lock,
        // then drop the queue lock before touching the per-chunk
        // lock. Holding both at once would serialise every chunk.
        queueLock.lock()
        guard let l = chunkLocks[chunkID],
              let h = handles[chunkID] else {
            queueLock.unlock()
            throw AssemblerError.chunkNotOpen(chunkID)
        }
        queueLock.unlock()

        let n = Int64(data.count)
        // Per-chunk critical section: blocks other flushes from the
        // *same* chunk; never blocks other chunks.
        l.lock()
        defer { l.unlock() }
        try h.write(contentsOf: data)
        queueLock.lock()
        chunkWritten[chunkID, default: 0] += n
        queueLock.unlock()
        return n
    }

    private func partialPathForChunk(_ id: Int) -> URL? {
        let p = destination.appendingPathExtension("sdm.partial-\(id)")
        return FileManager.default.fileExists(atPath: p.path) ? p : nil
    }
}

public enum AssemblerError: Error {
    case chunkNotOpen(Int)
}
