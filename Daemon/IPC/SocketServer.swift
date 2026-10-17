import Foundation

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
        let handle = FileHandle(fileDescriptor: fd)
        var buf = Data()
        while !Task.isCancelled {
            let chunk: Data
            do {
                chunk = try await handle.read(upToCount: 4096) ?? Data()
            } catch {
                break
            }
            if chunk.isEmpty { break }
            buf.append(chunk)
            // split on newlines
            while let nl = buf.firstIndex(of: 0x0A) {
                let line = buf.prefix(upTo: nl)
                buf.removeSubrange(buf.startIndex...nl)
                if line.isEmpty { continue }
                let response = await self.handler(line)
                handle.write(response + Data([0x0A]))
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
