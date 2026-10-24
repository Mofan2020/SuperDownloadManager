import Foundation
import Network
import SuperDownloadManagerCore

/// Resumes a continuation exactly once.
///
/// The `NWConnection` callbacks can fire in more than one way for a
/// single request (a send failure *and* a state change, say), and a
/// continuation may only be resumed once — resuming it twice traps.
final class Once: @unchecked Sendable {
    private let lock = NSLock()
    private var cont: CheckedContinuation<Data, Error>?
    private var finished = false

    init(_ cont: CheckedContinuation<Data, Error>) {
        self.cont = cont
    }

    func resume(returning value: Data) {
        lock.lock()
        guard !finished, let c = cont else { lock.unlock(); return }
        finished = true
        cont = nil
        lock.unlock()
        c.resume(returning: value)
    }

    func resume(throwing error: Error) {
        lock.lock()
        guard !finished, let c = cont else { lock.unlock(); return }
        finished = true
        cont = nil
        lock.unlock()
        c.resume(throwing: error)
    }
}

/// sdmdm — P4 CLI client.
///
/// A standalone command-line tool that talks to the local `sdmd`
/// daemon over the Unix Domain Socket. Defaults the socket path to
/// `~/Library/Application Support/SuperDownloadManager/sdmd.sock`.
///
/// Subcommands (v1.0):
///   add <url> [--referer R] [--filename F] [--category C] [--threads N]
///   list  [--json]
///   pause <id>
///   resume <id>
///   cancel <id>
///   relink <id> <new_url>
///   config speed <bytes_per_sec>
///   config threads <count>
///   ping
///
/// Output is one JSON object per request. The CLI prints results
/// to stdout and any error to stderr, returning a non-zero exit
/// code on failure — friendly to AI agents calling it via shell.
public struct SDMDM {
    public static let defaultSocketPath: String = {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let dir = home
            .appendingPathComponent("Library/Application Support/SuperDownloadManager")
            .path
        return "\(dir)/sdmd.sock"
    }()

    public static func run(args: [String]) -> Int32 {
        let parsed: CLIArgs
        do {
            parsed = try CLIArgs.parse(args)
        } catch {
            FileHandle.standardError.write(Data("sdmdm: \(error)\n".utf8))
            FileHandle.standardError.write(Data(CLIArgs.usage.utf8))
            return 64
        }
        let result = send(parsed)
        switch result {
        case .success(let json):
            print(json)
            return 0
        case .failure(let m):
            FileHandle.standardError.write(Data("sdmdm: \(m)\n".utf8))
            return 1
        }
    }

    enum CLIArgs {
        case add(url: String, referer: String?, filename: String?, category: String?, threads: Int?)
        case list
        case pause(id: String)
        case resume(id: String)
        case cancel(id: String)
        case relink(id: String, newURL: String)
        case configSpeed(bytes: Int)
        case configThreads(count: Int)
        case ping

        static func parse(_ args: [String]) throws -> CLIArgs {
            var i = 0
            let subcommand: String
            if i < args.count {
                subcommand = args[i]; i += 1
            } else {
                throw ParseError.missingSubcommand
            }
            switch subcommand {
            case "add":
                guard i < args.count else { throw ParseError.missingURL }
                let url = args[i]; i += 1
                var referer: String? = nil, filename: String? = nil
                var category: String? = nil, threads: Int? = nil
                while i < args.count {
                    switch args[i] {
                    case "--referer":
                        guard i + 1 < args.count else { throw ParseError.missingValue("--referer") }
                        referer = args[i + 1]; i += 2
                    case "--filename":
                        guard i + 1 < args.count else { throw ParseError.missingValue("--filename") }
                        filename = args[i + 1]; i += 2
                    case "--category":
                        guard i + 1 < args.count else { throw ParseError.missingValue("--category") }
                        category = args[i + 1]; i += 2
                    case "--threads":
                        guard i + 1 < args.count, let n = Int(args[i + 1]) else {
                            throw ParseError.missingValue("--threads")
                        }
                        threads = n; i += 2
                    default:
                        throw ParseError.unknownFlag(args[i])
                    }
                }
                return .add(url: url, referer: referer, filename: filename,
                            category: category, threads: threads)
            case "list":
                return .list
            case "pause":
                guard i < args.count else { throw ParseError.missingID }
                return .pause(id: args[i])
            case "resume":
                guard i < args.count else { throw ParseError.missingID }
                return .resume(id: args[i])
            case "cancel":
                guard i < args.count else { throw ParseError.missingID }
                return .cancel(id: args[i])
            case "relink":
                guard i + 1 < args.count else { throw ParseError.relinkArgs }
                return .relink(id: args[i], newURL: args[i + 1])
            case "config":
                guard i + 1 < args.count else { throw ParseError.configArgs }
                let key = args[i]
                switch key {
                case "speed":
                    guard let n = Int(args[i + 1]) else { throw ParseError.invalidInt }
                    return .configSpeed(bytes: n)
                case "threads":
                    guard let n = Int(args[i + 1]) else { throw ParseError.invalidInt }
                    return .configThreads(count: n)
                default:
                    throw ParseError.unknownConfigKey(key)
                }
            case "ping":
                return .ping
            default:
                throw ParseError.unknownSubcommand(subcommand)
            }
        }

        static let usage: String = """
        Usage:
          sdmdm add <url> [--referer R] [--filename F] [--category C] [--threads N]
          sdmdm list
          sdmdm pause <id> | resume <id> | cancel <id>
          sdmdm relink <id> <new_url>
          sdmdm config speed <bytes/sec> | config threads <count>
          sdmdm ping

        Talks to sdmd at \(SDMDM.defaultSocketPath).
        """
    }

    enum ParseError: Error, CustomStringConvertible {
        case missingSubcommand, missingURL, missingValue(String),
             unknownFlag(String), missingID, relinkArgs, configArgs,
             unknownConfigKey(String), invalidInt, unknownSubcommand(String)
        var description: String {
            switch self {
            case .missingSubcommand:  return "missing subcommand"
            case .missingURL:         return "missing URL"
            case .missingValue(let f): return "missing value for \(f)"
            case .unknownFlag(let f): return "unknown flag \(f)"
            case .missingID:          return "missing task id"
            case .relinkArgs:         return "relink: <id> <new_url> required"
            case .configArgs:         return "config: <key> <value> required"
            case .unknownConfigKey(let k): return "unknown config key '\(k)'"
            case .invalidInt:         return "value must be integer"
            case .unknownSubcommand(let s): return "unknown subcommand '\(s)'"
            }
        }
    }

    /// v1.1.10: made public — `UDSClient.send` is public and returns
    /// this type, so the compiler rejected the public signature
    /// ("method cannot be declared public because its result uses an
    /// internal type").
    public enum Result {
        case success(String)
        case failure(String)
    }

    static func send(_ parsed: CLIArgs) -> Result {
        let req = makeRequest(parsed)
        let path = ProcessInfo.processInfo.environment["SDM_SOCKET"] ?? defaultSocketPath
        guard FileManager.default.fileExists(atPath: path) else {
            return .failure("daemon not reachable at \(path); is sdmd running?")
        }
        // We use Network.framework NWConnection for Unix Sockets.
        return dispatchSync(req: req, path: path)
    }

    static func dispatchSync(req: IPCRequest, path: String) -> Result {
        // Synchronous via semaphore-backed async. CLI tools are
        // typically not async entry points; we wrap the call.
        let sem = DispatchSemaphore(value: 0)
        var outResult: Result = .failure("internal: no response")
        Task.detached {
            let result = await sendAsync(req: req, path: path)
            outResult = result
            sem.signal()
        }
        sem.wait()
        return outResult
    }

    static func sendAsync(req: IPCRequest, path: String) async -> Result {
        do {
            let data = try JSONEncoder().encode(req)
            var dataWithNL = data
            dataWithNL.append(0x0A)
            return try await UDSClient.send(path: path, payload: dataWithNL)
        } catch let e as StdioError {
            return .failure(e.description)
        } catch {
            return .failure("encode: \(error)")
        }
    }

    static func makeRequest(_ parsed: CLIArgs) -> IPCRequest {
        let id = UUID().uuidString
        switch parsed {
        case .add(let url, let referer, let filename, let category, let threads):
            var params: [String: AnyJSON] = ["url": .string(url)]
            if let v = referer  { params["referer"] = .string(v) }
            if let v = filename { params["filename"] = .string(v) }
            if let v = category { params["category"] = .string(v) }
            if let v = threads  { params["threads"] = .int(Int64(v)) }
            return IPCRequest(id: id, method: "add", params: params)
        case .list:
            return IPCRequest(id: id, method: "list", params: nil)
        case .pause(let t):
            return IPCRequest(id: id, method: "pause", params: ["id": .string(t)])
        case .resume(let t):
            return IPCRequest(id: id, method: "resume", params: ["id": .string(t)])
        case .cancel(let t):
            return IPCRequest(id: id, method: "cancel", params: ["id": .string(t)])
        case .relink(let id_, let u):
            return IPCRequest(id: id, method: "relink",
                              params: ["id": .string(id_), "url": .string(u)])
        case .configSpeed(let b):
            return IPCRequest(id: id, method: "config",
                              params: ["key": .string("speed"),
                                       "value": .int(Int64(b))])
        case .configThreads(let n):
            return IPCRequest(id: id, method: "config",
                              params: ["key": .string("threads"),
                                       "value": .int(Int64(n))])
        case .ping:
            return IPCRequest(id: id, method: "ping", params: nil)
        }
    }
}

public enum StdioError: Error, CustomStringConvertible {
    case cannotConnect(String)
    case timeout
    case readFailed(String)
    case writeFailed(String)
    public var description: String {
        switch self {
        case .cannotConnect(let m):  return "cannot connect: \(m)"
        case .timeout:               return "timeout"
        case .readFailed(let m):     return "read: \(m)"
        case .writeFailed(let m):    return "write: \(m)"
        }
    }
}

/// Tiny Unix Domain Socket client. Each call is one request, one
/// response line.
///
/// v1.1.10: rewritten with POSIX sockets instead of
/// Network.framework. `NWEndpoint.unix(path:)` exists, but
/// `NWParameters` has no `.unix` case — Network.framework simply
/// does not support AF_UNIX — so the v1.0 code could never have
/// compiled. The daemon's own `SocketServer` already speaks POSIX
/// AF_UNIX, so the client now does too.
///
/// The v1.0 code also blocked the calling thread on a
/// `DispatchSemaphore` inside an `async` function, which Swift 6
/// rejects ("instance method 'wait' is unavailable from asynchronous
/// contexts").
public enum UDSClient {
    public static func send(path: String, payload: Data) async throws -> SDMDM.Result {
        // The socket work is blocking, so it runs on a background
        // queue and the result comes back through a continuation.
        let responseData: Data = try await withCheckedThrowingContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    let data = try exchange(path: path, payload: payload)
                    cont.resume(returning: data)
                } catch {
                    cont.resume(throwing: error)
                }
            }
        }

        // Parse the JSONL response. Drop the trailing newline.
        var trimmed = responseData
        if trimmed.last == 0x0A { trimmed = trimmed.dropLast() }
        guard let obj = try JSONSerialization.jsonObject(with: trimmed) as? [String: Any] else {
            throw StdioError.readFailed("malformed response")
        }
        if obj["ok"] as? Bool == true {
            let pretty: String
            if let result = obj["result"] {
                let d = try JSONSerialization.data(
                    withJSONObject: result,
                    options: [.prettyPrinted, .sortedKeys])
                pretty = String(decoding: d, as: UTF8.self)
            } else {
                pretty = "{}"
            }
            return .success(pretty)
        } else {
            let msg = (obj["error"] as? String) ?? "unknown error"
            return .failure(msg)
        }
    }

    /// One request/response round trip over an AF_UNIX socket.
    private static func exchange(path: String, payload: Data) throws -> Data {
        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else {
            throw StdioError.cannotConnect("socket() failed")
        }
        defer { close(fd) }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(path.utf8) + [0]
        guard pathBytes.count <= MemoryLayout.size(ofValue: addr.sun_path) else {
            throw StdioError.cannotConnect("socket path too long")
        }
        withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
            ptr.withMemoryRebound(to: CChar.self, capacity: pathBytes.count) { dest in
                for (i, b) in pathBytes.enumerated() {
                    dest[i] = CChar(b)
                }
            }
        }
        let addrLen = socklen_t(MemoryLayout<sockaddr_un>.size)
        let connectResult = withUnsafePointer(to: &addr) { ptr -> Int32 in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { p in
                Darwin.connect(fd, p, addrLen)
            }
        }
        guard connectResult == 0 else {
            throw StdioError.cannotConnect("connect() failed errno=\(errno)")
        }

        // Write the request.
        let toWrite = payload
        try toWrite.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            guard let base = raw.baseAddress else { return }
            var sent = 0
            while sent < raw.count {
                let n = Darwin.write(fd, base.advanced(by: sent), raw.count - sent)
                if n < 0 {
                    if errno == EINTR { continue }
                    throw StdioError.writeFailed("write() failed errno=\(errno)")
                }
                sent += n
            }
        }

        // Read the response line.
        var buffer = Data()
        var chunk = [UInt8](repeating: 0, count: 4096)
        while buffer.last != 0x0A {
            let n = Darwin.read(fd, &chunk, chunk.count)
            if n < 0 {
                if errno == EINTR { continue }
                throw StdioError.readFailed("read() failed errno=\(errno)")
            }
            if n == 0 { break }
            buffer.append(contentsOf: chunk[0..<n])
            if buffer.count > 1024 * 1024 {
                throw StdioError.readFailed("response too large")
            }
        }
        return buffer
    }
}
