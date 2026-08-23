#!/usr/bin/env bash
set -u

login_mode="${1:-email}"
config_file="${2:-config.yaml}"
target="${NAUKRI_TARGET_APPLICATIONS:-30}"
project_root="$(cd "$(dirname "$0")/.." && pwd)"
python_bin="${PYTHON_BIN:-$project_root/.venv/bin/python}"
if [ ! -x "$python_bin" ]; then
  python_bin="$(command -v python3 || true)"
fi
if [ -z "$python_bin" ] || [ ! -x "$python_bin" ]; then
  echo "Python interpreter not found" >&2
  exit 1
fi
history_file=$(
  cd "$project_root" && "$python_bin" - "$config_file" <<'PY'
import os
import sys
from pathlib import Path

from naukri_bot import load_config, persistent_path

config = load_config(Path(sys.argv[1]))
print(persistent_path(config.get("history_file", "data/history/applied_jobs.json")))
PY
)
non_interactive_args=()
case "${NAUKRI_NON_INTERACTIVE:-}" in
  1|true|TRUE|yes|YES) non_interactive_args=(--non-interactive) ;;
esac

for freshness in 1 3 7 15; do
  applied=$("$python_bin" - "$project_root/$history_file" <<'PY'
import json, sys
from datetime import datetime
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    data = {}
today = datetime.now().astimezone().date().isoformat()
print(sum(
    1
    for item in data.values()
    if item.get("status") == "applied"
    and str(item.get("processed_at", ""))[:10] == today
))
PY
)
  remaining=$((target - applied))
  if [ "$remaining" -le 0 ]; then
    echo "Today's target reached: $applied successful applications"
    exit 0
  fi
  echo "Running freshness window: Last $freshness days ($remaining remaining)"
  (cd "$project_root" && env NAUKRI_FRESHNESS_OVERRIDE="$freshness" NAUKRI_MAX_JOBS_OVERRIDE="$remaining" "$python_bin" naukri_bot.py --config "$config_file" --login-mode "$login_mode" --submit "${non_interactive_args[@]}") || exit $?
done

echo "Freshness windows exhausted; today's target may not have been reached."
