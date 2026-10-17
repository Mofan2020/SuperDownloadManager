import XCTest
@testable import SuperDownloadManagerCore

/// `FileURLHeuristic.isLikelyFileURL` decides whether a
/// clipboard URL should be auto-promoted into a download
/// candidate. The rule is intentionally conservative — we
/// only want URLs whose path extension maps to a known file
/// category, not bare hostnames or HTML pages.
final class FileURLHeuristicTests: XCTestCase {

    func testRecognisesFileExtensions() {
        XCTAssertTrue(FileURLHeuristic.isLikelyFileURL(
            URL(string: "https://example.com/photo.png")!))
        XCTAssertTrue(FileURLHeuristic.isLikelyFileURL(
            URL(string: "https://example.com/movie.mp4")!))
        XCTAssertTrue(FileURLHeuristic.isLikelyFileURL(
            URL(string: "https://cdn.example.com/data.zip")!))
        XCTAssertTrue(FileURLHeuristic.isLikelyFileURL(
            URL(string: "https://example.com/Setup.dmg")!))
    }

    func testRejectsWebPages() {
        // Bare hostnames — would always resolve to HTML.
        XCTAssertFalse(FileURLHeuristic.isLikelyFileURL(
            URL(string: "https://example.com/")!))
        XCTAssertFalse(FileURLHeuristic.isLikelyFileURL(
            URL(string: "https://example.com")!))
        // HTML-ish extensions
        XCTAssertFalse(FileURLHeuristic.isLikelyFileURL(
            URL(string: "https://example.com/index.html")!))
        XCTAssertFalse(FileURLHeuristic.isLikelyFileURL(
            URL(string: "https://example.com/post.php?id=1")!))
        // Query-only with no extension
        XCTAssertFalse(FileURLHeuristic.isLikelyFileURL(
            URL(string: "https://example.com/search?q=hello")!))
    }

    func testRejectsUnknownExtensions() {
        // .other category — unrecognised extension
        XCTAssertFalse(FileURLHeuristic.isLikelyFileURL(
            URL(string: "https://example.com/file.xyz123")!))
        XCTAssertFalse(FileURLHeuristic.isLikelyFileURL(
            URL(string: "https://example.com/garbage.totallymadeup")!))
    }

    func testCategoryTableCoversCoreFileTypes() {
        // A few representative picks from each bucket to
        // guard against accidental table removal.
        XCTAssertEqual(FileURLHeuristic.category(forExtension: "mp4"), "video")
        XCTAssertEqual(FileURLHeuristic.category(forExtension: "mp3"), "music")
        XCTAssertEqual(FileURLHeuristic.category(forExtension: "png"), "pictures")
        XCTAssertEqual(FileURLHeuristic.category(forExtension: "zip"), "archives")
        XCTAssertEqual(FileURLHeuristic.category(forExtension: "pdf"), "documents")
        XCTAssertEqual(FileURLHeuristic.category(forExtension: "dmg"), "software")
    }
}
