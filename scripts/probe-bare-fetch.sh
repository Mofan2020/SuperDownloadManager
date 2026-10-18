#!/usr/bin/env bash
# Bare HTTPDownloader.fetch — no chunking, no taskgroup. If
# this hangs, the bug is in the basic data(for:) path; if it
# returns, the hang is downstream of chunking.

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
            .appendingPathComponent("sdm-probe-bare-\(UUID().uuidString).bin")
        defer { try? FileManager.default.removeItem(at: dest) }

        let downloader = HTTPDownloader()
        do {
            let probe = try await downloader.probe(url)
            print("PROBE-BARE probe len=\(probe.contentLength ?? 0) acceptRanges=\(probe.acceptRanges)")
            let start = Date()
            let assembler = FileAssembler(destination: dest, totalLength: probe.contentLength ?? 0)
            let chunk = Chunk(id: 0, start: 0, end: probe.contentLength ?? 1, bytesDone: 0)
            let req = downloader.request(for: url)
            let n = try await downloader.fetch(
                chunk: chunk, session: downloader.sharedSession,
                userAgent: downloader.userAgent,
                assembler: assembler, maxBytes: nil, progress: nil
            )
            let elapsed = Date().timeIntervalSince(start)
            let onDisk = (try? FileManager.default
                .attributesOfItem(atPath: dest.path)[.size] as? Int64) ?? 0
            print("PROBE-BARE n=\(n) elapsed=\(elapsed)s disk=\(onDisk)")
            try assembler.finalize()
            let final = (try? FileManager.default
                .attributesOfItem(atPath: dest.path)[.size] as? Int64) ?? 0
            print("PROBE-BARE-FINAL disk=\(final)")
        } catch {
            print("PROBE-BARE-ERR \(error)")
            exit(1)
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

DOWNLOAD_URL="$DOWNLOAD_URL" "$TMP/probe"
