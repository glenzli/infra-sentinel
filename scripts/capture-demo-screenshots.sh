#!/bin/sh
# Launch a non-collecting anonymous desktop instance for README screenshots.
set -eu

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
built_app="$root_dir/ui/src-tauri/target/release/bundle/macos/Infra Sentinel.app"
demo_root=$(mktemp -d "${TMPDIR:-/tmp}/infra-sentinel-demo.XXXXXX")
demo_app="$demo_root/Infra Sentinel Demo.app"
state_dir="$demo_root/state"
demo_locale=${INFRA_SENTINEL_DEMO_LOCALE:-zh}

cleanup() {
  rm -rf "$demo_root"
}
trap cleanup EXIT INT TERM

if [ ! -x "$built_app/Contents/MacOS/infra-sentinel-desktop" ]; then
  echo "Build the desktop app first: ./bin/build-desktop-app.sh" >&2
  exit 1
fi

case "$demo_locale" in
  en|zh) ;;
  *) echo "INFRA_SENTINEL_DEMO_LOCALE must be en or zh" >&2; exit 2 ;;
esac

PYTHONPATH="$root_dir/src${PYTHONPATH:+:$PYTHONPATH}" \
  python3 "$root_dir/scripts/write_demo_projection.py" --state-dir "$state_dir"
/usr/bin/ditto "$built_app" "$demo_app"
/usr/bin/plutil -replace CFBundleIdentifier -string com.glenzli.infra-sentinel.demo "$demo_app/Contents/Info.plist"
/usr/bin/plutil -replace CFBundleDisplayName -string "Infra Sentinel Demo" "$demo_app/Contents/Info.plist"
/usr/bin/plutil -replace CFBundleName -string "Infra Sentinel Demo" "$demo_app/Contents/Info.plist"
/usr/bin/plutil -insert LSEnvironment -json "{\"INFRA_SENTINEL_STATE_DIR\":\"$state_dir\",\"INFRA_SENTINEL_STATIC_PROJECTION\":\"$state_dir/projection.json\",\"INFRA_SENTINEL_STATIC_SHOW_DASHBOARD\":\"1\",\"INFRA_SENTINEL_STATIC_DEMO_LOCALE\":\"$demo_locale\"}" "$demo_app/Contents/Info.plist"
/usr/bin/codesign --force --deep --sign - "$demo_app" >/dev/null
echo "Infra Sentinel demo is using only: $state_dir/projection.json" >&2
/usr/bin/open -n -W "$demo_app"
