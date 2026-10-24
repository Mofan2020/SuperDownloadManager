import Foundation
import SuperDownloadManagerCore

/// Real-URL engine probe (v1.1.10 debugging).
///
/// Runs the *actual* engine code path (`HTTPDownloader` +
/// `DownloadFlow`) against a live URL and prints per-stage
/// timings so a stall can be attributed to a specific stage:
///
///   probe    → Content-Length / Accept-Ranges / final URL
///   chunk    → per-chunk status + elapsed
///   merge    → final on-disk size vs expected
///
/// Env vars:
///   URL          target URL (required)
///   CHUNKS       chunk count (default 8)
///   CHUNK_SIZE   planner initial chunk size (default 2 MB)
///   DEST         output path (default: temp file)
///   IGNORE_SSL   "1" to flip the TLS bypass on
///   PROBE_ONLY   "1" to only run the probe step
@main
struct EngineProbe {
    static func main() async {
        let env = ProcessInfo.processInfo.environment
        guard let urlString = env["URL"], let url = URL(string: urlString) else {
            print("PROBE-ERR missing URL")
            exit(64)
        }
        let chunks = Int(env["CHUNKS"] ?? "8") ?? 8
        let chunkSize = Int64(env["CHUNK_SIZE"] ?? String(2 * 1024 * 1024)) ?? 2 * 1024 * 1024
        let ignoreSSL = (env["IGNORE_SSL"] ?? "0") == "1"
        let probeOnly = (env["PROBE_ONLY"] ?? "0") == "1"

        let dest = env["DEST"].map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.temporaryDirectory
                .appendingPathComponent("sdm-engine-probe-\(UUID().uuidString).bin")

        let downloader = HTTPDownloader(ignoreSSL: ignoreSSL)

        // ---- Stage 1: probe ----
        let probeStart = Date()
        let probe: ContentProbe
        do {
            probe = try await downloader.probe(url)
        } catch {
            print("PROBE-FAIL elapsed=\(String(format: "%.2f", Date().timeIntervalSince(probeStart)))s error=\(error)")
            exit(2)
        }
        print("PROBE-OK elapsed=\(String(format: "%.2f", Date().timeIntervalSince(probeStart)))s "
              + "len=\(probe.contentLength.map(String.init) ?? "nil") "
              + "ranges=\(probe.acceptsRanges) "
              + "filename=\(probe.suggestedFilename ?? "nil")")
        print("PROBE-FINAL-URL \(probe.url.absoluteString)")

        guard let contentLength = probe.contentLength, contentLength > 0 else {
            print("PROBE-ERR no content length")
            exit(2)
        }
        if probeOnly { exit(0) }

        // ---- Stage 2: full flow ----
        var strategy = RangeStrategy.default
        strategy.defaultChunks = chunks
        strategy.initialChunkSize = chunkSize
        strategy.firstRearrangeAfter = 999   // no dynamic re-partition in the probe

        let flow = DownloadFlow(url: url,
                                destination: dest,
                                strategy: strategy,
                                parallelismCap: chunks,
                                downloader: downloader)

        let flowStart = Date()
        do {
            try await flow.run()
        } catch {
            print("FLOW-FAIL elapsed=\(String(format: "%.2f", Date().timeIntervalSince(flowStart)))s error=\(error)")
            exit(3)
        }
        let elapsed = Date().timeIntervalSince(flowStart)
        let onDisk = (try? FileManager.default
            .attributesOfItem(atPath: dest.path)[.size] as? Int64) ?? 0
        print("FLOW-OK elapsed=\(String(format: "%.2f", elapsed))s "
              + "disk=\(onDisk) expected=\(contentLength) "
              + "speed=\(String(format: "%.1f", Double(onDisk) / max(elapsed, 0.001))) B/s")
        if onDisk != contentLength {
            print("FLOW-MISMATCH")
            exit(4)
        }
        if env["KEEP"] != "1" {
            try? FileManager.default.removeItem(at: dest)
        } else {
            print("KEPT \(dest.path)")
        }
    }
}
