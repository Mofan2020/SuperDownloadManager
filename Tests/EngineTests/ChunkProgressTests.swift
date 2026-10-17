import XCTest
@testable import SuperDownloadManagerCore

/// ChunkProgress tests — lock the v1.0.7 fix for the
/// "progress bar ping-pongs between chunk completion
/// percentages" bug.
///
/// v1.0.5/v1.0.6 reported `chunkStart + byteCount` to the
/// progress channel (per-chunk cumulative), so the UI saw the
/// value jump between chunk 0 finishing (33%), chunk 1
/// finishing (66%), and chunk 2 finishing (100%) instead of
/// rising monotonically. v1.0.7 introduces `ChunkProgress`
/// which sums every per-flush report across all chunks and
/// forwards a monotonic file-level total.
final class ChunkProgressTests: XCTestCase {

    /// Sanity: a single chunk's reports should equal the
    /// running total of that chunk's per-flush bytes. The
    /// channel coalesces to a single delivered value per
    /// 50 ms window, so we expect the *last* value seen on
    /// the stream to equal the sum of every per-flush
    /// report in the burst.
    func testSingleChunkAggregate() async throws {
        let channel = ProgressChannel()
        let cp = ChunkProgress(channel: channel)
        var seen: [Int64] = []
        let task = Task { () -> Int64 in
            for await v in channel.stream { seen.append(v) }
            return seen.last ?? -1
        }
        cp.report(perChunkBytesDone: 1024)
        cp.report(perChunkBytesDone: 1024)
        cp.report(perChunkBytesDone: 2048)
        try await Task.sleep(nanoseconds: 80_000_000)  // wait past 50ms coalesce
        channel.flush()  // force-deliver pending
        channel.finish()
        let last = await task.value
        XCTAssertEqual(seen, [1024, 4096],
            "should see first report (1024) and then coalesced latest (4096)")
        XCTAssertEqual(last, 4096, "single chunk aggregate should be 1024+1024+2048")
    }

    /// The actual bug fix: multiple chunks reporting
    /// concurrently must produce a monotonically increasing
    /// aggregate, and the final delivered value must equal
    /// the sum of every chunk's contribution. v1.0.6
    /// reported per-chunk cumulative, so the UI saw the
    /// value jump between 33%/66%/100% as chunks finished.
    /// v1.0.7 sums to a monotonic file-level total.
    func testMultiChunkMonotonicAggregate() async throws {
        let channel = ProgressChannel()
        let cp = ChunkProgress(channel: channel)
        let chunks: [[Int64]] = [
            [1024, 2048, 4096],               // total 7168
            [5 * 1024 * 1024, 1024],          // total 5MB + 1024
            [10 * 1024 * 1024, 1024]          // total 10MB + 1024
        ]
        let expected = chunks.flatMap { $0 }.reduce(0, +)
        var seen: [Int64] = []
        let task = Task { () -> Int64 in
            for await v in channel.stream { seen.append(v) }
            return seen.last ?? -1
        }
        for chunkReports in chunks {
            for n in chunkReports {
                cp.report(perChunkBytesDone: n)
            }
        }
        try await Task.sleep(nanoseconds: 80_000_000)
        channel.flush()
        channel.finish()
        let last = await task.value
        // Monotonicity: every delivered value must be >= the
        // previous one. This is the property v1.0.6 violated.
        for i in 1..<seen.count {
            XCTAssertGreaterThanOrEqual(seen[i], seen[i-1],
                "value must be monotonic, got \(seen[i]) after \(seen[i-1])")
        }
        // Final value must equal the sum of every chunk's
        // contribution across all chunks.
        XCTAssertEqual(last, expected,
            "final aggregate must equal the sum of every chunk's bytes (15,737,856)")
    }

    /// The 50ms coalescing in `ProgressChannel` means the
    /// last report within a tight window wins. v1.0.7 still
    /// produces a monotonic file-level value because the
    /// `ChunkProgress` adds before forwarding.
    func testCoalesceKeepsMonotonic() async throws {
        let channel = ProgressChannel()
        let cp = ChunkProgress(channel: channel)
        var seen: [Int64] = []
        let drain = Task { () -> Int64 in
            for await v in channel.stream { seen.append(v) }
            return seen.last ?? -1
        }
        // Burst: many small reports in <50ms total
        for i in 1...100 {
            cp.report(perChunkBytesDone: Int64(i))
        }
        // After burst, send a clearly-later update
        try await Task.sleep(nanoseconds: 100_000_000)  // 100ms
        cp.report(perChunkBytesDone: 100_000)
        try await Task.sleep(nanoseconds: 80_000_000)
        channel.finish()
        let last = await drain.value
        // Verify the final value carries the burst
        XCTAssertGreaterThan(last, 5_000, "aggregate must include the burst")
        // Verify monotonicity across all delivered values
        for i in 1..<seen.count {
            XCTAssertGreaterThanOrEqual(seen[i], seen[i-1])
        }
    }
}
