#!/usr/bin/env bash
# Bare DownloadFlow.run against a real URL.
# Mirrors what the macOS app does end-to-end; lets the user
# observe whether the flow hangs, what fractionDone looks
# like at sample points, and whether parallel works.

set -euo pipefail

FRAMEWORK_PATH="${1:-$(dirname "$0")/../build/Build/Products/Debug/SuperDownloadManagerCore.framework}"
if [[ ! -d "$FRAMEWORK_PATH" ]]; then
    echo "Error: framework not found at $FRAMEWORK_PATH" >&2
    exit 1
fi

DOWNLOAD_URL="${DOWNLOAD_URL:-https://speed.cloudflare.com/__down?bytes=2097152}"

TMP=$(mktemp -d)
trap "rm -rf $TMP" EXIT

cat > "$TMP/main.swift" <<'EOF'
import Foundation
import SuperDownloadManagerCore

@main
struct Probe {
    static func main() async {
        let url = URL(string: ProcessInfo.processInfo.environment["DOWNLOAD_URL"] ?? "")!
        let dest = FileManager.default.temporaryDirectory
            .appendingPathComponent("sdm-probe-flow-\(UUID().uuidString).bin")
        defer { try? FileManager.default.removeItem(at: dest) }

        // v1.0.4 RangeStrategy defaults: 32 chunks, 2 MB initial,
        // dynamic re-partition. Tight parallelism cap so the
        // concurrency test is meaningful.
        var strategy = RangeStrategy.default
        strategy.initialChunkSize = 131072      // 128 KB
        strategy.defaultChunks = 16
        strategy.firstRearrangeAfter = 999       // disable re-partition

        let flow = DownloadFlow(
            url: url, destination: dest,
            strategy: strategy, parallelismCap: 16
        )

        var maxBytes: Int64 = 0
        var bytes: [Int64] = []
        let consumer = Task.detached { () -> Int64 in
            var m: Int64 = 0
            for await b in flow.progressChannel.stream {
                bytes.append(b)
                if b > m { m = b }
            }
            return m
        }
        let start = Date()
        do { try await flow.run() }
        catch { fputs("flow err: \(error)\n", stderr); exit(1) }
        let elapsed = Date().timeIntervalSince(start)
        flow.progressChannel.finish()
        let last = await consumer.value
        let disk = (try? FileManager.default
            .attributesOfItem(atPath: dest.path)[.size] as? Int64) ?? 0
        print("PROBE elapsed=\(elapsed)s disk=\(disk) channel_max=\(last) samples=\(bytes.count)")
        if bytes.count > 0 {
            // Print every tenth sample for an at-a-glance check.
            let stride = max(1, bytes.count / 10)
            var s: [Int64] = []
            for i in Swift.stride(from: 0, to: bytes.count, by: stride) {
                s.append(bytes[i])
            }
            print("  samples[0]=\(bytes[0]) [last]=\(bytes[bytes.count-1])")
            print("  stride=\(s)")
        }
    }
}
EOF

swiftc \
    -target arm64-apple-macos26.0 \
    -F "$FRAMEWORK_PATH/.." \
    -framework SuperDownloadManagerCore \
    -parse-as-library \
    -Xlinker -rpath -Xlinker "$(dirname "$FRAMEWORK_PATH")" \
    -o "$TMP/probe" \
    "$TMP/main.swift"

DOWNLOAD_URL="$DOWNLOAD_URL" timeout 30 "$TMP/probe"
