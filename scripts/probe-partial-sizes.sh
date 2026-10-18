#!/usr/bin/env bash
# Probe that walks the chunk workers' bytesDone directly and
# confirms sum of all chunks' bytesDone matches the file size.

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
    if nc -z 127.0.0.1 "$SERVER_PORT" 2>/dev/null; then break; fi
    sleep 0.1
done

cat > "$TMP/main.swift" <<'EOF'
import Foundation
import SuperDownloadManagerCore

@main
struct Probe {
    static func main() async {
        let port = UInt16(ProcessInfo.processInfo.environment["SERVER_PORT"] ?? "0") ?? 0
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
            url: url, destination: dest,
            strategy: strategy, parallelismCap: 8
        )

        do { try await flow.run() } catch { fputs("err: \(error)\n", stderr); exit(2) }

        // Sum partial file sizes — this is what a downloader
        // *should* see as the aggregate bytesDone. If the
        // progress channel says bytesDone exceeds file size,
        // partials are over-reporting; if it says less, partials
        // are under-reporting.
        let fm = FileManager.default
        var partialSum: Int64 = 0
        var partialCount = 0
        var over = 0
        for i in 0..<8 {
            let p = dest.appendingPathExtension("sdm.partial-\(i)")
            let attrs = try? fm.attributesOfItem(atPath: p.path)
            let size = (attrs?[.size] as? Int64) ?? 0
            partialSum += size
            partialCount += (size > 0 ? 1 : 0)
            if size > 65536 { over += 1 }
        }
        let final = (try? fm.attributesOfItem(atPath: dest.path)[.size] as? Int64) ?? 0
        print("PROBE-FINAL file_size=\(final) partial_sum=\(partialSum) partials_present=\(partialCount) oversize=\(over) expected_total=\(total)")
        if final != total {
            fputs("PROBE-BUG final size != contentLength\n", stderr)
            exit(3)
        }
        if partialSum != total {
            fputs("PROBE-BUG partial sum (\(partialSum)) != contentLength\n", stderr)
            exit(4)
        }
        if over > 0 {
            fputs("PROBE-BUG some partial(s) oversize their chunk range\n", stderr)
            exit(5)
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
