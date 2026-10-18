#!/usr/bin/env bash
# Multi-task concurrent DownloadFlow probe. Each flow has
# its own URL, its own destination, its own progressChannel,
# and its own URLSession (because they're separate flow
# instances). The probe verifies both flows complete to
# exactly their contentLength and that nothing crosses
# boundaries.

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

func runOne(port: UInt16, label: String, total: Int64) async -> Int64 {
    let url = URL(string: "http://127.0.0.1:\(port)/\(label).bin")!
    let dest = FileManager.default.temporaryDirectory
        .appendingPathComponent("mt-\(UUID().uuidString).bin")
    defer { try? FileManager.default.removeItem(at: dest) }
    var strategy = RangeStrategy.default
    strategy.initialChunkSize = 32768
    strategy.defaultChunks = 8
    strategy.firstRearrangeAfter = 999

    let flow = DownloadFlow(url: url, destination: dest,
                            strategy: strategy, parallelismCap: 8)
    var maxBytes: Int64 = 0
    let consumer = Task.detached { () -> Int64 in
        var m: Int64 = 0
        for await b in flow.progressChannel.stream {
            if b > m { m = b }
        }
        return m
    }
    do { try await flow.run() } catch {
        fputs("err \(label): \(error)\n", stderr); return -1
    }
    flow.progressChannel.finish()
    let peak = await consumer.value
    let disk = (try? FileManager.default
        .attributesOfItem(atPath: dest.path)[.size] as? Int64) ?? 0
    print("\(label) peak=\(peak) disk=\(disk) expected=\(total)")
    return peak
}

@main struct Probe {
    static func main() async {
        let port1 = UInt16(ProcessInfo.processInfo.environment["PORT1"] ?? "0") ?? 0
        let port2 = UInt16(ProcessInfo.processInfo.environment["PORT2"] ?? "0") ?? 0
        let total = Int64(ProcessInfo.processInfo.environment["TOTAL"] ?? "0") ?? 0
        async let p1 = runOne(port: port1, label: "a", total: total)
        async let p2 = runOne(port: port2, label: "b", total: total)
        let r1 = await p1
        let r2 = await p2
        if r1 != total || r2 != total {
            fputs("PROBE-BUG tasks exceeded their bound (r1=\(r1), r2=\(r2), expected=\(total))\n", stderr)
            exit(3)
        }
        print("OK")
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
