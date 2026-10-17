#!/bin/sh
# Build .icns from Resources/AppIcon.svg using rsvg-convert (Homebrew:
# `brew install librsvg`) + iconutil. Falls back to Inkscape if rsvg
# not present. No external logos.
set -eu

SRC="$(dirname "$0")/../Resources/AppIcon.svg"
DST_DIR="$(mktemp -d)"
ICNS="$(dirname "$0")/../Resources/AppIcon.icns"

if [ ! -f "${SRC}" ]; then
    echo "[make-icon.sh] missing ${SRC}" >&2
    exit 1
fi

render() {
    local size="$1"
    if command -v rsvg-convert >/dev/null 2>&1; then
        rsvg-convert -w "${size}" -h "${size}" "${SRC}" \
            -o "${DST_DIR}/icon_${size}.png"
    elif command -v inkscape >/dev/null 2>&1; then
        inkscape -w "${size}" -h "${size}" "${SRC}" \
            -o "${DST_DIR}/icon_${size}.png"
    elif command -v qlmanage >/dev/null 2>&1; then
        qlmanage -t -s "${size}" -o "${DST_DIR}" "${SRC}" >/dev/null
        mv "${DST_DIR}/AppIcon.svg.png" "${DST_DIR}/icon_${size}.png" 2>/dev/null \
            || true
    else
        echo "[make-icon.sh] need rsvg-convert / inkscape / qlmanage" >&2
        exit 1
    fi
}

# Apple docs:
#   16, 32, 64, 128, 256, 512, 1024 px squares;
#   32, 64, 256, 512 px @2x (so 16@2x = 32, etc.).
mkdir -p "${DST_DIR}"
for s in 16 32 64 128 256 512 1024; do
    render "${s}"
done

ICONSET="${DST_DIR}/icon.iconset"
mkdir -p "${ICONSET}"
cp "${DST_DIR}/icon_16.png"   "${ICONSET}/icon_16x16.png"
cp "${DST_DIR}/icon_32.png"   "${ICONSET}/icon_16x16@2x.png"
cp "${DST_DIR}/icon_32.png"   "${ICONSET}/icon_32x32.png"
cp "${DST_DIR}/icon_64.png"   "${ICONSET}/icon_32x32@2x.png"
cp "${DST_DIR}/icon_128.png"  "${ICONSET}/icon_128x128.png"
cp "${DST_DIR}/icon_256.png"  "${ICONSET}/icon_128x128@2x.png"
cp "${DST_DIR}/icon_256.png"  "${ICONSET}/icon_256x256.png"
cp "${DST_DIR}/icon_512.png"  "${ICONSET}/icon_256x256@2x.png"
cp "${DST_DIR}/icon_512.png"  "${ICONSET}/icon_512x512.png"
cp "${DST_DIR}/icon_1024.png" "${ICONSET}/icon_512x512@2x.png"

# Also a template (monochrome) for menu bar — use the same icon at 22pt.
mkdir -p "${ICONSET}/Template" 2>/dev/null || true

iconutil -c icns "${ICONSET}" -o "${ICNS}"
echo "[make-icon.sh] wrote ${ICNS}"

rm -rf "${DST_DIR}"
