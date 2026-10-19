#!/usr/bin/env python3
"""Security regression tests for access-log credential redaction.

Round-1 finding 2 (token leakage into durable logs) proved live during
deployment: stale browser tabs from the old bearer-token build reconnect
with a credential query parameter, and uvicorn echoes request lines into
both the access logger AND (for rejected WebSocket upgrades) the
uvicorn.error child loggers. server.py's dictConfig installs a
handler-level filter plus a redacting access formatter that scrub
credential-shaped query params and the literal bootstrap secret.
These tests lock that behavior (stdlib only):

    python3 ansible/roles/kanban_ui/tests/test_log_redaction.py

Exit 0 = all pass.
"""
import importlib.util
import json
import logging
import logging.config
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "files" / "app" / "server.py"

# server.py resolves REPO/TOKEN_FILE from env at import time; point them at
# throwaway paths so importing never touches a real Hermes home or token.
os.environ["KANBAN_UI_HERMES_REPO"] = tempfile.mkdtemp(prefix="kbui-test-repo-")
os.environ["KANBAN_UI_TOKEN_FILE"] = str(SERVER)  # exists; never read by import

_spec = importlib.util.spec_from_file_location("kanban_ui_server", SERVER)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)
# dictConfig resolves custom factories via "."-split class paths, which
# require the module to be importable by name; register the spec-loaded
# module so f"{__name__}.RedactingLogFilter" resolves under test.
sys.modules.setdefault("kanban_ui_server", server)

SECRET = "sup3r-s3cr3t-bootstrap-value-0123456789abcdef"

# Credential query param assembled at runtime so this test source never
# contains a literal key=value pair the secret scrubber would act on.
QKEY = "?tok" + "en="


def hostile_line(prefix=""):
    """A realistic uvicorn log line carrying the secret in the query."""
    return (
        prefix
        + "WebSocket /api/kanban/events"
        + QKEY
        + SECRET
        + "&board=homelab"
    )


class TestRedactCredentials(unittest.TestCase):
    def test_ws_query_token_scrubbed(self):
        # The exact stale-tab WS access line shape from the live incident.
        line = "INFO: 192.0.2.10:59238 - " + repr(hostile_line()) + " 403"
        out = server.redact_credentials(line, SECRET)
        self.assertNotIn(SECRET, out)
        self.assertIn("[redacted]", out)
        # Non-credential query params survive untouched.
        self.assertIn("board=homelab", out)

    def test_first_param_and_other_names_scrubbed(self):
        out = server.redact_credentials("GET /x/" + QKEY + SECRET + "&b=1", SECRET)
        self.assertIn("[redacted]", out)
        self.assertNotIn(SECRET, out)
        self.assertIn("b=1", out)
        out = server.redact_credentials("GET /x/?pass=hunter2&b=1", SECRET)
        self.assertIn("pass=[redacted]", out)
        self.assertNotIn("hunter2", out)
        out = server.redact_credentials("GET /x/?secret=abc&b=1", SECRET)
        self.assertIn("secret=[redacted]", out)
        self.assertNotIn("abc", out)

    def test_bearer_header_scrubbed(self):
        line = "authorization: Bearer abc" + "def.ghi" + "jkl"
        out = server.redact_credentials(line, SECRET)
        self.assertNotIn("abcdef.ghijkl", out)
        self.assertIn("authorization:", out)

    def test_literal_secret_always_scrubbed(self):
        # Even in a shape the regexes do not model (fragment, path), the
        # literal secret must never survive.
        out = server.redact_credentials("path /" + SECRET + "/done", SECRET)
        self.assertNotIn(SECRET, out)
        self.assertIn("[redacted]", out)

    def test_clean_lines_unchanged(self):
        line = 'INFO: 192.0.2.10:1 - "GET /api/health HTTP/1.1" 401'
        self.assertEqual(server.redact_credentials(line, SECRET), line)

    def test_token_shaped_word_without_query_context_untouched(self):
        # Over-redaction guard: an ordinary path/API word is preserved.
        line = 'INFO: - "GET /api/kanban/tasks/t_12345678/comments HTTP/1.1" 200'
        self.assertEqual(server.redact_credentials(line, SECRET), line)

    def test_dictconfig_is_credential_free(self):
        # dictConfig prints the offending config dict in its traceback on
        # failure (build 2026.10.10h leaked the token exactly this way).
        # The secret may only live in the module global, never the dict.
        cfg = server.access_log_config(SECRET)
        self.assertNotIn(SECRET, json.dumps(cfg))
        # Populating the global is the sanctioned handoff:
        self.assertEqual(server._UI_SECRET, SECRET)

    def test_dictconfig_loads_and_scrubs(self):
        # End-to-end: logging.config.dictConfig must ACCEPT the config
        # (build h crashed with 'Unable to add filter') and the configured
        # handlers must scrub hostile records. Uses the real filter
        # factory; formatters swapped to stdlib to avoid a uvicorn import.
        import copy

        cfg = copy.deepcopy(server.access_log_config(SECRET))
        cfg["formatters"]["default"].pop("()", None)
        cfg["formatters"]["default"]["class"] = "logging.Formatter"
        cfg["formatters"]["access"].pop("()", None)
        cfg["formatters"]["access"]["class"] = "logging.Formatter"
        cfg["handlers"]["access"]["stream"] = "ext://sys.stdout"

        import io

        buf = io.StringIO()
        saved = sys.stdout
        sys.stdout = buf  # bind BEFORE dictConfig: ext:// resolves at config time
        try:
            logging.config.dictConfig(cfg)  # must not raise
            logging.getLogger("uvicorn.access").info("%s", hostile_line("INFO: 1.2.3.4 - "))
            logging.getLogger("uvicorn.error").info("%s", hostile_line("INFO: "))
        finally:
            sys.stdout = saved
        out = buf.getvalue()
        self.assertIn("[redacted]", out)
        self.assertNotIn(SECRET, out)

    def test_dictconfig_formatter_redacts(self):
        # The access formatter factory wraps every formatted access line
        # with the module-global secret.
        import importlib.util as ilu

        cfg = server.access_log_config(SECRET)
        spec = dict(cfg["formatters"]["access"])
        factory = spec.pop("()")
        self.assertEqual(factory, f"{server.__name__}.RedactingAccessFormatter")
        self.assertNotIn("secret", spec)

        if ilu.find_spec("uvicorn") is None:
            self.skipTest("uvicorn not importable on this control node")

        spec["use_colors"] = False
        fmt = server.RedactingAccessFormatter(**spec)
        # Round-1 fix (review t_c93e7724): AccessFormatter.formatMessage in
        # the pinned uvicorn 0.41.0 UNCONDITIONALLY unpacks the 5-tuple
        # (client_addr, method, full_path, http_version, status_code) from
        # record.args (uvicorn/logging.py L99-105); an empty-args record
        # raised ValueError instead of exercising the formatter. Build the
        # record in the real shape the h11 protocol emits, hostile path in
        # the full_path slot.
        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 0,
            '%s - "%s %s HTTP/%s" %s',
            ("192.0.2.1:1", "GET",
             "/api/kanban/events" + QKEY + SECRET + "&board=homelab",
             "1.1", 101),
            None,
        )
        out = fmt.format(record)
        self.assertNotIn(SECRET, out)
        self.assertIn("[redacted]", out)
        self.assertIn("board=homelab", out)

    def test_handler_filter_scrubs_child_logger_records(self):
        # Regression for build 2026.10.10g: uvicorn's websockets protocol
        # logs rejected upgrades through uvicorn.error CHILD loggers, which
        # never pass through the access formatter. The handler-level filter
        # (declared top-level, referenced by NAME) must scrub those too.
        cfg = server.access_log_config(SECRET)
        fdef = cfg["filters"]["redact_credentials"]
        self.assertEqual(fdef["()"], f"{server.__name__}.RedactingLogFilter")
        for hname in ("default", "access"):
            self.assertEqual(
                cfg["handlers"][hname]["filters"], ["redact_credentials"]
            )
            filt = server.RedactingLogFilter()
            rec = logging.LogRecord(
                "uvicorn.error", logging.INFO, __file__, 0,
                "%s" + hostile_line(), ("192.0.2.5:1 - ",), None,
            )
            self.assertTrue(filt.filter(rec))
            rendered = rec.getMessage()
            self.assertNotIn(SECRET, rendered)
            self.assertIn("[redacted]", rendered)
            self.assertIn("board=homelab", rendered)

    def test_main_uses_config_with_log_config(self):
        # Regression for the build-f NameError crash: main() must pass
        # access_log_config(secret) to uvicorn.Config, and no call to the
        # non-existent install_access_log_redaction may remain.
        src = SERVER.read_text()
        self.assertIn("log_config=access_log_config(secret)", src)
        self.assertNotIn("install_access_log_redaction", src)


class TestAccessRecordTuplePreservation(unittest.TestCase):
    """PR#13 review comment 4244239564: for uvicorn.access records the
    handler filter must preserve the 5-tuple record.args (AccessFormatter
    in the pinned uvicorn 0.41.0 unpacks it unconditionally) and redact
    the string fields IN PLACE; only default-formatter records collapse.
    """

    def _access_record(self):
        # The exact call shape uvicorn's h11 protocol makes per request.
        return logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 0,
            '%s - "%s %s HTTP/%s" %s',
            ("192.0.2.10:59238", "GET",
             "/api/kanban/events" + QKEY + SECRET + "&board=homelab",
             "1.1", 403),
            None,
        )

    def test_filter_preserves_tuple_and_scrubs_path(self):
        server.configure_ui_secret(SECRET)
        filt = server.RedactingLogFilter()
        rec = self._access_record()
        self.assertTrue(filt.filter(rec))
        self.assertIsInstance(rec.args, tuple)
        self.assertEqual(len(rec.args), 5)
        client_addr, method, full_path, http_version, status = rec.args  # AccessFormatter's unpack
        self.assertEqual(method, "GET")
        self.assertEqual(http_version, "1.1")
        self.assertEqual(status, 403)
        self.assertNotIn(SECRET, full_path)
        self.assertIn("board=homelab", full_path)
        line = rec.getMessage()
        self.assertNotIn(SECRET, line)
        self.assertIn("[redacted]", line)

    def test_access_formatter_emits_redacted_line_without_exception(self):
        import importlib.util as ilu
        if ilu.find_spec("uvicorn") is None:
            self.skipTest("uvicorn not importable on this control node")
        server.configure_ui_secret(SECRET)
        cfg = server.access_log_config(SECRET)
        spec = dict(cfg["formatters"]["access"])
        spec.pop("()")
        spec["use_colors"] = False
        fmt = server.RedactingAccessFormatter(**spec)
        rec = self._access_record()
        server.RedactingLogFilter().filter(rec)
        out = fmt.format(rec)  # raised TypeError pre-fix
        self.assertNotIn(SECRET, out)
        self.assertIn("[redacted]", out)
        self.assertIn("board=homelab", out)

    def test_default_formatter_record_still_collapses(self):
        # uvicorn.error-style record whose FORMAT string itself carries
        # the secret: tuple scrubbing cannot help, so the collapse path
        # (msg replaced, args cleared) must still run for these.
        server.configure_ui_secret(SECRET)
        rec = logging.LogRecord(
            "uvicorn.error", logging.INFO, __file__, 0,
            hostile_line("INFO: "), None, None,
        )
        self.assertTrue(server.RedactingLogFilter().filter(rec))
        self.assertIsNone(rec.args)
        self.assertNotIn(SECRET, rec.getMessage())
        self.assertIn("[redacted]", rec.getMessage())


class TestSessionCookieSecureDefault(unittest.TestCase):
    """Round-2 finding 2: no plaintext credential/session fallback."""

    COMPOSE = HERE.parent / "templates" / "docker_compose.yaml.j2"

    def test_cookie_secure_is_the_default(self):
        src = SERVER.read_text()
        # The opt-out is INSECURE-flag-gated (deny-by-default), never an
        # opt-in that a missed env var silently downgrades.
        self.assertIn('secure=not os.environ.get("KANBAN_UI_COOKIE_INSECURE")', src)
        self.assertNotIn('secure=bool(os.environ.get("KANBAN_UI_COOKIE_SECURE"))', src)

    def test_compose_never_opts_into_insecure_cookies(self):
        # No ACTIVE assignment of the opt-out in the compose env (the
        # NOTE comment naming it for the prohibition is fine).
        active = [
            line
            for line in self.COMPOSE.read_text().splitlines()
            if line.strip().startswith("KANBAN_UI_COOKIE_INSECURE")
        ]
        self.assertEqual(active, [])


class TestVerifierArgvHygiene(unittest.TestCase):
    """Round-2 findings 1 and 4: the deploy verifier must never put the
    bootstrap token into any process argv and must never write
    predictable /tmp paths. Verified two ways: statically (every $TOK
    reference in verify_session.sh is a shell builtin construct, never
    an external-command argument) and live (a watcher harness, mirroring
    the one embedded in the script, detects a token-shaped argv in a
    controlled grep against /dev/zero and stays silent for the file-based
    transport the script actually uses). All tokens here are synthetic.
    """

    VERIFIER = HERE.parent / "files" / "verify_session.sh"
    FAKE = "synthetic-fake-probe-value-0123456789abcdef-XY"

    WATCHER = (
        'work=$(mktemp -d); chmod 700 "$work"; '
        '( trap "exit 0" TERM INT; while :; do '
        'for d in /proc/[0-9]*; do [ -r "$d/cmdline" ] || continue; arg=""; '
        'while IFS= read -r -d "" arg; do '
        '[[ "$arg" == *"$FAKE"* ]] && { echo leak > "$work/argv_leak"; exit 0; }; '
        'done 2>/dev/null < "$d/cmdline"; '
        '[[ "$arg" == *"$FAKE"* ]] && { echo leak > "$work/argv_leak"; exit 0; }; '
        "done; sleep 0.1 || exit 0; done ) & pid=$!; "
        '%(probe)s; '
        'sleep 0.4; kill $cpid 2>/dev/null; kill $pid 2>/dev/null; '
        'wait $pid 2>/dev/null; wait $cpid 2>/dev/null; '
        'if [ -f "$work/argv_leak" ]; then rm -rf "$work"; echo LEAKED; '
        'else rm -rf "$work"; echo CLEAN; fi'
    )

    def _harness(self, probe):
        import subprocess

        env = dict(os.environ, FAKE=self.FAKE)
        out = subprocess.run(
            ["bash", "-c", self.WATCHER % {"probe": probe}],
            env=env, capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip()

    def test_watcher_detects_token_shaped_argv(self):
        # Validity of the regression mechanism the deployed verifier uses:
        # a long-lived process whose argv carries the token MUST be caught
        # (the exact shape the round-2 finding flagged: grep/curl taking
        # the secret as an argument). `tail -f --retry` keeps that argv
        # resident for the whole sample window, then the harness kills it.
        self.assertEqual(
            self._harness('tail -f --retry "/nope/$FAKE" >/dev/null 2>&1 & cpid=$!'),
            "LEAKED",
        )

    def test_file_transport_keeps_argv_clean(self):
        # The transport the verifier actually uses: token material goes
        # into a private file, and only the @file path reaches argv.
        probe = (
            'umask 077; d=$(mktemp -d); printf \'{"%s":"%s"}\' tok "$FAKE" '
            '> "$d/b.json"; (command -v curl >/dev/null && '
            'curl -s --data-binary "@$d/b.json" http://127.0.0.1:9/ >/dev/null 2>&1) ; '
            'rm -rf "$d"'
        )
        self.assertEqual(self._harness(probe), "CLEAN")

    def test_verifier_only_uses_builtins_on_token(self):
        src = self.VERIFIER.read_text()
        offenders = []
        for i, line in enumerate(src.splitlines(), 1):
            if "TOK" not in line or line.strip().startswith("#"):
                continue
            code = line.split("#", 1)[0]
            stripped = code.strip()
            ok = (
                stripped.startswith('TOK="')                       # assignment
                or stripped.startswith('IFS= read -r TOK <')       # builtin file read
                or stripped.startswith(': "')                      # :- guard (builtin)
                or stripped.startswith("[[ ")                      # builtin test
                or stripped.startswith('"$BASE"')                  # printf arg line
                or stripped.startswith("unset ")
                or stripped.startswith("esc=")                      # param expansion
                or stripped.startswith("KANBAN_UI_VERIFY_TOKEN_FILE")  # env guard doc
                or "printf" in stripped                             # builtin printf args
            )
            if not ok:
                offenders.append((i, line))
            # Belt-and-braces: no external command may take the token.
            for exe in ("grep", "sed", "awk", "curl", "docker", "python"):
                if exe in code and "TOK" in code and not stripped.startswith("printf"):
                    offenders.append((i, line))
        self.assertEqual(offenders, [], f"token reaches external-command argv: {offenders}")

    def test_verifier_uses_private_dir_and_refuses_http(self):
        src = self.VERIFIER.read_text()
        self.assertIn("mktemp -d", src)
        self.assertIn('chmod 700', src)
        self.assertIn("trap 'rm -rf \"$WORK\"' EXIT", src)
        self.assertNotIn("/tmp/kbui", src)
        self.assertIn('https://*)', src)  # refuses any non-https base
        self.assertIn('argv_leak', src)   # the watcher gates the exit code

    def test_http_base_aborts_before_any_request_or_token_read(self):
        # ROUND-2 REVIEW FINDING 4301554415: fail() only sets FAIL=1 and
        # execution continued, so a mis-set http:// base still reached
        # the step-5 real-token login POST. Lock the ordering statically:
        #   (a) an abort() primitive exists that kills the watcher and
        #       exits nonzero,
        #   (b) the https gate uses abort (not fail) and sits BEFORE the
        #       watcher launch, the token read, and every POST/curl,
        #   (c) the login/cookie integrity gates (steps 5) also abort --
        #       they protect session material and gate steps 6-10.
        src = self.VERIFIER.read_text()
        self.assertRegex(src, r"abort\(\) \{[\s\S]*?kill \"\$WATCHER_PID\"[\s\S]*?exit 1")
        gate = src.index("https://*)")
        watcher = src.index("WATCHER_PID=$!")
        token_read = src.index('IFS= read -r TOK <')
        login_post = src.index('"$BASE/login"')
        self.assertLess(gate, watcher)
        self.assertLess(gate, token_read)
        self.assertLess(gate, login_post)
        # the case-arm that rejects a non-https base must abort, not fail
        self.assertRegex(src, r"\*\) abort \"verification base is not https")
        # direct-probe sanity gate in front of the three plaintext POSTs
        self.assertRegex(src, r"\*\) abort \"KANBAN_UI_DIRECT_URL must be")
        # step-5 integrity gates abort so no cookie-bearing request fires
        # after a failed/suspect login
        for msg in ("login expected 200", "session cookie is not HttpOnly",
                    "session cookie is not SameSite=strict",
                    "session cookie is not Secure", "no session cookie issued"):
            self.assertRegex(src, r"\|\| abort \"" + msg)
        # the old fail-and-continue form must be gone
        self.assertNotIn('fail "verification base is not https', src)
        self.assertNotIn('fail "login expected 200', src)

    def test_token_reaches_script_as_path_not_env_value(self):
        # ROUND-7 finding 3: Ansible renders task `environment:` into
        # `/bin/sh -c KEY=value ...` argv on the target, so the token
        # VALUE must never be an environment value. The task passes the
        # 0400 token FILE path; the script reads it with a builtin.
        src = self.VERIFIER.read_text()
        self.assertIn('KANBAN_UI_VERIFY_TOKEN_FILE:?', src)
        self.assertNotIn('KANBAN_UI_VERIFY_TOKEN:?', src)
        self.assertIn('read -r TOK < "$KANBAN_UI_VERIFY_TOKEN_FILE"', src)
        tasks = (HERE.parent / "tasks" / "main.yaml").read_text()
        task = tasks[tasks.index("Verify the full session lifecycle") :]
        task = task[: task.index("\n- name:") if "\n- name:" in task[10:] else len(task)]
        self.assertIn("KANBAN_UI_VERIFY_TOKEN_FILE: \"{{ kanban_ui_token_file }}\"", task)
        self.assertNotIn("kanban_ui_access_token", task)


class TestCheckModeCannotStopPoc(unittest.TestCase):
    """Round-2 finding 3: the POC stop task must be inert under --check."""

    TASKS = HERE.parent / "tasks" / "main.yaml"

    def test_stop_task_guarded_from_check_mode(self):
        src = self.TASKS.read_text()
        start = src.index("Stop the manually-launched")
        # Task block ends at the next top-level list item.
        end = src.index("\n- name:", start)
        block = src[start:end]
        self.assertIn("when: not ansible_check_mode", block)
        self.assertIn("check_mode: false", block)


class TestLoginTransportGate(unittest.TestCase):
    """Round-3 finding 1 + round-4 finding 1: the credential endpoint is
    unreachable over the published plaintext HTTP origin, even when the
    attacker spoofs X-Forwarded-Proto: https. Layers locked here:
      (a) the pure predicates login_proxy_marked (THE trust signal: a
          vaulted marker the SWAG vhost injects, unobservable and
          unforgeable by a direct client) and login_transport_allowed
          (defense-in-depth only),
      (b) the /login route ORDER: both gates run before the throttle,
          before the body is parsed, and before any compare_digest of
          the bootstrap secret,
      (c) the client-side refusal to render a credential form on a
          plaintext origin, plus the deployment artifacts (verifier
          spoofed-header probes, marker provisioning on BOTH sides,
          UFW source restriction, port pinning).
    """

    APPJS = HERE.parent / "files" / "app" / "static" / "app.js"
    VERIFIER = HERE.parent / "files" / "verify_session.sh"
    TASKS = HERE.parent / "tasks" / "main.yaml"
    COMPOSE = HERE.parent / "templates" / "docker_compose.yaml.j2"

    def test_predicate_refuses_direct_plaintext(self):
        # Direct hit on the published port: no proxy, no X-Forwarded-Proto.
        self.assertFalse(server.login_transport_allowed("", False))
        self.assertFalse(server.login_transport_allowed("http", False))
        self.assertFalse(server.login_transport_allowed("   ", False))
        # Proxied HTTPS path (SWAG sets the header from $scheme).
        self.assertTrue(server.login_transport_allowed("https", False))
        self.assertTrue(server.login_transport_allowed(" HTTPS , http", False))
        # First entry is the client-facing protocol of the outermost
        # trusted proxy; a trailing second hop does not downgrade it.
        self.assertTrue(server.login_transport_allowed("https,https", False))
        # Loopback-development opt-out only; compose never sets it.
        self.assertTrue(server.login_transport_allowed("", True))

    MARKER = "synthetic-marker-value-0123456789abcdef-DO-NOT-REUSE"

    def test_marker_predicate_refuses_spoofed_transport(self):
        # ROUND-4 FINDING 1 core regression: a forged
        # X-Forwarded-Proto: https buys nothing without the marker.
        # login_transport_allowed("https") would return True -- the
        # marker predicate is what actually closes the bypass, so it must
        # refuse every presentation that did not come stamped from nginx.
        self.assertFalse(server.login_proxy_marked("", self.MARKER, False))
        # Bogus / partial / padded-but-wrong marker values.
        self.assertFalse(server.login_proxy_marked("bogus", self.MARKER, False))
        self.assertFalse(server.login_proxy_marked(self.MARKER[:-2], self.MARKER, False))
        # No expected marker configured => deny EVERYTHING (deny-by-default).
        self.assertFalse(server.login_proxy_marked(self.MARKER, "", False))
        self.assertFalse(server.login_proxy_marked("anything", "", False))
        # Oversized garbage never reaches a comparison.
        self.assertFalse(server.login_proxy_marked("x" * 5000, self.MARKER, False))
        # Exact match from the trusted proxy is the ONLY pass.
        self.assertTrue(server.login_proxy_marked(self.MARKER, self.MARKER, False))
        # Surrounding whitespace tolerance (nginx value + trailing newline
        # in the file); inner whitespace is not tolerated.
        self.assertTrue(server.login_proxy_marked("  " + self.MARKER + " \t", self.MARKER, False))
        # Loopback-development opt-out is the single exception.
        self.assertTrue(server.login_proxy_marked("", "", True))

    def test_marker_never_derived_from_client_headers(self):
        # The expected marker may ONLY come from the mounted file
        # (load_proxy_marker) -- never from a request header, env inline
        # value, or any other client-reachable surface.
        src = SERVER.read_text()
        start = src.index("def load_proxy_marker")
        end = src.index("def build_app")
        fn = src[start:end]
        self.assertIn("MARKER_FILE.read_text()", fn)
        self.assertNotIn("headers", fn)
        # The route passes the header as the PRESENTED (client) side only.
        route_start = src.index('@app.post("/login")')
        route = src[route_start : src.index("@app.post(\"/logout\")")]
        gate = route.index("login_proxy_marked(")
        gate_call = route[gate : gate + 400]
        self.assertIn('req.headers.get("x-kanban-proxy-marker", "")', gate_call)
        self.assertIn("proxy_marker,", gate_call)  # expected = server-side value

    def test_boot_refuses_missing_marker_without_optout(self):
        # A missing marker file must NOT silently boot a login endpoint
        # that nothing can satisfy: main() exits loudly unless the
        # loopback-dev opt-out is set.
        src = SERVER.read_text()
        start = src.index("def main(")
        block = src[start:]
        self.assertIn("proxy_marker = load_proxy_marker()", block)
        gate = block.index("if not proxy_marker and not os.environ.get(\"KANBAN_UI_COOKIE_INSECURE\")")
        self.assertIn("raise SystemExit", block[gate : gate + 500])
        self.assertLess(gate, block.index("build_app(secret, proxy_marker"))

    def test_gate_runs_before_any_credential_touch(self):
        src = SERVER.read_text()
        start = src.index('@app.post("/login")')
        end = src.index("@app.post(\"/logout\")")
        route = src[start:end]
        marker_gate = route.index("login_proxy_marked(")
        gate = route.index("login_transport_allowed(")
        # The MARKER gate is FIRST (round-4: the unforgeable signal is
        # evaluated before the spoofable one), then the transport
        # predicate. Both refuse with 403 BEFORE throttle, body parse,
        # and the constant-time comparison of the bootstrap secret.
        self.assertLess(marker_gate, gate)
        self.assertIn("status_code=403", route[marker_gate : marker_gate + 600])
        self.assertIn("status_code=403", route[gate : gate + 600])
        for cred_touch in ("_login_throttled()", "await req.json()",
                           "login_token_matches(presented, secret)"):
            self.assertLess(marker_gate, route.index(cred_touch))
            self.assertLess(gate, route.index(cred_touch))

    def test_route_gate_uses_forwarded_proto_with_insecure_optout(self):
        src = SERVER.read_text()
        self.assertIn('req.headers.get("x-forwarded-proto", "")', src)
        self.assertIn('bool(os.environ.get("KANBAN_UI_COOKIE_INSECURE"))', src)

    def test_client_refuses_plaintext_origin(self):
        src = self.APPJS.read_text()
        start = src.index("function showLogin()")
        end = src.index("function connectWS()")
        fn = src[start:end]
        guard = fn.index('location.protocol !== "https:"')
        # The refusal path must precede the credential input creation.
        self.assertLess(guard, fn.index("type: \"password\""))
        # Loopback development is exempted; anything else never renders.
        self.assertIn("127", fn[guard : guard + 300])

    def test_verifier_probes_direct_http_for_rejection(self):
        # Live regression (round-3 + round-4): the deploy verifier POSTs a
        # STATIC probe token (never the real one) to the direct http base
        # THREE ways -- bare, with a spoofed `X-Forwarded-Proto: https`,
        # and with the spoofed proto PLUS a bogus marker value -- and
        # demands a transport-level refusal each time: 403 from the app
        # gate, or 000 when UFW's source restriction makes the port
        # unreachable from this host. Any credential-processing status
        # (200/401/429) fails the gate. The real-credential login only
        # ever goes to the https base.
        src = self.VERIFIER.read_text()
        self.assertIn('KANBAN_UI_DIRECT_URL:?', src)
        self.assertIn('http://*)', src)  # sanity: direct base must be http
        probe = src[src.index("KANBAN_UI_DIRECT_URL") :]
        # The round-4 bypass attempt itself must be probed.
        self.assertIn("X-Forwarded-Proto: https", probe[:1600])
        self.assertIn("X-Kanban-Proxy-Marker", probe[:1600])
        self.assertIn("spoofed-proto", probe[:1600])
        self.assertIn("403) : ;;", probe[:1600])
        self.assertIn("000) : ;;", probe[:1600])
        self.assertIn("401/429/200", src[: src.index("KANBAN_UI_DIRECT_URL")])
        self.assertNotIn("$TOK", probe[:1600])

    def test_marker_provisioned_on_both_sides(self):
        # Round-4 finding 1 + ROUND-5 FINDING 1 deployment lock: the
        # SAME vault variable renders the nginx stamp and the
        # container's expected-value file. Round-5 placement: the
        # directive lives in the dedicated PRIVATE include (deployed
        # 0600), included by BOTH location blocks; the 0664 vhost
        # itself must contain ZERO secret material.
        conf = (HERE.parent / "templates" / "kanban_ui.subdomain.conf.j2").read_text()
        marker = (HERE.parent / "templates" / "kanban_ui_marker.conf.j2").read_text()
        # ROUND-7 FINDING 1: the include path is the /config ROOT, not
        # /config/nginx (SWAG's init-permissions chmod -R g+w /config/nginx
        # sweep drifted an nginx/ placement 0600->0620 at every boot).
        self.assertEqual(conf.count("include /config/kanban_ui_marker.conf;"), 2)
        self.assertNotIn("/config/nginx/kanban_ui_marker.conf", conf)
        # The world-readable vhost must not carry the marker value or
        # even the vault variable reference (round-5 blocking finding).
        self.assertNotIn("kanban_ui_proxy_secret", conf)
        self.assertNotIn("X-Kanban-Proxy-Marker", conf)
        self.assertEqual(marker.count("proxy_set_header X-Kanban-Proxy-Marker"), 1)
        self.assertIn("{{ kanban_ui_proxy_secret }}", marker)
        compose = self.COMPOSE.read_text()
        self.assertIn("{{ kanban_ui_proxy_marker_file }}:/app/proxy_marker.txt:ro", compose)
        self.assertIn('KANBAN_UI_PROXY_MARKER_FILE: "/app/proxy_marker.txt"', compose)
        tasks = self.TASKS.read_text()
        deploy = tasks[tasks.index("Deploy Kanban UI proxy trust marker file") :]
        deploy = deploy[: deploy.index("\n- name:") if "\n- name:" in deploy else len(deploy)]
        self.assertIn('content: "{{ kanban_ui_proxy_secret }}', deploy)
        self.assertIn("mode: '0400'", deploy)
        self.assertIn("no_log: true", deploy)
        # The proxy-side include task: PRIVATE mode, outside proxy-confs
        # AND outside the chmod-swept /config/nginx subtree (round 7).
        inc = tasks[tasks.index("Deploy Kanban UI private proxy marker include") :]
        # ROUND-7: this include task is now the LAST task in the proxy
        # block (the mode gate moved after flush_handlers), so the slice
        # runs to the end of the block marker rather than the next task.
        inc = inc[: inc.index("\n    - name:") if "\n    - name:" in inc else len(inc)]
        self.assertIn("mode: '0600'", inc)
        self.assertIn("internal/kanban_ui_marker.conf", inc)
        self.assertNotIn("nginx/kanban_ui_marker.conf", inc)
        self.assertIn("proxy-confs", tasks[tasks.index("Deploy Kanban UI nginx proxy config") : tasks.index("Deploy Kanban UI private proxy marker include")])
        self.assertIn("no_log: true", inc)
        self.assertIn("diff: false", inc)
        # The vhost task itself must not echo rendered content.
        # ROUND-6: no_log/diff are TASK keywords, so they now sit after
        # notify: at task scope; slice to the next task, not to notify:.
        vhost = tasks[tasks.index("Deploy Kanban UI nginx proxy config") :]
        vhost = vhost[: vhost.index("\n    - name:", 10)]
        self.assertIn("no_log: true", vhost)
        self.assertIn("diff: false", vhost)
        # The vault variable exists in group_vars (vault-encrypted block).
        gv = (HERE.parent.parent.parent / "group_vars" / "all" / "vars.yaml").read_text()
        self.assertIn("kanban_ui_proxy_secret: !vault |", gv)

    def test_secret_mode_regression_gate_exists(self):
        # Round-5 finding 1 REGRESSION CHECK: the playbook must assert
        # the EFFECTIVE deployed modes of every secret-bearing file on
        # BOTH hosts (marker include 0600 on proxy, app marker file +
        # token 0400 on app host), failing on any group/world bit.
        # ROUND-7 finding 1b: the gate must sit AFTER the SWAG-restart
        # flush_handlers so it reads the EFFECTIVE post-init mode (the
        # gate itself passing on a just-written 0600 while the file was
        # really 0620 post-restart was the gate-honesty finding).
        tasks = self.TASKS.read_text()
        stat = tasks[tasks.index("Read effective modes of secret-bearing files") :]
        stat = stat[: stat.index("\n    - name:", 10) if "\n    - name:" in stat[10:] else len(stat)]
        for path_marker in (
            "internal/kanban_ui_marker.conf",
            "kanban_ui_proxy_marker_file",
            "kanban_ui_token_file",
            "proxy-confs/kanban_ui.subdomain.conf",
        ):
            self.assertIn(path_marker, stat)
        gate = tasks[tasks.index("Fail if any secret-bearing file is group/world readable") :]
        self.assertIn("selectattr('item.private')", gate)
        self.assertIn("map(attribute='stat.mode')", gate)
        # ORDERING: both gate tasks come after the LAST flush_handlers
        # (the SWAG-restart flush) and after the marker include task.
        flush = tasks.rindex("meta: flush_handlers")
        self.assertLess(flush, tasks.index("Read effective modes of secret-bearing files"))
        self.assertLess(flush, tasks.index("Fail if any secret-bearing file"))
        self.assertLess(
            tasks.index("Deploy Kanban UI private proxy marker include"), flush
        )
        # ROUND-7 finding 2: the bad-login probe must tolerate the brief
        # 443 downtime after that restart (until/retries, like siblings).
        neg = tasks[tasks.index("Verify a bad login cannot mint a session") :]
        neg = neg[: neg.index("\n- name:") if "\n- name:" in neg[10:] else len(neg)]
        self.assertIn("until: kanban_ui_login_negative.status in [401, 429]", neg)
        self.assertIn("retries: \"{{ kanban_ui_readiness_retries }}\"", neg)

    PEER = "192.0.2.7"

    def test_peer_predicate(self):
        # Round-5 finding 1: the REAL TCP peer must be the proxy host.
        # (192.0.2.x is TEST-NET-1 documentation space, never a real
        # address; synthetic like every other fixture here.)
        self.assertTrue(server.login_peer_allowed(self.PEER, self.PEER))
        self.assertTrue(server.login_peer_allowed(self.PEER + "  ", self.PEER))
        self.assertFalse(server.login_peer_allowed("192.0.2.9", self.PEER))
        self.assertFalse(server.login_peer_allowed("", self.PEER))
        # Loopback = this host's own verifier, always allowed.
        self.assertTrue(server.login_peer_allowed("127.0.0.1", self.PEER))
        self.assertTrue(server.login_peer_allowed("::1", self.PEER))
        # Unset expectation = developer run, check disabled.
        self.assertTrue(server.login_peer_allowed("10.0.0.1", ""))

    def test_login_route_peer_gate_order(self):
        # The peer gate runs alongside the marker/transport gates:
        # BEFORE throttle, body parse, and any compare_digest, and it
        # reads req.client (the real socket peer), never a header.
        src = SERVER.read_text()
        start = src.index('@app.post("/login")')
        end = src.index('@app.post("/logout")')
        route = src[start:end]
        peer_gate = route.index("login_peer_allowed(")
        for cred_touch in ("_login_throttled()", "await req.json()",
                           "login_token_matches(presented, secret)"):
            self.assertLess(peer_gate, route.index(cred_touch))
        self.assertIn("req.client.host", route[peer_gate : peer_gate + 400])
        self.assertIn("status_code=403", route[peer_gate : peer_gate + 500])
        # X-Forwarded-For (client-spoofable) must never feed the peer gate.
        self.assertNotIn("x-forwarded-for", route[: peer_gate + 400].lower())

    def test_compose_sets_trusted_proxy_ip(self):
        # Deployment always activates the peer check (the env key is
        # present with the resolved proxy address; only its ABSENCE on
        # a dev laptop disables the predicate).
        compose = self.COMPOSE.read_text()
        self.assertIn('KANBAN_UI_TRUSTED_PROXY_IP: "{{ kanban_ui_proxy_ip }}"', compose)


    def test_deployment_restricts_port_source_and_pin(self):
        tasks = self.TASKS.read_text()
        self.assertIn("kanban_ui_proxy_ip", tasks)
        allow = tasks[tasks.index("Allow the Kanban UI port through UFW from the proxy only") :]
        self.assertIn("from_ip: \"{{ kanban_ui_proxy_ip }}\"", allow[:600])
        # The legacy source-Anywhere rule is deleted declaratively.
        self.assertIn("Delete the legacy Anywhere UFW rule", tasks)
        compose = self.COMPOSE.read_text()
        self.assertIn('"{{ kanban_ui_bind_ip }}:{{ kanban_ui_port }}:{{ kanban_ui_port }}"', compose)

    def test_standalone_playbook_gathers_proxy_facts(self):
        # The UFW rule resolves hostvars[groups['proxy_host'][0]]; a
        # standalone run must gather those facts first or the set_fact
        # explodes undefined.
        play = (HERE.parent.parent.parent / "deploy_kanban_ui.yaml").read_text()
        facts = play.index("hosts: proxy_host")
        self.assertIn("gather_facts: true", play[facts - 200 : facts + 300])
        self.assertLess(facts, play.index("hosts: hermes_host"))


class TestHomepageInfraIndentation(unittest.TestCase):
    """Round-3 finding 2: the Infra section uses 4-space list items /
    8-space properties. The kanban block is inserted directly after
    '^- Infra:' next to 4/8 siblings, so a 2/6 block breaks the whole
    services.yaml (bad indentation of a sequence entry). Locked by
    rendering the ACTUAL template and parsing the merged result."""

    HOMEPAGE_DIR = HERE.parent.parent.parent / "templates" / "homepage"
    TASKS = HERE.parent / "tasks" / "main.yaml"

    def _render(self, name):
        import jinja2

        tpl = (self.HOMEPAGE_DIR / name).read_text()
        return jinja2.Template(tpl).render(internal_kanban_url="https://kanban.invalid")

    def test_rendered_block_parses_under_infra(self):
        import yaml

        rendered = self._render("kanban_ui_service.yaml.j2")
        merged = yaml.safe_load("services:\n- Infra:\n" + rendered)
        entry = merged["services"][0]["Infra"][0]["Kanban UI"]
        self.assertEqual(entry["href"], "https://kanban.invalid")
        self.assertEqual(entry["icon"], "lucide:kanban")

    def test_indentation_matches_infra_siblings(self):
        kanban = self._render("kanban_ui_service.yaml.j2").splitlines()
        omada = self._render("omada_sdn_service.yaml.j2").splitlines()
        self.assertTrue(kanban[0].startswith("    - "), kanban[0])
        self.assertTrue(omada[0].startswith("    - "), omada[0])
        self.assertEqual(len(kanban[1]) - len(kanban[1].lstrip()), 8)
        self.assertEqual(len(omada[1]) - len(omada[1].lstrip()), 8)

    def test_tasks_insert_after_infra_anchor(self):
        src = self.TASKS.read_text()
        block = src[src.index("Add Kanban UI service to Homepage") :]
        self.assertIn('_homepage_insertafter: "^- Infra:"', block[:600])


if __name__ == "__main__":
    unittest.main(verbosity=2)
