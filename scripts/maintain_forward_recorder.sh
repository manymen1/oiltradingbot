#!/usr/bin/env bash
set -euo pipefail

repo_dir="${POLYBOT_REPO_DIR:-/home/tstuv/poly/anthropic-exec-bot}"
config_path="${1:-configs/geopolitics/discovery.yaml}"
fleet_unit="${2:-polybot-fleet.service}"
geo_bin="$repo_dir/bin/geo"
maintenance_lock="$repo_dir/data/discovery/forward_recorder.maintenance.lock"

cd "$repo_dir"
mkdir -p "$(dirname "$maintenance_lock")"
exec 9>"$maintenance_lock"
if ! flock -n 9; then
  echo "forward recorder maintenance already running"
  exit 0
fi

status_file="$(mktemp)"
rotation_file="$(mktemp)"
cleanup() {
  find "$status_file" "$rotation_file" -maxdepth 0 -type f -delete 2>/dev/null || true
}
trap cleanup EXIT

if ! "$geo_bin" forward-recorder-rotation-due \
  --config "$config_path" >"$status_file"; then
  cat "$status_file"
  exit 0
fi
cat "$status_file"

was_active=0
restart_required=0
if systemctl --user is-active --quiet "$fleet_unit"; then
  was_active=1
  restart_required=1
  systemctl --user stop "$fleet_unit"
fi
restart_fleet() {
  if [[ "$was_active" -eq 1 && "$restart_required" -eq 1 ]]; then
    systemctl --user start "$fleet_unit"
  fi
}
trap 'restart_fleet; cleanup' EXIT

"$geo_bin" rotate-forward-recorder \
  --config "$config_path" >"$rotation_file"

if [[ "$was_active" -eq 1 ]]; then
  systemctl --user start "$fleet_unit"
  restart_required=0
fi

readarray -t maintenance_values < <(
  "$repo_dir/.venv/bin/python" - "$rotation_file" "$status_file" <<'PY'
import json
import sys

rotation = json.load(open(sys.argv[1], encoding="utf-8"))
status = json.load(open(sys.argv[2], encoding="utf-8"))
print(rotation["manifest_path"])
print(status["archive_compression"])
print(status["archive_compression_level"])
PY
)
manifest_path="${maintenance_values[0]}"
compression="${maintenance_values[1]}"
compression_level="${maintenance_values[2]}"

if [[ "$compression" == "gzip" ]]; then
  "$geo_bin" compress-forward-recorder-archive \
    --manifest "$manifest_path" \
    --level "$compression_level"
fi
