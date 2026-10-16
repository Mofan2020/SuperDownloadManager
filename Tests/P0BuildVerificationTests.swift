import XCTest

/// P0 stub — proves the test target wires correctly into the app target
/// under XcodeGen and that `xcodebuild test` finds at least one
/// runnable test (per Swift testing requirement: a test bundle with
/// zero tests fails to link).
final class P0BuildVerificationTests: XCTestCase {
    func testBuildScaffoldingLoads() {
        XCTAssertTrue(true, "P0 scaffold compiles and links")
    }
}
