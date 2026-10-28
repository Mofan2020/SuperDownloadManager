# Super Download Manager (SDM)

> An open-source cross-platform alternative to Internet Download Manager (IDM). macOS, Windows and Linux. One Python core, one web UI, no Electron, no bundled browser runtime.

IDM does it on Windows. macOS had no good equivalent. This is the cross-platform one.

[中文文档](./README.zh-CN.md)

![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Windows%20%7C%20Linux-blue.svg)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

## Features

- **Dynamic segmentation.** The segment layout grows while the transfer runs instead of being cut once at the start. The engine opens two connections, and only widens out once real progress arrives. A long-running segment gets split in half and the second half continues on a fresh connection.
- **Graceful fallback.** A server that answers a Range request with `200` instead of `206` makes the engine fall back to a single stream instead of writing a corrupt file.
- **Real streaming.** Bytes land on disk as they arrive. The progress bar moves while data is coming in, and a stalled connection is caught by an idle watchdog instead of a total-time limit.
- **Resume from expired links.** When a server returns 403/410/451 mid-download, the task waits for a new link. The replacement URL is checked against the ETag and Content-Length before the download continues from the bytes already on disk.
- **Session files.** The segment layout is written to `<name>.sdm.session.json`, so a crash or a kill can be resumed against the same layout.
- **Structured errors.** Failures are classified into twelve categories, each with a plain-language explanation of what happened and whether a retry can fix it.
- **Per-task and global speed limits,** plus time-of-day rules that throttle at night and unthrottle during the day.
- **Proxies, checksums and post-completion actions.** Per-task or global HTTP/SOCKS proxies, optional md5/sha1/sha256 verification, and open-file / open-folder / run-script / sleep on completion.
- **Automatic file categorization.** Downloads land in `~/Downloads/Video/`, `~/Downloads/Archives/` and so on. Files without extensions are identified by magic bytes.
- **Browser takeover over Native Messaging.** Chrome, Edge, Firefox and Safari extensions hand downloads to the app and carry the page's Cookie, User-Agent and Referer with them.
- **A task queue.** Only a bounded number of downloads run at once; the rest wait their turn and can be moved to the front or the back.
- **`sdm` CLI.** Every command returns JSON, built to be called from shell scripts and AI agents.
- **Background daemon with auto-start.** Survives logout and reboot.
- **Liquid glass interface.** The window resizes freely and the layout reflows instead of clipping. Light and dark themes.
- **macOS menu bar and Dock.** A live status line in the menu bar, the overall progress on the Dock icon, and an AppleScript dictionary.
- **Windows tray.** The same menu behaviour in the notification area, plus a registered `superdownloadmanager://` URL scheme.

## Requirements

- macOS 12 or later, Windows 22H2 or later, or a recent Linux
- Python 3.11 or later when running from source

## Install

Download the archive for your platform from [Releases](https://github.com/Mofan2020/SuperDownloadManager/releases), then extract it.

macOS:

```sh
ditto -x -k SuperDownloadManager-macOS.zip /Applications
open /Applications/SuperDownloadManager.app
```

Windows: extract the zip and run `SuperDownloadManager.exe`.

The CLI and the background daemon ship inside the app. The first launch puts
`sdm` on your PATH and registers a logon task that keeps the daemon running —
nothing else to run. Open a new terminal window afterwards so it picks up the
updated PATH.

```sh
sdm ping
```

## Run from source

```sh
git clone https://github.com/Mofan2020/SuperDownloadManager.git
cd SuperDownloadManager/pymain
python -m pip install -r requirements.txt
python -m sdm.gui.main
```

## CLI

The `sdm` command talks to the background daemon. It is installed on your
PATH the first time the app launches:

```sh
sdm ping
```

```
sdm add <url> [--referer R] [--filename F] [--category C] [--threads N]
sdm list
sdm pause <id> | resume <id> | cancel <id>
sdm relink <id> <new_url>
sdm config speed <bytes/sec> | config threads <count>
sdm ping
```

Every command returns one line of JSON.

## Browser extensions

See [`docs/extension-install.md`](docs/extension-install.md).

The extensions talk to the app over **Native Messaging**. The app ships a small
`sdmd-native-host` binary and installs the host manifest into your browser
profile on first launch — no setup needed. The fallback channel is a loopback
HTTP server that requires a pairing token.

- **Chrome / Edge** — load the unpacked extension from `Extensions/Chrome/` or `Extensions/Edge/`. Downloads are intercepted and the page's cookies travel with them.
- **Firefox** — load `Extensions/Firefox/manifest.json` as a temporary add-on for full download interception.
- **Safari** — allow the unsigned extension manually. Apple does not permit download interception in Safari, so the extension sniffs links and hands them off instead.

## AppleScript (macOS)

The app ships an AppleScript dictionary, so it can be driven from Script Editor
or any script that speaks Apple events:

```applescript
tell application "Super Download Manager"
    set taskId to add download "https://example.com/big.zip"
    pause all downloads
    resume all downloads
    get download list
end tell
```

The commands go through the same task service as the GUI and the CLI — they
never bypass the engine's state machine.

## File categorization

| Category | Path |
| --- | --- |
| Video | `~/Downloads/Video/` |
| Pictures | `~/Downloads/Pictures/` |
| Audio | `~/Downloads/Audio/` |
| Archives | `~/Downloads/Archives/` |
| Documents | `~/Downloads/Documents/` |
| Software | `~/Downloads/Software/` |
| Other | `~/Downloads/` |

## Resume from expired links

If the server returns 403/410/451 mid-download, SDM marks the task as "waiting for new link":

1. An orange banner appears at the top of the GUI.
2. Click "Refresh link" and paste the new URL.
3. SDM validates the ETag and Content-Length, then resumes from the segments already on disk.

CLI path: `sdm relink <task_id> <new_url>`.

## Build (developers)

```sh
cd pymain
python -m pip install -r requirements.txt pyinstaller
python scripts/build.py          # macOS .app or Windows .exe
```

Run tests:

```sh
cd pymain
python -m pytest tests -q
python -m sdm.gui.main --self-test
```

## Repository layout

| Path | What |
|---|---|
| `pymain/` | The application: `sdm/engine` (httpx download engine), `sdm/gui` (pywebview shell), `sdm/ipc`, `sdm/cli` |
| `pymain/tests/` | pytest suite |
| `Extensions/` | Chrome / Edge / Firefox / Safari browser extensions |
| `docs/` | API contract, UI spec, release process, decision log |

## Repository conventions

- Auto-commit + auto-push. A tag triggers the release workflow.
- `docs/notes.md` records what was deliberately not changed, and why.
- Public docs stay short and direct. Technical depth lives in `docs/release.md` and `docs/notes.md`.

## License

MIT. See [LICENSE](./LICENSE).
