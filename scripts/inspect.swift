// Inspect what the bypass probe produced. Counts the
// occurrences of 'a' bytes in the assembled partial file
// and writes a hexdump for any non-'a' bytes.

import Foundation

@main struct Inspect {
    static func main() async {
        let path = ProcessInfo.processInfo.environment["FILE"] ?? "/tmp/empty"
        let url = URL(fileURLWithPath: path)
        let data = (try? Data(contentsOf: url)) ?? Data()
        var a = 0; var other = 0; var otherBytes = ""
        for b in data {
            if b == 0x61 { a += 1 } else { other += 1; otherBytes += String(format: "%02x ", b) }
        }
        print("file=\(path) size=\(data.count) a's=\(a) non-a=\(other) non-a-bytes=[\(otherBytes)]")
    }
}
