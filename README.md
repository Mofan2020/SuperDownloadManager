# Super Download Manager (SDM)

> **A native macOS multi-threaded downloader written from scratch in Swift.** No Electron, no aria2, no third-party runtime. Just an `.app` you drag into `/Applications`.

IDM does it on Windows. There's no good equivalent on macOS. So I wrote one.

![Platform](https://img.shields.io/badge/Platform-macOS%2026%2B-blue.svg)
![Swift](https://img.shields.io/badge/Swift-5.9-orange.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)
![Architecture](https://img.shields.io/badge/Arch-Apple%20Silicon%20%7C%20Intel-lightgrey.svg)

[中文文档](./README.zh-CN.md)

## Highlights

- **From-scratch Swift implementation.** No Electron shell, no embedded browser, no external runtime. The download engine, segment scheduler, GUI, and CLI all live in the same `.app`.
- **32 dynamic segments by default, up to 128.** Adaptive segment count based on server behavior; no manual tuning required.
- **Smart speed limiting with time-of-day rules.** Throttle at night, unthrottle during the day, or schedule per-task caps.
- **Automatic file categorization.** Downloads land in `~/Downloads/Video/`, `~/Downloads/Archives/`, etc. Extension-less `.dmg` / `.zip` are detected via magic bytes.
- **Browser takeover.** Firefox extension intercepts downloads natively. Safari extension sniffs links and hands them off (macOS does not allow Safari download interception, so SDM does what it can).
- **`sdmdm` CLI, auto-installed to `~/.local/bin`.** Every command returns a single line of JSON — designed to be called by AI agents and shell scripts.
- **Background daemon with auto-start.** LaunchAgent registers on first launch; survives logout, reboots silently.
- **Resume from expired links.** When a server returns 403/410/451 mid-download, SDM pauses the task, validates ETag / Content-Length of the replacement URL, and continues from the existing segments.

## Requirements

- macOS 26.0 or later
- Apple Silicon or Intel Mac

## Install

Download the latest `SuperDownloadManager.dmg` from [Releases](https://github.com/Mofan2020/SuperDownloadManager/releases), double-click it, and drag `SuperDownloadManager.app` into `/Applications`.

Open the app once to trigger LaunchAgent registration:

```sh
open /Applications/SuperDownloadManager.app
# First launch automatically runs install.sh to register the daemon.
```

Or do it manually:

```sh
sh /Applications/SuperDownloadManager.app/Contents/Resources/install.sh
```

Verify the daemon:

```sh
sdmdm ping
```

Should return `{"ok":true, "result":{"pong":true}}`.

## CLI

```
sdmdm add <url> [--referer R] [--filename F] [--category C] [--threads N]
sdmdm list
sdmdm pause <id> | resume <id> | cancel <id>
sdmdm relink <id> <new_url>
sdmdm config speed <bytes/sec> | config threads <count>
sdmdm ping
```

Every command returns one line of JSON. Designed for direct AI agent invocation.

## Browser Extensions

See [`docs/extension-install.md`](docs/extension-install.md).

- **Firefox** — Load `manifest.json` as a temporary add-on; full download interception.
- **Safari** — Allow the unsigned extension manually; link sniffing + handoff only (Apple's restrictions prevent Safari download interception).

## File Categorization

Downloads land in these folders after completion (paths are configurable in Settings):

| Category | Path |
| --- | --- |
| Video | `~/Downloads/Video/` |
| Pictures | `~/Downloads/Pictures/` |
| Audio | `~/Downloads/Audio/` |
| Archives (zip/dmg/7z/...) | `~/Downloads/Archives/` |
| Documents (PDF/DOCX/...) | `~/Downloads/Documents/` |
| Executables | `~/Downloads/Software/` |
| Other | `~/Downloads/` |

Files without extensions (`.dmg`, `.zip`) are identified by magic bytes.

## Resume from Expired Links

If the server returns 403/410/451 mid-download, SDM marks the task as "waiting for new link":

1. An orange banner appears at the top of the GUI.
2. Click "Refresh link" and paste the new URL.
3. SDM validates ETag / Content-Length match, then resumes from the already-downloaded segments.

CLI path: `sdmdm relink <task_id> <new_url>`.

## Auto-Update

The in-app "Check for Updates..." entry uses Sparkle. Updates are published to GitHub Releases and verified with edDSA signatures.

## Build (Developers)

Requires macOS 26+, Xcode 27, and `brew install xcodegen`.

```sh
xcodegen generate
xcodebuild -project SuperDownloadManager.xcodeproj \
           -scheme SuperDownloadManager \
           -configuration Debug \
           -derivedDataPath build build
open build/Build/Products/Debug/SuperDownloadManager.app
```

Run tests:

```sh
xcodebuild test -scheme SDMSelfTest -destination 'platform=macOS' -derivedDataPath build
```

Package a Release build:

```sh
xcodebuild -configuration Release -derivedDataPath build_release build
bash scripts/make-dmg.sh
```

## Repository Conventions

- Auto-commit + auto-push (CI builds + tag triggers release).
- `docs/notes.md` records "what was deliberately not changed, and why."
- Public docs stay short and direct; technical depth lives in `docs/release.md` and `docs/notes.md`.
- When borrowing algorithms from third parties: **study the algorithm, never copy the source**; document three places.

## License

MIT. See [LICENSE](./LICENSE).