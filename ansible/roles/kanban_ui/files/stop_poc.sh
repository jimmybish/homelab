#!/bin/bash
# Stop the manually-launched kanban-ui proof-of-concept so the container
# can bind its port. Invoked via ansible.builtin.script; ALL variables
# arrive through the environment (never interpolated into shell syntax),
# and expansion is quoted. A process is killed only when BOTH hold:
#   1. /proc/<pid>/cmdline contains: server.py --lan --port <KANBAN_UI_PORT>
#      (exact integer token match, not a regex supplied by the caller)
#   2. /proc/<pid>/cwd equals KANBAN_UI_POC_DIR
# Anything else is left untouched.
set -euo pipefail

: "${KANBAN_UI_PORT:?KANBAN_UI_PORT must be set}"
: "${KANBAN_UI_POC_DIR:?KANBAN_UI_POC_DIR must be set}"

case "$KANBAN_UI_PORT" in
  (*[!0-9]*|'') echo "invalid port" >&2; exit 2 ;;
esac

killed=0
for pid_dir in /proc/[0-9]*; do
  pid="${pid_dir##*/}"
  [ -r "$pid_dir/cmdline" ] || continue
  # NUL-delimited argv -> one arg per line
  cmd="$(tr '\0' '\n' < "$pid_dir/cmdline" 2>/dev/null || true)"
  [ -n "$cmd" ] || continue
  # argv[0] must be a python interpreter, and the argv must carry
  # server.py plus the exact --lan --port <port> flags.
  first="$(printf '%s\n' "$cmd" | head -n 1)"
  case "$first" in
    (*python*) : ;;
    (*) continue ;;
  esac
  port_seen=0
  prev=""
  while IFS= read -r arg; do
    if [ "$prev" = "--port" ] && [ "$arg" = "$KANBAN_UI_PORT" ]; then
      port_seen=1
    fi
    prev="$arg"
  done <<< "$cmd"
  printf '%s\n' "$cmd" | grep -qx 'server.py' || continue
  printf '%s\n' "$cmd" | grep -qx -- '--lan' || continue
  [ "$port_seen" -eq 1 ] || continue
  cwd="$(readlink "$pid_dir/cwd" 2>/dev/null || true)"
  [ "$cwd" = "$KANBAN_UI_POC_DIR" ] || continue
  echo "killing POC pid=$pid"
  kill "$pid"
  killed=$((killed + 1))
done

if [ "$killed" -gt 0 ]; then
  echo "killed $killed"
else
  echo "no POC process found"
fi
exit 0
