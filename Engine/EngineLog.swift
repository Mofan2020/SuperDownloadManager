// EngineLog — thin compatibility shim. v1.1.9 retires the
// direct `Logger(subsystem:category:)` instances that were
// declared here in favour of `SDMLogger.shared` — see
// `Engine/SDMLogger.swift` for the centralised file/level/
// retention logic. The two `Logger`-typed globals kept below
// (`engineLog`, `downloadLog`) keep the existing call sites
// compiling while funnelling through the new shared instance.

import Foundation
import os.log

/// Compatibility wrapper. The original `os.Logger` API is what
/// `engineLog` / `downloadLog` were typed as, so we keep the
/// same call shape (`engineLog.notice("...")`) but forward the
/// line into `SDMLogger` so the file-side persistence +
/// retention/rotation logic applies uniformly. Existing call
/// sites keep their old form for free.
final class SDMLoggerProxy {
    let category: String
    init(category: String) { self.category = category }

    func debug(_ message: String)   { SDMLogger.shared.debug(category, message) }
    func info(_ message: String)    { SDMLogger.shared.info(category, message) }
    func notice(_ message: String)  { SDMLogger.shared.notice(category, message) }
    func warning(_ message: String) { SDMLogger.shared.warning(category, message) }
    func error(_ message: String)   { SDMLogger.shared.error(category, message) }

    /// Mirror Apple's os.Logger log(_:) signature so call sites
    /// using `.log(level: .default, "...")` still compile.
    /// Maps every `OSLogType` (including future ones like
    /// `.signpost`) via `rawValue` instead of a fixed `case` list
    /// so we don't have to chase the SDK every release.
    func log(level: OSLogType, _ message: String) {
        switch level.rawValue {
        case OSLogType.debug.rawValue:   debug(message)
        case OSLogType.info.rawValue:    info(message)
        case OSLogType.default.rawValue: notice(message)
        case OSLogType.error.rawValue:   warning(message)
        case OSLogType.fault.rawValue:   error(message)
        default: notice(message)
        }
    }
}

let _engineLog = SDMLoggerProxy(category: "engine")
let downloadLog = SDMLoggerProxy(category: "download")