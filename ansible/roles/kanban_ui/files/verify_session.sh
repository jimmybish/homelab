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
#     refuses to run against http://, mirroring the deployed Secure
#     session cookie; there is no plaintext credential/session fallback.
#   - All artifacts live in a private mktemp -d directory (mode 700,
#     removed by an EXIT trap). The play runs with become: true, so no
#     predictable /tmp paths may be written.
#
# Verifies (live):
#   0. the verification base is HTTPS
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
#   9. container logs since this container start are free of token material
set -u

: "${KANBAN_UI_VERIFY_URL:?}"
: "${KANBAN_UI_VERIFY_TOKEN_FILE:?}"
: "${KANBAN_UI_VERIFY_BOARD:?}"
BASE="${KANBAN_UI_VERIFY_URL%/}"
# ROUND-7 finding 3: the token arrives as a PATH only (see transport
# rule above). `read` is a builtin: no fork, nothing token-shaped can
# reach any /proc/<pid>/cmdline.
TOK=""
IFS= read -r TOK < "$KANBAN_UI_VERIFY_TOKEN_FILE"
[[ -n "$TOK" ]] || exit 1
FAIL=0
fail() { echo "FAIL: $1"; FAIL=1; }

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

# 0. credentials never transit plaintext
case "$BASE" in
  https://*) : ;;
  *) fail "verification base is not https; login must never traverse plaintext";;
esac

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
  *) fail "KANBAN_UI_DIRECT_URL must be the plaintext direct base (sanity of this probe)";;
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
[ "$STATUS" = "200" ] || fail "login expected 200, got $STATUS"
grep -qi 'set-cookie: kanban_ui_session=[^;]*;.*httponly' <<<"$HDRS" || fail "session cookie is not HttpOnly"
grep -qi 'set-cookie: kanban_ui_session=.*samesite=strict' <<<"$HDRS" || fail "session cookie is not SameSite=strict"
grep -qi 'set-cookie: kanban_ui_session=[^;]*;.*secure' <<<"$HDRS" || fail "session cookie is not Secure"
SESS=$(grep -io 'set-cookie: kanban_ui_session=[^;]*' <<<"$HDRS" | head -1 | cut -d= -f2)
[ -n "$SESS" ] || fail "no session cookie issued"

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
