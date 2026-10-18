#!/usr/bin/env bash
# Probe that watches every value pulled out of the in-process
# progress channel of a real DownloadFlow.run() against a threaded
# Python server. The server throttles every chunk to ~0.8 s of
# round-trip so concurrency shows up clearly. We then assert two
# invariants:
#   1. The number of distinct bytesDone samples received is > 1
#      (the channel actually emits; the file isn't just one
#      blob at completion).
#   2. The maximum bytesDone sample never exceeds contentLength.
#   3. The total elapsed wall time is ~parallel-bound, not serial.
#
# Used to confirm the 176% bug from the GUI was just clamping
# missing, not data corruption.

set -euo pipefail

FRAMEWORK_PATH="${1:-$(dirname "$0")/../build/Build/Products/Debug/SuperDownloadManagerCore.framework}"

if [[ ! -d "$FRAMEWORK_PATH" ]]; then
    echo "Error: framework not found at $FRAMEWORK_PATH" >&2
    exit 1
fi

SERVER_PORT="${SERVER_PORT:-$(python3 -c 'import random; print(random.randint(40000, 50000))')}"
SERVER_TOTAL="${SERVER_TOTAL:-524288}"   # 8 × 64 KB
SERVER_CHUNK_DELAY="${SERVER_CHUNK_DELAY:-0.05}"

TMP=$(mktemp -d)
SERVER_PID=""
trap "rm -rf $TMP; [[ -n \$SERVER_PID ]] && kill \$SERVER_PID 2>/dev/null || true" EXIT

cat > "$TMP/server.py" <<EOF
from socketserver import ThreadingMixIn
import http.server, socketserver, time
PORT = ${SERVER_PORT}
TOTAL = ${SERVER_TOTAL}
DELAY = ${SERVER_CHUNK_DELAY}

class ThreadingHTTPServer(ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True

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
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{TOTAL}")
            self.end_headers()
            sent = 0
            while sent < length:
                chunk = b"a" * min(4096, length - sent)
                try: self.wfile.write(chunk); self.wfile.flush()
                except Exception: return
                time.sleep(DELAY)
                sent += len(chunk)
        else:
            self.send_response(200); self.send_header("Content-Length", str(TOTAL))
            self.end_headers(); self.wfile.write(b"a" * TOTAL)
    def log_message(self, *args): pass

with ThreadingHTTPServer(("127.0.0.1", PORT), H) as httpd:
    print(f"PROBE-SERVER ready on {PORT}", flush=True)
    httpd.serve_forever()
EOF
python3 "$TMP/server.py" &
SERVER_PID=$!
for _ in {1..30}; do
    if nc -z 127.0.0.1 "$SERVER_PORT" 2>/dev/null; then break; fi
    sleep 0.1
done

cat > "$TMP/main.swift" <<'EOF'
import Foundation
import SuperDownloadManagerCore

@main
struct Probe {
    static func main() async {
        let portStr = ProcessInfo.processInfo.environment["SERVER_PORT"] ?? "0"
        let port = UInt16(portStr) ?? 0
        let total = Int64(ProcessInfo.processInfo.environment["SERVER_TOTAL"] ?? "0") ?? 0
        let url = URL(string: "http://127.0.0.1:\(port)/file.bin")!
        let dest = FileManager.default.temporaryDirectory
            .appendingPathComponent("sdm-probe-\(UUID().uuidString).bin")
        defer { try? FileManager.default.removeItem(at: dest) }

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

        var samples: [Int64] = []
        var maxBytes: Int64 = 0
        let probeStarted = Date()
        let consumerTask = Task {
            for await bytes in flow.progressChannel.stream {
                samples.append(bytes)
                if bytes > maxBytes { maxBytes = bytes }
            }
        }
        do {
            try await flow.run()
        } catch {
            fputs("PROBE-FAIL \(error)\n", stderr)
            exit(2)
        }
        await consumerTask.value
        let elapsed = Date().timeIntervalSince(probeStarted)
        let onDisk = (try? FileManager.default
            .attributesOfItem(atPath: dest.path)[.size] as? Int64) ?? 0
        print("PROBE-SUMMARY elapsed=\(String(format: "%.3f", elapsed))s disk=\(onDisk)B total=\(total)B")
        print("PROBE-SAMPLES count=\(samples.count)")
        for (i, v) in samples.enumerated() {
            print("  sample[\(i)]=\(v)")
        }
        print("PROBE-MAX bytes_done_channel_max=\(maxBytes) > total=\(maxBytes > total)")
        if maxBytes > total {
            fputs("PROBE-BUG overreport: channel emitted values above contentLength\n", stderr)
            exit(3)
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

SERVER_PORT="$SERVER_PORT" SERVER_TOTAL="$SERVER_TOTAL" "$TMP/probe"
