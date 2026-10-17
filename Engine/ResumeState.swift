import Foundation

/// ResumeState — P1 engine core. Persists the sidecar `.sdm` file
/// alongside a download's partial bytes so a relaunch can resume
/// from the same offsets.
///
/// On-disk format (JSON):
/// ```
/// {
///   "url": "...",
///   "content_length": 12345,
///   "etag": "...",
///   "last_modified": "...",
///   "accepts_ranges": true,
///   "filename": "...",
///   "chunks": [
///     { "id": 0, "start": 0, "end": 4096, "bytes_done": 4096 },
///     ...
///   ],
///   "saved_at": "2026-01-01T00:00:00Z"
/// }
/// ```
///
/// P3 extends this with `content_signature` validation: if a later
/// `relink` finds that the new URL returns a different
/// `Content-Length` or `ETag` we either refuse to resume or warn
/// the user — never silently write the new bytes into the old
/// hole.
public struct ResumeState: Codable, Sendable, Equatable {
    public var url: URL
    public var contentLength: Int64
    public var etag: String?
    public var lastModified: String?
    public var acceptsRanges: Bool
    public var filename: String
    public var chunks: [Chunk]
    public var savedAt: Date

    public static func from(probe: ContentProbe,
                            filename: String,
                            chunks: [Chunk]) -> ResumeState {
        ResumeState(
            url: probe.url,
            contentLength: probe.contentLength ?? 0,
            etag: probe.etag,
            lastModified: probe.lastModified,
            acceptsRanges: probe.acceptsRanges,
            filename: filename,
            chunks: chunks,
            savedAt: Date()
        )
    }

    /// Path alongside the final file: `<final>.sdm.json`.
    /// e.g. `/Users/.../Downloads/x.dmg` → `/Users/.../Downloads/x.dmg.sdm.json`
    public func sidecarPath(forFinalPath finalPath: URL) -> URL {
        let base = finalPath.lastPathComponent
        let stem = (base as NSString).deletingPathExtension
        let dir = finalPath.deletingLastPathComponent()
        return dir.appendingPathComponent("\(stem).sdm.json")
    }

    public func write(to finalPath: URL) throws {
        let dst = sidecarPath(forFinalPath: finalPath)
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        enc.dateEncodingStrategy = .iso8601
        let data = try enc.encode(self)
        try FileManager.default.createDirectory(at: dst.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        try data.write(to: dst, options: .atomic)
    }

    public static func read(from sidecar: URL) throws -> ResumeState {
        let data = try Data(contentsOf: sidecar)
        let dec = JSONDecoder()
        dec.dateDecodingStrategy = .iso8601
        return try dec.decode(ResumeState.self, from: data)
    }
}

public extension URL {
    /// Sidecar sibling of a download path; "x.dmg" → "x.dmg.sdm.json".
    var sdmSidecarPath: URL {
        appendingPathExtension("sdm.json")
    }
}
