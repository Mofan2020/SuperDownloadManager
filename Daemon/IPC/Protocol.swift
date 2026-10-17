import Foundation

/// IPC protocol — P4 daemon wire format.
///
/// Request shape:
///   {"id": "...uuid-or-counter", "method": "add", "params": {...}}
/// Response shape:
///   {"id": "...", "ok": true, "result": {...}}     on success
///   {"id": "...", "ok": false, "error": "..."}     on failure
///
/// Methods v1.0:
///   add(url, referer?, filename?, category?, threads?)
///     → enqueue a download, returns task_id
///   list()
///     → snapshot all tasks as an array
///   pause(id) / resume(id) / cancel(id)
///     → control an existing task by id
///   config(thread|max) | config(speed|bytes_per_sec)
///     → read or write a configuration value
///   relink(id, new_url)
///     → P3 expired-link continuation: re-validate and resume
///   ping()
///     → always returns {"pong": true} — used by CLI/healthchecks
public struct IPCRequest: Codable, Sendable {
    public var id: String
    public var method: String
    public var params: [String: AnyJSON]?
}

public struct IPCResponse: Codable, Sendable {
    public var id: String
    public var ok: Bool
    public var result: AnyJSON?
    public var error: String?

    public static func success(id: String, result: AnyJSON?) -> IPCResponse {
        IPCResponse(id: id, ok: true, result: result, error: nil)
    }
    public static func failure(id: String, error: String) -> IPCResponse {
        IPCResponse(id: id, ok: false, result: nil, error: error)
    }
}

/// `AnyJSON` is a permissive envelope that round-trips through
/// `JSONEncoder`/`JSONDecoder`. We keep it simple — values can be
/// `null`, `bool`, `int`, `double`, `string`, `array`, or `object` —
/// which is exactly the JSON vocabulary.
public enum AnyJSON: Codable, Sendable, Equatable, CustomStringConvertible {
    case null
    case bool(Bool)
    case int(Int64)
    case double(Double)
    case string(String)
    case array([AnyJSON])
    case object([String: AnyJSON])

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null; return }
        if let v = try? c.decode(Bool.self) { self = .bool(v); return }
        if let v = try? c.decode(Int64.self) { self = .int(v); return }
        if let v = try? c.decode(Double.self) { self = .double(v); return }
        if let v = try? c.decode(String.self) { self = .string(v); return }
        if let v = try? c.decode([AnyJSON].self) { self = .array(v); return }
        if let v = try? c.decode([String: AnyJSON].self) { self = .object(v); return }
        throw DecodingError.typeMismatch(
            AnyJSON.self,
            .init(codingPath: decoder.codingPath, debugDescription: "Unknown JSON value"))
    }
    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let v): try c.encode(v)
        case .int(let v): try c.encode(v)
        case .double(let v): try c.encode(v)
        case .string(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        }
    }

    public var description: String {
        switch self {
        case .null:           return "null"
        case .bool(let v):    return String(v)
        case .int(let v):     return String(v)
        case .double(let v):  return String(v)
        case .string(let v):  return "\"\(v)\""
        case .array(let arr): return arr.description
        case .object(let o):  return o.description
        }
    }
}
