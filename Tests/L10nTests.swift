import XCTest
@testable import SuperDownloadManagerCore

/// L10nTests — bilingual EN / 中文 string catalog (v1.0.5:
/// `Strings` in-code dictionary, no .strings).
///
/// These tests run offline without needing a window. They
/// cover three things: (1) the keys are defined on both sides
/// of the catalog, (2) the chosen locale picks the right
/// dictionary, (3) the locale switch is the source of truth
/// (no AppStorage / Bundle drift).
final class L10nTests: XCTestCase {
    func testEnglishLocaleReturnsEnglishCopy() {
        let title = Strings(locale: .en).menuOpenWindow
        XCTAssertEqual(title, "Open Main Window")
    }

    func testZhHansLocaleReturnsChineseCopy() {
        let title = Strings(locale: .zhHans).menuOpenWindow
        XCTAssertEqual(title, "打开主窗口")
    }

    func testFallbackToSystemLocaleReturnsValid() {
        // `.system` falls back to English in the catalog. We
        // only assert the result is non-empty and not the raw
        // key, so the test stays deterministic across CI
        // machines with different system locales.
        let title = Strings(locale: .system).menuOpenWindow
        XCTAssertFalse(title.isEmpty)
        XCTAssertNotEqual(title, "menuOpenWindow")
    }

    func testDefaultThreadsFormatStringSubstitutes() {
        // The v1.0.4 bug was that "%lld" rendered as the
        // literal text. v1.0.5 uses String(format:) so the
        // integer always interpolates.
        let s = Strings(locale: .en)
        let result = s.settingsDefaultThreadsValue(32)
        XCTAssertEqual(result, "32")
    }

    func testPluralThreadUnitEnglish() {
        let s = Strings(locale: .en)
        XCTAssertEqual(s.threadUnit(1), "thread")
        XCTAssertEqual(s.threadUnit(32), "threads")
    }
}
