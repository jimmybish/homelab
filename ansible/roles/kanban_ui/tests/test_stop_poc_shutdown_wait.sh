#!/bin/bash
# Regression tests for the bounded POC shutdown wait in stop_poc.sh
# (PR#13 round-9 finding 4369296896, hardened by the round-10 blocking
# finding on review 5641033023):
#
#   "kill only sends SIGTERM, so this helper reports success before a
#    gracefully stopping POC necessarily releases its listening socket. If
#    the image already exists, Compose startup can race that shutdown and
#    fail with an address-in-use error. Wait for shutdown and port release
#    with a bounded timeout, failing clearly if the POC does not stop."
#
#   round 10 (HIGH): "Use a race-free process handle for signaling (Linux
#   pidfd/pidfd_send_signal, held for the tracked process) rather than
#   converting the validated identity back into a bare PID before
#   escalation."
#
# Pre-fix behaviour being proven absent: the helper returned immediately
# after kill. These tests drive the REAL script (not a copy of the
# functions) against real processes and real listening sockets:
#
#   1. A POC that lingers ~0.8s after SIGTERM while still holding the port
#      must NOT let the script return until the process is reaped AND the
#      port has left LISTEN (pre-fix: returned instantly, port still bound).
#   2. A SIGTERM-ignoring POC is escalated to SIGKILL inside the grace
#      budget, the escalation is reported, and the script still exits 0.
#   3. A killed POC with the port STILL bound (held by a non-matching
#      listener) makes the port gate fail with rc != 0 and a clear
#      LISTEN-state message — never a silent success.
#   4. Redeploy safety: port bound by something unrelated but NO matching
#      POC process -> exit 0 "no POC process found" (the gate only runs on
#      the killed>0 path; the managed container legitimately owns the port
#      on every routine converge).
#   5. Non-numeric wait knobs are rejected with rc=2 (environment values
#      arrive from Ansible; never trust them into arithmetic).
#   6. Sensitivity: strip the two gate calls (pre-fix shape) and re-run
#      case 1 — assert the bug reproduces, proving the suite detects it.
#   7. PID reuse at the no-python3 FALLBACK path (round-9 blocking
#      finding): drive wait_for_exit directly (STOP_POC_LIB_ONLY=1 source
#      seam) with the external PATH shimmed so python3 is genuinely
#      unavailable. A tracked (pid, token) pair whose pid is EXITED and
#      then RECYCLED by a different process must return 0 promptly; the
#      helper must never signal the recycled pid and must never escalate
#      (the fallback has no race-free handle, so it refuses instead).
#      Sensitivity variant: with the survivor check degraded to bare
#      `[ -d /proc/$p ]` (the round-8 shape) the same scenario DOES
#      signal the unrelated process, proving the suite detects it.
#   8. The round-10 signal path itself (check-to-signal window): a
#      SIGTERM-stubborn ORIGINAL is validated pre-signal, then EXITS
#      mid-grace and its pid is REOCCUPIED by an unrelated TENANT before
#      the escalation fires. The fixed helper escalates through a held
#      pidfd handle, so the replacement is never signalled (and the
#      escalation is still reported). Sensitivity variant: degrade ONLY
#      the escalation survivor predicate to always-true (the round-8
#      shape: validate once, then signal the bare PID number with no
#      revalidation) and the same orchestrated scenario KILLS the
#      tenant, proving the suite exercises and detects the exact
#      check-to-signal race the reviewer flagged.
#   9. Round-11 fallback identity race (HIGH): the no-python3 fallback
#      must refuse BEFORE signalling. Deterministic check-to-guard case
#      inside a pid namespace: the harness pauses the fallback at its
#      test seam after identity_matches SUCCEEDED on a tracked process,
#      recycles that PID with an unrelated TENANT, and lets the guard
#      proceed. The fixed shape signals nothing (no "killing POC", no
#      SIGTERM, loud ERROR + rc=1) and the replacement is never
#      signalled. Sensitivity variant: inject the pre-fix bare
#      `kill "$p"` at the seam point (the exact flagged
#      check-then-signal shape) and the same orchestrated scenario
#      SIGTERMs the replacement tenant, proving the suite detects it.
#  10. Round-11 hunt identity binding (HIGH): run the REAL hunt (full
#      script, no pair injection) inside a pid namespace; pause it at
#      the hunt seam after cmdline/port/cwd selection succeeded, then
#      kill the selected POC and install an unrelated TENANT at the
#      recycled PID before the identity token is bound. Fixed shape:
#      the bracketed token reads reject the recycled PID (no pair, no
#      signal, replacement untouched) while a second, stable POC-shaped
#      keeper process IS still selected and killed via the pidfd path.
#      Sensitivity variant: remove ONLY the bracket comparisons (the
#      pre-fix shape that captured proc_token after selection with no
#      stability proof) and the same scenario binds the replacement's
#      token and SIGTERMs the tenant, proving the suite detects the
#      hunt-to-token race the reviewer flagged.
#
# Run standalone (stdlib + python3 on PATH, nothing else):
#     ansible/roles/kanban_ui/tests/test_stop_poc_shutdown_wait.sh
# Exit 0 = all pass.
set -u

SCRIPT="${SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../files" && pwd)/stop_poc.sh}"
[ -f "$SCRIPT" ] || { echo "FATAL: stop_poc.sh not found at $SCRIPT" >&2; exit 1; }

PORT=19471
WORK=$(mktemp -d /tmp/stop-poc-wait.XXXXXX)
POCDIR="$WORK/pocdir"
mkdir -p "$POCDIR"
FAILURES=0
PIDS=()

# shellcheck disable=SC2329  # invoked via `trap cleanup EXIT`
cleanup() {
  local p
  for p in "${PIDS[@]:-}"; do kill -KILL "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# Python program for the fake POCs. argv mimics the real POC
# (server.py --lan --port PORT <mode> <pad>) so the script's hunt matches;
# with -c the extra tokens land in sys.argv (same trick as the sigpipe
# suite's spawn_fake, including the >64KiB pad to keep pipefail hazards
# exercised on every run).
POC_PROG='
import signal, socket, sys, time
mode = sys.argv[1]
port = int(sys.argv[sys.argv.index("--port") + 1])
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", port)); s.listen(1)
def bye(*a):
    if mode == "slow":
        time.sleep(0.8)   # graceful-but-slow: socket lives past SIGTERM entry
        raise SystemExit
    # mode == "ignore": swallow SIGTERM entirely
signal.signal(signal.SIGTERM, bye)
time.sleep(300)
'
PAD=$(python3 -c 'print("A"*120000)')  # >64KiB pipe buffer, <MAX_ARG_STRLEN

wait_ready() {
  local pid=$1 dir=$2 tries=0
  while [ "$tries" -lt 100 ]; do
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

# spawn_poc <dir> <slow|ignore>: POC-shaped process holding PORT in LISTEN.
# Asserts the socket actually reached LISTEN (a leftover binder on PORT
# would otherwise let bind() fail and the case silently test nothing).
spawn_poc() {
  local dir=$1 mode=$2 tries=0
  ( cd "$dir" && exec python3 -c "$POC_PROG" "$mode" \
      server.py --lan --port "$PORT" "$PAD" >/dev/null 2>&1 ) &
  FAKE_PID=$!
  PIDS+=("$FAKE_PID")
  wait_ready "$FAKE_PID" "$dir"
  while [ "$tries" -lt 50 ]; do
    port_listening && return 0
    kill -0 "$FAKE_PID" 2>/dev/null || { echo "FATAL: fake pid=$FAKE_PID died before binding :$PORT" >&2; return 1; }
    tries=$((tries + 1)); sleep 0.1
  done
  echo "FATAL: fake pid=$FAKE_PID never bound :$PORT" >&2
  return 1
}

# spawn_stray: LISTEN on PORT but NOT POC-shaped (wrong cwd, no server.py
# token) so the script's hunt must leave it untouched.
spawn_stray() {
  ( cd "$WORK" && exec python3 -c '
import socket, sys, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", int(sys.argv[1]))); s.listen(1)
time.sleep(300)' "$PORT" >/dev/null 2>&1 ) &
  FAKE_PID=$!
  PIDS+=("$FAKE_PID")
  sleep 0.5
}

# spawn_dummy: POC-shaped (right cwd/argv, so it IS killed) but holds NO
# socket; lets the port be pinned by the stray while the kill path runs.
spawn_dummy() {
  ( cd "$POCDIR" && exec python3 -c 'import time; time.sleep(300)' \
      server.py --lan --port "$PORT" >/dev/null 2>&1 ) &
  FAKE_PID=$!
  PIDS+=("$FAKE_PID")
  wait_ready "$FAKE_PID" "$POCDIR"
}

port_listening() {  # exit 0 = PORT in LISTEN (state 0A)
  # NB: bare `exit 0` inside the pattern block still executes END, so the
  # status must be driven by a flag in END itself.
  awk -v p="$(printf '%04X' "$PORT")" '$4=="0A" { split($2,a,":"); if (a[2]==p) found=1 } END { exit (found ? 0 : 1) }' \
    /proc/net/tcp /proc/net/tcp6 2>/dev/null
}

run_script() {
  KANBAN_UI_PORT="$PORT" KANBAN_UI_POC_DIR="$POCDIR" \
  KANBAN_UI_POC_GRACE_S="${GRACE:-3}" KANBAN_UI_POC_KILL_S="${KILLS:-3}" KANBAN_UI_POC_PORT_WAIT_S="${PWAIT:-3}" \
    bash "$SCRIPT"
}

check() {
  local name=$1 rc=$2
  if [ "$rc" -eq 0 ]; then echo "PASS: $name"; else echo "FAIL: $name (rc=$rc)"; FAILURES=$((FAILURES + 1)); fi
}
contains() { case "$1" in (*"$2"*) return 0 ;; (*) return 1 ;; esac; }
kill_and_reap() { kill -KILL "$1" 2>/dev/null || true; wait "$1" 2>/dev/null || true; }

# ---- 1. graceful-but-slow stop: return only after reap AND port release
spawn_poc "$POCDIR" slow; pid=$FAKE_PID
out=$(run_script 2>&1); rc=$?
check "slow stop: script exits 0" $rc
kill -0 "$pid"; rc=$?; check "slow stop: process already reaped when script returned" $((1 - rc))
port_listening; check "slow stop: port already released when script returned" $((1 - $?))
contains "$out" "killed 1"; check "slow stop: reports kill count" $?

# ---- 2. SIGTERM-ignoring POC: escalate to SIGKILL, bounded, exit 0
spawn_poc "$POCDIR" ignore; pid=$FAKE_PID
t0=$(date +%s)
GRACE=2 KILLS=3 out=$(run_script 2>&1); rc=$?
t=$(( $(date +%s) - t0 ))
check "stubborn stop: script exits 0 after escalation" $rc
kill -0 "$pid" 2>/dev/null; rc=$?; check "stubborn stop: process killed" $((1 - rc))
contains "$out" "SIGKILL"; check "stubborn stop: escalation reported" $?
if [ "$t" -ge 2 ] && [ "$t" -le 15 ]; then rc=0; else rc=1; fi
check "stubborn stop: waited the grace window, bounded well under compose timeouts (t=${t}s)" $rc

# ---- 3. killed POC + port pinned by a stray -> bounded loud failure
spawn_stray; stray=$FAKE_PID
spawn_dummy; dpid=$FAKE_PID
GRACE=1 KILLS=1 PWAIT=2 out=$(run_script 2>&1); rc=$?
if [ "$rc" -ne 0 ]; then rc=0; else rc=1; fi
check "stuck port: script FAILS instead of reporting success" $rc
contains "$out" "still in LISTEN"; check "stuck port: failure message names the LISTEN state" $?
contains "$out" "$PORT"; check "stuck port: failure message names the port" $?
kill -0 "$dpid" 2>/dev/null; rc=$?; check "stuck port: the matched POC was still killed" $((1 - rc))

# ---- 4. redeploy safety: port bound by stray, no matching POC -> success
out=$(run_script 2>&1); rc=$?
check "unmatched + port bound: exits 0 (no gate on the no-kill path)" $rc
if [ "$out" = "no POC process found" ]; then rc=0; else rc=1; fi
check "unmatched + port bound: reports none found" $rc
# Clear the stray (and the dummy corpse) so the port is truly free before
# the sensitivity case tries to bind it again.
kill_and_reap "$stray"; kill_and_reap "$dpid"
sleep 0.5

# ---- 5. knob validation: non-numeric wait budget rejected with rc=2
out=$(KANBAN_UI_PORT="$PORT" KANBAN_UI_POC_DIR="$POCDIR" KANBAN_UI_POC_GRACE_S="10;reboot" bash "$SCRIPT" 2>&1); rc=$?
check "invalid GRACE knob: rejected with rc=2" "$((rc == 2 ? 0 : 1))"
contains "$out" "invalid KANBAN_UI_POC_GRACE_S"; check "invalid GRACE knob: names the knob" $?
out=$(KANBAN_UI_PORT="$PORT" KANBAN_UI_POC_DIR="$POCDIR" KANBAN_UI_POC_PORT_WAIT_S="x" bash "$SCRIPT" 2>&1); rc=$?
check "invalid PORT_WAIT knob: rejected with rc=2" "$((rc == 2 ? 0 : 1))"

# ---- 6. sensitivity: pre-fix shape (gates stripped) reproduces the race
PREFIXED="$WORK/stop_poc_prefix.sh"
# shellcheck disable=SC2016  # sed patterns are intentionally literal
sed 's/^  wait_for_exit "\${pairs\[@\]}"$/  :/; s/^  port_not_listening$/  :/' "$SCRIPT" > "$PREFIXED"
if diff -q "$SCRIPT" "$PREFIXED" >/dev/null 2>&1; then
  echo "FAIL: sensitivity: could not strip the wait gates (script shape changed, update suite)" >&2
  FAILURES=$((FAILURES + 1))
else
  spawn_poc "$POCDIR" slow; pid=$FAKE_PID
  out=$(KANBAN_UI_PORT="$PORT" KANBAN_UI_POC_DIR="$POCDIR" bash "$PREFIXED" 2>&1); rc=$?
  bound=0; port_listening || bound=1
  if [ "$rc" -eq 0 ] && [ "$bound" -eq 0 ]; then
    echo "PASS: sensitivity: pre-fix helper reports success while the port is still bound (bug reproduced)"
  else
    echo "FAIL: sensitivity: pre-fix shape did not reproduce the race (rc=$rc bound=$bound)"
    FAILURES=$((FAILURES + 1))
  fi
  kill_and_reap "$pid"
fi

# ---- 7. PID reuse at the no-python3 fallback (round-9 blocking finding)
# (round-10 update: with python3 present, wait_for_exit hands the pairs
# to the embedded helper; the bash loop below is the documented fallback
# for hosts WITHOUT python3. A PATH shim exposing only cat/tr/date/sleep
# — and no python3 — drives that fallback faithfully instead of
# pretending.) Orchestrated deterministically: a long-lived UNRELATED
# tenant occupies a pid, and the tracked (pid, token) pair carries a
# token captured from a DIFFERENT process — exactly what wait_for_exit
# observes once the original POC exits and its pid is reassigned. Real
# pid-wrapping cannot be forced on demand, so the pair-injection is the
# faithful stand-in.
TENANT_TOKEN=""
OTHER_TOKEN=""
TENANT_PID=""
DEAD_PID=""

# External-command shim for the fallback drive: exactly the externals
# the fallback loop shells out to, and deliberately NO python3.
SHIM="$WORK/shim"
mkdir -p "$SHIM"
for tool in bash cat tr date sleep grep; do
  ln -sf "$(command -v "$tool")" "$SHIM/$tool"
done

run_lib() {  # source the helper (seam) then run a fixed snippet
  local snippet=$1 script=${2:-$SCRIPT} shim=${3:-0} pathv="$PATH"
  if [ "$shim" = "1" ]; then
    # cat/tr/date/sleep resolve via the shim; `command -v python3` in
    # wait_for_exit must therefore report the fallback path.
    pathv="$SHIM"
  fi
  # shellcheck disable=SC2016  # snippets expand inside the sourced bash -c
  env KANBAN_UI_PORT="$PORT" KANBAN_UI_POC_DIR="$POCDIR" STOP_POC_LIB_ONLY=1 \
      KANBAN_UI_POC_GRACE_S="${GRACE:-2}" KANBAN_UI_POC_KILL_S="${KILLS:-3}" \
      KANBAN_UI_POC_PORT_WAIT_S="${PWAIT:-3}" PATH="$pathv" \
      BASH_SCRIPT="$script" HELPER_OVERRIDE="${HELPER_OVERRIDE:-}" \
      TENANT_PID="${TENANT_PID:-}" DEAD_PID="${DEAD_PID:-}" \
      STUBBORN_PID="${STUBBORN_PID:-}" STUBBORN_TOKEN="${STUBBORN_TOKEN:-}" \
      TENANT_TOKEN="${TENANT_TOKEN:-}" OTHER_TOKEN="${OTHER_TOKEN:-}" \
      FRAMING_PID="${FRAMING_PID:-}" \
      bash -c 'source "$BASH_SCRIPT"; '"$snippet"
}

( cd "$WORK" && exec python3 -c 'import time; time.sleep(300)' tenant-holds-pid >/dev/null 2>&1 ) &
TENANT_PID=$!
PIDS+=("$TENANT_PID")
wait_ready "$TENANT_PID" "$WORK"
( cd "$WORK" && exec python3 -c 'import time; time.sleep(300)' dead-poc-token-source >/dev/null 2>&1 ) &
DEAD_PID=$!
PIDS+=("$DEAD_PID")
wait_ready "$DEAD_PID" "$WORK"
# shellcheck disable=SC2016  # these snippets expand inside the sourced bash -c
TENANT_TOKEN=$(run_lib 'proc_token "$TENANT_PID"')
# shellcheck disable=SC2016
OTHER_TOKEN=$(run_lib 'proc_token "$DEAD_PID"')
if [ -n "$TENANT_TOKEN" ] && [ -n "$OTHER_TOKEN" ] && [ "$TENANT_TOKEN" != "$OTHER_TOKEN" ]; then rc=0; else rc=1; fi
check "pid-reuse prep: proc_token captures distinct stable identities" $rc
# shellcheck disable=SC2016  # these snippets expand inside the sourced bash -c
out_m=$(run_lib 'identity_matches "$TENANT_PID" "$TENANT_TOKEN" && echo MATCH')
# shellcheck disable=SC2016
out_b=$(run_lib 'identity_matches "$TENANT_PID" "$OTHER_TOKEN" && echo BADMATCH')  # errexit -> empty
# shellcheck disable=SC2016
out_g=$(run_lib 'identity_matches 4194303 "$OTHER_TOKEN" && echo GHOST')           # errexit -> empty
if [ "$out_m" = "MATCH" ] && [ -z "$out_b" ] && [ -z "$out_g" ]; then rc=0; else rc=1; fi
check "identity_matches: matches own identity only, rejects foreign token and ghost pid" $rc

# Confirm the shim genuinely removes python3 before trusting the
# fallback assertions built on it.
# shellcheck disable=SC2016
if run_lib 'command -v python3' "$SCRIPT" 1 | grep -q python3; then
  echo "FAIL: pid-reuse prep: PATH shim still exposes python3 (fallback not exercised)" >&2
  FAILURES=$((FAILURES + 1))
fi

# shellcheck disable=SC2016  # expands inside the sourced bash -c
GRACE=2 KILLS=3 out=$(run_lib 'rc=0; wait_for_exit "$TENANT_PID" "$OTHER_TOKEN" 2>&1 || rc=$?; echo "rc=$rc"; kill -0 "$TENANT_PID" 2>/dev/null && echo TENANT_ALIVE || echo TENANT_DEAD' "$SCRIPT" 1); rc=$?
check "fallback pid-reuse: wait_for_exit harness ran" $rc
contains "$out" "rc=0"; check "fallback pid-reuse: stale identity treated as exited, exits 0" $?
contains "$out" "TENANT_ALIVE"; check "fallback pid-reuse: recycled-pid tenant never signalled" $?
if contains "$out" "killing POC"; then rc=1; else rc=0; fi
check "fallback pid-reuse: not even SIGTERM aimed at a mismatched identity" $rc
if contains "$out" "SIGKILL"; then rc=1; else rc=0; fi
check "fallback pid-reuse: no escalation attempted against mismatched identity" $rc

# The fallback must also REFUSE to touch a genuinely stubborn tracked
# process at all (round-11: no handle -> NO signal, not even SIGTERM):
# loud ERROR, rc=1, process survives untouched.
( cd "$WORK" && exec python3 -c 'import signal, time
signal.signal(signal.SIGTERM, lambda *a: None)
time.sleep(300)' stubborn-fallback >/dev/null 2>&1 ) &
STUBBORN_PID=$!
PIDS+=("$STUBBORN_PID")
wait_ready "$STUBBORN_PID" "$WORK"
# shellcheck disable=SC2016
STUBBORN_TOKEN=$(run_lib 'proc_token "$STUBBORN_PID"')
# shellcheck disable=SC2016
GRACE=1 out=$(run_lib 'rc=0; wait_for_exit "$STUBBORN_PID" "$STUBBORN_TOKEN" 2>&1 || rc=$?; echo "rc=$rc"' "$SCRIPT" 1)
contains "$out" "rc=1"; check "fallback stubborn: refuses any signal instead of touching a bare PID" $?
contains "$out" "refusing to signal any bare PID"; check "fallback stubborn: failure states why (no pidfd without python3)" $?
if contains "$out" "killing POC"; then rc=1; else rc=0; fi
check "fallback stubborn: not even SIGTERM attempted on a matched identity" $rc
kill -0 "$STUBBORN_PID" 2>/dev/null; rc=$?; check "fallback stubborn: tracked process survives untouched" $rc
kill_and_reap "$STUBBORN_PID"

# Sensitivity: degrade the FALLBACK to the pre-fix shape (round-8/10:
# bare `[ -d /proc/$p ]` existence check followed by a bare-PID signal).
# Same scenario must now SIGNAL the unrelated tenant, proving the suite
# detects the reported bug.
DEGRADED="$WORK/stop_poc_pidreuse_prefix.sh"
# shellcheck disable=SC2016  # sed patterns are intentionally literal
sed -e 's@identity_matches "\$p" "\$tok"@[ -d "/proc/$p" ]@' \
    -e 's@^ *echo "ERROR: POC pid \$p still running.*$@      echo "killing POC pid=$p"; kill "$p" 2>/dev/null || true@' \
    "$SCRIPT" > "$DEGRADED"
if diff -q "$SCRIPT" "$DEGRADED" >/dev/null 2>&1; then
  echo "FAIL: pid-reuse sensitivity: could not degrade the identity check (script shape changed, update suite)" >&2
  FAILURES=$((FAILURES + 1))
else
  # shellcheck disable=SC2016  # expands inside the sourced bash -c
  GRACE=1 KILLS=3 out=$(run_lib 'wait_for_exit "$TENANT_PID" "$OTHER_TOKEN" 2>&1 || true; kill -0 "$TENANT_PID" 2>/dev/null && echo TENANT_ALIVE || echo TENANT_DEAD' "$DEGRADED" 1)
  contains "$out" "killing POC"; check "pid-reuse sensitivity: pre-fix fallback signals a bare PID" $?
  wait "$TENANT_PID" 2>/dev/null || true
  kill -0 "$TENANT_PID" 2>/dev/null; rc=$?
  if [ "$rc" -ne 0 ]; then rc=0; else rc=1; fi
  check "pid-reuse sensitivity: pre-fix fallback kills the unrelated tenant (bug reproduced)" $rc
fi
# The sensitivity run killed the tenant; respawn one for case 8.
( cd "$WORK" && exec python3 -c 'import time; time.sleep(300)' tenant-holds-pid >/dev/null 2>&1 ) &
TENANT_PID=$!
PIDS+=("$TENANT_PID")
wait_ready "$TENANT_PID" "$WORK"

# ---- 8. round-10 check-to-signal window: escalation must go through the
# held pidfd handle, never a bare PID re-derived after validation.
# The window between "survivor validated" and "signal delivered" is
# microseconds wide in production, so a harness cannot win it by luck.
# The embedded helper carries a no-op-in-production test seam
# (KANBAN_UI_POC_TEST_ESCALATE_MARK/_WINDOW_S, documented in
# stop_poc.sh): at the exact escalation point — survivors already
# validated, signal not yet delivered — it touches a mark file and
# pauses. The harness waits for the mark, kills+reaps the validated
# ORIGINAL, and re-installs an unrelated TENANT at the SAME pid number:
# inside a private user+pid namespace (unshare -rpf) the kernel hands
# out the lowest free pid, pinned deterministically via
# /proc/sys/kernel/ns_last_pid. Both shapes (fixed and degraded) pause
# at the identical seam, so the only difference under test is HOW the
# escalation signals: a held pidfd handle vs the bare PID number.
# run_pidfd_case <label> <helper-file>: renders a self-contained case
# script and executes the WHOLE orchestration inside a private
# user+pid namespace (unshare -rpf --mount-proc), because
# /proc/sys/kernel/ns_last_pid is per-pid-namespace and only writable
# there. Inside the namespace: victim -> helper pauses at the seam ->
# kill+reap victim -> pin ns_last_pid=victim-1 -> spawn tenant (kernel
# assigns the lowest free pid == victim's number) -> driver finishes.
# Echoes VICTIM_PID=/TENANT_PID=/RECYCLE-FAILED|NS-PIN-FAILED|
# TOKEN-EMPTY/TENANT_ALIVE|TENANT_DEAD/HELPER_RC=/then helper output.
run_pidfd_case() {
  local label=$1 helper=$2
  local casefile="$WORK/case_$1.sh" mark="$WORK/mark_$1" out="$WORK/out_$1"
  rm -f "$mark"
  cat > "$casefile" <<'CASEEOF'
#!/bin/bash
# Runs INSIDE unshare -rpf --mount-proc (uid 0, private pid namespace).
set -u
: "${LABEL:?}${HELPER:?}${WORK:?}${PORT:?}${SCRIPT:?}"
mark="$WORK/mark_$LABEL"; out="$WORK/out_$LABEL"
spawn_stubborn() {  # SIGTERM-stubborn, POC-shaped argv
  ( cd "$WORK" && exec python3 -c 'import signal, time
signal.signal(signal.SIGTERM, lambda *a: None)
time.sleep(300)' original-poc-stubborn server.py --lan --port "$PORT" >/dev/null 2>&1 ) &
}
spawn_tenant() {  # unrelated process to occupy the recycled pid
  ( cd "$WORK" && exec python3 -c 'import time; time.sleep(300)' recycled-tenant >/dev/null 2>&1 ) &
}
ready() {  # ready PID TOKEN-NEEDLE — wait until cmdline readable
  local pid=$1 needle=$2 tries=0
  while [ "$tries" -lt 150 ]; do
    if [ -r "/proc/$pid/cmdline" ] \
      && tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "$needle"; then
      return 0
    fi
    tries=$((tries + 1)); sleep 0.1
  done
  return 1
}
spawn_stubborn
victim=$!
ready "$victim" original-poc-stubborn || { echo "VICTIM-UNREADY"; exit 9; }
# real token via the script's own bash proc_token (library seam)
STOP_POC_LIB_ONLY=1 source "$SCRIPT"
tok=$(proc_token "$victim")
[ -n "$tok" ] || echo "TOKEN-EMPTY"
pfile="$WORK/pairs_$LABEL"
: > "$pfile"
printf '%s\0%s\0' "$victim" "$tok" >> "$pfile"
KANBAN_UI_POC_GRACE_S=1 KANBAN_UI_POC_KILL_S=3 \
KANBAN_UI_POC_TEST_ESCALATE_MARK="$mark" \
KANBAN_UI_POC_TEST_WINDOW_S="${WINDOW_S:-2}" \
  python3 "$HELPER" wait "$pfile" > "$out" 2>&1 &
driver=$!
tries=0
while [ ! -e "$mark" ] && [ "$tries" -lt 150 ]; do
  tries=$((tries + 1)); sleep 0.1
done
[ -e "$mark" ] || echo "MARK-TIMEOUT"
# The window is now widened: validate->signal has already passed the
# check for this iteration; the signal fires only when the sleep ends.
kill -KILL "$victim" 2>/dev/null || true
wait "$victim" 2>/dev/null || true
if ! echo $((victim - 1)) > /proc/sys/kernel/ns_last_pid 2>/dev/null; then
  echo "NS-PIN-FAILED"
fi
spawn_tenant
tenant=$!
ready "$tenant" recycled-tenant || echo "TENANT-UNREADY"
[ "$tenant" = "$victim" ] || echo "RECYCLE-FAILED"
wait "$driver" 2>/dev/null; rc=$?
tstate=TENANT_ALIVE
kill -0 "$tenant" 2>/dev/null || tstate=TENANT_DEAD
kill -KILL "$tenant" 2>/dev/null || true
wait "$tenant" 2>/dev/null || true
echo "VICTIM_PID=$victim"
echo "TENANT_PID=$tenant"
echo "HELPER_RC=$rc"
echo "$tstate"
cat "$out"
CASEEOF
  # The self-prefix-assignments are deliberate: they scope the case's
  # environment to the unshare invocation only (SC2097/SC2098 are about
  # the same statement, which is exactly the intent here).
  # shellcheck disable=SC2097,SC2098
  WINDOW_S="${WINDOW:-2}" LABEL="$label" HELPER="$helper" WORK="$WORK" \
  PORT="$PORT" SCRIPT="$SCRIPT" \
  KANBAN_UI_PORT="$PORT" KANBAN_UI_POC_DIR="$WORK" \
    unshare -rpf --mount-proc bash "$casefile" 2>&1
}

HELPER="$WORK/poc_helper.py"
dump_helper() {  # extract the embedded python helper from stop_poc.sh
  local dest=$1 script=${2:-$SCRIPT}
  STOP_POC_PY_DUMP=1 bash "$script" > "$dest" || return 1
  [ -s "$dest" ] || return 1
  python3 -c 'import sys, ast; ast.parse(open(sys.argv[1]).read())' "$dest"
}
if ! dump_helper "$HELPER"; then
  echo "FAIL: helper dump: STOP_POC_PY_DUMP did not produce parseable python (update suite)" >&2
  FAILURES=$((FAILURES + 1))
fi

if [ -s "$HELPER" ]; then
  # Fixed shape: escalation signals ONLY through the held pidfd handle.
  out8=$(WINDOW=2 run_pidfd_case fixed "$HELPER")
  vpid=$(printf '%s\n' "$out8" | sed -n 's/^VICTIM_PID=//p')
  tpid=$(printf '%s\n' "$out8" | sed -n 's/^TENANT_PID=//p')
  if [ -n "$vpid" ] && [ "$vpid" = "$tpid" ]; then rc=0; else rc=1; fi
  check "pidfd escalation: tenant genuinely recycled the validated pid" $rc
  if printf '%s\n' "$out8" | grep -q "TENANT_ALIVE"; then rc=0; else rc=1; fi
  check "pidfd escalation: replacement installed in the check-to-signal window is never signalled" $rc
  if printf '%s\n' "$out8" | grep -q "escalating to SIGKILL"; then rc=0; else rc=1; fi
  check "pidfd escalation: escalation path actually ran (reported)" $rc
  if printf '%s\n' "$out8" | grep -q "POC pid(s)"; then rc=0; else rc=1; fi
  check "pidfd escalation: helper reached escalation with the validated survivor" $rc
  if printf '%s\n' "$out8" | grep -q "HELPER_RC=0"; then rc=0; else rc=1; fi
  check "pidfd escalation: helper exits 0 after the tracked identity vanished" $rc

  # Sensitivity (the round-10 test gap the reviewer named): reproduce
  # the EXACT flagged shape — `identity_matches` true, the validated PID
  # converted back to a bare number, and `kill -KILL` aimed at that
  # number at escalation time with no identity-bound handle. Same
  # deterministic scenario must now KILL the replacement tenant,
  # proving this suite exercises the check-to-signal race and detects
  # the regression. Only the escalation signal LINE is degraded; the
  # seam, timing, and orchestration are identical to the fixed case.
  HELPER_DEGRADED="$WORK/poc_helper_degraded.py"
  # shellcheck disable=SC2016  # sed pattern is intentionally literal
  sed 's@^                        _pidfd_kill(kfd, signal.SIGKILL)  # ESCALATION_SEAM_LINE$@                        os.kill(kpid, signal.SIGKILL)  # bare-PID escalation (round-8 shape)@' "$HELPER" > "$HELPER_DEGRADED"
  if diff -q "$HELPER" "$HELPER_DEGRADED" >/dev/null 2>&1; then
    echo "FAIL: pidfd sensitivity: could not degrade the escalation to a bare os.kill (helper shape changed, update suite)" >&2
    FAILURES=$((FAILURES + 1))
  elif ! grep -q "bare-PID escalation" "$HELPER_DEGRADED"; then
    echo "FAIL: pidfd sensitivity: degraded helper missing the injected bare-PID kill (update suite)" >&2
    FAILURES=$((FAILURES + 1))
  else
    out8d=$(WINDOW=2 run_pidfd_case degraded "$HELPER_DEGRADED")
    if printf '%s\n' "$out8d" | grep -q "TENANT_DEAD"; then rc=0; else rc=1; fi
    check "pidfd sensitivity: bare-PID escalation kills the recycled-pid tenant installed in the window (round-8 bug reproduced)" $rc
    if printf '%s\n' "$out8d" | grep -q "escalating to SIGKILL"; then rc=0; else rc=1; fi
    check "pidfd sensitivity: degraded run reached the same escalation point" $rc
  fi
fi

# ---- 9. round-11 fallback check-to-guard window (HIGH finding 2): the
# no-python3 fallback must refuse BEFORE signalling. Deterministic via
# the bash-side test seam (STOP_POC_TEST_FALLBACK_MARK/_WINDOW_S, no-op
# in production): the fallback pauses AFTER identity_matches succeeded
# on the tracked pair; inside that widened window the harness kills the
# tracked process and installs an unrelated TENANT at the exact recycled
# PID (private user+pid namespace + ns_last_pid pinning, same trick as
# case 8). Fixed shape: guard sends NOTHING (no "killing POC"), fails
# loudly rc=1, tenant untouched. Sensitivity: inject the pre-fix bare
# `kill "$p"` at that point (the exact flagged check-then-signal shape)
# and the same scenario SIGTERMs the replacement tenant.
run_fallback_case() {  # <label> <script-file>
  local label=$1 script=$2
  local casefile="$WORK/case_fb_$1.sh" mark="$WORK/mark_fb_$1" out="$WORK/out_fb_$1"
  rm -f "$mark" "$out"
  cat > "$casefile" <<'FBEOf'
#!/bin/bash
# Runs INSIDE unshare -rpf --mount-proc (uid 0, private pid namespace).
set -u
: "${LABEL:?}${CASE_SCRIPT:?}${WORK:?}${PORT:?}${SHIM:?}"
mark="$WORK/mark_fb_$LABEL"; out="$WORK/out_fb_$LABEL"
ready() {  # ready PID NEEDLE
  local pid=$1 needle=$2 tries=0
  while [ "$tries" -lt 150 ]; do
    if [ -r "/proc/$pid/cmdline" ] \
      && tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "$needle"; then
      return 0
    fi
    tries=$((tries + 1)); sleep 0.1
  done
  return 1
}
( cd "$WORK" && exec python3 -c 'import time; time.sleep(300)' fallback-tracked >/dev/null 2>&1 ) &
tracked=$!
ready "$tracked" fallback-tracked || { echo "TRACKED-UNREADY"; exit 9; }
KANBAN_UI_PORT="$PORT" KANBAN_UI_POC_DIR="$WORK" STOP_POC_LIB_ONLY=1 \
KANBAN_UI_POC_GRACE_S=1 KANBAN_UI_POC_KILL_S=1 KANBAN_UI_POC_PORT_WAIT_S=1 \
  source "$CASE_SCRIPT"
tok=$(proc_token "$tracked")
[ -n "$tok" ] || echo "TOKEN-EMPTY"
# Drive wait_for_exit with the shimmed PATH (no python3) so the FALLBACK
# path runs for real; the seam marks after identity_matches succeeds.
# The sourced script's required-variable guards read these, so they must
# be EXPORTED into the driver (plain assignment would not cross into
# `bash -c`'s environment; the abort would then skip the fallback loop
# entirely and the seam would never fire).
STOP_POC_LIB_ONLY=1 KANBAN_UI_PORT="$PORT" KANBAN_UI_POC_DIR="$WORK" \
STOP_POC_TEST_FALLBACK_MARK="$mark" \
STOP_POC_TEST_FALLBACK_WINDOW_S="${WINDOW_S:-2}" \
  PATH="$SHIM" bash -c 'source "$0"; wait_for_exit "$1" "$2" >"$3" 2>&1' \
  "$CASE_SCRIPT" "$tracked" "$tok" "$out" &
driver=$!
tries=0
while [ ! -e "$mark" ] && [ "$tries" -lt 150 ]; do
  tries=$((tries + 1)); sleep 0.1
done
[ -e "$mark" ] || echo "MARK-TIMEOUT"
# Inside the widened check-to-signal window: recycle the validated PID.
kill -KILL "$tracked" 2>/dev/null || true
wait "$tracked" 2>/dev/null || true
if ! echo $((tracked - 1)) > /proc/sys/kernel/ns_last_pid 2>/dev/null; then
  echo "NS-PIN-FAILED"
fi
( cd "$WORK" && exec python3 -c 'import signal, time
signal.signal(signal.SIGTERM, signal.SIG_DFL)  # unshare -f inherits SIGTERM=SIG_IGN
time.sleep(300)' recycled-tenant >/dev/null 2>&1 ) &
tenant=$!
ready "$tenant" recycled-tenant || echo "TENANT-UNREADY"
[ "$tenant" = "$tracked" ] || echo "RECYCLE-FAILED"
rc=0; wait "$driver" 2>/dev/null || rc=$?
# A signalled-but-unreaped child is a ZOMBIE: kill -0 still succeeds.
# Treat Z state as dead (the degraded shape kills the tenant exactly at
# driver exit, racing reap), while the fixed shape leaves it R/S.
tstate=TENANT_ALIVE
tries=0
while [ "$tries" -lt 30 ]; do
  if ! kill -0 "$tenant" 2>/dev/null; then tstate=TENANT_DEAD; break; fi
  tstat=$(awk '{print $3}' "/proc/$tenant/stat" 2>/dev/null || true)
  if [ "$tstat" = "Z" ]; then tstate=TENANT_DEAD; break; fi
  tries=$((tries + 1)); sleep 0.1
done
kill -KILL "$tenant" 2>/dev/null || true
wait "$tenant" 2>/dev/null || true
echo "TRACKED_PID=$tracked"
echo "TENANT_PID=$tenant"
echo "DRIVER_RC=$rc"
echo "$tstate"
cat "$out" 2>/dev/null
FBEOf
  # shellcheck disable=SC2097,SC2098  # env scoped to the unshare call
  WINDOW_S="${WINDOW:-4}" LABEL="$label" CASE_SCRIPT="$script" WORK="$WORK" \
  PORT="$PORT" SHIM="$SHIM" \
  KANBAN_UI_PORT="$PORT" KANBAN_UI_POC_DIR="$WORK" \
    unshare -rpf --mount-proc bash "$casefile" 2>&1
}

out9=$(WINDOW=8 run_fallback_case fixed "$SCRIPT")
if printf '%s\n' "$out9" | grep -q "TENANT_ALIVE"; then rc=0; else rc=1; fi
check "fallback check-to-guard: replacement installed after identity_matches is never signalled" $rc
if printf '%s\n' "$out9" | grep -q "RECYCLE-FAILED\|TRACKED-UNREADY\|MARK-TIMEOUT\|TOKEN-EMPTY"; then rc=1; else rc=0; fi
check "fallback check-to-guard: deterministic recycle actually happened" $rc
if printf '%s\n' "$out9" | grep -q "killing POC"; then rc=1; else rc=0; fi
check "fallback check-to-guard: fixed shape emits no kill at all" $rc
if printf '%s\n' "$out9" | grep -q "refusing to signal any bare PID"; then rc=0; else rc=1; fi
check "fallback check-to-guard: refuses loudly with an explanation" $rc
if printf '%s\n' "$out9" | grep -q "DRIVER_RC=1"; then rc=0; else rc=1; fi
check "fallback check-to-guard: refuses with rc=1 (caller must not start Compose)" $rc

# Sensitivity: restore the flagged pre-fix shape at the seam point —
# identity_matches succeeded, then a bare-PID `kill` (the exact
# check-to-signal race the reviewer said revalidation cannot close).
FB_PREFIX="$WORK/stop_poc_fb_prefix.sh"
# shellcheck disable=SC2016  # sed pattern is intentionally literal
sed 's@^ *echo "ERROR: POC pid \$p still running.*$@      echo "killing POC pid=$p"; kill "$p" 2>/dev/null || true@' \
  "$SCRIPT" > "$FB_PREFIX"
if diff -q "$SCRIPT" "$FB_PREFIX" >/dev/null 2>&1; then
  echo "FAIL: fallback sensitivity: could not inject the bare-PID kill at the seam (script shape changed, update suite)" >&2
  FAILURES=$((FAILURES + 1))
else
  out9d=$(WINDOW=8 run_fallback_case prefix "$FB_PREFIX")
  if printf '%s\n' "$out9d" | grep -q "TENANT_DEAD"; then rc=0; else rc=1; fi
  if [ "$rc" -ne 0 ]; then
    echo "--- fallback sensitivity raw output ---" >&2
    printf '%s\n' "$out9d" >&2
    echo "--- end raw output ---" >&2
  fi
  check "fallback sensitivity: pre-fix check-then-kill signals the recycled-pid tenant (round-11 bug reproduced)" $rc
  if printf '%s\n' "$out9d" | grep -q "killing POC"; then rc=0; else rc=1; fi
  check "fallback sensitivity: degraded run reached the same signal point" $rc
fi

# ---- 10. round-11 hunt identity binding (HIGH finding 1): the REAL
# hunt (full script, nothing injected) selects a POC-shaped victim,
# then pauses at the hunt seam AFTER cmdline/port/cwd passed. The
# harness kills the selected victim and installs an unrelated TENANT at
# the recycled PID before the identity token is bound. Fixed shape: the
# bracketed identity reads reject the recycled PID (candidate dropped,
# "no POC process found", tenant untouched). Sensitivity: remove ONLY
# the bracket comparisons (pre-fix shape: proc_token captured after
# selection with no stability proof) and the same scenario binds the
# tenant's token and SIGTERMs it via the helper.
run_hunt_case() {  # <label> <script-file>
  local label=$1 script=$2
  local casefile="$WORK/case_hunt_$1.sh" mark="$WORK/mark_hunt_$1" out="$WORK/out_hunt_$1"
  rm -f "$mark" "$out"
  cat > "$casefile" <<'HUNTEOF'
#!/bin/bash
# Runs INSIDE unshare -rpf --mount-proc (uid 0, private pid namespace).
set -u
: "${LABEL:?}${CASE_SCRIPT:?}${WORK:?}${PORT:?}"
mark="$WORK/mark_hunt_$LABEL"; out="$WORK/out_hunt_$LABEL"
ready() {  # ready PID NEEDLE — cmdline readable AND cwd settled
  local pid=$1 needle=$2 tries=0
  while [ "$tries" -lt 150 ]; do
    if [ -r "/proc/$pid/cmdline" ] \
      && tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "$needle" \
      && [ "$(readlink "/proc/$pid/cwd" 2>/dev/null || true)" = "$WORK" ]; then
      return 0
    fi
    tries=$((tries + 1)); sleep 0.1
  done
  return 1
}
# POC-shaped victim (argv + cwd match the hunt); plain sleep so SIGTERM
# (default action) kills it — the degraded case proves the kill lands.
( cd "$WORK" && exec python3 -c 'import time; time.sleep(300)' hunt-victim \
    server.py --lan --port "$PORT" >/dev/null 2>&1 ) &
victim=$!
ready "$victim" hunt-victim || { echo "VICTIM-UNREADY"; exit 9; }
KANBAN_UI_PORT="$PORT" KANBAN_UI_POC_DIR="$WORK" \
KANBAN_UI_POC_GRACE_S=1 KANBAN_UI_POC_KILL_S=2 KANBAN_UI_POC_PORT_WAIT_S=1 \
STOP_POC_TEST_HUNT_MARK="$mark" STOP_POC_TEST_HUNT_WINDOW_S="${WINDOW_S:-2}" \
  bash "$CASE_SCRIPT" > "$out" 2>&1 &
driver=$!
tries=0
while [ ! -e "$mark" ] && [ "$tries" -lt 150 ]; do
  tries=$((tries + 1)); sleep 0.1
done
[ -e "$mark" ] || echo "MARK-TIMEOUT"
# Selection already passed for SOME candidate when the mark appeared;
# recycle the victim's PID inside the pre-binding window.
kill -KILL "$victim" 2>/dev/null || true
wait "$victim" 2>/dev/null || true
if ! echo $((victim - 1)) > /proc/sys/kernel/ns_last_pid 2>/dev/null; then
  echo "NS-PIN-FAILED"
fi
( cd "$WORK" && exec python3 -c 'import signal, time
signal.signal(signal.SIGTERM, signal.SIG_DFL)  # unshare -f children inherit SIGTERM=SIG_IGN
time.sleep(300)' recycled-tenant >/dev/null 2>&1 ) &
tenant=$!
ready "$tenant" recycled-tenant || echo "TENANT-UNREADY"
[ "$tenant" = "$victim" ] || echo "RECYCLE-FAILED"
wait "$driver" 2>/dev/null; rc=$?
tstate=TENANT_ALIVE
kill -0 "$tenant" 2>/dev/null || tstate=TENANT_DEAD
kill -KILL "$tenant" 2>/dev/null || true
wait "$tenant" 2>/dev/null || true
echo "VICTIM_PID=$victim"
echo "TENANT_PID=$tenant"
echo "SCRIPT_RC=$rc"
echo "$tstate"
cat "$out" 2>/dev/null
HUNTEOF
  # shellcheck disable=SC2097,SC2098  # env scoped to the unshare call
  WINDOW_S="${WINDOW:-4}" LABEL="$label" CASE_SCRIPT="$script" WORK="$WORK" \
  PORT="$PORT" \
    unshare -rpf --mount-proc bash "$casefile" 2>&1
}

out10=$(WINDOW=8 run_hunt_case fixed "$SCRIPT")
if printf '%s\n' "$out10" | grep -q "TENANT_ALIVE"; then rc=0; else rc=1; fi
check "hunt bracket: replacement installed after selection is never paired or signalled" $rc
if printf '%s\n' "$out10" | grep -q "RECYCLE-FAILED\|VICTIM-UNREADY\|MARK-TIMEOUT\|NS-PIN-FAILED"; then rc=1; else rc=0; fi
check "hunt bracket: deterministic recycle inside the binding window happened" $rc
if printf '%s\n' "$out10" | grep -q "killing POC\|killed 1"; then rc=1; else rc=0; fi
check "hunt bracket: recycled PID rejected before token binding (no kill)" $rc
if printf '%s\n' "$out10" | grep -q "no POC process found"; then rc=0; else rc=1; fi
check "hunt bracket: hunt reports nothing matched once the candidate is discarded" $rc
if printf '%s\n' "$out10" | grep -q "SCRIPT_RC=0"; then rc=0; else rc=1; fi
check "hunt bracket: script exits 0 with no POC to stop" $rc

# Sensitivity: strip ONLY the bracket comparisons + the pre-selection
# identity read (the pre-fix hunt: selection first, proc_token captured
# afterwards with no stability proof). Same scenario must bind the
# tenant's token and SIGTERM it — the exact flagged race.
HUNT_PREFIX="$WORK/stop_poc_hunt_prefix.sh"
# shellcheck disable=SC2016  # sed patterns are intentionally literal
sed -e 's@^ *tok_pre="\$(proc_token "\$pid")"  # BRACKET_PRE$@  tok_pre=pre-fix-blind  # BRACKET_PRE-blind@' \
    -e 's@^ *\[ "\$tok_mid" = "\$tok_pre" \] || continue  # BRACKET_MID_CHECK$@  :  # BRACKET_MID_CHECK-removed@' \
    -e 's@^ *\[ "\$token" = "\$tok_pre" \] || continue  # BRACKET_BIND_CHECK$@  :  # BRACKET_BIND_CHECK-removed@' \
    -e 's@^ *\[ "\$tok_post" = "\$tok_pre" \] || continue  # BRACKET_POST_CHECK$@  :  # BRACKET_POST_CHECK-removed@' \
    "$SCRIPT" > "$HUNT_PREFIX"
if diff -q "$SCRIPT" "$HUNT_PREFIX" >/dev/null 2>&1; then
  echo "FAIL: hunt sensitivity: could not strip the identity bracket (script shape changed, update suite)" >&2
  FAILURES=$((FAILURES + 1))
else
  out10d=$(WINDOW=8 run_hunt_case prefix "$HUNT_PREFIX")
  if printf '%s\n' "$out10d" | grep -q "TENANT_DEAD"; then rc=0; else rc=1; fi
  check "hunt sensitivity: unbracketed token capture pairs the recycled PID and kills the tenant (round-11 bug reproduced)" $rc
  if printf '%s\n' "$out10d" | grep -q "killing POC"; then rc=0; else rc=1; fi
  check "hunt sensitivity: degraded hunt reached the same signal point" $rc
fi

# ---- 11. _read_pairs NUL-pair framing (round-11 sub-finding): an
# empty token field must NOT be silently dropped (that shifted every
# later pair, orphaning a live POC so it was never stopped). The fixed
# helper fails loudly on a malformed pairs file instead. Deterministic:
# ghost pid + empty token first, then a REAL live process second — the
# pre-fix drop makes the live pid inherit the ghost's slot and vanish
# from tracking with rc=0; the fixed shape refuses with rc=1.
( cd "$WORK" && exec python3 -c 'import time; time.sleep(300)' framing-victim server.py --lan --port "$PORT" >/dev/null 2>&1 ) &
FRAMING_PID=$!
PIDS+=("$FRAMING_PID")
wait_ready "$FRAMING_PID" "$WORK"
# shellcheck disable=SC2016
FRAMING_TOKEN=$(run_lib 'proc_token "$FRAMING_PID"')
FR_FILE="$WORK/pairs_framing"
printf '4194303\0\0%s\0%s\0' "$FRAMING_PID" "$FRAMING_TOKEN" > "$FR_FILE"
if [ -s "$HELPER" ]; then
  FR_OUT=$(KANBAN_UI_POC_GRACE_S=1 KANBAN_UI_POC_KILL_S=1 \
    python3 "$HELPER" term "$FR_FILE" 2>&1); FR_RC=$?
  if [ "$FR_RC" -ne 0 ]; then rc=0; else rc=1; fi
  check "pairs framing: empty token field refuses loudly (no silent shift)" $rc
  contains "$FR_OUT" "malformed pairs file"; check "pairs framing: failure names the malformed pairs file" $?
  kill -0 "$FRAMING_PID" 2>/dev/null; rc=$?; check "pairs framing: live process never signalled on malformed input" $rc
  # Well-formed control: same helper, intact framing, still tracks and
  # stops the process (proves the guard is not a blanket refusal).
  printf '%s\0%s\0' "$FRAMING_PID" "$FRAMING_TOKEN" > "$FR_FILE"
  if KANBAN_UI_POC_GRACE_S=2 KANBAN_UI_POC_KILL_S=2 \
    python3 "$HELPER" term "$FR_FILE" >/dev/null 2>&1; then rc=0; else rc=1; fi
  check "pairs framing: well-formed pairs file still stops the tracked process" $rc
  kill -0 "$FRAMING_PID" 2>/dev/null; rc=$?; check "pairs framing: tracked process gone after well-formed run" $((1 - rc))
else
  echo "FAIL: pairs framing: helper dump unavailable (update suite)" >&2
  FAILURES=$((FAILURES + 1))
fi

if [ "$FAILURES" -gt 0 ]; then
  echo "RESULT: $FAILURES failure(s)" >&2
  exit 1
fi
echo "RESULT: all stop_poc.sh shutdown-wait regression tests passed"
exit 0
