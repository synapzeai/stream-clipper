#!/usr/bin/env bash
# Runs the clipping machine every 2 hours (cron), then lets Claude Code learn from the stats.
#   bash schedule.sh install   # add the cron job
#   bash schedule.sh remove    # take it off
#   bash schedule.sh once      # run one cycle now
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/.venv/bin/python"
CLAUDE="$(command -v claude || true)"
run_once() {
  mkdir -p "$DIR/state"
  cd "$DIR"
  { echo "=== $(date) ==="
    "$PY" scripts/machine.py cycle --live
    if [ -n "$CLAUDE" ]; then
      "$CLAUDE" -p "$(cat CYCLE.md)" --allowedTools "Read,Edit,Write,Bash(cat:*),Bash(head:*),Bash(tail:*)"
    fi
  } >> "$DIR/state/cycle.log" 2>&1
}
case "${1:-}" in
  install) ( crontab -l 2>/dev/null | grep -v "stream-clipper/schedule.sh"; echo "0 */2 * * * PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin bash \"$DIR/schedule.sh\" once" ) | crontab - ; echo "installed: every 2 hours";;
  remove)  crontab -l 2>/dev/null | grep -v "stream-clipper/schedule.sh" | crontab - ; echo "removed";;
  once)    run_once;;
  *) echo "usage: bash schedule.sh install|remove|once"; exit 1;;
esac
