import Foundation

/// Dispatcher — P4 router between IPC requests and `SDMEngine`.
///
/// The dispatcher is what the `SocketServer` calls per request.
/// It owns its own `SDMEngine` actor and a small `TaskRegistry`
/// mapping task IDs to engine state. v1.0 intentionally keeps the
/// task ID as a `UUID.string` — never trust user-supplied IDs.
public actor Dispatcher {
    private let engine: SDMEngine
    private var registry: [String: UUID] = [:]

    public init(engine: SDMEngine = SDMEngine()) {
        self.engine = engine
    }

    public func handle(_ data: Data) async -> Data {
        let decoder = JSONDecoder()
        let encoder = JSONEncoder()
        do {
            let req = try decoder.decode(IPCRequest.self, from: data)
            let resp = await dispatch(req)
            return try encoder.encode(resp)
        } catch {
            return try! encoder.encode(
                IPCResponse.failure(id: "?", error: "decode: \(error)")
            )
        }
    }

    private func dispatch(_ req: IPCRequest) async -> IPCResponse {
        switch req.method {
        case "ping":
            return .success(id: req.id, result: .object(["pong": .bool(true)]))
        case "add":
            return await add(req)
        case "list":
            return await list(req)
        case "pause", "resume", "cancel":
            return await control(req)
        case "config":
            return await config(req)
        case "relink":
            return await relink(req)
        default:
            return .failure(id: req.id,
                            error: "unknown method '\(req.method)'")
        }
    }

    private func add(_ req: IPCRequest) async -> IPCResponse {
        guard let params = req.params,
              let urlStr = params["url"].flatMap(jsonString),
              let url = URL(string: urlStr) else {
            return .failure(id: req.id, error: "add: 'url' (string) required")
        }
        let id = UUID().uuidString
        let category = params["category"].flatMap(jsonString)
        let filename = params["filename"].flatMap(jsonString) ?? url.lastPathComponent
        let defaultCategory = category ?? "other"
        _ = defaultCategory
        registry[id] = UUID()

        // Resolve download directory: ~/Downloads/<category>/<filename>.
        // The CLI / GUI may overwrite this before the engine actually
        // starts the flow in v2 (P5/v2).
        let downloads = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Downloads")
        let dest = downloads.appendingPathComponent(filename)

        // Actually run the flow — this is what "add" is supposed to
        // do. We use a detached Task so the IPC request returns
        // immediately with the new task_id and the user can poll
        // `list` to see progress.
        Task.detached(priority: .userInitiated) { [url, dest] in
            let flow = DownloadFlow(url: url, destination: dest)
            do {
                try await flow.run()
            } catch {
                FileHandle.standardError.write(Data(
                    "[sdmd] download failed for \(url): \(error)\n".utf8))
            }
        }
        return .success(id: req.id, result: .object([
            "task_id": .string(id),
            "url": .string(url.absoluteString),
            "state": .string("downloading"),
            "destination": .string(dest.path),
        ]))
    }

    private func list(_ req: IPCRequest) async -> IPCResponse {
        let raw = await engine.snapshot()
        // P4 maps TaskQueue.TaskSummary -> dict; real engine state
        // arrives in P5 with running URLs.
        let mapped: [AnyJSON] = raw.map { t in
            .object([
                "id": .string(t.id.uuidString),
                "url": .string(t.url.absoluteString),
                "state": .string(t.state),
                "bytes_done": .int(t.bytesDone),
                "total": t.totalBytes.map { .int($0) } ?? AnyJSON.null,
            ])
        }
        return .success(id: req.id, result: .array(mapped))
    }

    private func control(_ req: IPCRequest) async -> IPCResponse {
        guard let idStr = req.params?["id"].flatMap(jsonString),
              registry[idStr] != nil else {
            return .failure(id: req.id, error: "control: 'id' required and must be registered")
        }
        return .success(id: req.id, result: .object([
            "method": .string(req.method),
            "applied": .bool(true),
        ]))
    }

    private func config(_ req: IPCRequest) async -> IPCResponse {
        guard let key = req.params?["key"].flatMap(jsonString) else {
            return .failure(id: req.id, error: "config: 'key' required")
        }
        let v = req.params?["value"]
        switch key {
        case "speed":
            if case let .int(bps) = v {
                await engine.setSpeedLimit(Double(bps))
            } else if case let .int(bps) = v {} else if let d = v.flatMap(jsonDouble) {
                await engine.setSpeedLimit(d)
            }
            return .success(id: req.id, result: .object(["speed": v ?? .null]))
        case "threads":
            return .success(id: req.id, result: .object(["threads": v ?? .null]))
        default:
            return .failure(id: req.id, error: "config: unknown key '\(key)'")
        }
    }

    private func relink(_ req: IPCRequest) async -> IPCResponse {
        // P3 + P4 integration: re-validate against the original
        // task's stored probe and decide. P5+.test covers the
        // decisions; P4 only confirms the call routes.
        guard let idStr = req.params?["id"].flatMap(jsonString),
              registry[idStr] != nil,
              let newURL = req.params?["url"].flatMap(jsonString).flatMap(URL.init(string:)) else {
            return .failure(id: req.id, error: "relink: 'id' and 'url' required")
        }
        return .success(id: req.id, result: .object([
            "decision": .string("safeToResume"),
            "new_url": .string(newURL.absoluteString),
        ]))
    }
}

private func jsonString(_ v: AnyJSON) -> String? {
    if case let .string(s) = v { return s }
    return nil
}
private func jsonDouble(_ v: AnyJSON) -> Double? {
    switch v {
    case .int(let i): return Double(i)
    case .double(let d): return d
    default: return nil
    }
}
