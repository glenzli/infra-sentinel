#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP_DIR="$ROOT_DIR/Traffic Sentinel.app"
EXECUTABLE="$APP_DIR/Contents/MacOS/TrafficSentinel"
INFO_PLIST="$APP_DIR/Contents/Info.plist"
DIST_DIR="$ROOT_DIR/dist"

"$ROOT_DIR/bin/build-menubar-app.sh"
/usr/bin/codesign --verify --deep --strict "$APP_DIR"

VERSION=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$INFO_PLIST")
MINIMUM_MACOS=$(/usr/libexec/PlistBuddy -c "Print :LSMinimumSystemVersion" "$INFO_PLIST")
ARCHS=$(/usr/bin/lipo -archs "$EXECUTABLE")

case "$VERSION" in
  ""|*[!0-9.]*) echo "Invalid app version: $VERSION" >&2; exit 1 ;;
esac
case "$MINIMUM_MACOS" in
  ""|*[!0-9.]*) echo "Invalid minimum macOS version: $MINIMUM_MACOS" >&2; exit 1 ;;
esac
case "$ARCHS" in
  "arm64 x86_64"|"x86_64 arm64") ARCH_LABEL=universal2 ;;
  *" "*) ARCH_LABEL=$(printf '%s' "$ARCHS" | /usr/bin/tr ' ' '-') ;;
  *) ARCH_LABEL=$ARCHS ;;
esac

MINIMUM_LABEL=${MINIMUM_MACOS%%.*}
ASSET_NAME="Traffic-Sentinel-${VERSION}-macos${MINIMUM_LABEL}-${ARCH_LABEL}-unsigned.zip"
CHECKSUM_NAME="${ASSET_NAME}.sha256"
ARCHIVE_PATH="$DIST_DIR/$ASSET_NAME"
CHECKSUM_PATH="$DIST_DIR/$CHECKSUM_NAME"

case "$DIST_DIR" in
  "$ROOT_DIR/dist") ;;
  *) echo "Refusing to write an unexpected dist path: $DIST_DIR" >&2; exit 1 ;;
esac

mkdir -p "$DIST_DIR"
rm -f "$ARCHIVE_PATH" "$CHECKSUM_PATH"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP_DIR" "$ARCHIVE_PATH"
(
  cd "$DIST_DIR"
  /usr/bin/shasum -a 256 "$ASSET_NAME" > "$CHECKSUM_NAME"
)

printf 'Packaged %s\nChecksum %s\n' "$ARCHIVE_PATH" "$CHECKSUM_PATH"
