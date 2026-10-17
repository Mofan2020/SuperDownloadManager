import Foundation
import SwiftUI
import SuperDownloadManagerCore

/// Compact denormalised summary the view consumes. The store hands
/// these out already-formatted for display so SwiftUI doesn't have
/// to recompute strings on every tick.
///
/// v1.0.4: extended with `urlString`, `urlHost`, `totalBytesOrZero`
/// (for `Table` column sorting), and `category` (the auto-classified
/// destination category, e.g. Video / Music / Archives).
public struct TaskSummary: Identifiable, Equatable {
    public let id: UUID
    public var filename: String
    public var urlString: String
    public var urlHost: String?
    public var state: String
    public var bytesDone: Int64
    public var totalBytes: Int64?
    public var speedBytesPerSec: Double
    public var destinationPath: String
    public var errorMessage: String?
    public var category: DownloadCategory?

    /// Convenience for `Table` sort comparators: optional Int64
    /// → 0 for "no info" so columns sort unknown to the end.
    public var totalBytesOrZero: Int64 { totalBytes ?? 0 }

    public var fractionDone: Double {
        guard let total = totalBytes, total > 0 else { return 0 }
        return Double(bytesDone) / Double(total)
    }

    public var speedDescription: String {
        ByteRateFormatter.string(from: speedBytesPerSec)
    }

    public var etaDescription: String? {
        guard let total = totalBytes,
              total > bytesDone,
              speedBytesPerSec > 0 else { return nil }
        let remaining = Double(total - bytesDone)
        return TimeFormatter.string(from: remaining / speedBytesPerSec)
    }

    public var isTerminal: Bool {
        switch state {
        case "completed", "failed", "cancelled": return true
        default: return false
        }
    }
}

/// Auto-classification bucket used by the destination-folder
/// rule. v1.0.4 P1-1: engine maps URL extension + content type
/// to one of these; the GUI renders an icon next to the row.
public enum DownloadCategory: String, CaseIterable, Codable, Equatable, Sendable {
    case video
    case music
    case pictures
    case archives
    case documents
    case software
    case other

    public var iconName: String {
        switch self {
        case .video:     return "play.rectangle.fill"
        case .music:     return "music.note"
        case .pictures:  return "photo.fill"
        case .archives:  return "archivebox.fill"
        case .documents: return "doc.text.fill"
        case .software:  return "shippingbox.fill"
        case .other:     return "questionmark.circle"
        }
    }

    public var tint: Color {
        switch self {
        case .video:     return .pink
        case .music:     return .purple
        case .pictures:  return .teal
        case .archives:  return .orange
        case .documents: return .blue
        case .software:  return .indigo
        case .other:     return .gray
        }
    }

    public var folderName: String {
        switch self {
        case .video:     return "Video"
        case .music:     return "Music"
        case .pictures:  return "Pictures"
        case .archives:  return "Archives"
        case .documents: return "Documents"
        case .software:  return "Software"
        case .other:     return "Other"
        }
    }

    /// Best-effort category from a URL's path extension. The
    /// engine uses a fuller rule (magic bytes + content type)
    /// when available, but for new tasks we can classify from
    /// the URL alone.
    public static func from(url: URL) -> DownloadCategory {
        let ext = url.pathExtension.lowercased()
        switch ext {
        case "mp4", "mov", "mkv", "webm", "avi", "flv", "m4v", "wmv", "ts":
            return .video
        case "mp3", "flac", "wav", "aac", "ogg", "m4a", "opus":
            return .music
        case "jpg", "jpeg", "png", "gif", "webp", "heic", "svg", "bmp", "tiff":
            return .pictures
        case "zip", "7z", "rar", "tar", "gz", "bz2", "xz",
             "iso", "tgz", "tbz2", "txz":
            return .archives
        case "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
             "txt", "md", "epub", "mobi", "rtf", "csv":
            return .documents
        case "exe", "msi", "deb", "rpm", "apk", "app", "dmg", "pkg":
            return .software
        default:
            return .other
        }
    }
}

public enum ByteRateFormatter {
    public static func string(from bytesPerSec: Double) -> String {
        if bytesPerSec >= 1_048_576 { return String(format: "%.2f MB/s", bytesPerSec / 1_048_576) }
        if bytesPerSec >= 1_024     { return String(format: "%.1f KB/s", bytesPerSec / 1_024) }
        return String(format: "%.0f B/s", bytesPerSec)
    }
}

public enum TimeFormatter {
    public static func string(from seconds: Double) -> String {
        if seconds < 60 { return String(format: "%.0fs", seconds) }
        let totalMin = Int(seconds / 60)
        let secs = Int(seconds.truncatingRemainder(dividingBy: 60))
        if totalMin < 60 { return "\(totalMin)m \(secs)s" }
        let h = totalMin / 60
        let m = totalMin % 60
        return "\(h)h \(m)m"
    }
}
