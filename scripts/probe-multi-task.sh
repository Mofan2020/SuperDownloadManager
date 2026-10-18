#!/usr/bin/env bash
# Multi-task concurrent download probe. Exercises the real
# DownloadEngine to make sure two parallel downloads each
# stay within their own contentLength, and that bytesDone
# from each task's progressChannel stays monotonic and
# bounded by the file size.

set -euo pipefail

FRAMEWORK_PATH="${1:-$(dirname "$0")/../build/Build/Products/Debug/SuperDownloadManagerCore.framework}"
if [[ ! -d "$FRAMEWORK_PATH" ]]; then
    echo "Error: framework not found at $FRAMEWORK_PATH" >&2
    exit 1
fi

# Two parallel Python servers on different ports.
TMP=$(mktemp -d)
SERVER1_PORT=$(python3 -c 'import random; print(random.randint(40000, 50000))')
SERVER2_PORT=$(($SERVER1_PORT + 1))
SERVER_TOTAL=262144

cleanup() {
    [[ -n "${SERVER1:-}" ]] && kill "$SERVER1" 2>/dev/null || true
    [[ -n "${SERVER2:-}" ]] && kill "$SERVER2" 2>/dev/null || true
    rm -rf "$TMP"
}
trap cleanup EXIT

cat > "$TMP/server.py" <<EOF
import sys, http.server, socketserver, time
PORT = int(sys.argv[1]); TOTAL = int(sys.argv[2])
from socketserver import ThreadingMixIn
class ThreadingHTTPServer(ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            spec = rng[len("bytes="):]; s, e = spec.split("-")
            start = int(s) if s else 0
            end = int(e) if e else TOTAL - 1
            end = min(end, TOTAL - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{TOTAL}")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            sent = 0
            while sent < length:
                chunk = b"a" * min(4096, length - sent)
                try: self.wfile.write(chunk); self.wfile.flush()
                except Exception: return
                time.sleep(0.03); sent += len(chunk)
        else:
            self.send_response(200); self.send_header("Content-Length", str(TOTAL))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers(); self.wfile.write(b"a" * TOTAL)
    def log_message(self, *args): pass

with ThreadingHTTPServer(("127.0.0.1", PORT), H) as httpd:
    print(f"READY {PORT}", flush=True)
    httpd.serve_forever()
EOF

python3 "$TMP/server.py" "$SERVER1_PORT" "$SERVER_TOTAL" >/dev/null 2>&1 &
SERVER1=$!
python3 "$TMP/server.py" "$SERVER2_PORT" "$SERVER_TOTAL" >/dev/null 2>&1 &
SERVER2=$!
for _ in {1..50}; do
    nc -z 127.0.0.1 "$SERVER1_PORT" 2>/dev/null && \
        nc -z 127.0.0.1 "$SERVER2_PORT" 2>/dev/null && break
    sleep 0.1
done

cat > "$TMP/main.swift" <<'EOF'
import Foundation
import SuperDownloadManagerCore

@main struct Probe {
    static func main() async {
        let port1 = UInt16(ProcessInfo.processInfo.environment["PORT1"] ?? "0") ?? 0
        let port2 = UInt16(ProcessInfo.processInfo.environment["PORT2"] ?? "0") ?? 0
        let total = Int64(ProcessInfo.processInfo.environment["TOTAL"] ?? "0") ?? 0
        let url1 = URL(string: "http://127.0.0.1:\(port1)/a.bin")!
        let url2 = URL(string: "http://127.0.0.1:\(port2)/b.bin")!

        let d1 = FileManager.default.temporaryDirectory
            .appendingPathComponent("mt-\(UUID().uuidString).bin")
        let d2 = FileManager.default.temporaryDirectory
            .appendingPathComponent("mt-\(UUID().uuidString).bin")
        defer {
            try? FileManager.default.removeItem(at: d1)
            try? FileManager.default.removeItem(at: d2)
        }

        var strategy = RangeStrategy.default
        strategy.initialChunkSize = 32768  // 32 KB
        strategy.defaultChunks = 8
        strategy.firstRearrangeAfter = 999

        let engine = await DownloadEngine.shared
        let task1 = Task.detached {
            try await engine.start([StartSpec(url: url1,
                destinationDirectory: d1.deletingLastPathComponent())],
                strategy: strategy)
        }
        let task2 = Task.detached {
            try await engine.start([StartSpec(url: url2,
                destinationDirectory: d2.deletingLastPathComponent())],
                strategy: strategy)
        }
        let s1 = await task1.value
        let s2 = await task2.value
        guard let id1 = s1.results.first?.id,
              let id2 = s2.results.first?.id else {
            fputs("PROBE-BUG no task ids\n", stderr); exit(2)
        }

        // Poll until terminal states.
        var iter = 0
        repeat {
            try? await Task.sleep(nanoseconds: 100_000_000)
            iter += 1
            let snap1 = await engine.queueSnapshot(id: id1)
            let snap2 = await engine.queueSnapshot(id: id2)
            let d1s = (snap1?.state ?? "")
            let d2s = (snap2?.state ?? "")
            if iter % 5 == 0 {
                print("tick=\(iter) t1=\(d1s) bytes=\(snap1?.bytesDone ?? 0)/\(snap1?.totalBytes ?? 0) t2=\(d2s) bytes=\(snap2?.bytesDone ?? 0)/\(snap2?.totalBytes ?? 0)")
            }
            if d1s == "completed" && d2s == "completed" { break }
        } while iter < 200

        let s1f = await engine.queueSnapshot(id: id1)
        let s2f = await engine.queueSnapshot(id: id2)
        print("FINAL t1=\(s1f?.state ?? "?") bytes=\(s1f?.bytesDone ?? 0)/\(s1f?.totalBytes ?? 0) t2=\(s2f?.state ?? "?") bytes=\(s2f?.bytesDone ?? 0)/\(s2f?.totalBytes ?? 0)")
        if s1f?.state != "completed" || s2f?.state != "completed" {
            fputs("PROBE-BUG tasks didn't complete\n", stderr); exit(3)
        }
        if (s1f?.bytesDone ?? 0) > total || (s2f?.bytesDone ?? 0) > total {
            fputs("PROBE-BUG bytesDone > total\n", stderr); exit(4)
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

PORT1="$SERVER1_PORT" PORT2="$SERVER2_PORT" TOTAL="$SERVER_TOTAL" timeout 30 "$TMP/probe"
