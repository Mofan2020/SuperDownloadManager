import Foundation

// sdmdm CLI entry point.
let exitCode = SDMDM.run(args: CommandLine.arguments.dropFirst().map(String.init))
exit(exitCode)
