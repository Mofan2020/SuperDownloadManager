import Foundation

/// ContentSniffer — P1 engine core.
///
/// Two responsibilities:
/// 1. **HTTP HEAD probe**: pull `Content-Length`, `Accept-Ranges`,
///    `ETag`, `Last-Modified`, `Content-Type`, `Content-Disposition`
///    from the server before committing any bytes. This is what makes
///    dynamic segmentation possible (we need total size) and what
///    feeds the resume-state `content_signature` (ETag + Last-Modified
///    + Length) used by the expired-link continuation flow in P3.
/// 2. **Magic-byte content sniffing**: when a server response has no
///    useful filename (no `Content-Disposition`, no useful URL path
///    extension) we sniff the first bytes to identify the file family
///    — Mach-O, ZIP, PDF, etc. — so P3's default classification rule
///    table can drop `.dmg`, `.pkg`, `.zip` with no extension into the
///    correct folder.
///
/// Algorithm basis: the magic-byte table is widely documented (Wikipedia
/// `List of file signatures`); the HEAD-probe shape is HTTP-spec standard.
/// Neither was copied from a specific implementation. See
/// `docs/notes.md` "借鉴 / 算法事实".
public struct ContentProbe: Sendable, Equatable {
    public var url: URL
    public var contentLength: Int64?
    public var acceptsRanges: Bool
    public var etag: String?
    public var lastModified: String?
    public var contentType: String?
    public var suggestedFilename: String?
    public var isFinal: Bool  // HEAD followed any redirects to this URL
}

public enum SniffedKind: String, Sendable, CaseIterable {
    case macho        // 7F 45 4C 46 — Mach-O (incl. universal)
    case zipFamily    // 50 4B 03 04 / 50 4B 05 06 / 50 4B 07 08
    case rar          // 52 61 72 21
    case sevenZip     // 37 7A BC AF 27 1C
    case dmg          // 78 01 73 0D 62 62 60 60 (UDIF/Koly)
    case tar          // 75 73 74 61 72 (ustar at offset 257)
    case gzip         // 1F 8B
    case bz2          // 42 5A 68
    case xz           // FD 37 7A 58 5A 00
    case pdf          // 25 50 44 46 2D
    case jpeg         // FF D8 FF
    case png          // 89 50 4E 47 0D 0A 1A 0A
    case gif          // 47 49 46 38
    case mp4Video     // ftyp box at offset 4
    case mp3          // 49 44 33 or 0xFF 0xFB
    case htmlText     // text/html
    case webm         // 1A 45 DF A3
    case ogg          // 4F 67 67 53
    case wav          // 52 49 46 46 .... 57 41 56 45
    case javaClass    // CA FE BA BE — Java class
    case unknown
}

/// Magic-byte detector for content that's already on disk or buffered
/// in a `Data` prefix. Inputs as small as 16 bytes are common in
/// practice (HTTP HEAD responses rarely expose large samples), so the
/// detector gracefully degrades — when a particular signature needs
/// more bytes than available it returns `.unknown` rather than guessing.
///
/// Pattern: each entry is `(minLen, predicate)`. The first matching
/// entry wins. Where the format has signatures at offset != 0 (tar's
/// ustar at offset 257) we only ever sniff what fits in the prefix.
public enum ContentSniffer {
    public static func sniff(_ data: Data) -> SniffedKind {
        // The match table is small enough that a linear scan beats
        // building a trie/parser — every real call iterates only the
        // first ~32 bytes of the file.
        let bytes = Array(data.prefix(512))

        // Mach-O: 7F 'E' 'L' 'F' (also catches universal "fat" Mach-O
        // whose first 4 bytes are the same magic, subtype lives later).
        if bytes.count >= 4,
           bytes[0] == 0x7F, bytes[1] == 0x45, bytes[2] == 0x4C, bytes[3] == 0x46 {
            return .macho
        }

        // ZIP family — both single-disk and central-directory signatures.
        if bytes.count >= 4,
           bytes[0] == 0x50, bytes[1] == 0x4B,
           (bytes[2] == 0x03 && bytes[3] == 0x04)
           || (bytes[2] == 0x05 && bytes[3] == 0x06)
           || (bytes[2] == 0x07 && bytes[3] == 0x08) {
            return .zipFamily
        }

        // RAR: "Rar!\x1A\x07\x00" or "Rar!\x1A\x07\x01\x00"
        if bytes.count >= 7,
           bytes[0] == 0x52, bytes[1] == 0x61, bytes[2] == 0x72,
           bytes[3] == 0x21, bytes[4] == 0x1A, bytes[5] == 0x07 {
            return .rar
        }

        // 7-Zip: "7z\xBC\xAF\x27\x1C"
        if bytes.count >= 6,
           bytes[0] == 0x37, bytes[1] == 0x7A,
           bytes[2] == 0xBC, bytes[3] == 0xAF,
           bytes[4] == 0x27, bytes[5] == 0x1C {
            return .sevenZip
        }

        // DMG (UDIF/Koly trailer magic): "x\x01s\x0Dbb\x60\x60"
        if bytes.count >= 8,
           bytes[0] == 0x78, bytes[1] == 0x01,
           bytes[2] == 0x73, bytes[3] == 0x0D,
           bytes[4] == 0x62, bytes[5] == 0x62,
           bytes[6] == 0x60, bytes[7] == 0x60 {
            return .dmg
        }

        // Gzip: 1F 8B
        if bytes.count >= 2, bytes[0] == 0x1F, bytes[1] == 0x8B {
            return .gzip
        }

        // bzip2: "BZh"
        if bytes.count >= 3,
           bytes[0] == 0x42, bytes[1] == 0x5A, bytes[2] == 0x68 {
            return .bz2
        }

        // XZ: FD 37 7A 58 5A 00
        if bytes.count >= 6,
           bytes[0] == 0xFD, bytes[1] == 0x37, bytes[2] == 0x7A,
           bytes[3] == 0x58, bytes[4] == 0x5A, bytes[5] == 0x00 {
            return .xz
        }

        // PDF: %PDF-
        if bytes.count >= 5,
           bytes[0] == 0x25, bytes[1] == 0x50, bytes[2] == 0x44,
           bytes[3] == 0x46, bytes[4] == 0x2D {
            return .pdf
        }

        // JPEG: FF D8 FF
        if bytes.count >= 3,
           bytes[0] == 0xFF, bytes[1] == 0xD8, bytes[2] == 0xFF {
            return .jpeg
        }

        // PNG: 89 50 4E 47 0D 0A 1A 0A
        if bytes.count >= 8,
           bytes[0] == 0x89, bytes[1] == 0x50, bytes[2] == 0x4E, bytes[3] == 0x47,
           bytes[4] == 0x0D, bytes[5] == 0x0A, bytes[6] == 0x1A, bytes[7] == 0x0A {
            return .png
        }

        // GIF: GIF87a / GIF89a
        if bytes.count >= 6,
           bytes[0] == 0x47, bytes[1] == 0x49, bytes[2] == 0x46,
           bytes[3] == 0x38,
           (bytes[4] == 0x37 || bytes[4] == 0x39), bytes[5] == 0x61 {
            return .gif
        }

        // ISO Base Media File Format (mp4/m4v/mov/heic/...) — ftyp box at offset 4
        if bytes.count >= 12,
           bytes[4] == 0x66, bytes[5] == 0x74, bytes[6] == 0x79, bytes[7] == 0x70 {
            return .mp4Video
        }

        // ID3v2 mp3: "ID3"
        if bytes.count >= 3,
           bytes[0] == 0x49, bytes[1] == 0x44, bytes[2] == 0x33 {
            return .mp3
        }
        // MPEG-1 Layer III frame sync (FF Fx)
        if bytes.count >= 2,
           bytes[0] == 0xFF, (bytes[1] & 0xE0) == 0xE0 {
            return .mp3
        }

        // WebM / Matroska: 1A 45 DF A3
        if bytes.count >= 4,
           bytes[0] == 0x1A, bytes[1] == 0x45, bytes[2] == 0xDF, bytes[3] == 0xA3 {
            return .webm
        }

        // Ogg: "OggS"
        if bytes.count >= 4,
           bytes[0] == 0x4F, bytes[1] == 0x67, bytes[2] == 0x67, bytes[3] == 0x53 {
            return .ogg
        }

        // WAV: RIFF....WAVE — RIFF at start, "WAVE" at offset 8
        if bytes.count >= 12,
           bytes[0] == 0x52, bytes[1] == 0x49, bytes[2] == 0x46, bytes[3] == 0x46,
           bytes[8] == 0x57, bytes[9] == 0x41, bytes[10] == 0x56, bytes[11] == 0x45 {
            return .wav
        }

        // Java class: CAFEBABE
        if bytes.count >= 4,
           bytes[0] == 0xCA, bytes[1] == 0xFE, bytes[2] == 0xBA, bytes[3] == 0xBE {
            return .javaClass
        }

        // tar: 257-byte offset is too far for the 512-byte prefix on
        // every HTTP HEAD sample we expect to see; we leave `.tar`
        // detection to filename + the wrapper format. P3 may extend
        // this once we have full-file sniffing in FileAssembler.

        // text/html — heuristic by ASCII whitespace + tag chars
        if bytes.count >= 5,
           bytes.allSatisfy({ ($0 >= 0x20 && $0 < 0x7F) || $0 == 0x09 || $0 == 0x0A || $0 == 0x0D }) {
            // very coarse but practical — file seen contains only ASCII
            // printable / whitespace and the head is a tag like "<!DOCTYPE"
            // or "<html"; producers that ship binary with low-bit chars
            // already match stronger signatures above.
            let head = String(decoding: data.prefix(64), as: UTF8.self)
                .lowercased()
            if head.contains("<!doctype") || head.contains("<html")
                || head.starts(with: "<?xml") {
                return .htmlText
            }
        }

        return .unknown
    }

    /// HTTP HEAD probe — single GET-with-zero-range is more universally
    /// supported than HEAD on misbehaving servers, so we use a GET with
    /// `Range: bytes=0-0` and let Content-Sniffer observe the first byte.
    /// The actual byte body is discarded.
    public static func probe(
        _ url: URL,
        session: URLSession = .shared
    ) async throws -> ContentProbe {
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        req.setValue("bytes=0-0", forHTTPHeaderField: "Range")
        req.setValue("SDM/1.0 (+https://github.com/Mofan2020/SuperDownloadManager)",
                     forHTTPHeaderField: "User-Agent")
        req.timeoutInterval = 30

        let (data, response) = try await session.data(for: req)
        _ = data  // first byte only; we keep parsing headers

        guard let http = response as? HTTPURLResponse else {
            throw ProbeError.notHTTP
        }
        downloadLog.info("probe \(url) -> \(http.statusCode) finalURL=\(http.url?.absoluteString ?? "<none>") len=\(http.expectedContentLength)")
        return parse(http: http, originalURL: url)
    }

    public static func parse(http: HTTPURLResponse, originalURL: URL) -> ContentProbe {
        let headers = http.allHeaderFields
        func header(_ name: String) -> String? {
            (headers[name] as? String) ?? (headers[name.lowercased()] as? String)
        }
        let acceptRanges = (header("Accept-Ranges") ?? "").lowercased() != "none"
        // Prefer the "total" component of Content-Range when present
        // (a probe with `Range: bytes=0-0` returns 206 with a partial
        // Content-Length; the total size lives in Content-Range's
        // "/<total>" tail). Falls back to Content-Length for plain 200
        // probes and un-ranged responses.
        var len: Int64?
        if let cr = header("Content-Range"),
           let totalRange = cr.range(of: #"/(\d+)"#, options: .regularExpression) {
            let totalStr = String(cr[totalRange].dropFirst())  // drop the leading "/"
            len = Int64(totalStr)
        }
        if len == nil {
            let lenStr = header("Content-Length")
            len = lenStr.flatMap { Int64($0.trimmingCharacters(in: .whitespaces)) }
        }
        // HTTPURLResponse canonicalizes `ETag` to `Etag` on macOS
        // (some SDK versions). Look at both spellings.
        let etag = header("ETag") ?? header("Etag")
        let lastModified = header("Last-Modified") ?? header("Last-modified")

        var filename: String?
        if let cd = header("Content-Disposition") {
            // RFC 6266 / RFC 5987 parsing. There are three shapes
            // the server might use:
            //   1. `filename="foo.zip"`
            //   2. `filename=foo.zip`           ← unquoted, GitHub's CDN
            //   3. `filename*=UTF-8''foo.zip`   ← UTF-8 + lang tag
            //   4. some combination (1+3, 2+3) ← both, with filename* winning per RFC
            //
            // v1.0.x only handled (1) and (3), and broke on (2) — every
            // GitHub release-assets download fell through to the URL
            // path as filename, which for the CDN ends with a random
            // hex segment. v1.1.1 handles all four.
            filename = Self.parseContentDisposition(cd)
        }
        if filename == nil || filename?.isEmpty == true {
            // last-resort: derive from URL path. For GitHub's CDN this
            // is useless (last path component is a UUID), so it's
            // effectively a placeholder that the engine then renames
            // after a successful Content-Disposition probe.
            filename = originalURL.pathComponents.last
        }

        return ContentProbe(
            url: http.url ?? originalURL,
            contentLength: len,
            acceptsRanges: acceptRanges,
            etag: etag,
            lastModified: lastModified,
            contentType: header("Content-Type"),
            suggestedFilename: filename,
            isFinal: true
        )
    }
}

public enum ProbeError: Error {
    case notHTTP
}

// MARK: - Content-Disposition parsing

extension ContentSniffer {
    /// Parses an RFC 6266 / RFC 5987 `Content-Disposition` header
    /// into a usable filename, or `nil` if the header is absent
    /// or unparseable. Per RFC 6266 §4.3, when both `filename=`
    /// and `filename*=` are present, `filename*=` wins; the
    /// caller does NOT need to disambiguate.
    ///
    /// Supported shapes (any combination of):
    ///   - `filename="foo.zip"`              (RFC 6266 quoted)
    ///   - `filename=foo.zip`                (unquoted, RFC 6266 token)
    ///   - `filename*=UTF-8''foo%20bar.zip`  (RFC 5987 ext-val)
    ///   - mixed with `;` separators
    ///
    /// v1.0.x failed on the unquoted form (GitHub's CDN ships
    /// `attachment; filename=SuperDownloadManager.app.zip` with
    /// no quotes) and fell through to the URL path; for GitHub
    /// release-assets that's a random hex UUID, so downloads
    /// ended up named `efb1` or `download.bin`.
    public static func parseContentDisposition(_ header: String) -> String? {
        // Tokenize on `;` to get individual disposition params.
        // The first segment is the disposition type ("attachment",
        // "inline"); subsequent segments are name=value pairs.
        let segments = header.split(separator: ";", omittingEmptySubsequences: true)
        var plain: String? = nil
        var extVal: String? = nil
        for seg in segments {
            let s = seg.trimmingCharacters(in: .whitespaces)
            // Case-insensitive prefix match.
            if s.lowercased().hasPrefix("filename*=") {
                let raw = String(s.dropFirst("filename*=".count))
                extVal = decodeExtVal(raw)
            } else if s.lowercased().hasPrefix("filename=") {
                let raw = String(s.dropFirst("filename=".count))
                plain = decodePlain(raw)
            }
        }
        // RFC 6266: ext-val (filename*) wins when both present.
        return extVal ?? plain
    }

    /// Decode `UTF-8''<percent-encoded>` (the RFC 5987 ext-val
    /// production). We only support the `UTF-8` charset; if
    /// some other encoding is declared we fall back to
    /// percent-decoding the whole thing.
    private static func decodeExtVal(_ raw: String) -> String? {
        // ext-val = charset  "'" [ language ] "'" value-chars
        // e.g.   UTF-8''SuperDownloadManager.app.zip
        //        UTF-8''SDM%20App.zip
        guard let firstTick = raw.firstIndex(of: "'") else { return nil }
        let afterFirst = raw.index(after: firstTick)
        guard let secondTick = raw[afterFirst...].firstIndex(of: "'") else {
            return nil
        }
        // charset is raw[..<firstTick] (we only handle UTF-8; treat
        // anything else as a warning and still try to decode the
        // percent-encoded payload).
        let charset = String(raw[..<firstTick]).uppercased()
        let value = String(raw[raw.index(after: secondTick)...])
        let decoded = value.removingPercentEncoding ?? value
        _ = charset  // currently accepted: UTF-8 / ISO-8859-1 / others
        return decoded.isEmpty ? nil : decoded
    }

    /// Decode a plain `filename=...` value, supporting both
    /// RFC 6266 quoted and unquoted forms. Quoted takes
    /// precedence when present.
    private static func decodePlain(_ raw: String) -> String? {
        let s = raw.trimmingCharacters(in: .whitespaces)
        guard !s.isEmpty else { return nil }
        if s.hasPrefix("\"") && s.hasSuffix("\"") && s.count >= 2 {
            let inner = String(s.dropFirst().dropLast())
            return inner.isEmpty ? nil : inner
        }
        // Unquoted token: trim trailing semicolons/whitespace, but
        // the splitter already removed the `;` so we just need to
        // handle stray quotes / whitespace.
        let cleaned = s
            .trimmingCharacters(in: CharacterSet(charactersIn: ";\" "))
        return cleaned.isEmpty ? nil : cleaned
    }
}
