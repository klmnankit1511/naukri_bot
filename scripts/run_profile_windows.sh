#!/usr/bin/env bash
set -u

config_file="config.yaml"
login_mode="${1:-email}"
target="${NAUKRI_TARGET_APPLICATIONS:-40}"
history_file="data/history/applied_jobs.json"

for freshness in 1 3 7 15; do
  applied=$(python3 - "$history_file" <<'PY'
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
  NAUKRI_FRESHNESS_OVERRIDE="$freshness" \
  NAUKRI_MAX_JOBS_OVERRIDE="$remaining" \
  python3 naukri_bot.py --config "$config_file" --login-mode "$login_mode" --submit || exit $?
done

echo "Freshness windows exhausted; today's target may not have been reached."
