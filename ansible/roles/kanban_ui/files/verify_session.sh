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
#   6. the board renders with the session cookie
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
#      rejects a FULL session with the documented 403 body and leaves the
#      board byte-identical; the anon tick 401s at the session gate
#      (PR#13 review finding 4323915007)
set -u

: "${KANBAN_UI_VERIFY_URL:?}"
: "${KANBAN_UI_VERIFY_TOKEN_FILE:?}"
: "${KANBAN_UI_VERIFY_BOARD:?}"
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
trap 'rm -rf "$WORK"' EXIT

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

# 6. board renders with the cookie
{
  printf 'header = "Cookie: kanban_ui_session=%s"\n' "$SESS"
  printf 'url = "%s/api/kanban/board?board=%s"\n' "$BASE" "$KANBAN_UI_VERIFY_BOARD"
} > "$WORK/board.conf"
chmod 600 "$WORK/board.conf"
C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 \
  --config "$WORK/board.conf" -H 'X-Kanban-UI: 1')
[ "$C" = "200" ] || fail "board read with session expected 200, got $C"

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
# session gate, and the refused tick must leave the board byte-identical
# (the request never reaches dispatch_once).
{
  printf 'header = "Cookie: kanban_ui_session=%s"\n' "$SESS"
  printf 'url = "%s/api/kanban/board?board=%s"\n' "$BASE" "$KANBAN_UI_VERIFY_BOARD"
} > "$WORK/board_sha.conf"
chmod 600 "$WORK/board_sha.conf"
board_sha() {
  curl -s --max-time 60 --config "$WORK/board_sha.conf" -H 'X-Kanban-UI: 1' | sha256sum | cut -d' ' -f1
}
SHA_BEFORE=$(board_sha)
{
  printf 'header = "Cookie: kanban_ui_session=%s"\n' "$SESS"
  printf 'url = "%s/api/kanban/dispatch?board=%s"\n' "$BASE" "$KANBAN_UI_VERIFY_BOARD"
} > "$WORK/wo_dispatch.conf"
chmod 600 "$WORK/wo_dispatch.conf"
C=$(curl -s -o "$WORK/wo_dispatch.body" -w '%{http_code}' --max-time 15 -X POST \
  --config "$WORK/wo_dispatch.conf" -H 'X-Kanban-UI: 1' \
  -H 'Content-Type: application/json')
[ "$C" = "403" ] || fail "dispatcher-pass dispatch expected 403, got $C"
grep -q 'dispatcher passes are disabled' "$WORK/wo_dispatch.body" \
  || fail "dispatcher-pass dispatch 403 body is not the documented reject"
SHA_AFTER=$(board_sha)
[ "$SHA_BEFORE" = "$SHA_AFTER" ] || fail "refused dispatch pass mutated board state"
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
