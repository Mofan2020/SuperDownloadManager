# Browser Extension Install

> Quick install steps for Firefox and Safari. Both extensions dispatch downloads to the running SDM via `http://127.0.0.1:7955/add`. The host app must be running.

## Firefox

1. Make sure SDM is running (status-bar icon present, or `launchctl list | grep com.skyc8266.superdownloadmanager`).
2. Firefox → `about:debugging#/runtime/this-firefox`
3. **Load Temporary Add-on...** → pick `Extensions/Firefox/manifest.json`
4. The toolbar shows a purple ↓ icon. Click any download link in Firefox — SDM takes over.

> **Persistence**: temporary add-ons vanish on Firefox restart. Re-load `manifest.json` after each restart. v1.0 doesn't ship a signed XPI (would need an AMO / addons.mozilla.org account).

## Safari (unsigned)

1. Safari → Settings → Advanced → **Show features for web developers** ✓
2. Open the Develop menu (top menu bar). If the option is grayed out, the toggle above didn't take. Some macOS versions require logging out and back in.
3. Develop → **Allow Unsigned Extensions** (press Option to reveal in some macOS versions)
4. Develop → **Load Unpacked Extension...** → pick `Extensions/Safari/`
5. Safari → Settings → Extensions → enable **Super Download Manager**

> **macOS re-authorization**: on every macOS reboot Safari asks again "Allow extensions from 'SuperDownloadManager.app' developer?" — click Always Allow. macOS may also require opening Safari once after the Allow to register the extension permanently.
>
> **Sandbox warning**: Safari extensions run in a sandbox. The fetch to `127.0.0.1:7955` is allowed because the extension declares `host_permissions: ["<all_urls>"]`, but some macOS versions (15+) prompt for local network access the first time the extension tries to talk to the daemon. Click **Allow** when prompted.
>
> **No download interception on Safari**: Safari's WebExtension API doesn't expose `browser.downloads.onCreated` the way Firefox does. The Safari extension can only react to the user's hover button on media / file links. For actual take-over of all downloads, use Firefox.

## Verify

Quick check from a terminal:

```sh
curl 'http://127.0.0.1:7955/add?url=https://example.com/test.zip'
```

Should return `{"ok":true,"result":{"taskId":"..."}}` and a new row should appear in SDM.

## Uninstall

1. Firefox: `about:debugging#/runtime/this-firefox` → **Remove**
2. Safari: Settings → Extensions → uncheck + remove SDM
3. CLI: `~/.local/bin/sdm` (the `sdmd` daemon and the `sdm` CLI ship inside the app bundle; `scripts/install.sh` links the CLI into `~/.local/bin`)
