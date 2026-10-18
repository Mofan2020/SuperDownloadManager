// Engine startup: log every URLSession connection attempt.
// Helps diagnose "download does nothing" by checking
// Console.app for SDM.

import Foundation
import os.log

let _engineLog = Logger(subsystem: "com.skyc8266.superdownloadmanager",
                        category: "engine")
let downloadLog = Logger(subsystem: "com.skyc8266.superdownloadmanager",
                         category: "download")

