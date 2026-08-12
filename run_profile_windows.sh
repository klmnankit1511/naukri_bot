#!/usr/bin/env bash
set -u

config_file="${1:-config.person3.yaml}"
login_mode="${2:-email}"
target="${NAUKRI_TARGET_APPLICATIONS:-30}"

case "$config_file" in
  *person2*) history_file="applied_jobs.person2.json" ;;
  *person3*) history_file="applied_jobs.person3.json" ;;
  *) history_file="applied_jobs.json" ;;
esac

for freshness in 1 3 7 15; do
  applied=$(python3 - "$history_file" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    data = {}
print(sum(1 for item in data.values() if item.get("status") == "applied"))
PY
)
  remaining=$((target - applied))
  if [ "$remaining" -le 0 ]; then
    echo "Target reached: $applied successful applications"
    exit 0
  fi
  echo "Running freshness window: Last $freshness days ($remaining remaining)"
  NAUKRI_FRESHNESS_OVERRIDE="$freshness" \
  NAUKRI_MAX_JOBS_OVERRIDE="$remaining" \
  python3 naukri_bot.py --config "$config_file" --login-mode "$login_mode" --submit || exit $?
done

echo "Freshness windows exhausted; target may not have been reached."
