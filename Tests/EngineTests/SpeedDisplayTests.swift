import XCTest
@testable import SuperDownloadManagerCore

/// `SpeedDisplay` computes the per-row speed value the list
/// view shows. The v1.0.8 fix is a state-driven hard-zero
/// on terminal and paused rows; these tests lock the
/// contract.
final class SpeedDisplayTests: XCTestCase {

    func testActiveDownloadUsesEMA() {
        let out = SpeedDisplay.compute(
            input: .init(state: "downloading",
                         engineSpeedBytesPerSec: 1_000_000,
                         deltaBytesSinceLastTick: 0,
                         tickIntervalSeconds: 0.2),
            previousEMA: 0)
        XCTAssertEqual(out.displayedSpeed, 1_000_000)
        XCTAssertEqual(out.nextEMA, 1_000_000)
    }

    /// The actual bug. A row that just hit 100 % should
    /// show 0 KB/s immediately, not the pre-completion EMA
    /// decaying over 1 s.
    func testCompletedRowShowsZeroImmediately() {
        // Pretend the previous tick had 500 KB/s smoothed.
        let out = SpeedDisplay.compute(
            input: .init(state: "completed",
                         engineSpeedBytesPerSec: 0,
                         deltaBytesSinceLastTick: 0,
                         tickIntervalSeconds: 0.2),
            previousEMA: 500_000)
        XCTAssertEqual(out.displayedSpeed, 0,
            "v1.0.7 leaked the pre-completion speed into the terminal read")
        XCTAssertEqual(out.nextEMA, 0,
            "EMA must be reset on terminal, not left to decay")
    }

    func testFailedRowShowsZeroImmediately() {
        let out = SpeedDisplay.compute(
            input: .init(state: "failed",
                         engineSpeedBytesPerSec: 0,
                         deltaBytesSinceLastTick: 0,
                         tickIntervalSeconds: 0.2),
            previousEMA: 200_000)
        XCTAssertEqual(out.displayedSpeed, 0)
    }

    func testCancelledRowShowsZeroImmediately() {
        let out = SpeedDisplay.compute(
            input: .init(state: "cancelled",
                         engineSpeedBytesPerSec: 0,
                         deltaBytesSinceLastTick: 0,
                         tickIntervalSeconds: 0.2),
            previousEMA: 1_500_000)
        XCTAssertEqual(out.displayedSpeed, 0)
    }

    func testPausedRowShowsZeroImmediately() {
        // Pause should freeze the speed at 0 even though the
        // engine's bytesDone hasn't changed in 200 ms.
        let out = SpeedDisplay.compute(
            input: .init(state: "paused",
                         engineSpeedBytesPerSec: 0,
                         deltaBytesSinceLastTick: 0,
                         tickIntervalSeconds: 0.2),
            previousEMA: 800_000)
        XCTAssertEqual(out.displayedSpeed, 0,
            "paused rows must not keep showing the pre-pause rate")
    }

    func testEMASmoothingPreservedForActiveRows() {
        // A second consecutive tick with a new raw speed
        // should blend the new sample with the old EMA at α=0.4.
        let first = SpeedDisplay.compute(
            input: .init(state: "downloading",
                         engineSpeedBytesPerSec: 1_000_000,
                         deltaBytesSinceLastTick: 0,
                         tickIntervalSeconds: 0.2),
            previousEMA: 0)
        let second = SpeedDisplay.compute(
            input: .init(state: "downloading",
                         engineSpeedBytesPerSec: 0,
                         deltaBytesSinceLastTick: 50_000,
                         tickIntervalSeconds: 0.2),
            previousEMA: first.nextEMA)
        // rawSpeed = 50_000 / 0.2 = 250_000
        // EMA = 0.4 * 250_000 + 0.6 * 1_000_000 = 700_000
        XCTAssertEqual(second.nextEMA, 700_000, accuracy: 0.5)
    }
}
