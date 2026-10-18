#!/usr/bin/env bash
# SSL bypass probe. Runs two cases against a self-signed
# loopback HTTPS server:
#   (a) IGNORE_SSL=0 — engine rejects the cert (URLError -1202),
#       probe exits non-zero — *expected* failure mode.
#   (b) IGNORE_SSL=1 — engine accepts the cert, full file
#       downloaded with status 0 — *expected* success mode.

set -euo pipefail

FRAMEWORK_PATH="${1:-$(dirname "$0")/../Users/mofan/Documents/MyProjects/SuperDownloadManager/build/Build/Products/Debug/SuperDownloadManagerCore.framework}"
if [[ ! -d "$FRAMEWORK_PATH" ]]; then
    echo "Error: framework not found at $FRAMEWORK_PATH" >&2
    exit 1
fi

TMP=$(mktemp -d)
SERVER_PID=""
trap 'rm -rf "$TMP"; [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null || true' EXIT

openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$TMP/key.pem" -out "$TMP/cert.pem" \
    -days 1 -subj "/CN=127.0.0.1" 2>/dev/null

cat > "$TMP/server.py" <<EOF
import http.server, ssl, sys, socketserver
PORT = int(sys.argv[1]); TOTAL = int(sys.argv[2])
class S(socketserver.TCPServer):
    daemon_threads = True; allow_reuse_address = True
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Length", str(TOTAL))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(b"a" * TOTAL)
    def log_message(self, *a): pass
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(certfile=sys.argv[3], keyfile=sys.argv[4])
httpd = S(("127.0.0.1", PORT), H)
httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
print(f"READY {PORT}", flush=True)
httpd.serve_forever()
EOF

cat > "$TMP/probe.swift" <<'EOF'
import Foundation
import SuperDownloadManagerCore

@main struct Probe {
    static func main() async {
        let port = UInt16(ProcessInfo.processInfo.environment["PORT"] ?? "0") ?? 0
        let ignoreSSL = (ProcessInfo.processInfo.environment["IGNORE_SSL"] ?? "0") == "1"
        let url = URL(string: "https://127.0.0.1:\(port)/ssl.bin")!

        let downloader = HTTPDownloader(ignoreSSL: ignoreSSL)
        do {
            let p = try await downloader.probe(url)
            guard let contentLength = p.contentLength, contentLength > 0 else {
                print("PROBE-ERR no contentLength ignoreSSL=\(ignoreSSL)")
                exit(2)
            }
            let dest = FileManager.default.temporaryDirectory
                .appendingPathComponent("ssl-\(UUID().uuidString).bin")
            defer { try? FileManager.default.removeItem(at: dest) }
            var s = RangeStrategy.default
            s.initialChunkSize = 1024
            s.defaultChunks = 2
            s.firstRearrangeAfter = 999
            let flow = DownloadFlow(url: url, destination: dest,
                                    strategy: s, parallelismCap: 2,
                                    downloader: downloader)
            try await flow.run()
            let onDisk = (try? FileManager.default
                .attributesOfItem(atPath: dest.path)[.size] as? Int64) ?? 0
            print("PROBE-OK ignoreSSL=\(ignoreSSL) len=\(contentLength) disk=\(onDisk)")
            if onDisk != contentLength { print("PROBE-MISMATCH ignoreSSL=\(ignoreSSL)"); exit(3) }
        } catch {
            print("PROBE-FAIL ignoreSSL=\(ignoreSSL) \(error)")
            // For (a) this is *expected*: URLError -1202.
            // For (b) this is *unexpected*: full bypass
            // should not raise.
            exit(ignoreSSL ? 99 : 0)
        }
    }
}
EOF

PORT=$(python3 -c 'import random; print(random.randint(40000, 50000))')

# Both runs share the server (the bypass doesn't change the
# server's behavior — only the client's trust evaluation).
python3 "$TMP/server.py" "$PORT" 2048 "$TMP/cert.pem" "$TMP/key.pem" >/dev/null 2>&1 &
SERVER_PID=$!
for _ in {1..30}; do nc -z 127.0.0.1 "$PORT" 2>/dev/null && break; sleep 0.1; done

swiftc \
    -target arm64-apple-macos26.0 \
    -F "$FRAMEWORK_PATH/.." \
    -framework SuperDownloadManagerCore \
    -parse-as-library \
    -Xlinker -rpath -Xlinker "$(dirname "$FRAMEWORK_PATH")" \
    -o "$TMP/probe" \
    "$TMP/probe.swift"

echo "## Case A: IGNORE_SSL=0 (engine should reject self-signed cert)"
PORT="$PORT" IGNORE_SSL=0 timeout 15 "$TMP/probe" 2>&1 | head -2 || true
echo
echo "## Case B: IGNORE_SSL=1 (engine should accept and download)"
PORT="$PORT" IGNORE_SSL=1 timeout 15 "$TMP/probe" 2>&1 | head -2
