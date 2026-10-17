import Foundation

/// SpeedRule — P6 rule-system for time-windowed rate limiting.
///
/// v1.0 model: a stack of `(from, to, bytesPerSec)` windows; we
/// pick the *first* window whose `[from, to)` covers "now". When no
/// window matches, the global ceiling applies. So a user can:
///   - keep working hours at full speed (no rule → ceiling)
///   - cap night hours to 5 MB/s
///   - cap any hour 18-22 to 1 MB/s (kids bedtime)
///
/// Rules are stored in `UserDefaults` keyed by
/// `sdm.speed.rules` as JSON. v1.0 GUI in `SettingsView` lets the
/// user edit/add/remove rules.
public struct SpeedRule: Codable, Sendable, Equatable {
    public var fromMinute: Int   // 0..1439
    public var toMinute: Int     // exclusive; may wrap (<from)
    public var bytesPerSec: Double

    public init(fromMinute: Int, toMinute: Int, bytesPerSec: Double) {
        self.fromMinute = fromMinute
        self.toMinute = toMinute
        self.bytesPerSec = bytesPerSec
    }

    /// Does `minute` (0..1439) fall inside this rule's range?
    public func covers(_ minute: Int) -> Bool {
        if fromMinute < toMinute {
            return minute >= fromMinute && minute < toMinute
        }
        // wrap-around (e.g. 22:00–06:00)
        return minute >= fromMinute || minute < toMinute
    }
}

public actor SpeedPolicy {
    private(set) var rules: [SpeedRule]
    private(set) var ceilingBytesPerSec: Double

    public init(rules: [SpeedRule] = [], ceiling: Double = 0) {
        self.rules = rules
        self.ceilingBytesPerSec = ceiling
    }

    public func add(_ rule: SpeedRule) {
        rules.append(rule)
    }

    public func remove(at index: Int) {
        guard rules.indices.contains(index) else { return }
        rules.remove(at: index)
    }

    public func setCeiling(_ bps: Double) {
        ceilingBytesPerSec = max(0, bps)
    }

    /// Effective rate right now. `0` means "unlimited".
    public func effectiveRateBytesPerSec(at date: Date = Date()) -> Double {
        let minute = Calendar.current.component(.minute, from: date) +
                     60 * Calendar.current.component(.hour, from: date)
        for r in rules where r.covers(minute) {
            return r.bytesPerSec
        }
        return ceilingBytesPerSec
    }
}
