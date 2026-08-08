#!/bin/sh
set -eu

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ui_dir="$root_dir/ui"
app_dir="$ui_dir/src-tauri/target/release/bundle/macos/Infra Sentinel.app"
dependency_stamp="$ui_dir/node_modules/.infra-sentinel-package-lock.json"

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required. Install a current Node.js LTS release first." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.11 or newer is required to package the local Agent." >&2
  exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "Python 3.11 or newer is required." >&2
  exit 1
fi

if ! python3 -c 'import PyInstaller' 2>/dev/null; then
  echo "PyInstaller is required. Install it with: python3 -m pip install pyinstaller" >&2
  exit 1
fi

(
  cd "$ui_dir"
  if [ ! -d node_modules ] || [ ! -f "$dependency_stamp" ] || ! cmp -s package-lock.json "$dependency_stamp"; then
    npm ci
    cp package-lock.json "$dependency_stamp"
  fi
  npm run tauri -- build
)

if [ ! -d "$app_dir" ]; then
  echo "Tauri build did not produce the expected macOS app: $app_dir" >&2
  exit 1
fi

/usr/bin/codesign --verify --deep --strict "$app_dir"
printf 'Built %s\n' "$app_dir"
