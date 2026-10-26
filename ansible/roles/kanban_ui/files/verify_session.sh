#!/bin/bash
# Deploy-time end-to-end session verification for kanban-ui.
#
# Invoked via ansible.builtin.script with no_log; EVERY variable arrives
# through the environment (never interpolated into shell syntax). The
# playbook gate is this script's exit code; stdout carries booleans and
# HTTP statuses only - never the token, never response bodies.
#
# Credential-transport rules (round-2 review findings 1, 2, 4;
# hardened by ROUND-7 finding 3):
#   - The bootstrap token and the session cookie NEVER appear in any
#     process argv. Mechanism note: the task `environment:` block is
#     delivered by the remote wrapper as
#     `/bin/sh -c KEY=value ... python3 AnsiballZ_command.py`, so ANY
#     credential passed via task environment lands in that sh's argv
#     and the watcher below correctly flags it. The token therefore
#     arrives as a PATH (KANBAN_UI_VERIFY_TOKEN_FILE -> the deployed
#     0400 token file, readable through the play's become:root) and is
#     loaded with the forkless builtin read below; every other env
#     value here is credential-free. curl reads the login body from a
#     0600 file and takes secret-bearing URLs/headers from 0600
#     --config files; every match against the token uses bash builtin
#     [[ ]] conditionals, never grep pattern arguments. Builtins
#     (printf, [[, redirection, read) do not fork, so /proc never sees
#     the secret.
#   - The verification base MUST be the HTTPS SWAG URL. This script
#     ABORTS (exit 1, not fail-and-continue) against a non-https base,
#     BEFORE mktemp, before the watcher starts, and before a single
#     request is constructed - mirroring the deployed Secure session
#     cookie; there is no plaintext credential/session fallback.
#     (Round-2 review finding 4301554415: the old fail()-and-continue
#     ordering still reached the step-5 login POST in plaintext.)
#   - All artifacts live in a private mktemp -d directory (mode 700,
#     removed by an EXIT trap). The play runs with become: true, so no
#     predictable /tmp paths may be written.
#
# Verifies (live):
#   0. the verification base is HTTPS (hard abort before ANY request or
#      token read, never fail-and-continue - finding 4301554415)
#   1. anonymous /api/health is 401 (session gate enforcing)
#   2. anonymous index is 200 and carries NO token material
#   3. static assets (app.js/app.css/md.js) serve 200 and app.js is
#      credential-free with no ?token= WS transport
#   4. a bad login is rejected (401)
#   5. the correct login mints an HttpOnly SameSite=strict Secure cookie
#   6. the board renders with the session cookie AND the response is a
#      JSON board envelope (200 + application/json + valid JSON with a
#      non-empty `columns` array of {name,tasks} plus tenants/assignees/
#      latest_event_id) - any-200 fallback pages fail (PR#13 review
#      finding 4373043552)
#   7. mutations without the session marker header are 403 (CSRF guard),
#      and with it the request passes the gate (handler-level status)
#   8. the WS upgrade is refused; the REAL token in the URL buys nothing
#   8b. the WS upgrade with the valid session cookie but a FORGED Origin
#       is refused (PR review comment 4244239544 sibling-subdomain gate)
#   9. container logs since this container start are free of token material
#  10. worker-control ops (reclaim / run terminate / reclaim-first reassign)
#      reject a FULL session with the documented 403 body, while the plain
#      reassign reaches its handler and the anon reclaim 401s at the
#      session gate (PR#13 review finding 4301554389)
#  10b. the container-local dispatcher pass (POST /api/kanban/dispatch)
#      rejects a FULL session with the documented 403 body, and the
#      refused tick leaves the DISPATCH GATE BOARD's persisted state
#      fingerprint-identical; the anon tick 401s at the session gate
#      (PR#13 review finding 4323915007; non-mutation measured on an
#      isolated on-disk gate board, not the live /board presentation
#      JSON, per PR#13 review finding 4331497811)
set -u

: "${KANBAN_UI_VERIFY_URL:?}"
: "${KANBAN_UI_VERIFY_TOKEN_FILE:?}"
: "${KANBAN_UI_VERIFY_BOARD:?}"
# Gate-board plumbing for step 10b (all credential-free; see 10b): the
# Hermes home anchoring kanban state (same value the container gets as
# HERMES_KANBAN_HOME), the pinned hermes-agent checkout + its venv
# python (used ONLY by the task-gated fault-injection tick), and the
# numeric ids owning board state (the gate dispatch-lock file is chowned
# to them so the real gateway tick contends on the flock).
: "${KANBAN_UI_KANBAN_HOME:?}"
: "${KANBAN_UI_HERMES_REPO:?}"
: "${KANBAN_UI_HERMES_PY:?}"
# Numeric ids owning board state. The dispatch-lock file must ALREADY be
# owned by the board owner the instant it becomes visible at its final
# path: the pinned _dispatch_tick_lock maps any OSError from open() to
# "acquired" (hermes_cli/kanban_db.py, except OSError -> acquired=True),
# so ANY root-owned window between create and chown lets a gateway tick
# bypass flock contention and write INSIDE the fingerprint window
# (PR#13 round-6d review finding 3). Step 10b therefore publishes the
# lock via an O_EXCL staging file + fchown + os.link: the final path
# goes from absent to board-owned 0644 atomically, with no root-owned
# intermediate state for a tick to trip over.
: "${KANBAN_UI_BOARD_UID:?}"
: "${KANBAN_UI_BOARD_GID:?}"
# Task-gated fault injection, NEVER set in production (see the sibling
# playbook task). Only the literal 'dispatch-once' means anything: it
# runs a real pinned dispatch_once against the gate board between the
# before/after fingerprints, so the gate must FAIL if detection ever
# regresses. Unset/anything else = the branch is dead code. The flake
# evidence for the retired live-board hash lives in
# tests/test_dispatch_nonmutation_gate.py (presentation-drift model).
KANBAN_UI_VERIFY_TEST_INJECT="${KANBAN_UI_VERIFY_TEST_INJECT:-}"
BASE="${KANBAN_UI_VERIFY_URL%/}"

FAIL=0
fail() { echo "FAIL: $1"; FAIL=1; }
# ROUND-2 REVIEW FINDING 4301554415: fail() only sets FAIL=1; execution
# continues to the next line, and the next line may be ANOTHER REQUEST.
# Credential-transport gates therefore use abort(): report, stop the
# already-started argv watcher, and exit nonzero HERE so no subsequent
# request (least of all the real-token login POST) is ever constructed.
abort() {
  echo "ABORT: $1"
  if [ -n "${WATCHER_PID:-}" ]; then
    kill "$WATCHER_PID" 2>/dev/null || true
    wait "$WATCHER_PID" 2>/dev/null || true
  fi
  exit 1
}

# 0. credentials never transit plaintext. This gate runs BEFORE the
# token file is read, before mktemp, before the watcher starts, and
# before a single request is constructed (finding 4301554415: the old
# ordering started the watcher first and merely failed, so a mis-set
# http:// base still reached the step-5 real-token login POST).
case "$BASE" in
  https://*) : ;;
  *) abort "verification base is not https; login must never traverse plaintext";;
esac

# ROUND-7 finding 3: the token arrives as a PATH only (see transport
# rule above). `read` is a builtin: no fork, nothing token-shaped can
# reach any /proc/<pid>/cmdline.
TOK=""
IFS= read -r TOK < "$KANBAN_UI_VERIFY_TOKEN_FILE"
[[ -n "$TOK" ]] || exit 1

WORK=$(mktemp -d) || exit 1
chmod 700 "$WORK"
# Teardown hook for the step-10b disposable gate board (no-op until 10b
# defines the real one; the trap fires on EVERY exit path, including
# abort() and fail-and-continue endings, so a gate board can never be
# orphaned by a later failing step).
gate_teardown() { :; }
trap 'gate_teardown; rm -rf "$WORK"' EXIT

# Live argv-hygiene watcher (round-2 finding 1 regression, proven live).
# Samples every readable /proc/<pid>/cmdline while this script runs and
# flags any argv that carries the bootstrap token. Comparison is a
# builtin [[ ]] against content read via redirection; only the static
# tr/sleep arguments are ever forked, so the watcher itself cannot leak
# the secret either. If the file appears, the playbook gate fails.
(
  trap 'exit 0' TERM INT
  while :; do
    for d in /proc/[0-9]*; do
      [ -r "$d/cmdline" ] || continue
      # $(<file) cannot read /proc (st_size 0), and $(cmd) forks leak;
      # the builtin NUL-delimited read loop is forkless and accurate.
      arg=""
      while IFS= read -r -d '' arg; do
        [[ "$arg" == *"$TOK"* ]] && { echo leak > "$WORK/argv_leak"; exit 0; }
      done 2>/dev/null < "$d/cmdline"
      [[ "$arg" == *"$TOK"* ]] && { echo leak > "$WORK/argv_leak"; exit 0; }
    done
    sleep 0.1 || exit 0
  done
) &
WATCHER_PID=$!

# (step 0, the https gate, already ran above before the token was even
# read - finding 4301554415; a non-https base can no longer reach here)

# 1. anonymous API gate
C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$BASE/api/health")
[ "$C" = "401" ] || fail "anon /api/health expected 401, got $C"

# 2. index has no credential ([[ ]] builtin keeps $TOK out of argv)
BODY=$(curl -s --max-time 15 "$BASE/")
[ -n "$BODY" ] || fail "index returned empty body"
grep -q 'Kanban' <<<"$BODY" || fail "index missing app shell"
[[ "$BODY" == *"$TOK"* ]] && fail "index discloses the bootstrap token"

# 3. static assets (all writes inside the private $WORK dir)
for asset in app.js app.css md.js; do
  C=$(curl -s -o "$WORK/$asset" -w '%{http_code}' --max-time 15 "$BASE/$asset")
  [ "$C" = "200" ] || fail "$asset expected 200, got $C"
  asset_body=$(cat "$WORK/$asset")
  [[ "$asset_body" == *"$TOK"* ]] && fail "$asset discloses the bootstrap token"
done
grep -qE 'ws.*[?&]token=' "$WORK/app.js" && fail "app.js still transports credentials in WS URLs"

# 4. bad login rejected (probe body is static text, safe as an argument)
C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST "$BASE/login" \
  -H 'Content-Type: application/json' --data '{"token":"***"}')
[ "$C" = 401 ] || [ "$C" = 429 ] || fail "bad login expected 401, got $C"

# 4b. round-3 finding 1 + round-4/5 finding 1 regression: a DIRECT
# plaintext-HTTP hit on the published port must be refused BEFORE
# credential validation, even when the attacker FORGES the trust signals:
#   (i)  no headers at all          -> app gate 403 (or 000 unreachable)
#   (ii) X-Forwarded-Proto: https   -> the round-4 bypass attempt; the
#        proxy marker is absent AND the real TCP peer is this host, not
#        the proxy -> 403 (or 000)
#   (iii) forged proto + a bogus marker value -> same, 403 (or 000):
#        the peer-address gate still refuses (round-5)
# The trust signal is now the vaulted X-Kanban-Proxy-Marker the SWAG
# vhost injects (proxy_set_header REPLACES client-supplied copies); the
# UFW source rule may additionally make the port unreachable from this
# host - curl then reports 000. Steps 1-4 already proved the app is live
# over the HTTPS proxy path, so 000 here cannot mean "container down".
# What is FORBIDDEN is any credential-processing status (401/429/200) on
# any of these direct probes. All bodies are static probe text - never
# the real token - so the checks themselves carry no credential.
# KANBAN_UI_DIRECT_URL is the http:// base the deployment publishes; it
# is credential-free to probe (the bogus marker value is a static string,
# not the real secret).
: "${KANBAN_UI_DIRECT_URL:?}"
case "$KANBAN_UI_DIRECT_URL" in
  http://*) : ;;
  # Same continue-after-fail hazard as finding 4301554415: this is the
  # gate in front of three direct POSTs. A malformed base must stop the
  # script, not fall through and fire the probes at an unknown target.
  *) abort "KANBAN_UI_DIRECT_URL must be the plaintext direct base (sanity of this probe)";;
esac
for probe_name in plain spoofed-proto spoofed-proto-and-bogus-marker; do
  hdr_args=()
  case "$probe_name" in
    plain) ;;
    spoofed-proto) hdr_args=(-H 'X-Forwarded-Proto: https') ;;
    spoofed-proto-and-bogus-marker)
      hdr_args=(-H 'X-Forwarded-Proto: https' -H 'X-Kanban-Proxy-Marker: bogus-static-marker-not-the-secret') ;;
  esac
  C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST "$KANBAN_UI_DIRECT_URL/login" \
    "${hdr_args[@]}" -H 'Content-Type: application/json' --data '{"token":"***"}')
  case "$C" in
    403) : ;;                                # app gate rejected it
    000) : ;;                                # network layer: port unreachable
    *) fail "direct-HTTP login ($probe_name) expected 403/000 transport rejection, got $C";;
  esac
done
# Contrast: step 5 below logs in SUCCESSFULLY over the HTTPS proxy path
# (where SWAG stamps the real marker), proving the gate is trust-based,
# not a blanket shutdown.

# 5. good login mints a hardened cookie. The token JSON is assembled by
# the printf/parameter-expansion builtins (no fork) into a 0600 file
# inside $WORK and handed to curl via @file, so the secret never touches
# argv or any world-readable path. The header dump arrives on stdout ->
# HDRS variable only.
esc=${TOK//\\/\\\\}
esc=${esc//\"/\\\"}
( umask 077; printf '{"%s":"%s"}' 'token' "$esc" > "$WORK/login.json" )
HDRS=$(curl -s -D - -o /dev/null --max-time 15 -X POST "$BASE/login" \
  -H 'Content-Type: application/json' --data-binary "@$WORK/login.json")
STATUS=$(grep -oE 'HTTP/[0-9.]+ [0-9]+' <<<"$HDRS" | tail -1 | grep -oE '[0-9]+$')
# Finding 4301554415 audit: the login itself failed or minted suspect
# session material. fail() here would keep executing and fire every
# remaining cookie-bearing request (steps 6-10) against the live app;
# these gates abort instead.
[ "$STATUS" = "200" ] || abort "login expected 200, got $STATUS"
grep -qi 'set-cookie: kanban_ui_session=[^;]*;.*httponly' <<<"$HDRS" || abort "session cookie is not HttpOnly"
grep -qi 'set-cookie: kanban_ui_session=.*samesite=strict' <<<"$HDRS" || abort "session cookie is not SameSite=strict"
grep -qi 'set-cookie: kanban_ui_session=[^;]*;.*secure' <<<"$HDRS" || abort "session cookie is not Secure"
SESS=$(grep -io 'set-cookie: kanban_ui_session=[^;]*' <<<"$HDRS" | head -1 | cut -d= -f2)
[ -n "$SESS" ] || abort "no session cookie issued"

# Cookie-bearing requests: the session value goes into a 0600 curl
# --config file via printf (builtin); only the file path is argv.

# 6. board renders with the cookie AND is the real board API envelope.
# PR#13 review finding 4373043552: the old check threw the body away and
# accepted any HTTP 200, so a SWAG/HTML fallback page or any
# proxy-generated success page would have declared the board read
# verified without the board API ever working. Capture status + content
# type + body, then require: 200, a JSON content type, valid JSON, and
# the pinned plugin's envelope - a `columns` array whose entries each
# carry a string `name` and a `tasks` array, plus the sibling top-level
# keys the pinned get_board returns (tenants, assignees,
# latest_event_id). The body is written into the private 700 $WORK dir
# and only its shape is ever reported; nothing token-shaped can appear
# in the board presentation JSON, and the token-leak watcher stays live
# across this step regardless.
{
  printf 'header = "Cookie: kanban_ui_session=%s"\n' "$SESS"
  printf 'url = "%s/api/kanban/board?board=%s"\n' "$BASE" "$KANBAN_UI_VERIFY_BOARD"
} > "$WORK/board.conf"
chmod 600 "$WORK/board.conf"
# -D writes the response headers to a 0600 file (mode forced explicitly
# so a pre-existing path can never widen); the body goes to its own file,
# never stdout, per the script's stdout-carries-no-bodies rule.
C=$(curl -s -o "$WORK/board.json" -D "$WORK/board.hdrs" -w '%{http_code}' \
  --max-time 60 --config "$WORK/board.conf" -H 'X-Kanban-UI: 1')
[ "$C" = "200" ] || fail "board read with session expected 200, got $C"
# Content type must name JSON, with any parameters (charset) stripped.
# curl -D writes CRLF-terminated headers, so \r must be stripped too
# (caught by the harness: a real application/json response otherwise
# compares as 'application/json\r' and false-fails).
CTYPE=$(grep -i '^content-type:' "$WORK/board.hdrs" | tail -1 | cut -d: -f2- \
  | cut -d';' -f1 | tr -d ' \t\r' | tr '[:upper:]' '[:lower:]')
case "$CTYPE" in
  application/json|text/json) : ;;
  *) # The raw content type is RESPONSE data: never echo it into the
     # fail message (a malicious endpoint could smuggle the session
     # value or token material into a Content-Type header and have the
     # verifier reprint it into the playbook log). Fixed text only.
     fail "board read returned a non-JSON content type, expected application/json (fallback page?)" ;;
esac
# Envelope validation. The JSON is opened by path inside python (argv
# only ever carries the static file path), and the script is
# passed via a quoted heredoc, so no response content ever reaches a
# process command line. python prints exactly one fixed verdict token
# (OK/INVALID) captured into a variable; stderr is dropped so a python
# traceback can never echo body content into the playbook log.
BOARD_VERDICT=$(python3 - "$WORK/board.json" <<'BOARD_ENVELOPE_PY' 2>/dev/null
import json, sys

try:
    with open(sys.argv[1], "r", encoding="utf-8", errors="strict") as fh:
        data = json.load(fh)
except Exception:
    # One generic verdict line: the body itself must never be echoed.
    print("INVALID")
    sys.exit(0)

ok = isinstance(data, dict)
cols = data.get("columns") if ok else None
ok = ok and isinstance(cols, list) and len(cols) > 0
if ok:
    for c in cols:
        if (not isinstance(c, dict)) or not isinstance(c.get("name"), str) \
                or not isinstance(c.get("tasks"), list):
            ok = False
            break
ok = ok and all(k in data for k in ("tenants", "assignees", "latest_event_id"))
print("OK" if ok else "INVALID")
BOARD_ENVELOPE_PY
)
[ "$BOARD_VERDICT" = "OK" ] \
  || fail "board read returned HTTP 200 but not the board/columns JSON envelope (fallback page?)"

# 7. CSRF guard: cookie alone cannot mutate; cookie+header passes the gate
{
  printf 'header = "Cookie: kanban_ui_session=%s"\n' "$SESS"
  printf 'url = "%s/api/kanban/tasks/kanban-ui-probe/comments?board=%s"\n' \
    "$BASE" "$KANBAN_UI_VERIFY_BOARD"
} > "$WORK/csrf.conf"
chmod 600 "$WORK/csrf.conf"
C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST \
  --config "$WORK/csrf.conf" \
  -H 'Content-Type: application/json' --data '{"body":"probe","author":"probe"}')
[ "$C" = "403" ] || fail "mutation without marker header expected 403, got $C"
C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST \
  --config "$WORK/csrf.conf" -H 'X-Kanban-UI: 1' \
  -H 'Content-Type: application/json' --data '{"body":"probe","author":"probe"}')
# handler rejects the unknown task (4xx) but the auth/CSRF gate let it through
[ "$C" != "401" ] && [ "$C" != "403" ] || fail "marker-header request blocked by gate: $C"

# 8. WS upgrade: the REAL token in the URL must buy nothing. The URL is
# written into a 0600 curl --config file (printf builtin), never argv.
{
  printf 'url = "%s/api/kanban/%s?board=%s&%s=%s"\n' \
    "$BASE" 'events' "$KANBAN_UI_VERIFY_BOARD" 'token' "$TOK"
  printf 'header = "Upgrade: websocket"\nheader = "Connection: Upgrade"\n'
  printf 'header = "Sec-WebSocket-Version: 13"\nheader = "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ=="\n'
} > "$WORK/ws.conf"
chmod 600 "$WORK/ws.conf"
C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 --http1.1 \
  --config "$WORK/ws.conf")
[ "$C" != "101" ] || fail "WS upgrade accepted with a URL token"

# 8b. PR-review finding (comment 4244239544): the WS gate is cookie AND
# exact-Origin. Replay the legitimate session cookie (minted in step 5,
# exactly what a compromised sibling subdomain's JS gets, because
# SameSite is site-scoped) behind a FORGED upgrade Origin and require
# rejection. Cookie-only acceptance was the finding; 101 here fails.
# The correct-Origin leg cannot be exercised from here: curl cannot
# complete a WS handshake, and a live 101 probe would need a real
# browser. Its evidence is threefold: (a) the sibling probe below is the
# SAME request with only the Origin changed, so an Origin-blind gate
# would answer 101 to it too; (b) the operator's live browser tab keeps
# its event stream after this deploy (dashboard live tail watched
# green); (c) the pure predicate is locked by tests/test_ws_origin.py.
{
  printf 'url = "%s/api/kanban/%s?board=%s"\n' "$BASE" 'events' "$KANBAN_UI_VERIFY_BOARD"
  printf 'header = "Cookie: kanban_ui_session=%s"\n' "$SESS"
  printf 'header = "Origin: https://sibling-not-the-kanban-origin.invalid"\n'
  printf 'header = "Upgrade: websocket"\nheader = "Connection: Upgrade"\n'
  printf 'header = "Sec-WebSocket-Version: 13"\nheader = "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ=="\n'
} > "$WORK/ws-origin.conf"
chmod 600 "$WORK/ws-origin.conf"
C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 --http1.1 \
  --config "$WORK/ws-origin.conf")
[ "$C" != "101" ] || fail "WS upgrade accepted with a forged Origin and a replayed session cookie"

# 10. PR#13 review finding 4301554389: worker-control operations (task
# reclaim, run terminate, reclaim-first reassign) must be refused
# server-side with the documented 403 body even for a fully
# authenticated session (cookie + marker header), because a
# container-side reclaim releases the claim without signalling the host
# worker (duplicate-dispatch hazard). Unaffected ops must still pass the
# gate: the plain reassign on the same unknown id must NOT carry the
# guard body (over-rejection check), and the anon reclaim still 401s at
# the session gate (auth middleware stays outermost).
wop() {
  local name="$1" path="$2" data="$3"
  {
    printf 'header = "Cookie: kanban_ui_session=%s"\n' "$SESS"
    printf 'url = "%s%s?board=%s"\n' "$BASE" "$path" "$KANBAN_UI_VERIFY_BOARD"
  } > "$WORK/wo_$name.conf"
  chmod 600 "$WORK/wo_$name.conf"
  C=$(curl -s -o "$WORK/wo_$name.body" -w '%{http_code}' --max-time 15 -X POST \
    --config "$WORK/wo_$name.conf" -H 'X-Kanban-UI: 1' \
    -H 'Content-Type: application/json' --data "$data")
  [ "$C" = "403" ] || fail "worker-control $name expected 403, got $C"
  grep -q 'worker control operations are disabled' "$WORK/wo_$name.body" \
    || fail "worker-control $name 403 body is not the documented reject"
}
wop reclaim "/api/kanban/tasks/kanban-ui-probe/reclaim" '{"reason":"probe"}'
wop terminate "/api/kanban/runs/999999/terminate" '{"reason":"probe"}'
wop reassign-reclaim "/api/kanban/tasks/kanban-ui-probe/reassign" '{"profile":"probe","reclaim_first":true}'
# 10b. PR#13 review finding 4323915007: the container-local dispatcher
# pass (POST /api/kanban/dispatch -> dispatch_once -> release_stale_claims
# clearing foreign host claims without signalling) must be refused with
# the documented 403 body for a full session, must 401 anonymously at the
# session gate, and the refused tick must leave persisted kanban state
# untouched (the request never reaches dispatch_once).
# PR#13 review finding 4331497811: non-mutation is NOT measured by hashing
# the live /board response of the shared homelab board. That render embeds
# per-read computed fields (task ages, current-run started-ats) and the
# gateway's embedded dispatcher ticks every live board every 60s, so two
# reads of an UNCHANGED database can legitimately differ and a correctly
# refused dispatch could fail this gate. Instead, non-mutation is measured
# against ISOLATED persisted state: a disposable gate board (unique slug,
# torn down on every exit path) seeded with a sentinel card that a real
# dispatch_once WOULD mutate (expired foreign claim => release_stale_claims
# CAS + bookkeeping events; parked blocked row pins recompute_ready), with
# the fingerprint read DIRECTLY from the board's SQLite file - a refused
# endpoint cannot fake a persisted-state fingerprint the way it could fake
# a presentation render. The board's own .dispatch.lock (the same
# non-blocking advisory lock the pinned dispatcher's _dispatch_tick_lock
# takes; the loser writes nothing) is held for the whole seed->compare
# window so the 60s gateway tick on this board skips instead of injecting
# legitimate reclaim churn between the fingerprints. Task-gated fault
# injection (KANBAN_UI_VERIFY_TEST_INJECT=dispatch-once, never set in
# production) runs the REAL pinned _dispatch_once_locked between the reads
# and inverts the expectation, proving the fingerprint catches a mutation
# rather than trivially passing.
gate_teardown() {
  [ -n "${GATE_SLUG:-}" ] || return 0
  # Primary teardown through the API (the container owns the board dir),
  # best-effort; the root fallback below is unconditional.
  if [ -n "${SESS:-}" ]; then
    {
      printf 'header = "Cookie: kanban_ui_session=%s"\n' "$SESS"
      printf 'url = "%s/api/kanban/boards/%s?delete=true"\n' \
        "$BASE" "$GATE_SLUG"
    } > "$WORK/gate_delete.conf"
    chmod 600 "$WORK/gate_delete.conf"
    curl -s -o /dev/null --max-time 30 -X DELETE \
      --config "$WORK/gate_delete.conf" -H 'X-Kanban-UI: 1' || true
  fi
  # Root fallback: only ever OUR OWN board, and only when its slug
  # matches the exact disposable grammar kanban-ui-verify-<pid>-<epoch>
  # (PR#13 round-6d review finding 2: the retired case pattern
  # 'kanban-ui-verify-[0-9]*' admitted arbitrary suffixes such as
  # kanban-ui-verify-1-production). Never a glob, never an absolute
  # value, never outside the boards root.
  if [[ "$GATE_SLUG" =~ ^kanban-ui-verify-[0-9]+-[0-9]+$ ]]; then
    rm -rf -- "${KANBAN_UI_KANBAN_HOME:?}/kanban/boards/$GATE_SLUG"
  else
    echo "REFUSING cleanup of unexpected gate slug shape: $GATE_SLUG" >&2
  fi
}
GATE_SLUG="kanban-ui-verify-$$-$(date +%s)"
GATE_DIR="${KANBAN_UI_KANBAN_HOME:?}/kanban/boards/$GATE_SLUG"
GATE_DB="$GATE_DIR/kanban.db"
# PR#13 round-6d review finding 2: the previous crash-recovery sweep
# (a root-side find over the boards root, pruning age-stale directories
# with a recursive forced delete) is REMOVED, not merely tightened. Its
# permissive glob admitted unrelated names (e.g.
# kanban-ui-verify-1-production) and its age test could delete a
# legitimate verifier board belonging to a run that was still going (or
# hung) past the age threshold. The only root rm -rf allowed against the
# boards root is gate_teardown above, on this run's OWN exact-grammar
# slug, from the EXIT trap that fires on every non-SIGKILL exit path. An
# orphaned gate board after a SIGKILL is a harmless empty disposable
# dir; it is never swept by a later run because guessing "orphaned" from
# a glob and an mtime is exactly the false-mutation-by-root-deletion
# this step must not contain.
{
  printf 'header = "Cookie: kanban_ui_session=%s"\n' "$SESS"
  printf 'url = "%s/api/kanban/boards"\n' "$BASE"
} > "$WORK/gate_create.conf"
chmod 600 "$WORK/gate_create.conf"
( umask 077; printf '{"%s":"%s"}' 'slug' "$GATE_SLUG" > "$WORK/gate_body.json" )
C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X POST \
  --config "$WORK/gate_create.conf" -H 'X-Kanban-UI: 1' \
  -H 'Content-Type: application/json' --data-binary "@$WORK/gate_body.json")
[ "$C" = "200" ] || fail "gate board creation expected 200, got $C"
# Hold the board's dispatch lock for the whole gate window (same lock the
# pinned dispatcher uses; flock(2) - compatible with _try_lock_nb).
# PR#13 round-6d review finding 3: the lock must NEVER be visible at its
# final path while root-owned. The retired sequence (exec 9> as root ->
# flock -> chown) left a window where the gateway's _dispatch_tick_lock
# open() raised PermissionError, which the pinned code maps to
# acquired=True ("degrade to a no-op so a probe failure never blocks
# dispatch") - a 60s tick landing there entered _dispatch_once_locked
# despite fd 9 and wrote inside the fingerprint window. Publication now
# happens inside one python process: O_EXCL-create the staging file (0600
# via mkstemp), fchown it to the board owner, then os.link it to the
# final path. Between stages the final path is ABSENT (a gateway tick
# there creates it itself, takes the flock, and its legitimate reclaim
# writes are absorbed by the repair-seed below); the instant it exists it
# already carries final 0644 board-owner ownership, so every subsequent
# gateway open is a real flock contention the loser loses. os.link fails
# if the final path exists: on a unique pid+epoch slug that means
# something else owns the lock and we fail closed rather than unlinking
# a lock we never created.
python3 - "$GATE_DB.dispatch.lock" "$KANBAN_UI_BOARD_UID" \
  "$KANBAN_UI_BOARD_GID" <<'GATE_LOCK_PY' > "$WORK/lock_publish.err" 2>&1
import os, stat, sys, tempfile
final, uid, gid = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
d = os.path.dirname(final)
fd, stage = tempfile.mkstemp(prefix=".dispatch.lock.stage-", dir=d)
try:
    os.fchown(fd, uid, gid)
    os.fchmod(fd, 0o644)
    os.close(fd)
    fd = -1
    try:
        os.link(stage, final)
        published = "created"
    except FileExistsError:
        # A legitimate gateway tick created the lock itself in the
        # pre-link window (the final path was absent, so its open was
        # ordinary, never a PermissionError degrade). If it already
        # carries the board owner's uid the contention path stays real
        # and we adopt it; our flock below then simply waits for the
        # tick to finish and the repair-seed absorbs its writes. Any
        # other shape is fail-closed: NEVER unlink a lock we did not
        # create (that is the classic flock double-owner hazard).
        published = "adopted"
finally:
    if fd != -1:
        os.close(fd)
    try:
        os.unlink(stage)
    except FileNotFoundError:
        pass
st = os.stat(final)
assert stat.S_ISREG(st.st_mode), "lock path is not a regular file"
assert st.st_uid == uid, st.st_uid
assert st.st_gid == gid, st.st_gid
if published == "created":
    assert stat.S_IMODE(st.st_mode) == 0o644, hex(stat.S_IMODE(st.st_mode))
else:
    # Adopted (gateway-created): must at least be owner-writable so the
    # gateway's own a+b open and our flock contend on one inode.
    assert stat.S_IMODE(st.st_mode) & 0o200, hex(stat.S_IMODE(st.st_mode))
print(published)
GATE_LOCK_PY
[ $? -eq 0 ] || fail "cannot publish the gate dispatch lock with final ownership"
exec 9<>"$GATE_DB.dispatch.lock" || fail "cannot open the gate dispatch lock"
flock -x 9 || fail "cannot take the gate dispatch lock"
# Seed the sentinel rows directly (the play runs become:root). Fixed
# created_at constants keep the fingerprint reproducible; claim_expires
# is one hour in the past so a REAL dispatch_once pass reclaims the card
# (release_stale_claims: running + foreign claim_lock prefix + expired).
# The seed is a REPAIR-SEED: it deletes any pre-existing gate rows first
# and re-inserts the canonical state. Between board creation and taking
# the lock above, the 60s gateway tick may legitimately have already
# reclaimed the sentinel; the repair runs UNDER the lock, so the
# before-fingerprint always measures the canonical fixture and the
# window after it is write-free.
GATE_SEED_EPOCH=1780000000
GATE_CLAIM_EXPIRES=$(( $(date +%s) - 3600 ))
python3 - "$GATE_DB" "$GATE_SEED_EPOCH" "$GATE_CLAIM_EXPIRES" <<'GATE_SEED_PY' > "$WORK/seed.err" 2>&1
import sqlite3, sys
db, epoch, claim_expires = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
conn = sqlite3.connect(db, timeout=30)
try:
    with conn:
        # Repair-seed: clear any prior gate rows (e.g. a legitimate
        # gateway reclaim that landed before the caller took the board
        # dispatch lock) so the canonical fixture is what gets
        # fingerprinted.
        for gate_id in ("kanban-ui-gate", "kanban-ui-gate-parked"):
            for table in ("tasks", "task_events", "task_runs",
                          "task_comments", "task_links"):
                column = "id" if table == "tasks" else (
                    "parent_id" if table == "task_links" else "task_id")
                conn.execute(
                    f"DELETE FROM {table} WHERE {column} = ?", (gate_id,))
            conn.execute(
                "DELETE FROM kanban_notify_subs WHERE task_id = ?",
                (gate_id,))
        # Disposable board: only gate rows exist, so rewinding the
        # AUTOINCREMENT counters makes the repaired fixture byte-canonical
        # (fresh event ids), not merely logically equal.
        try:
            conn.execute(
                "DELETE FROM sqlite_sequence WHERE name IN "
                "('task_events', 'task_comments', 'task_runs')")
        except sqlite3.OperationalError:
            pass  # no autoincrement inserts have happened yet
        conn.execute(
            "INSERT INTO tasks (id,title,body,assignee,status,priority,"
            "created_by,created_at,workspace_kind,claim_lock,claim_expires,"
            "consecutive_failures,current_run_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("kanban-ui-gate", "gate sentinel", "disposable", None,
             "running", 0, "kanban-ui-verifier", epoch, "scratch",
             "kanban-ui-verifier:probe", claim_expires, 0, 1))
        conn.execute(
            "INSERT INTO tasks (id,title,status,priority,created_by,"
            "created_at,workspace_kind) VALUES (?,?,?,?,?,?,?)",
            ("kanban-ui-gate-parked", "gate parked row", "blocked", 0,
             "kanban-ui-verifier", epoch, "scratch"))
        conn.execute(
            "INSERT INTO task_events (task_id,run_id,kind,payload,created_at) "
            "VALUES (?,?,?,?,?)",
            ("kanban-ui-gate", 1, "claimed",
             '{"source_status": "ready", "claim_lock": '
             '"kanban-ui-verifier:probe"}', epoch))
        conn.execute(
            "INSERT INTO task_events (task_id,run_id,kind,payload,created_at) "
            "VALUES (?,?,?,?,?)",
            ("kanban-ui-gate-parked", None, "blocked",
             '{"reason": "gate fixture; sticky"}', epoch))
        conn.execute(
            "INSERT INTO task_runs (id,task_id,profile,status,claim_lock,"
            "claim_expires,started_at) VALUES (?,?,?,?,?,?,?)",
            (1, "kanban-ui-gate", "kanban-ui-verifier-probe", "running",
             "kanban-ui-verifier:probe", claim_expires, epoch))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
finally:
    conn.close()
GATE_SEED_PY
[ $? -eq 0 ] || fail "gate board seeding failed (see playbook stderr)"
# The API create ran as the container uid, but the seed (and a possible
# WAL sidecar) ran as root: hand the whole pinned board dir back to the
# board owner so the gateway's connect() (init_db opens BEGIN IMMEDIATE
# even before the tick lock) touches ordinary permissions, not a root-
# owned -wal it cannot write.
chown -R "$KANBAN_UI_BOARD_UID:$KANBAN_UI_BOARD_GID" "$GATE_DIR" \
  || fail "cannot chown the gate board dir back to the board owner"
# Canonical logical fingerprint of the gate board's persisted state.
# Every table the dispatcher's reclaim/reclaim-record/failure path can
# write is covered with a deterministic ORDER BY; presentation-only
# computed fields (the flake source Copilot flagged) are structurally
# absent because this reads the SQLite rows, not the /board render.
gate_fingerprint() {
  python3 - "$GATE_DB" <<'GATE_FP_PY'
import hashlib, sqlite3, sys
conn = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True, timeout=30)
try:
    h = hashlib.sha256()
    for sql in (
        "SELECT id,status,assignee,priority,claim_lock,claim_expires,"
        "worker_pid,worker_started_at,consecutive_failures,current_run_id,"
        "started_at,completed_at,result,last_failure_error,block_kind,"
        "block_recurrences FROM tasks ORDER BY id",
        "SELECT id,task_id,profile,step_key,status,claim_lock,"
        "claim_expires,worker_pid,last_heartbeat_at,started_at,ended_at,"
        "outcome,summary,metadata,error FROM task_runs ORDER BY id",
        "SELECT id,task_id,run_id,kind,payload,created_at "
        "FROM task_events ORDER BY id",
        "SELECT id,task_id,author,body,created_at "
        "FROM task_comments ORDER BY id",
        "SELECT parent_id,child_id FROM task_links "
        "ORDER BY parent_id, child_id",
        "SELECT task_id,platform,chat_id,thread_id,user_id,last_event_id,"
        "last_ping_event_id,created_at FROM kanban_notify_subs ORDER BY rowid",
    ):
        for row in conn.execute(sql):
            h.update("\x1f\n".join("" if v is None else str(v) for v in row).encode())
            h.update(b"\n")
    print(h.hexdigest())
finally:
    conn.close()
GATE_FP_PY
}
SHA_BEFORE=$(gate_fingerprint)
[ -n "$SHA_BEFORE" ] || fail "gate fingerprint (before) is empty"
{
  printf 'header = "Cookie: kanban_ui_session=%s"\n' "$SESS"
  printf 'url = "%s/api/kanban/dispatch?board=%s"\n' "$BASE" "$GATE_SLUG"
} > "$WORK/wo_dispatch.conf"
chmod 600 "$WORK/wo_dispatch.conf"
C=$(curl -s -o "$WORK/wo_dispatch.body" -w '%{http_code}' --max-time 15 -X POST \
  --config "$WORK/wo_dispatch.conf" -H 'X-Kanban-UI: 1' \
  -H 'Content-Type: application/json')
[ "$C" = "403" ] || fail "dispatcher-pass dispatch expected 403, got $C"
grep -q 'dispatcher passes are disabled' "$WORK/wo_dispatch.body" \
  || fail "dispatcher-pass dispatch 403 body is not the documented reject"
if [ "$KANBAN_UI_VERIFY_TEST_INJECT" = "dispatch-once" ]; then
  # TASK-GATED fault injection (never set in production): run the pinned
  # upstream tick itself against the gate board (directly the *_locked
  # variant, since the verifier intentionally holds the advisory tick
  # lock). If the deny-list ever regresses, this is exactly the code the
  # endpoint would run - and the fingerprint below MUST have changed.
  # PR#13 round-6d review finding 1: the repo pins Hermes 0.20.0 at
  # b3aa561f (ansible/roles/hermes/vars/main.yaml), whose tree contains
  # ONLY the hermes_cli/kanban_db.py monolith - kanban_db_dispatch.py and
  # kanban_db_connect.py exist in newer revisions (the live checkout HEAD
  # that produced the earlier PASS postdates the pin, which is why the
  # split-only import appeared to work). Both dispatcher and connect
  # imports therefore try the split modules first and fall back to the
  # pinned monolith, so injection executes on the declared revision.
  # tests/test_dispatch_nonmutation_gate.py runs this exact heredoc
  # against stub pinned-layout and split-layout trees.
  env HOME=/tmp HERMES_HOME=/tmp/kanban-ui-verify-nohome \
    HERMES_KANBAN_HOME="$KANBAN_UI_KANBAN_HOME" \
    KANBAN_UI_HERMES_REPO="$KANBAN_UI_HERMES_REPO" \
    "$KANBAN_UI_HERMES_PY" - "$GATE_SLUG" <<'GATE_INJECT_PY' > "$WORK/inject.log" 2>&1
import os, sys
sys.path.insert(0, os.environ["KANBAN_UI_HERMES_REPO"])
try:
    from hermes_cli import kanban_db_dispatch as _dispatch_mod
except ImportError:  # pinned b3aa561f monolith layout
    from hermes_cli import kanban_db as _dispatch_mod
try:
    from hermes_cli import kanban_db_connect as _connect_mod
except ImportError:  # pinned b3aa561f: connect() lives in the monolith
    _connect_mod = _dispatch_mod
board = sys.argv[1]
conn = _connect_mod.connect(board=board)
def _no_spawn(task, workspace, board=None):
    return None
result = _dispatch_mod._dispatch_once_locked(conn, spawn_fn=_no_spawn, board=board)
print("injected tick completed")
conn.close()
GATE_INJECT_PY
  [ $? -eq 0 ] || fail "fault-injection tick did not complete (see playbook stderr)"
fi
SHA_AFTER=$(gate_fingerprint)
[ -n "$SHA_AFTER" ] || fail "gate fingerprint (after) is empty"
if [ "$KANBAN_UI_VERIFY_TEST_INJECT" = "dispatch-once" ]; then
  [ "$SHA_BEFORE" != "$SHA_AFTER" ] \
    || fail "fault injection mutated the gate board but the persisted-state gate did NOT detect it"
else
  [ "$SHA_BEFORE" = "$SHA_AFTER" ] \
    || fail "refused dispatch pass mutated gate-board persisted state"
fi
exec 9>&- || true
# Same middleware-ordering contrast as reclaim: anonymous dispatch tick
# hits the OUTER session gate first (401), never the deny-list.
C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST \
  "$BASE/api/kanban/dispatch")
[ "$C" = "401" ] || fail "anon dispatch expected 401 (session gate outermost), got $C"
# Contrast 1: plain reassign (no reclaim_first) must reach the upstream
# handler, not the guard (unknown id => upstream 409/404; the guard body
# here would mean the deny-list over-rejects the sanctioned op).
C=$(curl -s -o "$WORK/wo_plain.body" -w '%{http_code}' --max-time 15 -X POST \
  --config "$WORK/wo_reassign-reclaim.conf" -H 'X-Kanban-UI: 1' \
  -H 'Content-Type: application/json' --data '{"profile":"probe"}')
grep -q 'worker control operations are disabled' "$WORK/wo_plain.body" \
  && fail "plain reassign over-rejected by the worker-control guard"
[ "$C" != "401" ] || fail "plain reassign blocked by session gate: $C"
# Contrast 2: anonymous reclaim hits the OUTER session gate first (401),
# never the deny-list -- middleware order is auth-outermost.
C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST \
  "$BASE/api/kanban/tasks/kanban-ui-probe/reclaim")
[ "$C" = "401" ] || fail "anon reclaim expected 401 (session gate outermost), got $C"

# 9. container logs since this container start are free of token material.
# The old bearer-token image printed the token at startup; its retained
# json-file history is scrubbed by the redeploy, not by this check.
if command -v docker >/dev/null 2>&1; then
  if docker inspect kanban-ui >/dev/null 2>&1; then
    docker logs kanban-ui --since "$(docker inspect -f '{{.State.StartedAt}}' kanban-ui)" \
      > "$WORK/logs.txt" 2>&1
    logs_txt=$(cat "$WORK/logs.txt")
    [[ "$logs_txt" == *"$TOK"* ]] && fail "container logs contain the bootstrap token"
  fi
fi

unset TOK SESS HDRS BODY logs_txt asset_body esc
kill "$WATCHER_PID" 2>/dev/null
wait "$WATCHER_PID" 2>/dev/null || true
[ -f "$WORK/argv_leak" ] && fail "the bootstrap token appeared in some process argv during verification"
if [ "$FAIL" = 0 ]; then
  echo "session lifecycle verified: https, gate, index hygiene, login, cookie, board, csrf, ws, logs, argv-free"
fi
exit "$FAIL"
