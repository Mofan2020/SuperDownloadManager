#!/bin/sh
# Build a release DMG from the built .app. Output goes to the user's
# preferred Downloads/Archives/ directory.
set -eu

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="${PROJECT_ROOT}/build_release/Build/Products/Release/SuperDownloadManager.app"
OUT_DIR="${HOME}/Downloads/Archives"
OUT_NAME="SuperDownloadManager-$(defaults read "${APP}/Contents/Info" CFBundleShortVersionString)-$(uname -m).dmg"
OUT="${OUT_DIR}/${OUT_NAME}"

if [ ! -d "${APP}" ]; then
    echo "[make-dmg.sh] missing ${APP}; build Release first." >&2
    exit 1
fi

mkdir -p "${OUT_DIR}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT
cp -R "${APP}" "${TMP_DIR}/"

# create-dmg renders a nicer window but is optional; we fall back to
# hdiutil when create-dmg isn't installed.
if command -v create-dmg >/dev/null 2>&1; then
    create-dmg \
        --volname "Super Download Manager" \
        --window-pos 200 120 \
        --window-size 600 400 \
        --icon-size 100 \
        --icon "SuperDownloadManager.app" 175 190 \
        --hide-extension "SuperDownloadManager.app" \
        --app-drop-link 425 190 \
        "${OUT}" "${TMP_DIR}/" || true
    # create-dmg exits non-zero when the app has ad-hoc signing issues;
    # fall through to hdiutil if so.
fi
if [ ! -f "${OUT}" ]; then
    hdiutil create -ov -volname "Super Download Manager" \
        -fs HFS+ -srcfolder "${TMP_DIR}" \
        -format UDZO "${OUT}"
fi

echo "[make-dmg.sh] wrote ${OUT}"
echo "[make-dmg.sh] shasum: $(shasum -a 256 "${OUT}" | awk '{print $1}')"
