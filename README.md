# Super Download Manager (SDM)

> A multi-threaded downloader for macOS, Windows and Linux. One Python core, one web UI, no Electron, no bundled browser runtime.

IDM does it on Windows. macOS had no good equivalent. So this is one.

[中文文档](./README.zh-CN.md)

![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Windows%20%7C%20Linux-blue.svg)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

## Features

- **32 segments per download by default, up to 128.** The scheduler re-plans the segment layout before transfer starts when the file size and segment size do not line up.
- **Real streaming.** Bytes land on disk as they arrive. The progress bar moves while data is coming in, and a stalled connection is caught by an idle watchdog instead of a total-time limit.
- **Resume from expired links.** When a server returns 403/410/451 mid-download, the task waits for a new link. The replacement URL is checked against the ETag and Content-Length before the download continues from the bytes already on disk.
- **Per-task and global speed limits,** plus time-of-day rules that throttle at night and unthrottle during the day.
- **Automatic file categorization.** Downloads land in `~/Downloads/Video/`, `~/Downloads/Archives/` and so on. Files without extensions are identified by magic bytes.
- **Browser takeover.** The Firefox extension intercepts downloads natively. The Safari extension sniffs links and hands them off.
- **`sdm` CLI.** Every command returns JSON, built to be called from shell scripts and AI agents.
- **Background daemon with auto-start.** Survives logout and reboot.
- **Liquid glass interface.** The window resizes freely and the layout reflows instead of clipping.

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

## Run from source

```sh
git clone https://github.com/Mofan2020/SuperDownloadManager.git
cd SuperDownloadManager/pymain
python -m pip install -r requirements.txt
python -m sdm.gui.main
```

## CLI

The `sdm` command talks to the background daemon. Run the installer once to
put it on your PATH and register the daemon:

```sh
sh /Applications/SuperDownloadManager.app/Contents/Resources/install.sh
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

- **Firefox** — load `manifest.json` as a temporary add-on for full download interception.
- **Safari** — allow the unsigned extension manually. Apple does not permit download interception in Safari, so the extension sniffs links and hands them off instead.

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

## Repository conventions

- Auto-commit + auto-push. A tag triggers the release workflow.
- `docs/notes.md` records what was deliberately not changed, and why.
- Public docs stay short and direct. Technical depth lives in `docs/release.md` and `docs/notes.md`.

## License

MIT. See [LICENSE](./LICENSE).
