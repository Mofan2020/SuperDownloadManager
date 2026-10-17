import Foundation

/// ExpiredLinkHandler — P3 sub-task. Bridges the engine's
/// `HTTPDownloader.Failure.forbidden` (403/410/451) into a
/// "user must supply a new URL" state.
///
/// Design:
/// - When a chunk fails with `forbidden`, the engine marks the
///   whole task as `awaiting_new_link`. We don't drop the partial
///   bytes — that's the whole point.
/// - The handler keeps an "expiry evidence" record so the GUI can
///   show: "Original URL expired (server returned 403). Provide a
///   new URL that points to the same file.".
///
/// Verification of "same file":
/// 1. Re-probe the candidate URL with `ContentSniffer`.
/// 2. Compare content_signature (length / ETag / Last-Modified)
///    to the original probe. If they match → safe to resume.
/// 3. If ETag matches but length differs → warn user (file has
///    changed, not just re-uploaded with a new link).
/// 4. If probe returns no Accept-Ranges or 200 instead of 206 →
///    warn user (server doesn't allow segmented restart; we'll
///    have to throw away the partial).
///
/// All decisions flow through one method,
/// `evaluateRelink(original:new:)`, which returns a `Decision`.
public enum RelinkDecision: Sendable, Equatable {
    case safeToResume                // ETag + length match + Accept-Ranges
    case unsafeButProceed(reason: String)   // warn user but allow
    case unsafeReject(reason: String)       // block relink until user confirms manually
}

public struct ExpiredLinkHandler: Sendable {
    public let original: ResumeState
    public let downloader: HTTPDownloader

    public init(original: ResumeState, downloader: HTTPDownloader = .init()) {
        self.original = original
        self.downloader = downloader
    }

    public func evaluateRelink(candidate: URL) async -> RelinkDecision {
        let probe: ContentProbe
        do {
            probe = try await downloader.probe(candidate)
        } catch {
            return .unsafeReject(reason: "new URL probe failed: \(error)")
        }

        // Length check first — protects against classic "different
        // file, same ETag surprise" where a CDN rotates
        // content but the upstream happens to share an ETag
        // schema.
        if let newLen = probe.contentLength,
           original.contentLength > 0,
           newLen != original.contentLength {
            return .unsafeReject(reason: "content length differs (was \(original.contentLength), now \(newLen))")
        }
        if let o = original.etag, let n = probe.etag, o != n {
            return .unsafeReject(reason: "ETag differs (was \(o), now \(n))")
        }
        if !probe.acceptsRanges {
            return .unsafeButProceed(reason: "new URL does not advertise Accept-Ranges; existing partials will be discarded")
        }
        return .safeToResume
    }
}
