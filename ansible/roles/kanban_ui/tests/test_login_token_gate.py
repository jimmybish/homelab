#!/usr/bin/env python3
"""Security regression tests for the bootstrap-token comparison gate.

PR#13 review finding (comment 4301554425): the /login route compared the
JSON body token with the raw expression
``secrets.compare_digest(presented, secret)``. compare_digest only accepts
ASCII *strings*, so a non-ASCII token such as "é" -- which passes the
``isinstance(presented, str)`` check -- raised TypeError. Starlette turned
that into a 500, and because the exception escaped before the
``login_failures.append`` on the invalid-credentials branch, the
brute-force throttle never engaged for exactly the inputs an attacker
would use. server.py now compares UTF-8 *bytes* in the pure predicate
``login_token_matches`` and treats encoding/compare failures as invalid
credentials (401 + failed-attempt counter).

    python3 ansible/roles/kanban_ui/tests/test_login_token_gate.py

Exit 0 = all pass. Stdlib only on the control node (same harness rules as
test_ws_origin.py). The end-to-end classes additionally need fastapi (and
the mounted plugin router) and skip when absent; they are executed inside
the deployed container where both exist:

    docker cp test_login_token_gate.py kanban-ui:/tmp/
    docker exec -e KANBAN_UI_SERVER_PATH=/app/server.py kanban-ui \
        python /tmp/test_login_token_gate.py
"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Deployed-container runs override the server path (the image ships
# /app/server.py and has no tests/ tree); control-node runs use the
# vendored source next to the role.
SERVER = Path(os.environ.get("KANBAN_UI_SERVER_PATH", str(HERE.parent / "files" / "app" / "server.py")))

# server.py resolves REPO/TOKEN_FILE from env at import time; point them
# at throwaway paths so importing never touches a real Hermes home or
# token. In the container, the real KANBAN_UI_HERMES_REPO is already set
# by compose (the mounted checkout) and must be kept for plugin import.
os.environ.setdefault("KANBAN_UI_HERMES_REPO", tempfile.mkdtemp(prefix="kbui-test-repo-"))
os.environ.setdefault("KANBAN_UI_TOKEN_FILE", str(SERVER))  # exists; never read by import

_spec = importlib.util.spec_from_file_location("kanban_ui_server", SERVER)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)

SECRET = "synthetic-test-secret-0123456789abcdef"
MARKER = "synthetic-marker-value-0123456789abcdef-DO-NOT-REUSE"
# Non-ASCII presentation the finding names, plus a lone surrogate:
# json.loads admits "\\udXXX" escapes, and a lone surrogate cannot even be
# UTF-8 encoded, so the compare and the encode both have to be guarded.
NON_ASCII = "\u00e9"
LONE_SURROGATE = "\ud800"


def _fastapi_available() -> bool:
    import importlib.util as ilu

    return ilu.find_spec("fastapi") is not None


class TestTokenPredicate(unittest.TestCase):
    """Pure predicate: refuse every non-matching presentation, never raise."""

    def test_ascii_mismatch_refused(self):
        self.assertFalse(server.login_token_matches("wrong", SECRET))
        # Constant-time hygiene: equal-length hostile inputs behave
        # exactly like unequal ones.
        self.assertFalse(server.login_token_matches(SECRET[:-1] + "x", SECRET))

    def test_exact_match_passes(self):
        self.assertTrue(server.login_token_matches(SECRET, SECRET))

    def test_non_ascii_token_refused_not_raised(self):
        # THE finding: pre-fix this raised TypeError (500, no counter).
        self.assertFalse(server.login_token_matches(NON_ASCII, SECRET))
        self.assertFalse(server.login_token_matches("caf\u00e9-" + SECRET, SECRET))
        # Non-ASCII on the SECRET side must not crash the compare either.
        self.assertFalse(server.login_token_matches(SECRET, NON_ASCII))
        # Multi-byte / astral / RTL presentations.
        self.assertFalse(server.login_token_matches("\u65e5\u672c\u8a9e", SECRET))
        self.assertFalse(server.login_token_matches("\U0001f600", SECRET))

    def test_lone_surrogate_refused_not_raised(self):
        # json.loads admits these; they are unencodable in UTF-8.
        self.assertFalse(server.login_token_matches(LONE_SURROGATE, SECRET))
        self.assertFalse(server.login_token_matches(SECRET[:4] + LONE_SURROGATE, SECRET))
        # A pair of surrogates (valid-looking JSON escape) too.
        self.assertFalse(server.login_token_matches("\ud800\udc00", SECRET))

    def test_non_string_and_empty_refused(self):
        for junk in (None, 42, 3.5, True, [], {}, ["x"], b" bytes"):
            self.assertFalse(server.login_token_matches(junk, SECRET), repr(junk))
        self.assertFalse(server.login_token_matches("", SECRET))

    def test_unicode_equal_returns_true(self):
        # The compare runs on bytes, so a genuinely matching non-ASCII
        # secret/pair must still pass (guards against an over-eager
        # "reject anything non-ASCII" implementation).
        secret = "s\u00e9cret-\u65e5\U0001f600"
        self.assertTrue(server.login_token_matches(secret, secret))


class TestRouteWiring(unittest.TestCase):
    """Static locks: the route uses the guarded predicate, and the
    invalid branch still feeds the throttle."""

    def _route(self):
        src = SERVER.read_text()
        start = src.index('@app.post("/login")')
        end = src.index('@app.post("/logout")')
        return src[start:end]

    def test_route_uses_guarded_predicate(self):
        route = self._route()
        self.assertIn("login_token_matches(presented, secret)", route)
        # The raw string-vs-string compare is GONE from the route: a
        # regression back to it would silently re-open the 500 path.
        self.assertNotIn("compare_digest(presented, secret)", route)
        self.assertNotIn("compare_digest(", route)

    def test_gate_runs_after_transport_gates_before_session_mint(self):
        route = self._route()
        match = route.index("login_token_matches(")
        for cred_gate in ("login_proxy_marked(", "login_peer_allowed(",
                          "login_transport_allowed(", "_login_throttled()",
                          "await req.json()"):
            self.assertLess(route.index(cred_gate), match)
        # A successful match is the ONLY path to session minting.
        self.assertLess(match, route.index("secrets.token_urlsafe(32)"))

    def test_invalid_branch_feeds_failed_attempt_counter(self):
        route = self._route()
        match = route.index("login_token_matches(")
        tail = route[match:]
        self.assertLess(tail.index("if not ok:"), tail.index("login_failures.append(time.time())"))
        self.assertLess(tail.index("login_failures.append(time.time())"),
                        tail.index('status_code=401'))
        # No session material on the failure path.
        self.assertLess(tail.index('status_code=401'),
                        tail.index("secrets.token_urlsafe(32)"))

    def test_predicate_compares_utf8_bytes_and_swallows_encoding_failures(self):
        src = SERVER.read_text()
        start = src.index("def login_token_matches")
        fn = src[start: src.index("\ndef ", start + 10)]
        self.assertIn('presented.encode("utf-8")', fn)
        self.assertIn('secret.encode("utf-8")', fn)
        self.assertIn("except (UnicodeEncodeError, TypeError):", fn)
        self.assertIn("return False", fn)
        # Deny-by-default shape: non-str/empty never reaches a compare.
        self.assertIn('if not isinstance(presented, str) or presented == "":', fn)


class TestLoginEndpoint(unittest.TestCase):
    """End-to-end through the real ASGI app (skips on the control node
    without fastapi; runs inside the container). No sockets: the app is
    driven directly over raw ASGI so no port, proxy, or secret file is
    needed. The proxy gates are satisfied the same way the verifier does
    it -- marker header + forwarded proto -- with synthetic values."""

    def _post_login(self, token_value, *, marker=MARKER, proto="https"):
        import asyncio

        app = server.build_app(SECRET, MARKER, "", "")
        body = json.dumps({"token": token_value}).encode("utf-8")
        headers = [(b"host", b"kanban.test.invalid"), (b"content-type", b"application/json")]
        if marker is not None:
            headers.append((b"x-kanban-proxy-marker", marker.encode("latin-1")))
        if proto is not None:
            headers.append((b"x-forwarded-proto", proto.encode("latin-1")))
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "POST", "path": "/login",
            "raw_path": b"/login", "query_string": b"", "root_path": "",
            "scheme": "https", "client": ("127.0.0.1", 54321),
            "server": ("kanban.test.invalid", 443), "headers": headers,
        }
        sent = []

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            sent.append(message)

        async def run():
            await app(scope, receive, send)

        asyncio.run(run())
        status = None
        for m in sent:
            if m["type"] == "http.response.start":
                status = m["status"]
        self.assertIsNotNone(status, "app produced no response-start message")
        return status

    def setUp(self):
        # The loopback-dev opt-out must never be set in a test run: it
        # silently disables the proxy gates this class depends on.
        self.assertNotIn("KANBAN_UI_COOKIE_INSECURE", os.environ)

    def test_proxied_correct_token_mints_session(self):
        self.assertEqual(self._post_login(SECRET), 200)

    def test_non_ascii_token_is_401_not_500(self):
        # THE finding, end to end: pre-fix this raised TypeError inside
        # the route and Starlette answered 500.
        self.assertEqual(self._post_login(NON_ASCII), 401)
        self.assertEqual(self._post_login("\u65e5\u672c\u8a9e"), 401)
        self.assertEqual(self._post_login("\U0001f600"), 401)

    def test_lone_surrogate_token_is_401_not_500(self):
        self.assertEqual(self._post_login(LONE_SURROGATE), 401)
        self.assertEqual(self._post_login("\ud800\udc00"), 401)

    def test_empty_and_missing_and_wrong_type_are_401(self):
        self.assertEqual(self._post_login(""), 401)
        self.assertEqual(self._post_login("wrong-but-ascii"), 401)

    def test_failed_attempts_engage_lockout(self):
        # Every hostile presentation must flow through the counter: after
        # LOGIN_FAIL_LIMIT failures the throttle answers 429 even to the
        # CORRECT token. Pre-fix, non-ASCII attempts skipped the append,
        # so an attacker could probe the limit indefinitely without ever
        # tripping it.
        import asyncio

        app = server.build_app(SECRET, MARKER, "", "")
        n = server.LOGIN_FAIL_LIMIT

        def post(token_value):
            body = json.dumps({"token": token_value}).encode("utf-8")
            headers = [
                (b"host", b"kanban.test.invalid"),
                (b"content-type", b"application/json"),
                (b"x-kanban-proxy-marker", MARKER.encode("latin-1")),
                (b"x-forwarded-proto", b"https"),
            ]
            scope = {
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1", "method": "POST", "path": "/login",
                "raw_path": b"/login", "query_string": b"", "root_path": "",
                "scheme": "https", "client": ("127.0.0.1", 54321),
                "server": ("kanban.test.invalid", 443), "headers": headers,
            }
            sent = []

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                sent.append(message)

            async def run():
                await app(scope, receive, send)

            asyncio.run(run())
            return next(m["status"] for m in sent if m["type"] == "http.response.start")

        hostile = [NON_ASCII, LONE_SURROGATE, "", "wrong", "\U0001f600"]
        for i in range(n):
            self.assertIn(post(hostile[i % len(hostile)]), (401, 429),
                          f"attempt {i} escaped 401/429 -- counter path broken")
        # Counter tripped: even the correct token now waits behind the throttle.
        self.assertEqual(post(SECRET), 429)


if __name__ == "__main__":
    if not _fastapi_available():
        TestLoginEndpoint.__unittest_skip__ = True
        TestLoginEndpoint.__unittest_skip_why__ = "fastapi absent on control node; runs inside the container"
    unittest.main(verbosity=2)
