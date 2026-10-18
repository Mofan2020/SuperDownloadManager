#!/usr/bin/env bash
# End-to-end test using a *real* download URL. Catches the
# 176% pathology the user reported — bytesDone > totalBytes —
# even when the server returns a real Content-Length.

set -euo pipefail

FRAMEWORK_PATH="${1:-$(dirname "$0")/../build/Build/Products/Debug/SuperDownloadManagerCore.framework}"
if [[ ! -d "$FRAMEWORK_PATH" ]]; then
    echo "Error: framework not found at $FRAMEWORK_PATH" >&2
    exit 1
fi

DOWNLOAD_URL="${DOWNLOAD_URL:-https://files.hexaqa.com/size/2mb.bin}"

TMP=$(mktemp -d)
trap "rm -rf $TMP" EXIT

cat > "$TMP/main.swift" <<'EOF'
import Foundation
import SuperDownloadManagerCore

@MainActor
func mainAsync() async -> Int32 {
    let url = URL(string: ProcessInfo.processInfo.environment["DOWNLOAD_URL"] ?? "")!
    let dest = FileManager.default.temporaryDirectory
        .appendingPathComponent("sdm-probe-real-\(UUID().uuidString).bin")
    defer { try? FileManager.default.removeItem(at: dest) }

    var strategy = RangeStrategy.default
    strategy.initialChunkSize = 65536
    strategy.defaultChunks = 16
    strategy.firstRearrangeAfter = 999

    let flow = DownloadFlow(
        url: url, destination: dest,
        strategy: strategy, parallelismCap: 16
    )

    var maxBytesDone: Int64 = 0
    var allBytes: [Int64] = []
    let consumer = Task { () -> Int64 in
        for await bytes in flow.progressChannel.stream {
            allBytes.append(bytes)
            if bytes > maxBytesDone { maxBytesDone = bytes }
        }
        return maxBytesDone
    }
    let start = Date()
    do { try await flow.run() }
    catch { fputs("flow err: \(error)\n", stderr); return 1 }
    let elapsed = Date().timeIntervalSince(start)
    flow.progressChannel.finish()
    let _ = await consumer.value

    let fileSize = (try? FileManager.default
        .attributesOfItem(atPath: dest.path)[.size] as? Int64) ?? 0
    print("PROBE-REAL elapsed=\(elapsed)s file_size=\(fileSize) max_bytesDone=\(maxBytesDone) samples=\(allBytes.count)")
    if allBytes.count > 0 {
        print("  sample[0]=\(allBytes[0]) sample[last]=\(allBytes[allBytes.count-1])")
    }
    if fileSize == 0 {
        fputs("PROBE-BUG downloaded file is empty\n", stderr)
        return 2
    }
    if maxBytesDone > fileSize {
        fputs("PROBE-BUG channel max (\(maxBytesDone)) > file size (\(fileSize))\n", stderr)
        return 3
    }
    return 0
}

@main
struct Probe {
    static func main() async {
        let code = await mainAsync()
        exit(Int32(code))
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

DOWNLOAD_URL="$DOWNLOAD_URL" "$TMP/probe"
