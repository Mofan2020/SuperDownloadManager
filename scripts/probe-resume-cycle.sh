#!/usr/bin/env bash
# Simulate: download → pause → resume. Confirm bytesDone
# channel never exceeds contentLength on either run, and
# that the second run picks up from the disk bytes.

set -euo pipefail

FRAMEWORK_PATH="${1:-$(dirname "$0")/../build/Build/Products/Debug/SuperDownloadManagerCore.framework}"
if [[ ! -d "$FRAMEWORK_PATH" ]]; then
    echo "Error: framework not found at $FRAMEWORK_PATH" >&2
    exit 1
fi

SERVER_PORT="${SERVER_PORT:-$(python3 -c 'import random; print(random.randint(40000, 50000))')}"
SERVER_TOTAL="${SERVER_TOTAL:-524288}"

TMP=$(mktemp -d)
SERVER_PID=""
trap "rm -rf $TMP; [[ -n \$SERVER_PID ]] && kill \$SERVER_PID 2>/dev/null || true" EXIT

cat > "$TMP/server.py" <<EOF
from socketserver import ThreadingMixIn
import http.server, socketserver, time
PORT = ${SERVER_PORT}; TOTAL = ${SERVER_TOTAL}

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
                time.sleep(0.05); sent += len(chunk)
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
    nc -z 127.0.0.1 "$SERVER_PORT" 2>/dev/null && break
    sleep 0.1
done

# Two runs share the same destination. The first run is
# cancelled mid-flight (Task.isCancelled); the second picks up.
cat > "$TMP/main.swift" <<'EOF'
import Foundation
import SuperDownloadManagerCore

@MainActor
func mainAsync() async -> Int32 {
    let port = UInt16(ProcessInfo.processInfo.environment["SERVER_PORT"] ?? "0") ?? 0
    let total = Int64(ProcessInfo.processInfo.environment["SERVER_TOTAL"] ?? "0") ?? 0
    let url = URL(string: "http://127.0.0.1:\(port)/file.bin")!
    let dest = FileManager.default.temporaryDirectory
        .appendingPathComponent("sdm-resume-\(UUID().uuidString).bin")
    defer { try? FileManager.default.removeItem(at: dest) }

    var strategy = RangeStrategy.default
    strategy.initialChunkSize = 65536
    strategy.defaultChunks = 8
    strategy.firstRearrangeAfter = 999

    // First run: cancel mid-flight.
    let flow1 = DownloadFlow(
        url: url, destination: dest,
        strategy: strategy, parallelismCap: 8
    )
    var maxBytes1: Int64 = 0
    let consumer1 = Task { () -> Int64 in
        for await bytes in flow1.progressChannel.stream {
            if bytes > maxBytes1 { maxBytes1 = bytes }
        }
        return maxBytes1
    }
    let run1 = Task {
        do { try await flow1.run() }
        catch { fputs("run1 err: \(error)\n", stderr) }
    }
    try? await Task.sleep(nanoseconds: 600_000_000)
    flow1.progressChannel.finish()
    run1.cancel()
    await run1.value
    let _ = await consumer1.value

    // After partial cancellation, partials should still be on
    // disk for the second run to find.
    let fm = FileManager.default
    var partialSum: Int64 = 0
    var partialCount = 0
    var zeroCount = 0
    for i in 0..<8 {
        let p = dest.appendingPathExtension("sdm.partial-\(i)")
        let s = (try? fm.attributesOfItem(atPath: p.path)[.size] as? Int64) ?? -1
        if s > 0 { partialSum += s; partialCount += 1 }
        if s == 0 { zeroCount += 1 }
        print("  partial-\(i) size=\(s)")
    }
    print("PROBE-AFTER-CANCEL partials_present=\(partialCount) zeros=\(zeroCount) partial_sum=\(partialSum) max_bytes_emitted=\(maxBytes1) total=\(total)")
    if partialSum == 0 {
        fputs("PROBE-BUG partials didn't survive cancel\n", stderr)
        return 4
    }

    // Second run picks up where partials left off.
    let flow2 = DownloadFlow(
        url: url, destination: dest,
        strategy: strategy, parallelismCap: 8
    )
    var maxBytes2: Int64 = 0
    let consumer2 = Task { () -> Int64 in
        for await bytes in flow2.progressChannel.stream {
            if bytes > maxBytes2 { maxBytes2 = bytes }
        }
        return maxBytes2
    }
    var ran = false
    do {
        try await flow2.run()
        ran = true
    } catch {
        fputs("run2 err: \(error)\n", stderr)
    }
    flow2.progressChannel.finish()
    let _ = await consumer2.value

    let onDisk = (try? fm.attributesOfItem(atPath: dest.path)[.size] as? Int64) ?? 0
    print("PROBE-AFTER-RESUME disk=\(onDisk) max_bytes_run2=\(maxBytes2) total=\(total)")
    if !ran { return 5 }
    if onDisk != total {
        fputs("PROBE-BUG final size \(onDisk) != contentLength \(total)\n", stderr)
        return 6
    }
    if maxBytes2 > total {
        fputs("PROBE-BUG bytesDone on resume exceeded contentLength\n", stderr)
        return 7
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

SERVER_PORT="$SERVER_PORT" SERVER_TOTAL="$SERVER_TOTAL" "$TMP/probe"
