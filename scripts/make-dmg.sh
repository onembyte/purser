#!/bin/bash
# Build a distributable disk image from the py2app bundle.
#
#   .venv/bin/python setup.py py2app     # produces dist/Purser.app
#   scripts/make-dmg.sh                  # -> dist/Purser-macOS.dmg
#
# The .app is ad-hoc signed (required to launch on Apple Silicon) but NOT notarized,
# so first launch needs a right-click -> Open. The dmg lays out the app next to an
# /Applications symlink for drag-to-install. Uses only built-in hdiutil — no brew deps.
set -euo pipefail
cd "$(dirname "$0")/.."

APP="dist/Purser.app"
DMG="dist/Purser-macOS.dmg"
VOL="Purser"

[ -d "$APP" ] || { echo "error: $APP not found — run: .venv/bin/python setup.py py2app" >&2; exit 1; }

echo "==> ad-hoc signing $APP"
codesign --force --deep --sign - "$APP"

echo "==> staging dmg contents"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

echo "==> building $DMG"
rm -f "$DMG"
hdiutil create \
  -volname "$VOL" \
  -srcfolder "$STAGE" \
  -fs HFS+ \
  -format UDZO \
  -imagekey zlib-level=9 \
  -ov "$DMG" >/dev/null

SIZE="$(du -h "$DMG" | cut -f1 | tr -d ' ')"
echo "==> done: $DMG ($SIZE)"
