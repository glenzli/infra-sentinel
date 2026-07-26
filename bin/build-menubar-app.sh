#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP_DIR="$ROOT_DIR/Codex Traffic Sentinel.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources/Sentinel"
MODULE_CACHE_DIR="$ROOT_DIR/.build/clang-module-cache"

case "$APP_DIR" in
  "$ROOT_DIR/"*.app) ;;
  *) echo "Refusing to clean unexpected app path: $APP_DIR" >&2; exit 1 ;;
esac
rm -rf "$APP_DIR"
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR" "$MODULE_CACHE_DIR"
cp "$ROOT_DIR/app/Info.plist" "$CONTENTS_DIR/Info.plist"
cp "$ROOT_DIR/bin/sentinel.py" "$RESOURCES_DIR/sentinel.py"
cp "$ROOT_DIR/bin/codex_activity.py" "$RESOURCES_DIR/codex_activity.py"
cp "$ROOT_DIR/bin/codex_hook.py" "$RESOURCES_DIR/codex_hook.py"
cp "$ROOT_DIR/bin/config_migration.py" "$RESOURCES_DIR/config_migration.py"
cp "$ROOT_DIR/bin/proxy_segments.py" "$RESOURCES_DIR/proxy_segments.py"
cp "$ROOT_DIR/bin/session.py" "$RESOURCES_DIR/session.py"
cp "$ROOT_DIR/bin/traffic_estimation.py" "$RESOURCES_DIR/traffic_estimation.py"
cp "$ROOT_DIR/bin/vps.py" "$RESOURCES_DIR/vps.py"
cp "$ROOT_DIR/bin/snapshot.py" "$RESOURCES_DIR/snapshot.py"
cp "$ROOT_DIR/config.example.toml" "$RESOURCES_DIR/config.example.toml"
/usr/bin/clang -O2 -fblocks -fobjc-arc -fmodules -fmodules-cache-path="$MODULE_CACHE_DIR" -framework Cocoa -framework UserNotifications \
  "$ROOT_DIR/app/MenuBarApp.m" \
  "$ROOT_DIR/app/DashboardController.m" \
  "$ROOT_DIR/app/CodexActivityPanel.m" \
  "$ROOT_DIR/app/TrafficOverviewPanel.m" \
  "$ROOT_DIR/app/Localization.m" \
  -o "$MACOS_DIR/CodexTrafficSentinel"
/usr/bin/codesign --force --sign - "$APP_DIR" >/dev/null
printf 'Built %s\n' "$APP_DIR"
