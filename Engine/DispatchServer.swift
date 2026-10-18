import Foundation
import Network

/// DispatchServer — P5 loopback HTTP server for browser-extension
/// dispatch.
///
/// Browser extensions (Firefox + Safari) POST to
/// `http://127.0.0.1:7955/add?...` to enqueue a download. The
/// server is bound to the loopback interface only.
///
/// v1.0 keeps this thread + Network.framework free of the actor
/// jungle — the dispatcher on the daemon side owns the engine
/// state, and this server just hands URLs off into the queue.
public final class DispatchServer: @unchecked Sendable {
    private let port: UInt16
    private let queue: DispatchQueue
    private var listener: NWListener?
    private let onAdd: (URL, String?, String?) -> Void

    public init(port: UInt16 = 7955,
                onAdd: @escaping (URL, String?, String?) -> Void) {
        self.port = port
        self.queue = DispatchQueue(label: "com.skyc8266.superdownloadmanager.dispatch")
        self.onAdd = onAdd
    }

    public func start() throws {
        let nwPort = NWEndpoint.Port(rawValue: port)!
        let params = NWParameters.tcp
        let listener = try NWListener(using: params, on: nwPort)
        self.listener = listener
        listener.newConnectionHandler = { [weak self] conn in
            self?.handle(conn)
        }
        listener.start(queue: queue)
    }

    public func stop() {
        listener?.cancel()
        listener = nil
    }

    private func handle(_ conn: NWConnection) {
        var buf = Data()
        let chunkSize = 4096
        func loop() {
            conn.receive(minimumIncompleteLength: 1,
                         maximumLength: chunkSize) { data, _, isComplete, error in
                if let data = data { buf.append(data) }
                if isComplete || error != nil {
                    let req = String(decoding: buf.prefix(chunkSize), as: UTF8.self)
                    let response = Self.respond(req, onAdd: self.onAdd)
                    conn.send(content: response,
                              completion: .contentProcessed { _ in conn.cancel() })
                } else {
                    loop()
                }
            }
        }
        loop()
    }

    private static func respond(_ req: String,
                                onAdd: (URL, String?, String?) -> Void) -> Data {
        let firstLine = req.split(separator: "\n").first.map(String.init) ?? ""
        let parts = firstLine.split(separator: " ")
        guard parts.count >= 2 else {
            return httpResponse(400, body: #"{"ok":false,"error":"bad request"}"#)
        }
        let target = String(parts[1])
        let qIndex = target.firstIndex(of: "?") ?? target.endIndex
        let query = target[target.index(after: qIndex)...]
        let path = String(target[..<qIndex])

        if path == "/ping" || path == "/ping/" {
            return httpResponse(200,
                                body: #"{"ok":true,"result":{"pong":true}}"#)
        }
        if path == "/add" || path == "/add/" {
            var flat: [String: String] = [:]
            for pair in query.split(separator: "&") {
                let kv = pair.split(separator: "=", maxSplits: 1)
                if kv.count == 2,
                   let key = String(kv[0]).removingPercentEncoding {
                    let val = String(kv[1]).removingPercentEncoding ?? ""
                    flat[key] = val
                }
            }
            guard let urlStr = flat["url"], let url = URL(string: urlStr) else {
                return httpResponse(400,
                                    body: #"{"ok":false,"error":"missing url"}"#)
            }
            let filename: String? = (flat["filename"]?.isEmpty == false) ? flat["filename"] : nil
            let referer:  String? = (flat["referer"]?.isEmpty  == false) ? flat["referer"]  : nil
            onAdd(url, filename, referer)
            return httpResponse(200,
                                body: #"{"ok":true,"result":{"accepted":true,"url":"\#(url.absoluteString)"}}"#)
        }
        return httpResponse(404, body: #"{"ok":false,"error":"not found"}"#)
    }

    private static func httpResponse(_ code: Int, body: String) -> Data {
        let reason: String
        switch code {
        case 200: reason = "OK"
        case 400: reason = "Bad Request"
        case 404: reason = "Not Found"
        default:  reason = "OK"
        }
        let resp = """
        HTTP/1.1 \(code) \(reason)\r
        Content-Type: application/json\r
        Content-Length: \(body.utf8.count)\r
        Connection: close\r
        \r
        \(body)
        """
        return Data(resp.utf8)
    }
}
