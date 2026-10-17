import XCTest
@testable import SuperDownloadManagerCore

final class ContentSnifferTests: XCTestCase {
    func testMachO() {
        XCTAssertEqual(ContentSniffer.sniff(Data([0x7F, 0x45, 0x4C, 0x46, 0x02, 0x01])), .macho)
    }

    func testZIPFamily() {
        // PK\x03\x04 — single disk entry
        XCTAssertEqual(ContentSniffer.sniff(Data([0x50, 0x4B, 0x03, 0x04])), .zipFamily)
        // PK\x05\x06 — end-of-central-directory
        XCTAssertEqual(ContentSniffer.sniff(Data([0x50, 0x4B, 0x05, 0x06])), .zipFamily)
        // PK\x07\x08 — spanned
        XCTAssertEqual(ContentSniffer.sniff(Data([0x50, 0x4B, 0x07, 0x08])), .zipFamily)
    }

    func testRAR() {
        XCTAssertEqual(ContentSniffer.sniff(Data([0x52, 0x61, 0x72, 0x21, 0x1A, 0x07, 0x00])), .rar)
    }

    func testSevenZip() {
        XCTAssertEqual(ContentSniffer.sniff(Data([0x37, 0x7A, 0xBC, 0xAF, 0x27, 0x1C])), .sevenZip)
    }

    func testDMG() {
        XCTAssertEqual(ContentSniffer.sniff(Data([0x78, 0x01, 0x73, 0x0D, 0x62, 0x62, 0x60, 0x60])), .dmg)
    }

    func testGzip() {
        XCTAssertEqual(ContentSniffer.sniff(Data([0x1F, 0x8B])), .gzip)
    }

    func testBz2() {
        XCTAssertEqual(ContentSniffer.sniff(Data([0x42, 0x5A, 0x68])), .bz2)
    }

    func testXZ() {
        XCTAssertEqual(ContentSniffer.sniff(Data([0xFD, 0x37, 0x7A, 0x58, 0x5A, 0x00])), .xz)
    }

    func testPDF() {
        XCTAssertEqual(ContentSniffer.sniff(Data([0x25, 0x50, 0x44, 0x46, 0x2D])), .pdf)
    }

    func testJPEG() {
        XCTAssertEqual(ContentSniffer.sniff(Data([0xFF, 0xD8, 0xFF, 0xE0])), .jpeg)
    }

    func testPNG() {
        XCTAssertEqual(ContentSniffer.sniff(Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])), .png)
    }

    func testGIF() {
        XCTAssertEqual(ContentSniffer.sniff(Data([0x47, 0x49, 0x46, 0x38, 0x39, 0x61])), .gif)
        XCTAssertEqual(ContentSniffer.sniff(Data([0x47, 0x49, 0x46, 0x38, 0x37, 0x61])), .gif)
    }

    func testMP4Ftyp() {
        // ftyp at offset 4 — bytes 4..7 are 'f','t','y','p'
        let bytes = Data([0x00, 0x00, 0x00, 0x18, 0x66, 0x74, 0x79, 0x70, 0x6D, 0x70, 0x34, 0x32])
        XCTAssertEqual(ContentSniffer.sniff(bytes), .mp4Video)
    }

    func testMP3ID3() {
        XCTAssertEqual(ContentSniffer.sniff(Data([0x49, 0x44, 0x33, 0x03, 0x00])), .mp3)
    }

    func testMP3Frame() {
        // FF Fx (any sync)
        XCTAssertEqual(ContentSniffer.sniff(Data([0xFF, 0xFB])), .mp3)
    }

    func testWebM() {
        XCTAssertEqual(ContentSniffer.sniff(Data([0x1A, 0x45, 0xDF, 0xA3])), .webm)
    }

    func testOgg() {
        XCTAssertEqual(ContentSniffer.sniff(Data([0x4F, 0x67, 0x67, 0x53])), .ogg)
    }

    func testWAV() {
        let bytes = Data([0x52, 0x49, 0x46, 0x46, 0x00, 0x00, 0x00, 0x00,
                          0x57, 0x41, 0x56, 0x45])
        XCTAssertEqual(ContentSniffer.sniff(bytes), .wav)
    }

    func testJavaClass() {
        XCTAssertEqual(ContentSniffer.sniff(Data([0xCA, 0xFE, 0xBA, 0xBE])), .javaClass)
    }

    func testHTMLDoctype() {
        let s = "<!DOCTYPE html><html></html>"
        XCTAssertEqual(ContentSniffer.sniff(Data(s.utf8)), .htmlText)
    }

    func testUnknownEmpty() {
        XCTAssertEqual(ContentSniffer.sniff(Data()), .unknown)
    }

    func testParseContentRangeTotal() {
        // Server returned 206 with Content-Range "bytes 0-0/65536";
        // the total file size lives in the "/<total>" tail and
        // must override Content-Length 1 (which is the partial
        // response's body, not the file size).
        let url = URL(string: "https://example.com/foo.dmg")!
        let headers: [AnyHashable: Any] = [
            "Content-Length": "1",
            "Content-Range": "bytes 0-0/65536",
            "Accept-Ranges": "bytes",
            "Content-Type": "application/octet-stream",
            "Etag": "x"  // HTTPURLResponse reports canonical case-sensitive keys
        ]
        let http = HTTPURLResponse(url: url, statusCode: 206,
                                   httpVersion: "HTTP/1.1",
                                   headerFields: headers as? [String: String])!
        let probe = ContentSniffer.parse(http: http, originalURL: url)
        XCTAssertEqual(probe.contentLength, 65_536)
        XCTAssertNotNil(probe.etag)
        XCTAssertEqual(probe.acceptsRanges, true)
    }

    func testParseNoContentRange() {
        // 200 OK (no probe Range); Content-Length is the total.
        let url = URL(string: "https://example.com/foo.dmg")!
        let headers: [AnyHashable: Any] = [
            "Content-Length": "2048",
            "Accept-Ranges": "bytes",
            "Content-Type": "application/octet-stream",
        ]
        let http = HTTPURLResponse(url: url, statusCode: 200,
                                   httpVersion: "HTTP/1.1",
                                   headerFields: headers as? [String: String])!
        let probe = ContentSniffer.parse(http: http, originalURL: url)
        XCTAssertEqual(probe.contentLength, 2_048)
    }

    func testUnknownLarger() {
        // random binary that matches no signatures above
        let bytes = Data([0x00, 0xFF, 0x13, 0x37, 0x42])
        XCTAssertEqual(ContentSniffer.sniff(bytes), .unknown)
    }
}
