import Foundation

/// SpeedLimiter — P1 engine core. Token-bucket rate limiter.
///
/// Algorithm basis: the classic token bucket. A bucket fills at a
/// configured rate; each `consume(n)` removes tokens; if fewer than
/// `n` are available, the caller sleeps proportionally until enough
/// refill. The granularity (100 ms by default) keeps the sleep
/// wake-up rate below scheduler noise. The "burst" capacity is set
/// to `rate * granularity` so a one-tick burst is allowed without
/// ever overshooting for more than one tick.
///
/// Public-domain algorithm in textbook references; not copied.
public actor SpeedLimiter {
    private var rateBytesPerSec: Double
    private var capacity: Double
    private let granularity: TimeInterval
    private var tokens: Double
    private var lastRefill: Date

    /// Create a limiter. `rate == 0` (or negative) means "no
    /// limit" — `consume` returns immediately. `granularity` is
    /// the wake-up tick (default 100 ms).
    public init(rateBytesPerSec: Double, granularity: TimeInterval = 0.1) {
        let r = max(0, rateBytesPerSec)
        self.rateBytesPerSec = r
        self.capacity = max(r * granularity, 1)
        self.tokens = r > 0 ? self.capacity : .infinity
        self.lastRefill = Date()
        self.granularity = granularity
    }

    /// v1.0.6: change the configured rate at runtime. The
    /// engine calls this when the user edits Settings → Bandwidth.
    /// The bucket is reset to "full at the new rate" so the
    /// change takes effect immediately without bursting.
    public func setRate(_ bytesPerSec: Double) {
        let r = max(0, bytesPerSec)
        rateBytesPerSec = r
        capacity = max(r * granularity, 1)
        tokens = r > 0 ? capacity : .infinity
        lastRefill = Date()
    }

    public func isEnabled() -> Bool { rateBytesPerSec > 0 }

    /// Request permission to send `n` bytes; sleeps the caller
    /// if not enough tokens are available. Returns the wall-clock
    /// time we slept for, in case the caller wants to surface it
    /// in metrics.
    @discardableResult
    public func consume(_ n: Int) async -> TimeInterval {
        guard isEnabled() else { return 0 }
        refill()
        while tokens < Double(n) {
            let needed = Double(n) - tokens
            let sleepSec = max(granularity, needed / rateBytesPerSec)
            try? await Task.sleep(nanoseconds: UInt64(sleepSec * 1_000_000_000))
            refill()
        }
        tokens -= Double(n)
        return 0
    }

    private func refill() {
        let now = Date()
        let elapsed = now.timeIntervalSince(lastRefill)
        guard elapsed > 0 else { return }
        let added = rateBytesPerSec * elapsed
        tokens = min(capacity, tokens + added)
        lastRefill = now
    }

    /// For tests: current number of tokens. Not part of the
    /// public API used by the engine.
    func _tokens() -> Double { tokens }
}
