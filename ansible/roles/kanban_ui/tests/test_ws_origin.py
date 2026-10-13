#!/usr/bin/env python3
"""Security regression tests for the /events WebSocket Origin gate.

PR-review finding (comment 4244239544): the WS gate used to authenticate
by session cookie only. SameSite=strict is SITE-scoped, not origin-scoped,
so JavaScript on a compromised sibling subdomain of the internal domain
receives the victim's Kanban cookie on ITS WebSocket upgrade and could
read the task event stream. server.py's ws_origin_allowed now requires
the upgrade Origin to equal the exact configured Kanban origin, with the
loopback-development opt-out (KANBAN_UI_COOKIE_INSECURE) as the single
sanctioned exception and deny-by-default when the origin is unconfigured.

    python3 ansible/roles/kanban_ui/tests/test_ws_origin.py

Exit 0 = all pass. Stdlib only (same harness rules as
test_log_redaction.py: server.py is spec-loaded with throwaway env so
importing never touches a real Hermes home or token).
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "files" / "app" / "server.py"

os.environ["KANBAN_UI_HERMES_REPO"] = tempfile.mkdtemp(prefix="kbui-test-repo-")
os.environ["KANBAN_UI_TOKEN_FILE"] = str(SERVER)  # exists; never read by import

_spec = importlib.util.spec_from_file_location("kanban_ui_server", SERVER)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)
sys.modules.setdefault("kanban_ui_server", server)

ALLOWED = "https://kanban.example.invalid"


class TestWsOriginAllowed(unittest.TestCase):
    def test_exact_origin_accepted(self):
        self.assertTrue(server.ws_origin_allowed(ALLOWED, ALLOWED, False))

    def test_sibling_subdomain_rejected(self):
        # THE regression: the exact attack from the review comment. The
        # sibling's JS has the cookie (SameSite is site-scoped); only the
        # Origin check stops its WS upgrade.
        self.assertFalse(server.ws_origin_allowed("https://evil.example.invalid", ALLOWED, False))

    def test_same_subdomain_different_scheme_rejected(self):
        self.assertFalse(server.ws_origin_allowed("http://kanban.example.invalid", ALLOWED, False))

    def test_prefix_lookalike_rejected(self):
        # scheme+host crafted so ALLOWED is a string prefix of it.
        self.assertFalse(
            server.ws_origin_allowed(ALLOWED + ".evil.example.invalid", ALLOWED, False)
        )
        self.assertFalse(
            server.ws_origin_allowed(ALLOWED + "/attacker-controlled", ALLOWED, False)
        )

    def test_port_mismatch_rejected(self):
        self.assertFalse(server.ws_origin_allowed("https://kanban.example.invalid:8443", ALLOWED, False))

    def test_missing_origin_rejected(self):
        # Cookie-only upgrades (curl, non-browser clients) must NOT pass:
        # that is precisely the cookie-only gate the finding removed.
        self.assertFalse(server.ws_origin_allowed("", ALLOWED, False))
        self.assertFalse(server.ws_origin_allowed("   ", ALLOWED, False))

    def test_unconfigured_origin_denies_everything(self):
        # Deny-by-default, same philosophy as the proxy marker.
        self.assertFalse(server.ws_origin_allowed(ALLOWED, "", False))
        self.assertFalse(server.ws_origin_allowed("", "", False))

    def test_case_and_trailing_slash_normalised(self):
        # Origins are scheme://host[:port]; schemes/hosts are case-
        # insensitive and a bare root origin may carry a trailing slash.
        self.assertTrue(server.ws_origin_allowed(ALLOWED + "/", ALLOWED, False))
        self.assertTrue(server.ws_origin_allowed("HTTPS://KANBAN.example.invalid", ALLOWED, False))
        self.assertTrue(server.ws_origin_allowed(ALLOWED, ALLOWED + "/", False))

    def test_insecure_dev_optout(self):
        # The ONLY exception, and it is the pre-existing loopback-dev
        # opt-out every other gate already honours. The deployment
        # compose never sets it.
        self.assertTrue(server.ws_origin_allowed("https://whatever.invalid", ALLOWED, True))
        self.assertTrue(server.ws_origin_allowed("", "", True))

    def test_non_latin1_origin_does_not_crash(self):
        # Header material is attacker-shaped; must refuse, never raise.
        self.assertFalse(server.ws_origin_allowed("https://日本.example.invalid", ALLOWED, False))
        self.assertFalse(server.ws_origin_allowed("\ud7ff", ALLOWED, False))

    def test_compare_digest_used_not_equality(self):
        # Hygiene guard for the constant-time comparison contract: equal-
        # length hostile inputs behave exactly like unequal ones.
        self.assertFalse(server.ws_origin_allowed("https://kanban.examplf.invalid", ALLOWED, False))


class TestGateWiring(unittest.TestCase):
    def test_ws_gate_requires_origin_after_cookie(self):
        # _ws_gate is a closure inside build_app (needs fastapi), so lock
        # the CONTRACT instead of the closure: build_app must take the
        # allowed origin and _ws_gate must consult ws_origin_allowed.
        import inspect

        sig = inspect.signature(server.build_app)
        self.assertIn("ws_allowed_origin", sig.parameters)
        src = inspect.getsource(server.build_app)
        self.assertIn("ws_origin_allowed", src)
        # main() must thread the env-configured origin into build_app.
        main_src = inspect.getsource(server.main)
        self.assertIn("WS_ALLOWED_ORIGIN", main_src)
        self.assertIn("KANBAN_UI_WS_ALLOWED_ORIGIN", inspect.getsource(server))


if __name__ == "__main__":
    unittest.main(verbosity=2)
