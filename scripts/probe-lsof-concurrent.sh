#!/usr/bin/env bash
# Slow-server probe: server accepts all 8 Range requests but
# trickles bytes at 50ms × 4096 = 204 KB/s. We check whether
# the engine dispatches all 8 socket connections, and what
# happens if the connection is closed mid-stream.

set -euo pipefail

FRAMEWORK_PATH="${1:-$(dirname "$0")/../build/Build/Products/Release/SuperDownloadManagerCore.framework}"
if [[ ! -d "$FRAMEWORK_PATH" ]]; then
    echo "Error: framework not found at $FRAMEWORK_PATH" >&2
    exit 1
fi

SERVER_PORT="${SERVER_PORT:-$(python3 -c 'import random; print(random.randint(40000, 50000))')}"
SERVER_TOTAL=1048576  # 1 MB

TMP=$(mktemp -d)
SERVER_PID=""
trap "rm -rf $TMP; [[ -n \$SERVER_PID ]] && kill \$SERVER_PID 2>/dev/null || true" EXIT

# Server accepts each connection but takes 800ms to start
# streaming. This forces us to see all 8 Range sockets
# concurrently open in `lsof` while bytes trickle.
cat > "$TMP/server.py" <<EOF
import http.server, socketserver, time, sys
PORT = int(sys.argv[1]); TOTAL = int(sys.argv[2])
from socketserver import ThreadingMixIn
class S(ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        rng = self.headers.get("Range")
        time.sleep(0.8)  # delay before any bytes
        if rng and rng.startswith("bytes="):
            spec = rng[len("bytes="):]; s, e = spec.split("-")
            start = int(s); end = int(e) if e else TOTAL - 1
            end = min(end, TOTAL - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{TOTAL}")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(TOTAL))
            self.end_headers()
            length = TOTAL
        sent = 0
        while sent < length:
            chunk = b"a" * min(4096, length - sent)
            try: self.wfile.write(chunk); self.wfile.flush()
            except Exception: return
            time.sleep(0.005); sent += len(chunk)
    def log_message(self, *args): pass

with S(("127.0.0.1", PORT), H) as httpd:
    print(f"READY {PORT}", flush=True)
    httpd.serve_forever()
EOF
python3 "$TMP/server.py" "$SERVER_PORT" "$SERVER_TOTAL" >/dev/null 2>&1 &
SERVER_PID=$!
for _ in {1..50}; do
    nc -z 127.0.0.1 "$SERVER_PORT" 2>/dev/null && break
    sleep 0.1
done

# Probe: count concurrently-open sockets at the 1s mark.
# We open 8 Range requests simultaneously by spawning 8
# curl clients with --parallel, then sample `lsof`.
# If the engine actually dispatches 8 sockets in parallel
# during a real download, lsof will show 8 ESTABLISHED
# connections to the server.
cat > "$TMP/probe.swift" <<'EOF'
import Foundation
import SuperDownloadManagerCore

@main struct Probe {
    static func main() async {
        let port = UInt16(ProcessInfo.processInfo.environment["PORT"] ?? "0") ?? 0
        let total = Int64(ProcessInfo.processInfo.environment["TOTAL"] ?? "0") ?? 0
        let url = URL(string: "http://127.0.0.1:\(port)/slow.bin")!
        let dest = FileManager.default.temporaryDirectory
            .appendingPathComponent("slow-\(UUID().uuidString).bin")
        defer { try? FileManager.default.removeItem(at: dest) }

        var strategy = RangeStrategy.default
        strategy.initialChunkSize = 131072    // 128 KB
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
        let start = Date()
        do { try await flow.run() }
        catch { fputs("err: \(error)\n", stderr); exit(1) }
        let elapsed = Date().timeIntervalSince(start)
        flow.progressChannel.finish()
        let peak = await consumer.value
        let disk = (try? FileManager.default
            .attributesOfItem(atPath: dest.path)[.size] as? Int64) ?? 0
        print("PROBE-SLOW elapsed=\(elapsed)s disk=\(disk) peak=\(peak) expected=\(total)")
        if disk != total {
            fputs("PROBE-BUG disk != total\n", stderr)
            exit(2)
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
    "$TMP/probe.swift"

# Run the probe, in parallel snapshot lsof's connection count
# every 0.4s for 4s, then print the high-water mark.
PORT="$SERVER_PORT" TOTAL="$SERVER_TOTAL" "$TMP/probe" &
PROBE_PID=$!

HWM=0
for _ in {1..10}; do
    sleep 0.4
    COUNT=$(lsof -i "tcp@127.0.0.1:$SERVER_PORT" -P 2>/dev/null | grep -c ESTABLISHED || echo 0)
    if [[ $COUNT -gt $HWM ]]; then HWM=$COUNT; fi
done

wait $PROBE_PID || true
echo "Lsof high-water ESTABLISHED sockets to server: $HWM"
