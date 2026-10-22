#!/bin/bash
# Regression tests for stop_poc.sh (PR#13 round-7 finding 4343269737).
#
# Under `set -euo pipefail`, pipelines that exit before their writer drains
# (head -n 1, grep -q) leave printf holding SIGPIPE: once /proc cmdline is
# larger than the 64KiB pipe buffer the script either aborted the deploy
# with rc=141 (the | head -n 1 case, fatal via the role's failed_when) or
# silently skipped the real POC process (the | grep -qx || continue case).
# Both were replaced with pipe-free parameter expansion / case matching.
#
# Run standalone (stdlib + a python3 on PATH, nothing else):
#     ansible/roles/kanban_ui/tests/test_stop_poc_sigpipe.sh
# Exit 0 = all pass.
set -u

# SCRIPT may be overridden (used to run this suite against a pre-fix copy
# and prove the regressions are detected); defaults to the shipped file.
SCRIPT="${SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../files" && pwd)/stop_poc.sh}"
[ -f "$SCRIPT" ] || { echo "FATAL: stop_poc.sh not found at $SCRIPT" >&2; exit 1; }

PORT=19470   # unused test port; the fake process carries it in argv
WORK=$(mktemp -d /tmp/stop-poc-test.XXXXXX)
POCDIR="$WORK/pocdir"
mkdir -p "$POCDIR"
OVERSIZED=$(python3 -c 'print("A"*120000)')  # >64KiB pipe buffer, <MAX_ARG_STRLEN
FAILURES=0
PIDS=()

# shellcheck disable=SC2329  # invoked via `trap cleanup EXIT`
cleanup() {
  local p
  for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# wait_ready <pid> <dir>: block until argv AND cwd are observable, so the
# script sees a fully realised process (exec replaces argv/cwd after fork).
# NB: -s never works on /proc cmdline (procfs reports size 0); read it.
wait_ready() {
  local pid=$1 dir=$2 tries=0
  while [ "$tries" -lt 50 ]; do
    if [ -n "$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null || true)" ] \
      && [ "$(readlink "/proc/$pid/cwd" 2>/dev/null || true)" = "$dir" ]; then
      return 0
    fi
    tries=$((tries + 1))
    sleep 0.1
  done
  echo "FATAL: fake pid=$pid never became ready in $dir" >&2
  return 1
}

# spawn_fake <dir> <extra-arg>: background a process whose cwd is <dir> and
# whose argv mimics the POC (server.py --lan --port PORT ...). stdout/stderr
# MUST be redirected: a backgrounded child inheriting an inherited pipe would
# wedge any caller that captures output. Sets FAKE_PID.
spawn_fake() {
  local dir=$1 big=$2
  ( cd "$dir" && exec python3 -c 'import time; time.sleep(300)' \
      server.py --lan --port "$PORT" "$big" >/dev/null 2>&1 ) &
  FAKE_PID=$!
  PIDS+=("$FAKE_PID")
  wait_ready "$FAKE_PID" "$dir"
}

# spawn_impostor <dir> <argv2>: same shape but caller picks argv[1] token.
spawn_impostor() {
  local dir=$1 tok=$2
  ( cd "$dir" && exec python3 -c 'import time; time.sleep(300)' \
      "$tok" --lan --port "$PORT" "$OVERSIZED" >/dev/null 2>&1 ) &
  FAKE_PID=$!
  PIDS+=("$FAKE_PID")
  wait_ready "$FAKE_PID" "$dir"
}

alive() { kill -0 "$1" 2>/dev/null; }

run_script() {
  KANBAN_UI_PORT="$PORT" KANBAN_UI_POC_DIR="$POCDIR" bash "$SCRIPT"
}

# check <name> <rc>: rc 0 passes, anything else fails the suite.
check() {
  local name=$1 rc=$2
  if [ "$rc" -eq 0 ]; then
    echo "PASS: $name"
  else
    echo "FAIL: $name (rc=$rc)"
    FAILURES=$((FAILURES + 1))
  fi
}

contains() { case "$1" in (*"$2"*) return 0 ;; (*) return 1 ;; esac; }

# 1. Oversized command line: exit 0 (no SIGPIPE abort) and the POC is killed.
spawn_fake "$POCDIR" "$OVERSIZED"; pid=$FAKE_PID
alive "$pid"; check "oversized fake is running pre-run" $?
out=$(run_script 2>&1); rc=$?
check "oversized cmdline: script exits 0 (was 141 pre-fix)" $rc
contains "$out" "killing POC pid=$pid"; check "oversized cmdline: reports killing the pid" $?
contains "$out" "killed 1"; check "oversized cmdline: reports kill count" $?
sleep 0.3
alive "$pid"; rc=$?
check "oversized cmdline: fake process actually killed" $((1 - rc))

# 2. Idempotency on already-stopped: exit 0, nothing found.
out=$(run_script 2>&1); rc=$?
check "already-stopped: script exits 0" $rc
if [ "$out" = "no POC process found" ]; then rc=0; else rc=1; fi
check "already-stopped: reports none found" $rc

# 3. Normal short command line still matched and killed.
spawn_fake "$POCDIR" "short"; pid=$FAKE_PID
alive "$pid"; check "short fake is running pre-run" $?
out=$(run_script 2>&1); rc=$?
check "short cmdline: script exits 0" $rc
sleep 0.3
alive "$pid"; rc=$?
check "short cmdline: fake process killed" $((1 - rc))
contains "$out" "killing POC pid=$pid"; check "short cmdline: reports the kill" $?

# 4. Oversized command line on an UNRELATED process (wrong cwd) must not be
#    killed and must not abort the script.
mkdir -p "$WORK/elsewhere"
spawn_fake "$WORK/elsewhere" "$OVERSIZED"; pid=$FAKE_PID
alive "$pid"; check "wrong-cwd fake is running pre-run" $?
out=$(run_script 2>&1); rc=$?
check "wrong-cwd oversized: script exits 0" $rc
if [ "$out" = "no POC process found" ]; then rc=0; else rc=1; fi
check "wrong-cwd oversized: reports none found" $rc
alive "$pid"; check "wrong-cwd oversized: still alive" $?
kill "$pid" 2>/dev/null || true

# 5. Oversized command line missing the server.py token (non-POC python)
#    must not match even with the right cwd and port.
spawn_impostor "$POCDIR" "notserver.py"; pid=$FAKE_PID
out=$(run_script 2>&1); rc=$?
check "non-POC oversized: script exits 0" $rc
if [ "$out" = "no POC process found" ]; then rc=0; else rc=1; fi
check "non-POC oversized: reports none found" $rc
alive "$pid"; check "non-POC oversized: still alive" $?
kill "$pid" 2>/dev/null || true

if [ "$FAILURES" -gt 0 ]; then
  echo "RESULT: $FAILURES failure(s)" >&2
  exit 1
fi
echo "RESULT: all stop_poc.sh regression tests passed"
exit 0
