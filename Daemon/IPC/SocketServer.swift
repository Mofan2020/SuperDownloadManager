import Foundation
import SuperDownloadManagerCore

/// SocketServer — P4 daemon IPC.
///
/// A tiny line-delimited JSON server bound to a Unix Domain Socket.
/// Clients connect, write one JSON request per line, read one JSON
/// response per line. Concurrency: the server accepts N clients
/// concurrently and dispatches each request independently.
///
/// Why JSONL over a binary protocol: a CLI sitting in
/// `~/.local/bin/` shares the same wire format as the local GUI
/// controller — debug-by-`cat /tmp/sdmd-sock | tee` is more valuable
/// than the 5% efficiency we'd gain from a binary stream.
///
/// The socket path lives in `~/Library/Application Support/
/// SuperDownloadManager/sdmd.sock`. We pick the path in `daemon
/// main` and pass it to `SocketServer.make(path:)`.
public actor SocketServer {
    public typealias Handler = @Sendable (Data) async -> Data

    private let path: String
    private let handler: Handler
    private var listener: Task<Void, Never>?

    public init(path: String, handler: @escaping Handler) {
        self.path = path
        self.handler = handler
    }

    public func start() throws {
        let fm = FileManager.default
        let dir = (path as NSString).deletingLastPathComponent
        try fm.createDirectory(atPath: dir, withIntermediateDirectories: true)
        // Remove any stale socket so the new daemon binds cleanly.
        try? fm.removeItem(atPath: path)

        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw SocketError.bind("socket() failed") }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(path.utf8) + [0]
        guard pathBytes.count <= MemoryLayout.size(ofValue: addr.sun_path) else {
            close(fd)
            throw SocketError.bind("socket path too long")
        }
        withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
            ptr.withMemoryRebound(to: CChar.self, capacity: pathBytes.count) { dest in
                for (i, b) in pathBytes.enumerated() {
                    dest[i] = CChar(b)
                }
            }
        }
        let addrLen = socklen_t(MemoryLayout<sockaddr_un>.size)
        let bindRes = withUnsafePointer(to: &addr) { ptr -> Int32 in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { p in
                Darwin.bind(fd, p, addrLen)
            }
        }
        guard bindRes == 0 else {
            close(fd)
            throw SocketError.bind("bind() failed errno=\(errno)")
        }
        let listenRes = Darwin.listen(fd, 16)
        guard listenRes == 0 else {
            close(fd)
            throw SocketError.bind("listen() failed errno=\(errno)")
        }
        listener = Task {
            await self.serve(fd: fd)
        }
    }

    public func stop() {
        listener?.cancel()
        listener = nil
        try? FileManager.default.removeItem(atPath: path)
    }

    private func serve(fd: Int32) async {
        while !Task.isCancelled {
            let client = await acceptOne(fd: fd)
            guard let client = client else { continue }
            Task { await self.handleClient(fd: client) }
        }
        close(fd)
    }

    private func acceptOne(fd: Int32) async -> Int32? {
        await withCheckedContinuation { (cont: CheckedContinuation<Int32?, Never>) in
            Task.detached {
                let c = Darwin.accept(fd, nil, nil)
                if c < 0 { cont.resume(returning: nil) }
                else { cont.resume(returning: c) }
            }
        }
    }

    private func handleClient(fd: Int32) async {
        defer { close(fd) }
        // v1.1.10: read the socket with raw POSIX `read(2)`.
        //
        // `FileHandle.read(upToCount:)` is *not* a "read whatever is
        // available" call — it loops until it has the full requested
        // count or the peer closes. With a 4096-byte request the
        // daemon sat waiting for bytes that never came, while the
        // client sat waiting for the reply: a deadlock. `read(2)`
        // returns whatever the kernel has, which is what a
        // line-oriented protocol needs.
        var buf = Data()
        let chunk = [UInt8](repeating: 0, count: 4096)
        while !Task.isCancelled {
            let data: Data
            do {
                data = try await readBlocking(fd: fd, buffer: chunk)
            } catch {
                break
            }
            if data.isEmpty { break }
            buf.append(data)
            // split on newlines
            while let nl = buf.firstIndex(of: 0x0A) {
                let line = buf.prefix(upTo: nl)
                buf.removeSubrange(buf.startIndex...nl)
                if line.isEmpty { continue }
                let response = await self.handler(line)
                var out = response
                out.append(0x0A)
                do {
                    try writeAll(fd: fd, data: out)
                } catch {
                    return
                }
            }
        }
    }

    /// Read whatever the socket has, without blocking the actor.
    ///
    /// The read blocks the calling thread until data arrives, so it
    /// runs on a background queue and the result comes back through
    /// a continuation.
    private func readBlocking(fd: Int32, buffer: [UInt8]) async throws -> Data {
        try await withCheckedThrowingContinuation { cont in
            DispatchQueue.global(qos: .utility).async {
                var scratch = [UInt8](repeating: 0, count: buffer.count)
                let n = scratch.withUnsafeMutableBytes { raw -> Int in
                    guard let base = raw.baseAddress else { return -1 }
                    return Darwin.read(fd, base, raw.count)
                }
                if n < 0 {
                    if errno == EINTR {
                        cont.resume(returning: Data())
                        return
                    }
                    cont.resume(throwing: SocketError.bind("read() failed errno=\(errno)"))
                    return
                }
                cont.resume(returning: Data(scratch[0..<max(n, 0)]))
            }
        }
    }

    /// Write every byte, retrying on EINTR.
    private func writeAll(fd: Int32, data: Data) throws {
        try data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            guard let base = raw.baseAddress else { return }
            var sent = 0
            while sent < raw.count {
                let n = Darwin.write(fd, base.advanced(by: sent), raw.count - sent)
                if n < 0 {
                    if errno == EINTR { continue }
                    throw SocketError.bind("write() failed errno=\(errno)")
                }
                sent += n
            }
        }
    }
}

public enum SocketError: Error, CustomStringConvertible {
    case bind(String)
    public var description: String {
        switch self {
        case .bind(let m): return "Socket bind error: \(m)"
        }
    }
}
