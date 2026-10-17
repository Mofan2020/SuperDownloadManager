import Foundation

/// Heuristic that decides whether an http(s) URL is "almost
/// certainly a downloadable file" — used by the clipboard
/// watcher (and any other auto-capture) to decide whether to
/// surface a download candidate. The rule is intentionally
/// conservative: we'd rather miss a candidate than pester
/// the user with a download prompt for every chat-message
/// URL.
///
/// v1.0.7: this used to live in `DownloadCategory` in the
/// App target, but the Engine test target couldn't reach it
/// for unit testing. We extracted the pure URL → Bool
/// predicate into the Engine framework.
///
/// Returns `true` only if the URL's path extension maps to
/// one of the known file categories (video, music, picture,
/// archive, document, software). HTML, query-only URLs, and
/// bare hostnames all return `false`.
public enum FileURLHeuristic {
    public static func isLikelyFileURL(_ url: URL) -> Bool {
        let ext = url.pathExtension
        guard !ext.isEmpty else { return false }
        if ext.contains("/") || ext.contains("?") { return false }
        return category(forExtension: ext) != nil
    }

    /// Best-effort category from a path extension. Returns
    /// `nil` for unknown / web extensions. Duplicates the
    /// `DownloadCategory.from(url:)` table that lives in the
    /// App target — keeping it in lockstep is intentional.
    /// If you add a new category, also update
    /// `App/MainWindow/TaskSummary.swift` and the two
    /// `Localizable.strings` files.
    public static func category(forExtension ext: String) -> String? {
        switch ext.lowercased() {
        case "mp4", "mov", "mkv", "webm", "avi", "flv", "m4v", "wmv", "ts":
            return "video"
        case "mp3", "flac", "wav", "aac", "ogg", "m4a", "opus":
            return "music"
        case "jpg", "jpeg", "png", "gif", "webp", "heic", "svg", "bmp", "tiff":
            return "pictures"
        case "zip", "7z", "rar", "tar", "gz", "bz2", "xz",
             "iso", "tgz", "tbz2", "txz":
            return "archives"
        case "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
             "txt", "md", "epub", "mobi", "rtf", "csv":
            return "documents"
        case "exe", "msi", "deb", "rpm", "apk", "app", "dmg", "pkg":
            return "software"
        default:
            return nil
        }
    }
}
