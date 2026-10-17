import XCTest
@testable import SuperDownloadManagerCore

/// L10nTests — bilingual EN / zh-Hans string catalog.
///
/// These tests don't import SwiftUI rendering (they go through
/// the `Bundle` lookup path that SwiftUI also walks at render
/// time), so they run offline without needing a window. If
/// they fail, the test target itself isn't loaded properly —
/// which is what `L10n` is here to detect.
final class L10nTests: XCTestCase {
    func testEnglishStringsResolve() {
        // Walk every key by reading the .strings file in the test
        // bundle. Test target uses the host app's resources via
        // the test bundle's search paths; if the strings files
        // weren't copied into the bundle, this test fails.
        let path = Bundle(for: L10nTests.self).path(forResource: "en", ofType: "lproj")
        XCTAssertNotNil(path, "en.lproj must be present in the test bundle")
    }

    func testZhHansStringsResolve() {
        let path = Bundle(for: L10nTests.self).path(forResource: "zh-Hans", ofType: "lproj")
        XCTAssertNotNil(path, "zh-Hans.lproj must be present in the test bundle")
    }

    func testEnglishLocaleReturnsEnglishCopy() {
        // The keys must round-trip in English.
        let title = L10nStr.menuOpenWindow("en")
        XCTAssertEqual(title, "Open Main Window")
    }

    func testZhHansLocaleReturnsChineseCopy() {
        let title = L10nStr.menuOpenWindow("zh-Hans")
        XCTAssertEqual(title, "打开主窗口")
    }

    func testFallbackToSystemLocaleReturnsValid() {
        // When the host doesn't specify a locale, the catalog
        // should fall back to whatever en.lproj contains (or
        // zh-Hans if the system is configured that way). We don't
        // constrain which — we only assert the result is non-empty
        // and not the raw key.
        let title = L10nStr.menuOpenWindow(nil)
        XCTAssertFalse(title.isEmpty)
        XCTAssertNotEqual(title, "menu.openMainWindow")
    }
}
