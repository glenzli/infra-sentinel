#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP_DIR="$ROOT_DIR/Infra Sentinel.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
APP_RESOURCES_DIR="$CONTENTS_DIR/Resources"
SENTINEL_RESOURCES_DIR="$APP_RESOURCES_DIR/Sentinel"
MODULE_CACHE_DIR="$ROOT_DIR/.build/clang-module-cache"
MINIMUM_MACOS_VERSION=13.0

if ! CLANG=$(/usr/bin/xcrun --sdk macosx --find clang 2>/dev/null); then
  echo "Xcode Command Line Tools are required. Run: xcode-select --install" >&2
  exit 1
fi
if ! SDK_ROOT=$(/usr/bin/xcrun --sdk macosx --show-sdk-path 2>/dev/null); then
  echo "The macOS SDK is unavailable. Reinstall Xcode Command Line Tools." >&2
  exit 1
fi

if ! PYTHON3=$(command -v python3); then
  echo "Python 3.11 or newer is required." >&2
  exit 1
fi

if ! "$PYTHON3" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "Python 3.11 or newer is required; found $("$PYTHON3" --version 2>&1)." >&2
  exit 1
fi

PLIST_MINIMUM=$(/usr/libexec/PlistBuddy -c "Print :LSMinimumSystemVersion" "$ROOT_DIR/app/Info.plist")
if [ "$PLIST_MINIMUM" != "$MINIMUM_MACOS_VERSION" ]; then
  echo "Info.plist minimum macOS version must be $MINIMUM_MACOS_VERSION." >&2
  exit 1
fi

case "$APP_DIR" in
  "$ROOT_DIR/"*.app) ;;
  *) echo "Refusing to clean unexpected app path: $APP_DIR" >&2; exit 1 ;;
esac
rm -rf "$APP_DIR"
mkdir -p "$MACOS_DIR" "$SENTINEL_RESOURCES_DIR" "$MODULE_CACHE_DIR"
cp "$ROOT_DIR/app/Info.plist" "$CONTENTS_DIR/Info.plist"
cp "$ROOT_DIR/assets/TrafficSentinel.icns" "$APP_RESOURCES_DIR/TrafficSentinel.icns"
cp "$ROOT_DIR/bin/sentinel.py" "$SENTINEL_RESOURCES_DIR/sentinel.py"
cp "$ROOT_DIR/bin/configuration.py" "$SENTINEL_RESOURCES_DIR/configuration.py"
cp "$ROOT_DIR/bin/infra_model.py" "$SENTINEL_RESOURCES_DIR/infra_model.py"
cp "$ROOT_DIR/bin/infra_registry.py" "$SENTINEL_RESOURCES_DIR/infra_registry.py"
cp "$ROOT_DIR/bin/infra_projection.py" "$SENTINEL_RESOURCES_DIR/infra_projection.py"
cp "$ROOT_DIR/bin/network_metrics.py" "$SENTINEL_RESOURCES_DIR/network_metrics.py"
cp "$ROOT_DIR/bin/metric_store.py" "$SENTINEL_RESOURCES_DIR/metric_store.py"
cp "$ROOT_DIR/bin/mihomo_traffic.py" "$SENTINEL_RESOURCES_DIR/mihomo_traffic.py"
cp "$ROOT_DIR/bin/remote_ssh.py" "$SENTINEL_RESOURCES_DIR/remote_ssh.py"
cp "$ROOT_DIR/bin/remote.py" "$SENTINEL_RESOURCES_DIR/remote.py"
cp "$ROOT_DIR/bin/sample_timing.py" "$SENTINEL_RESOURCES_DIR/sample_timing.py"
cp "$ROOT_DIR/bin/session.py" "$SENTINEL_RESOURCES_DIR/session.py"
cp "$ROOT_DIR/bin/traffic_estimation.py" "$SENTINEL_RESOURCES_DIR/traffic_estimation.py"
cp "$ROOT_DIR/bin/vps.py" "$SENTINEL_RESOURCES_DIR/vps.py"
cp "$ROOT_DIR/bin/xray_stats.py" "$SENTINEL_RESOURCES_DIR/xray_stats.py"
cp "$ROOT_DIR/bin/snapshot.py" "$SENTINEL_RESOURCES_DIR/snapshot.py"
cp "$ROOT_DIR/config.example.toml" "$SENTINEL_RESOURCES_DIR/config.example.toml"
"$CLANG" -O2 -fblocks -fobjc-arc -fmodules \
  -isysroot "$SDK_ROOT" \
  -mmacosx-version-min="$MINIMUM_MACOS_VERSION" \
  -Werror=unguarded-availability-new \
  -fmodules-cache-path="$MODULE_CACHE_DIR" \
  -framework Cocoa -framework UserNotifications \
  "$ROOT_DIR/app/MenuBarApp.m" \
  "$ROOT_DIR/app/DashboardController.m" \
  "$ROOT_DIR/app/InfraOverviewPanel.m" \
  "$ROOT_DIR/app/SettingsController.m" \
  "$ROOT_DIR/app/SettingsStore.m" \
  "$ROOT_DIR/app/TrafficOverviewPanel.m" \
  "$ROOT_DIR/app/XrayUserPanel.m" \
  "$ROOT_DIR/app/TrafficFormatting.m" \
  "$ROOT_DIR/app/MonitorHealth.m" \
  "$ROOT_DIR/app/Localization.m" \
  -o "$MACOS_DIR/TrafficSentinel"
/usr/bin/codesign --force --sign - "$APP_DIR" >/dev/null
APP_VERSION=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$CONTENTS_DIR/Info.plist")
APP_ARCH=$(/usr/bin/file "$MACOS_DIR/TrafficSentinel" | /usr/bin/sed 's/.*Mach-O 64-bit executable //')
printf 'Built %s · version %s · macOS %s+ · %s\n' \
  "$APP_DIR" "$APP_VERSION" "$MINIMUM_MACOS_VERSION" "$APP_ARCH"
