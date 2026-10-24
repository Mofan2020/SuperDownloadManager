import Foundation
import SuperDownloadManagerCore

// sdmdm CLI entry point.
//
// v1.1.10: `CommandLine.arguments` is `[String]`, and both
// `dropFirst()` and the `[_...]` slice are ambiguous here because
// `CommandLine.arguments` also has a `Count`/`Index` overload set
// on the `Collection` protocol. The unambiguous form is to build
// the argument list from `arguments` explicitly:
//
//     CommandLine.arguments  ->  ["sdmdm", "add", "https://…"]
//
// so the subcommand and its flags start at index 1.
var args: [String] = []
args.reserveCapacity(CommandLine.arguments.count)
for index in 1..<CommandLine.arguments.count {
    args.append(CommandLine.arguments[index])
}

let exitCode = SDMDM.run(args: args)
exit(exitCode)
