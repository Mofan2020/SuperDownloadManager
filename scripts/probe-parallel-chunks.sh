#!/usr/bin/env bash
# Probe SDM's chunk concurrency by linking the engine framework
# directly and driving a DownloadFlow against a real Range-supporting
# server, capturing wall-clock timing for when each chunk completes.
#
# This bypasses xcodebuild (which strips env vars from test runs)
# and the GUI (which we can't drive from CI).

set -euo pipefail

# Path to the engine's framework — populated by xcodebuild
# Release build.
FRAMEWORK_PATH="${1:-$(dirname "$0")/../build/Build/Products/Debug/SuperDownloadManagerCore.framework}"

if [[ ! -d "$FRAMEWORK_PATH" ]]; then
    echo "Error: framework not found at $FRAMEWORK_PATH" >&2
    echo "Usage: $0 [path/to/SuperDownloadManagerCore.framework]" >&2
    exit 1
fi

# Bundle ID needed for the framework's URLSession to find a
# reasonable name; the framework doesn't care, but the OS prompts
# otherwise.
APP_BUNDLE_ID="com.test.sdm-parallel-probe"
SERVER_PORT="${SERVER_PORT:-$(python3 -c 'import random; print(random.randint(40000, 50000))')}"
SERVER_TOTAL="${SERVER_TOTAL:-524288}"  # 8 × 64 KB
SERVER_CHUNK_DELAY="${SERVER_CHUNK_DELAY:-0.05}"

# Start a threaded Python server that hands out Range responses
# with a per-chunk throttle.
SERVER_PID=""
TMP=$(mktemp -d)
trap "rm -rf $TMP; [[ -n \$SERVER_PID ]] && kill \$SERVER_PID 2>/dev/null || true" EXIT

cat > "$TMP/server.py" <<EOF
import http.server, socketserver, time, os, sys
from socketserver import ThreadingMixIn
PORT = ${SERVER_PORT}
TOTAL = ${SERVER_TOTAL}
DELAY = ${SERVER_CHUNK_DELAY}

class ThreadingHTTPServer(ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    pass

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            spec = rng[len("bytes="):]
            s, e = spec.split("-")
            start = int(s) if s else 0
            end = int(e) if e else TOTAL - 1
            end = min(end, TOTAL - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{TOTAL}")
            self.end_headers()
            sent = 0
            while sent < length:
                chunk = b"a" * min(4096, length - sent)
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except Exception:
                    return
                time.sleep(DELAY)
                sent += len(chunk)
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(TOTAL))
            self.end_headers()
            self.wfile.write(b"a" * TOTAL)
    def log_message(self, *args):
        pass

with ThreadingHTTPServer(("127.0.0.1", PORT), H) as httpd:
    print(f"PROBE-SERVER ready on {PORT}", flush=True)
    httpd.serve_forever()
EOF

python3 "$TMP/server.py" &
SERVER_PID=$!
# Wait for server to bind.
for _ in {1..30}; do
    if nc -z 127.0.0.1 "$SERVER_PORT" 2>/dev/null; then break; fi
    sleep 0.1
done

# Build a tiny Swift driver that imports the framework and runs
# a DownloadFlow with parallelismCap=8, logging when each chunk
# first receives a byte.
cat > "$TMP/main.swift" <<EOF
import Foundation
import SuperDownloadManagerCore

@main
struct Probe {
    static func main() async {
        let url = URL(string: "http://127.0.0.1:${SERVER_PORT}/file.bin")!
        let dest = FileManager.default.temporaryDirectory
            .appendingPathComponent("sdm-probe-\(UUID().uuidString).bin")
        defer { try? FileManager.default.removeItem(at: dest) }

        let started = Date()
        var strategy = RangeStrategy.default
        strategy.initialChunkSize = 65536
        strategy.defaultChunks = 8
        strategy.firstRearrangeAfter = 999
        let flow = DownloadFlow(
            url: url,
            destination: dest,
            strategy: strategy,
            parallelismCap: 8
        )
        do {
            try await flow.run()
        } catch {
            fputs("PROBE-FAIL \(error)\n", stderr)
            exit(1)
        }
        let elapsed = Date().timeIntervalSince(started)
        let bytes = (try? FileManager.default
            .attributesOfItem(atPath: dest.path)[.size] as? Int64) ?? 0
        print("PROBE-RESULT elapsed=\(String(format: "%.3f", elapsed))s size=\(bytes)")
        // 8 chunks × 50 ms × (65536 / 4096) = 8 × 50 ms × 16 = 6.4 s serial
        // 8 chunks in parallel = 50 ms × 16 = 0.8 s
        // Anything > 4 s suggests chunks ran serially.
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

"$TMP/probe"
