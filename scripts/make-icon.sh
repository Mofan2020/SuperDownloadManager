#!/bin/sh
# Build the macOS app icon set from Resources/AppIcon.svg.
#
# Output:
#   App/Assets.xcassets/AppIcon.appiconset/icon_<size>[x<scale>].png
#   Resources/AppIcon.icns  (legacy icns for tools that need it)
#
# Renders once at 1024x1024 via qlmanage (always available on
# macOS), then downscales with sips for each AppIcon slot. We
# don't use rsvg-convert or Inkscape because the CI image
# doesn't always ship them; sips is in /usr/bin and is the same
# downsampler Finder uses.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${ROOT}/Resources/AppIcon.svg"
ICONSET="${ROOT}/App/Assets.xcassets/AppIcon.appiconset"
ICNS="${ROOT}/Resources/AppIcon.icns"

if [ ! -f "${SRC}" ]; then
    echo "[make-icon.sh] missing ${SRC}" >&2
    exit 1
fi

WORK="$(mktemp -d -t sdm-icon)"
trap 'rm -rf "${WORK}"' EXIT

# 1) Render the SVG to a 1024x1024 PNG. qlmanage always succeeds
#    on macOS without Homebrew. The output is a tight-cropped
#    thumbnail; the squircle background is inside the SVG, so the
#    thumbnail is already a proper app-icon candidate.
echo "[make-icon.sh] rendering source PNG"
qlmanage -t -s 1024 -o "${WORK}" "${SRC}" >/dev/null
if [ ! -f "${WORK}/AppIcon.svg.png" ]; then
    echo "[make-icon.sh] qlmanage failed to render" >&2
    exit 1
fi
SRC_PNG="${WORK}/AppIcon.svg.png"

# 2) Generate every AppIcon slot.  Sizes are 16/32/64/128/256/512
#    at 1x, plus @2x duplicates (which equal the next 1x size).
echo "[make-icon.sh] generating appiconset PNGs"
mkdir -p "${ICONSET}"
for tuple in "16:16x16" "32:16x16@2x" "32:32x32" "64:32x32@2x" \
             "128:128x128" "256:128x128@2x" "256:256x256" \
             "512:256x256@2x" "512:512x512" "1024:512x512@2x"; do
    px="${tuple%%:*}"
    slot="${tuple##*:}"
    out="${ICONSET}/icon_${slot}.png"
    sips -z "${px}" "${px}" "${SRC_PNG}" --out "${out}" >/dev/null
    echo "  ${slot} -> ${px}x${px}  ($(stat -f%z "${out}") bytes)"
done

# 3) Build a .icns for tools that still need it (Sparkle, the
#    install scripts, finder preview in some contexts).
echo "[make-icon.sh] generating AppIcon.icns"
ICONSET_DIR="${WORK}/icon.iconset"
mkdir -p "${ICONSET_DIR}"
sips -z 16   16   "${SRC_PNG}" --out "${ICONSET_DIR}/icon_16x16.png"      >/dev/null
sips -z 32   32   "${SRC_PNG}" --out "${ICONSET_DIR}/icon_16x16@2x.png"   >/dev/null
sips -z 32   32   "${SRC_PNG}" --out "${ICONSET_DIR}/icon_32x32.png"      >/dev/null
sips -z 64   64   "${SRC_PNG}" --out "${ICONSET_DIR}/icon_32x32@2x.png"   >/dev/null
sips -z 128  128  "${SRC_PNG}" --out "${ICONSET_DIR}/icon_128x128.png"    >/dev/null
sips -z 256  256  "${SRC_PNG}" --out "${ICONSET_DIR}/icon_128x128@2x.png" >/dev/null
sips -z 256  256  "${SRC_PNG}" --out "${ICONSET_DIR}/icon_256x256.png"    >/dev/null
sips -z 512  512  "${SRC_PNG}" --out "${ICONSET_DIR}/icon_256x256@2x.png" >/dev/null
sips -z 512  512  "${SRC_PNG}" --out "${ICONSET_DIR}/icon_512x512.png"    >/dev/null
sips -z 1024 1024 "${SRC_PNG}" --out "${ICONSET_DIR}/icon_512x512@2x.png" >/dev/null
iconutil -c icns "${ICONSET_DIR}" -o "${ICNS}"

echo "[make-icon.sh] done:"
echo "  asset catalog: ${ICONSET}"
echo "  icns:          ${ICNS}"
