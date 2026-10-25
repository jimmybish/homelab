#!/bin/bash
# Stop the manually-launched kanban-ui proof-of-concept so the container
# can bind its port. Invoked via ansible.builtin.script; ALL variables
# arrive through the environment (never interpolated into shell syntax),
# and expansion is quoted. A process is killed only when BOTH hold:
#   1. /proc/<pid>/cmdline contains: server.py --lan --port <KANBAN_UI_PORT>
#      (exact integer token match, not a regex supplied by the caller)
#   2. /proc/<pid>/cwd equals KANBAN_UI_POC_DIR
# Anything else is left untouched.
# After matching, the helper WAITS (bounded) for every matched process to
# exit AND for the port to leave LISTEN state before reporting success
# (PR#13 review 4369296896): a graceful stop still racing its socket
# release lets Compose startup fail with address-in-use. Escalation past
# SIGTERM goes through a RACE-FREE PROCESS HANDLE (PR#13 round-10
# blocking finding): the matched (pid, identity) pair is bound to a
# pidfd (verify -> pidfd_open -> verify), and SIGTERM/SIGKILL are sent
# with pidfd_send_signal against that held handle, NEVER against a bare
# PID number re-derived after validation. If the tracked task exits and
# its PID is recycled, the handle still refers to the original (dead)
# task_struct and the signal can never reach the replacement process.
# Round-11 blocking findings close the OTHER side of that guarantee: a
# held handle only proves WHICH task it signals, so the hunt must first
# prove the token it hands over still describes the task whose
# cmdline/port/cwd passed selection. Every selection check is now
# bracketed by stable-identity reads (before selection, after selection,
# and again after the token is bound); any drift rejects the candidate,
# so a PID that was recycled in the middle of the hunt can never have its
# replacement's identity bound to a pair. The helper keeps its
# verify -> pidfd_open -> verify bracket on top of that. And because no
# check-then-signal sequence is identity-safe without a handle, the
# python3-absent fallback sends NO signal at all - it fails safely before
# touching a bare PID (not even SIGTERM).
# Exits non-zero with a clear message if the POC refuses to stop or the
# socket never frees.
set -euo pipefail

# ---------------------------------------------------------------------------
# Race-free signalling helper (PR#13 round-10 blocking finding on review
# 4369296896): "Use a race-free process handle for signaling (Linux
# pidfd/pidfd_send_signal, held for the tracked process) rather than
# converting the validated identity back into a bare PID before
# escalation." The Python source below is the SINGLE source of truth:
# the production path writes it to a private tempfile and runs it, and
# the test seam STOP_POC_PY_DUMP prints it verbatim, so the suite always
# exercises the exact code the role deploys. stdlib-only; the production
# path never signals by PID number.
# ---------------------------------------------------------------------------
POC_HELPER_SRC='
import ctypes
import errno
import os
import signal
import sys
import time

# pidfd_send_signal is NOT exported as a glibc wrapper on the fleet
# (verified: CDLL(None) lookup raises undefined symbol on Ubuntu 22.04 /
# glibc 2.35), so it is invoked via raw syscall. The number is 424 on
# EVERY supported arch, x86_64 included: proven empirically on the mgmt
# kernel (6.17 x86_64) where 424 delivers (sig-0 probe returns 0) and 54
# is setsockopt, NOT pidfd_send_signal (it answers ENOTSOCK=88 for a
# pidfd). pidfd_open is 434 on all post-2019 ABIs. Unknown arch ->
# refuse to escalate rather than fall back to a bare kill (round-10
# finding: never signal by bare PID number).
_SIGSYS = {
    "x86_64": 424, "aarch64": 424, "armv7l": 424, "armv6l": 424,
    "i686": 424, "i386": 424, "riscv64": 424, "ppc64le": 424,
}
_OPENSYS = 434
_libc = ctypes.CDLL(None, use_errno=True)


def _fail(msg, rc=1):
    sys.stderr.write("ERROR: " + msg + "\n")
    sys.exit(rc)


def _token(pid):
    # Stable identity: /proc/<pid>/stat field 22 (starttime) plus the
    # NUL-translated cmdline, byte-identical to the bash proc_token()
    # that captured it pre-kill. Field 2 (comm) is parenthesised and may
    # contain spaces or ")", so field 22 is located from the LAST ")" in
    # the line, never by naive whitespace splitting.
    try:
        with open("/proc/%d/stat" % pid, "rb") as fh:
            stat = fh.read()
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            cmd = fh.read()
    except OSError:
        return None
    sep = stat.rfind(b") ")
    if sep < 0:
        return None
    fields = stat[sep + 2:].split(b" ")
    if len(fields) < 20:
        return None
    return fields[19] + b" " + cmd.replace(b"\x00", b"\x02")


def _alive(pid, tok):
    # Survivor predicate used by both the acquire bracket and the wait
    # loop: the PID holds the SAME task only when the captured
    # starttime+cmdline read back identically. Any mismatch (or a
    # vanished/unreadable proc) means the tracked process EXITED, and
    # whatever occupies the PID now must never be signalled.
    if _token(pid) != tok:
        return False
    return True


def _pidfd_open(pid):
    try:
        if hasattr(os, "pidfd_open"):
            try:
                return os.pidfd_open(pid, 0)
            except OSError as exc:
                if exc.errno in (errno.ESRCH, errno.EINVAL):
                    return None
                raise
        rc = _libc.syscall(_OPENSYS, pid, ctypes.c_uint(0))
        if rc < 0:
            err = ctypes.get_errno()
            if err in (errno.ESRCH, errno.EINVAL):
                return None
            raise OSError(err, "pidfd_open")
        return rc
    except OSError as exc:
        _fail("pidfd_open(%d) failed: %s; refusing to signal by bare PID"
              % (pid, exc))


def _pidfd_kill(fd, sig):
    # Signals the HELD handle, not a PID number: if the tracked task has
    # already exited (and even if its PID was long recycled), this can
    # only ESRCH - it can never reach a replacement process.
    num = _SIGSYS.get(os.uname().machine)
    if num is None:
        raise RuntimeError("pidfd_send_signal syscall unknown for arch "
                           + os.uname().machine)
    rc = _libc.syscall(num, fd, sig, None, 0)
    if rc < 0:
        err = ctypes.get_errno()
        if err == errno.ESRCH:
            return "esrch"
        raise OSError(err, "pidfd_send_signal")
    return "ok"


def _acquire(pid, tok):
    # verify -> open -> verify. If the PID occupant changed at ANY point
    # in that sequence the two token reads bracketing pidfd_open cannot
    # both equal the captured token (a reused PID carries a different
    # starttime/cmdline), so the fd we keep is tied to the very
    # task_struct whose identity we validated. None = treat as exited.
    if not _alive(pid, tok):
        return None
    fd = _pidfd_open(pid)
    if fd is None:
        return None
    if not _alive(pid, tok):
        os.close(fd)
        return None
    return fd


def _widen_window():
    # TEST SEAM ONLY (PR#13 round-10 blocking finding). Production never
    # sets these variables, so this is a no-op in the role. The harness
    # sets KANBAN_UI_POC_TEST_ESCALATE_MARK to a path and, to make the
    # check-to-signal race deterministic, KANBAN_UI_POC_TEST_WINDOW_S to
    # a float. When the mark path is set the helper touches it AT the
    # exact check-to-signal point (after survivors were validated,
    # before any escalation signal is delivered) and then waits the
    # window, so the harness can exit the validated process and install
    # an unrelated tenant at the recycled PID between the check and the
    # signal - the precise window the round-10 finding says a bare-PID
    # escalation cannot close and a held pidfd handle can. The seam
    # never changes WHICH handle/number is signalled, only when.
    mark = os.environ.get("KANBAN_UI_POC_TEST_ESCALATE_MARK")
    if not mark:
        return
    try:
        with open(mark, "w"):
            pass
        window = float(os.environ.get("KANBAN_UI_POC_TEST_WINDOW_S", "0"))
    except ValueError:
        return
    if window > 0:
        time.sleep(window)


def _read_pairs(path):
    # NUL-pair framing is POSITIONAL (round-11 finding): the writer emits
    # exactly two fields per tracked process, so dropping empty fields
    # here would let one empty token shift EVERY later pair and pair a
    # live pid with a foreign token. Split positionally, tolerate only
    # the trailing NUL the writer always appends, and refuse loudly
    # anything that is not whole pairs of non-empty fields (the hunt
    # guarantees non-empty tokens; a malformed file means a bug).
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw.endswith(b"\x00"):
        raw = raw[:-1]
    parts = raw.split(b"\x00") if raw else []
    if len(parts) % 2 != 0:
        _fail("malformed pairs file: odd number of NUL-separated fields")
    pairs = []
    for i in range(0, len(parts), 2):
        if not parts[i] or not parts[i + 1]:
            _fail("malformed pairs file: empty pid or token field at "
                  "pair %d" % (i // 2))
        pairs.append((int(parts[i]), parts[i + 1]))
    return pairs


def _knob(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        _fail("invalid " + name, 2)


def main(argv):
    if len(argv) != 3 or argv[1] not in ("term", "wait"):
        _fail("usage: poc_exit_helper term|wait <pairs-file>", 2)
    pairs = _read_pairs(argv[2])
    if not pairs:
        return 0
    grace = _knob("KANBAN_UI_POC_GRACE_S", 10)
    kill_s = _knob("KANBAN_UI_POC_KILL_S", 5)
    tracked = []
    for pid, tok in pairs:
        fd = _acquire(pid, tok)
        if fd is None:
            # Already exited or the PID was recycled pre-signal: never
            # touch whatever occupies it now.
            continue
        tracked.append((pid, tok, fd))
        if argv[1] == "term":
            try:
                _pidfd_kill(fd, signal.SIGTERM)
            except (OSError, RuntimeError) as exc:
                _fail("cannot deliver SIGTERM via pidfd for pid %d: %s"
                      % (pid, exc))
            print("killing POC pid=%d" % pid, flush=True)
    try:
        deadline = time.monotonic() + grace
        killed9 = False
        while True:
            survivors = [(pid, fd) for (pid, tok, fd) in tracked
                         if _alive(pid, tok)]
            if not survivors:
                return 0
            now = time.monotonic()
            if not killed9 and now >= deadline:
                ids = " ".join(str(p) for p, _ in survivors)
                sys.stderr.write(
                    "POC pid(s) %s ignored SIGTERM after %ds; escalating "
                    "to SIGKILL\n" % (ids, grace))
                sys.stderr.flush()
                # TEST SEAM: with the seam unset this returns instantly.
                # When driven by the harness it marks AND pauses AT the
                # check-to-signal point below (survivors already
                # validated), which is where a bare-PID escalation loses
                # the race and a held pidfd handle does not.
                _widen_window()
                try:
                    for kpid, kfd in survivors:
                        _pidfd_kill(kfd, signal.SIGKILL)  # ESCALATION_SEAM_LINE
                except (OSError, RuntimeError) as exc:
                    _fail("cannot escalate via pidfd: %s; refusing to "
                          "signal by bare PID" % exc)
                killed9 = True
                deadline = now + kill_s
            elif killed9 and now >= deadline:
                ids = " ".join(str(p) for p, _ in survivors)
                _fail("POC process(es) %s still alive after SIGKILL; "
                      "refusing to report a stopped POC" % ids)
            time.sleep(0.2)
    finally:
        for _, _, fd in tracked:
            try:
                os.close(fd)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main(sys.argv))
'

# Test seam (PR#13 round-10 blocking finding): dump the embedded python
# helper verbatim and exit. Checked BEFORE the required-variable
# validation so the harness can extract the helper without production
# knobs. Never set in production; the role always executes the script
# without it, which skips straight to the validation below.
if [ "${STOP_POC_PY_DUMP:-0}" = "1" ]; then
  printf '%s\n' "$POC_HELPER_SRC"
  exit 0
fi

: "${KANBAN_UI_PORT:?KANBAN_UI_PORT must be set}"
: "${KANBAN_UI_POC_DIR:?KANBAN_UI_POC_DIR must be set}"
# Bounded wait knobs (seconds); overridable via environment, validated numeric.
: "${KANBAN_UI_POC_GRACE_S:=10}"
: "${KANBAN_UI_POC_KILL_S:=5}"
: "${KANBAN_UI_POC_PORT_WAIT_S:=10}"
for v in KANBAN_UI_POC_GRACE_S KANBAN_UI_POC_KILL_S KANBAN_UI_POC_PORT_WAIT_S; do
  case "${!v}" in
    (*[!0-9]*|'') echo "invalid $v" >&2; exit 2 ;;
  esac
done

case "$KANBAN_UI_PORT" in
  (*[!0-9]*|'') echo "invalid port" >&2; exit 2 ;;
esac

# proc_token PID — capture a STABLE process identity for a running pid:
# the starttime field (field 22) of /proc/<pid>/stat plus the full NUL
# -joined cmdline. A bare PID number is NOT a process identity: PIDs are
# recycled, so /proc/<pid> existing (or matching anything) later in the
# wait window can describe a brand-new, unrelated process (PR#13
# round-9 blocking finding on review 4369296896). Field 2 (comm) is
# parenthesised and may itself contain spaces or ')', so field 22 is
# located by counting backwards from the LAST ')' rather than by naive
# whitespace splitting. Empty return = proc vanished right now (treat
# the process as exited).
proc_token() {
  local pid=$1 stat rest i tok
  stat=$(cat "/proc/$pid/stat" 2>/dev/null) || return 0
  rest=${stat##*) }                       # everything after comm's last ')'
  # rest begins at field 3 (state); starttime is field 22 = 20th token.
  for ((i = 0; i < 19; i++)); do
    tok=${rest%% *}
    [ "$tok" = "$rest" ] && return 0      # unexpected shape -> treat as exited
    rest=${rest#* }
  done
  tok=${rest%% *}
  local cmd
  cmd=$(tr '\0' '\2' < "/proc/$pid/cmdline" 2>/dev/null) || return 0
  printf '%s %s' "$tok" "$cmd"
}

# identity_matches PID TOKEN — the survivor check used by the no-python3
# fallback below. PID is the SAME original process only when the captured
# starttime AND cmdline still read back identically. Any mismatch
# (starttime changed, cmdline changed, proc gone, unreadable) means the
# original process EXITED and the PID must be treated as released, never
# escalated. The cmdline component is not redundant with starttime: it
# also protects against the sub-jiffy capture window, where a
# fast-exiting process could be replaced before starttime was sampled; a
# replacement cannot replicate the POC cmdline AND cwd captured pre-kill.
identity_matches() {
  local pid=$1 token=$2 cur
  cur=$(proc_token "$pid") || return 1
  [ -n "$cur" ] || return 1
  [ "$cur" = "$token" ]
}

# Test seam (PR#13 round-11 blocking finding): production never sets
# these, so all are no-ops in the role. The harness sets MARK_PATH to a
# path and WINDOW_S to a number of seconds (float allowed) to make the
# identity-binding races deterministic: the FALLBACK seam pauses inside
# wait_for_exit after identity_matches succeeded but BEFORE the guard
# decides, and the HUNT seam pauses inside the hunt after every selection
# check passed but BEFORE the identity token is read and bound. The
# seams only change WHEN the code proceeds, never WHAT is selected or
# signalled, so the harness can recycle the tracked PID inside the exact
# window the round-11 finding names. Mirrors
# KANBAN_UI_POC_TEST_ESCALATE_MARK/_WINDOW_S in the embedded helper.
poc_test_widen() {  # MARK_PATH WINDOW_S
  local mark=$1 w=$2
  [ -n "$mark" ] || return 0
  : > "$mark" 2>/dev/null || true
  case "$w" in
    (*[!0-9.]*|'') w=0 ;;
  esac
  [ "$w" != "0" ] && sleep "$w"
  return 0
}

# wait_for_exit PAIRS... — bounded graceful-shutdown wait (PR#13 review
# 4369296896), hardened against PID reuse (round-9), made race-free for
# signalling (round-10), and made identity-bound before any signal
# (round-11). PAIRS is a flat "pid token pid token ..." list captured
# BEFORE any signal. Primary path: hand the pairs to the embedded Python
# helper, which validates the token before AND after pidfd_open and then
# delivers SIGTERM and the escalation SIGKILL exclusively through the
# held pidfd handle, so a validated identity is never converted back
# into a bare PID number for signalling. The bash fallback runs only
# when python3 is genuinely unavailable: without a race-free handle NO
# check-then-signal sequence is identity-safe, so it sends NO signal at
# all (not even SIGTERM) and fails safely before touching a bare PID.
# Exit 0 = all matched processes gone; exit 1 = a POC process could not
# be stopped safely (caller must not start Compose over it).
wait_for_exit() {
  local it=("$@") i p tok file py rc
  if command -v python3 >/dev/null 2>&1; then
    file=$(mktemp "${TMPDIR:-/tmp}/stop-poc-pairs.XXXXXX") \
      || { echo "ERROR: mktemp failed for POC pairs" >&2; return 1; }
    py=$(mktemp "${TMPDIR:-/tmp}/stop-poc-helper.XXXXXX") \
      || { rm -f "$file"; echo "ERROR: mktemp failed for POC helper" >&2; return 1; }
    chmod 600 "$file" "$py"
    printf '%s\n' "$POC_HELPER_SRC" > "$py"
    : > "$file"
    for ((i = 0; i < ${#it[@]}; i += 2)); do
      printf '%s\0%s\0' "${it[i]}" "${it[i + 1]}" >> "$file"
    done
    if python3 "$py" term "$file"; then
      rc=0
    else
      rc=$?
    fi
    rm -f "$file" "$py"
    return "$rc"
  fi
  # Fallback (python3 absent): this path sends NO signal whatsoever
  # (round-11 blocking finding). identity_matches returning true only
  # proves the PID holds the tracked task AT THAT INSTANT; the tracked
  # task can exit and its PID be recycled before any later `kill "$p"`,
  # so revalidation cannot make a bare-PID SIGTERM identity-safe. The
  # only fail-safe action is to refuse BEFORE signalling: loud ERROR,
  # return 1, and the caller must not start Compose over a live POC.
  for ((i = 0; i < ${#it[@]}; i += 2)); do
    p=${it[i]}
    tok=${it[i + 1]}
    if identity_matches "$p" "$tok"; then
      # TEST SEAM (no-op in production): pauses in the exact
      # check-to-signal window the round-11 finding names — after
      # identity_matches succeeded, where the OLD fallback used to
      # `kill "$p"` next. The fixed shape signals nothing here at all;
      # the sensitivity shape restores the bare-PID kill at this point.
      poc_test_widen "${STOP_POC_TEST_FALLBACK_MARK:-}" "${STOP_POC_TEST_FALLBACK_WINDOW_S:-0}"
      # FALLBACK_GUARD_LINE
      echo "ERROR: POC pid $p still running and python3 is unavailable for race-free pidfd signalling; refusing to signal any bare PID (not even SIGTERM)" >&2
      return 1
    fi
  done
  return 0
}

# port_not_listening — port-release gate. Primary check is `ss -ltn`
# filtered to ":<port> "; if ss is unavailable, fall back to
# /proc/net/tcp + /proc/net/tcp6 (LISTEN is state 0A) so the script
# keeps its guarantee on a bare host. Exit 0 = released; exit 1 = still
# in LISTEN at timeout, reported clearly instead of racing Compose.
port_not_listening() {
  local deadline=$(( $(date +%s) + KANBAN_UI_POC_PORT_WAIT_S )) now
  while :; do
    if command -v ss >/dev/null 2>&1; then
      # Match the Local Address:Port COLUMN ONLY (field 4 after the
      # header): a blanket ":port " grep can false-positive on a remote
      # peer port in field 5 and spin the wait to its timeout. `|| true`
      # guards SIGPIPE-style early grep exits under pipefail (round-7
      # finding 4343269737 class).
      now=$(ss -ltn 2>/dev/null | awk -v p=":${KANBAN_UI_PORT}$" 'NR>1 && $4 ~ p {print "yes"; exit}' || true)
    else
      # Hex port compare on local_address (field 2), LISTEN = state 0A.
      now=$( { awk -v p="$(printf '%04X' "$KANBAN_UI_PORT")" '$4=="0A" { split($2,a,":"); if (a[2]==p) { print "yes"; exit } }' /proc/net/tcp /proc/net/tcp6 2>/dev/null || true; } )
    fi
    if [ -z "$now" ]; then
      return 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "ERROR: port $KANBAN_UI_PORT is still in LISTEN state after ${KANBAN_UI_POC_PORT_WAIT_S}s; the POC (or something else) never released the socket" >&2
      return 1
    fi
    sleep 0.5
  done
}

# Test seam (PR#13 round-9 blocking finding): when STOP_POC_LIB_ONLY=1
# the caller SOURCES this script and gets ONLY the function definitions
# above, so the test suite can drive wait_for_exit/identity_matches/
# proc_token against orchestrated PID-reuse scenarios without running
# the hunt below. Never set in production; the role always executes the
# script (bash stop_poc.sh), which ignores this and runs the hunt.
if [ "${STOP_POC_LIB_ONLY:-0}" = "1" ]; then
  # shellcheck disable=SC2317  # reached only when SOURCED via the test seam
  return 0 2>/dev/null || true
fi

killed=0
pairs=()
for pid_dir in /proc/[0-9]*; do
  pid="${pid_dir##*/}"
  [ -r "$pid_dir/cmdline" ] || continue
  # HUNT BRACKET (round-11 blocking finding), read 1: capture the
  # stable identity BEFORE any selection predicate touches /proc. Every
  # later read must return this exact token or the candidate is
  # discarded, so the (pid, token) pair handed to the helper can only
  # describe the very task whose cmdline/port/cwd passed selection. A
  # PID recycled anywhere inside the hunt reads a different
  # starttime/cmdline and is rejected before it can be paired.
  tok_pre="$(proc_token "$pid")"  # BRACKET_PRE
  [ -n "$tok_pre" ] || continue  # BRACKET_PRE_CHECK
  # NUL-delimited argv -> one arg per line
  cmd="$(tr '\0' '\n' < "$pid_dir/cmdline" 2>/dev/null || true)"
  [ -n "$cmd" ] || continue
  # argv[0] must be a python interpreter, and the argv must carry
  # server.py plus the exact --lan --port <port> flags.
  # First line via parameter expansion, NOT `printf | head -n 1`: under
  # `set -euo pipefail` head exits after line 1 and printf takes SIGPIPE
  # (141) once the remaining cmdline exceeds the 64KiB pipe buffer, which
  # the role's failed_when turns into a fatal deploy abort while merely
  # inspecting an unrelated process (round-7 finding 4343269737).
  first="${cmd%%$'\n'*}"
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
  # Exact-line argv membership via case on the NUL-translated string,
  # NOT `printf | grep -qx`: grep -q exits on first match, so with a
  # command line larger than the pipe buffer printf takes SIGPIPE (141)
  # under pipefail and `|| continue` silently skips the real POC (same
  # round-7 finding 4343269737 class as the `| head -n 1` above).
  case $'\n'"$cmd"$'\n' in
    (*$'\n'server.py$'\n'*) : ;;
    (*) continue ;;
  esac
  case $'\n'"$cmd"$'\n' in
    (*$'\n'--lan$'\n'*) : ;;
    (*) continue ;;
  esac
  [ "$port_seen" -eq 1 ] || continue
  cwd="$(readlink "$pid_dir/cwd" 2>/dev/null || true)"
  [ "$cwd" = "$KANBAN_UI_POC_DIR" ] || continue
  # HUNT BRACKET, read 2: the cmdline and cwd predicates above must
  # still describe the SAME task that tok_pre identified. Any change
  # (recycled PID, vanished proc) rejects the candidate outright.
  tok_mid="$(proc_token "$pid")"  # BRACKET_MID
  [ "$tok_mid" = "$tok_pre" ] || continue  # BRACKET_MID_CHECK
  # TEST SEAM (no-op in production): pauses exactly in the window the
  # round-11 finding names — after successful selection, before the
  # identity is bound to the pair handed to the helper.
  poc_test_widen "${STOP_POC_TEST_HUNT_MARK:-}" "${STOP_POC_TEST_HUNT_WINDOW_S:-0}"
  # HUNT BRACKET, read 3 + binding: capture the stable identity ONLY
  # while the PID is proven (twice, bracketing this read) to still be
  # the checked task, and do NOT kill from the hunt — signalling
  # happens exclusively inside wait_for_exit via held pidfd handles.
  # The helper re-validates this same token before AND after
  # pidfd_open, so selection predicates and signalled task_struct are
  # bound to one identity end to end (round-11 blocking finding on
  # review 5641033023).
  token="$(proc_token "$pid")"  # BRACKET_BIND
  [ "$token" = "$tok_pre" ] || continue  # BRACKET_BIND_CHECK
  tok_post="$(proc_token "$pid")"  # BRACKET_POST
  [ "$tok_post" = "$tok_pre" ] || continue  # BRACKET_POST_CHECK
  pairs+=("$pid" "$token")
  killed=$((killed + 1))
done

if [ "$killed" -gt 0 ]; then
  # Bounded graceful-shutdown wait, then port-release gate (finding
  # 4369296896). Both must pass before this helper may report success:
  # otherwise the compose build/start tasks below race the dying
  # listener and fail with an address-in-use error when the image is
  # already present (no build delay to absorb the race). NOTE: the port
  # gate is ONLY run on this path. On a routine redeploy nothing is
  # killed because the managed container itself legitimately holds the
  # port; failing there would break every converge run.
  wait_for_exit "${pairs[@]}"
  port_not_listening
  echo "killed $killed"
else
  echo "no POC process found"
fi
exit 0
