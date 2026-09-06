#!/bin/sh
set -eu

ui_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
root_dir=$(CDPATH= cd -- "$ui_dir/.." && pwd)
target_triple=${1:-$(rustc --print host-tuple)}
agent_name="infra-agent"

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) agent_name="infra-agent.exe" ;;
esac

build_dir="$root_dir/.build/tauri-agent"
output_dir="$ui_dir/src-tauri/binaries"
python_bin=${PYTHON3:-python3}

"$python_bin" -c 'import PyInstaller, certifi' 2>/dev/null || {
  echo "PyInstaller and the Agent dependencies are required. Install them with: $python_bin -m pip install . pyinstaller" >&2
  exit 1
}

rm -rf "$build_dir"
mkdir -p "$build_dir" "$output_dir"

"$python_bin" -m PyInstaller \
  --noconfirm \
  --clean \
  --onefile \
  --name "$agent_name" \
  --distpath "$build_dir/dist" \
  --workpath "$build_dir/work" \
  --specpath "$build_dir/spec" \
  --paths "$root_dir/src" \
  --add-data "$root_dir/src/infra_sentinel/resources/ai/api-price.toml:infra_sentinel/resources/ai" \
  --hidden-import infra_sentinel.cli.snapshot \
  --collect-data certifi \
  --hidden-import infra_sentinel.resources.system.backends.macos \
  --hidden-import infra_sentinel.resources.system.backends.linux \
  --hidden-import infra_sentinel.resources.system.backends.windows \
  "$root_dir/bin/infra_agent.py"

cp "$build_dir/dist/$agent_name" "$output_dir/infra-agent-$target_triple"
chmod 755 "$output_dir/infra-agent-$target_triple"
