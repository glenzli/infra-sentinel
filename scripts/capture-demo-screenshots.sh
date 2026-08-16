#!/bin/sh
# Launch a non-collecting anonymous desktop instance for README screenshots.
set -eu

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
app_binary="$root_dir/ui/src-tauri/target/release/bundle/macos/Infra Sentinel.app/Contents/MacOS/infra-sentinel-desktop"
demo_root=$(mktemp -d "${TMPDIR:-/tmp}/infra-sentinel-demo.XXXXXX")
state_dir="$demo_root/state"
demo_locale=${INFRA_SENTINEL_DEMO_LOCALE:-zh}

cleanup() {
  rm -rf "$demo_root"
}
trap cleanup EXIT INT TERM

if [ ! -x "$app_binary" ]; then
  echo "Build the desktop app first: ./bin/build-desktop-app.sh" >&2
  exit 1
fi

case "$demo_locale" in
  en|zh) ;;
  *) echo "INFRA_SENTINEL_DEMO_LOCALE must be en or zh" >&2; exit 2 ;;
esac

python3 "$root_dir/scripts/write_demo_projection.py" --state-dir "$state_dir"
echo "Infra Sentinel demo is using only: $state_dir/projection.json" >&2
env \
  INFRA_SENTINEL_STATE_DIR="$state_dir" \
  INFRA_SENTINEL_STATIC_PROJECTION="$state_dir/projection.json" \
  INFRA_SENTINEL_STATIC_SHOW_DASHBOARD=1 \
  INFRA_SENTINEL_STATIC_DEMO_LOCALE="$demo_locale" \
  "$app_binary" &
demo_pid=$!
wait "$demo_pid"
