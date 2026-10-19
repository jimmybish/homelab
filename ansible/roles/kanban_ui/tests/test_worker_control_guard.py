#!/usr/bin/env python3
"""Security regression tests for the worker-control deny-list.

PR#13 review finding (comment 4301554389): mounting the full plugin
router exposed worker termination, reclaim, and reclaim-first reassignment
inside this isolated container. In the pinned Hermes code
``reclaim_task()`` clears the claim and returns the task to ready
unconditionally, while ``_terminate_reclaimed_worker()`` skips signalling
whenever the claim's hostname differs from the container's hostname. A
UI-side reclaim therefore left the HOST worker running while the
dispatcher could start a second worker for the same task. server.py now
refuses those three operations server-side (403, before any DB access)
and static/app.js disables the corresponding controls.

    python3 ansible/roles/kanban_ui/tests/test_worker_control_guard.py

Exit 0 = all pass. Stdlib only on the control node (same harness rules as
test_ws_origin.py). The end-to-end wiring class additionally needs
fastapi (and the mounted plugin router) and skips when absent; it is
executed inside the deployed container where both exist:

    docker cp test_worker_control_guard.py kanban-ui:/tmp/
    docker exec -e KANBAN_UI_SERVER_PATH=/app/server.py kanban-ui \
        python /tmp/test_worker_control_guard.py
"""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Deployed-container runs override the server path (the image ships
# /app/server.py and has no tests/ tree); control-node runs use the
# vendored source next to the role.
SERVER = Path(os.environ.get("KANBAN_UI_SERVER_PATH", str(HERE.parent / "files" / "app" / "server.py")))
APPJS = SERVER.parent / "static" / "app.js"

# server.py resolves REPO/TOKEN_FILE from env at import time; point them
# at throwaway paths so importing never touches a real Hermes home or
# token. In the container, the real KANBAN_UI_HERMES_REPO is already set
# by compose (the mounted checkout) and must be kept for plugin import.
os.environ.setdefault("KANBAN_UI_HERMES_REPO", tempfile.mkdtemp(prefix="kbui-test-repo-"))
os.environ.setdefault("KANBAN_UI_TOKEN_FILE", str(SERVER))  # exists; never read by import

_spec = importlib.util.spec_from_file_location("kanban_ui_server", SERVER)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)
sys_modules_name = "kanban_ui_server"


def _fastapi_available() -> bool:
    import importlib.util as ilu

    return ilu.find_spec("fastapi") is not None


TASK_ID = "t_deadbeef"


class TestPathPredicate(unittest.TestCase):
    """The pure (method, path) deny-list -- exact paths, no prefix games."""

    def test_reclaim_denied(self):
        self.assertTrue(server.worker_control_denied("POST", f"/api/kanban/tasks/{TASK_ID}/reclaim"))

    def test_run_terminate_denied(self):
        self.assertTrue(server.worker_control_denied("POST", "/api/kanban/runs/41/terminate"))

    def test_method_case_and_padding_tolerated(self):
        self.assertTrue(server.worker_control_denied(" post ", f"/api/kanban/tasks/{TASK_ID}/reclaim"))

    def test_wrong_method_not_denied(self):
        self.assertFalse(server.worker_control_denied("GET", f"/api/kanban/tasks/{TASK_ID}/reclaim"))
        self.assertFalse(server.worker_control_denied("DELETE", "/api/kanban/runs/41/terminate"))

    def test_lookalike_paths_not_denied(self):
        # Over/under-match guards: the deny-list must be exactly the two
        # worker-control paths, nothing the UI legitimately needs.
        self.assertFalse(server.worker_control_denied("POST", f"/api/kanban/tasks/{TASK_ID}/reclaim/extra"))
        self.assertFalse(server.worker_control_denied("POST", "/api/kanban/tasks/" + TASK_ID + "/reclaim/"))
        self.assertFalse(server.worker_control_denied("POST", f"/api/kanban/tasks/{TASK_ID}/comments"))
        self.assertFalse(server.worker_control_denied("POST", f"/api/kanban/tasks/{TASK_ID}"))
        self.assertFalse(server.worker_control_denied("POST", f"/api/kanban/tasks/{TASK_ID}/reassign"))
        self.assertFalse(server.worker_control_denied("POST", "/api/kanban/runs/41/inspect"))
        self.assertFalse(server.worker_control_denied("POST", "/api/kanban/dispatch"))
        self.assertFalse(server.worker_control_denied("POST", "/login"))
        # Empty/garbage input must refuse cleanly, never raise.
        self.assertFalse(server.worker_control_denied("", ""))
        self.assertFalse(server.worker_control_denied(None, None))

    def test_segments_wildcard_only_over_single_segment(self):
        # [^/]+ keeps a path-traversal task id from smuggling another op.
        self.assertFalse(
            server.worker_control_denied("POST", "/api/kanban/tasks/x/../../runs/1/terminate")
        )


class TestReassignPredicate(unittest.TestCase):
    """reclaim_first is body-driven: accept the validated model or a
    dict; anything unrecognised FAILS CLOSED."""

    def test_dict_reclaim_first_true_denied(self):
        self.assertTrue(server.worker_control_reassign_denied({"reclaim_first": True}))

    def test_dict_reclaim_first_false_allowed(self):
        self.assertFalse(server.worker_control_reassign_denied({"reclaim_first": False}))

    def test_dict_missing_key_allowed(self):
        # Upstream default is False -- a payload that never asks for the
        # reclaim half is the plain reassign the finding keeps alive.
        self.assertFalse(server.worker_control_reassign_denied({"profile": "virtuajimmy"}))

    def test_model_object_supported(self):
        class Payload:
            reclaim_first = True

        self.assertTrue(server.worker_control_reassign_denied(Payload()))
        Payload.reclaim_first = False
        self.assertFalse(server.worker_control_reassign_denied(Payload()))

    def test_unrecognised_shape_fails_closed(self):
        for junk in (None, "reclaim", 42, ["reclaim_first"], object()):
            self.assertTrue(server.worker_control_reassign_denied(junk), repr(junk))


class TestRejectBody(unittest.TestCase):
    def test_reject_body_explains_and_points_to_host_cli(self):
        detail = server.WORKER_CONTROL_REJECT_BODY["detail"]
        self.assertIn("disabled", detail)
        self.assertIn("hermes kanban reclaim", detail)
        # No credential material may ever leak into a reject body.
        self.assertNotIn("token", detail.lower())


class TestServerWiringContract(unittest.TestCase):
    """Static locks on build_app's wiring (contract, not closure): the
    middleware consults the predicate, the reassign wrapper is swapped
    into dependant.call, and BOTH attach paths fail CLOSED."""

    def test_middleware_uses_predicate_and_rejects_403(self):
        src = SERVER.read_text()
        mw = src[src.index("async def worker_control_mw"):]
        mw = mw[: mw.index("@app.middleware")]
        self.assertIn("worker_control_denied(request.method, request.url.path)", mw)
        self.assertIn("status_code=403", mw)
        self.assertIn("WORKER_CONTROL_REJECT_BODY", mw)

    def test_auth_middleware_is_outermost(self):
        # Starlette makes the LAST-registered middleware the OUTERMOST.
        # auth_mw must be registered after worker_control_mw so an
        # anonymous probe hits the session gate (401) before the deny
        # list -- the reject body never tells strangers anything.
        src = SERVER.read_text()
        self.assertLess(
            src.index("async def worker_control_mw"),
            src.index("async def auth_mw"),
        )

    def test_reassign_guard_swaps_dependant_call(self):
        src = SERVER.read_text()
        self.assertIn("_reassign_route.dependant.call = _reassign_guarded_call", src)
        self.assertIn("_reassign_guarded_call._worker_control_guarded = True", src)
        # The wrapper must consult the body predicate with fail-closed
        # key lookup (payload is upstream's parameter name; body kept as
        # a defensive alias).
        self.assertIn('worker_control_reassign_denied(values.get("payload", values.get("body")))', src)

    def test_attach_failures_refuse_to_boot(self):
        # Fail closed on router drift: wrong route count / missing
        # dependant / dispatch-shape mismatch all raise SystemExit
        # instead of serving the reassign op unguarded.
        src = SERVER.read_text()
        self.assertIn("len(_reassign_routes) != 1", src)
        guard_block = src[src.index("_reassign_path ="):]
        self.assertGreaterEqual(guard_block.count("raise SystemExit"), 2)
        self.assertIn("is_coroutine_callable", guard_block)

    def test_wrapper_preserves_sync_dispatch_shape(self):
        # The pinned upstream endpoint is a sync def; FastAPI dispatches
        # dependant.call per dependant.is_coroutine_callable captured at
        # route construction. Swapping in an async wrapper for a sync
        # route (or vice versa) would break threadpool dispatch; the
        # code must branch on _upstream_is_async.
        src = SERVER.read_text()
        self.assertIn("if _upstream_is_async:", src)
        guard = src[src.index("if _upstream_is_async:"):]
        guard = guard[: guard.index("_reassign_guarded_call.__name__")]
        self.assertIn("await _reassign_upstream_call(**values)", guard)
        self.assertIn("return _reassign_upstream_call(**values)", guard)
        # And the JSONResponse 403 must exist in BOTH branches.
        self.assertEqual(guard.count("status_code=403"), 2)


class TestUiControlsDisabled(unittest.TestCase):
    """static/app.js must no longer reach the three refused ops."""

    def test_no_reclaim_or_terminate_calls(self):
        src = APPJS.read_text()
        self.assertNotIn("/reclaim", src)
        self.assertNotIn("/terminate", src)

    def test_reclaim_first_never_true(self):
        src = APPJS.read_text()
        # Both reassign call sites hard-code false (modal + drawer).
        # Count only executable lines -- the explanatory comments
        # mention the literal too.
        exec_lines = [ln for ln in src.splitlines() if not ln.strip().startswith("//")]
        self.assertEqual(sum(ln.count("reclaim_first: false") for ln in exec_lines), 2)
        self.assertNotIn("reclaim_first: reclaim.checked", src)
        self.assertNotIn('reclaim_first: t.status === "running"', src)

    def test_controls_present_but_disabled(self):
        src = APPJS.read_text()
        # The affordances stay visible with an explanation (finding asked
        # for "disable their UI controls until supported", not removal
        # of the entire reassign UX).
        self.assertIn("disabled in the WebUI", src)
        self.assertIn('disabled: ""', src)
        self.assertIn("hermes kanban reassign", src)  # host CLI pointer in the title


class TestLiveWiring(unittest.TestCase):
    """End-to-end through the REAL assembled app: the three ops return
    403 with the documented body BEFORE any DB touch, unaffected ops
    still work, and the guard refuses to boot if it cannot attach.
    Needs fastapi + the mounted plugin router (runs inside the deployed
    container; skipped on a bare control node)."""

    @classmethod
    def setUpClass(cls):
        import importlib.util as ilu

        if not _fastapi_available():
            raise unittest.SkipTest("fastapi not importable here (run inside the container)")
        # The ASGI harness logs in over plain ASGI (no SWAG marker header
        # is meaningful here); the loopback-dev opt-out is the sanctioned
        # way to exercise the credential endpoint without the proxy
        # trust stack. This is a SEPARATE process from the running server
        # (docker exec python /tmp/...), so setting it cannot weaken the
        # live container, which never has this variable.
        os.environ["KANBAN_UI_COOKIE_INSECURE"] = "1"
        try:
            cls.app = server.build_app("synthetic-test-secret-0123456789abcdef", "", "", "")
        except Exception as e:  # plugin router unimportable outside the container
            raise unittest.SkipTest(f"plugin router not importable here: {e!r}")

    # -- minimal stdlib ASGI client (httpx is not in the pinned image) --
    def _request(self, method, path, cookie="", header=True, json_body=None):
        import asyncio
        import json as _json

        headers = []
        if cookie:
            headers.append((b"cookie", f"kanban_ui_session={cookie}".encode()))
        if header:
            headers.append((b"x-kanban-ui", b"1"))
        body = b"" if json_body is None else _json.dumps(json_body).encode()
        if body:
            headers.append((b"content-type", b"application/json"))
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "path": path, "raw_path": path.encode(),
            "query_string": b"", "root_path": "", "scheme": "https",
            "server": ("testserver", 443), "client": ("192.0.2.1", 1234),
            "headers": headers,
        }
        sent = {"status": None, "body": b""}

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                sent["status"] = message["status"]
            elif message["type"] == "http.response.body":
                sent["body"] += message.get("body", b"")

        asyncio.run(self.app(scope, receive, send))
        return sent["status"], sent["body"]

    def setUp(self):
        # Mint a session by calling /login directly with a header-capture
        # send (setUp keeps the harness method itself for op probes).
        import asyncio
        import json as _json

        body = _json.dumps({"token": "synthetic-test-secret-0123456789abcdef"}).encode()
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "path": "/login", "raw_path": b"/login",
            "query_string": b"", "root_path": "", "scheme": "https",
            "server": ("testserver", 443), "client": ("127.0.0.1", 1234),
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
        captured = {}

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                captured["headers"] = dict(message.get("headers", []))

        asyncio.run(self.app(scope, receive, send))
        raw = captured["headers"].get(b"set-cookie", b"")
        self.assertEqual(captured.get("status"), 200, "test login must mint a session")
        self._cookie = raw.decode().split("kanban_ui_session=")[1].split(";")[0]

    def test_anonymous_reclaim_still_gets_session_401_first(self):
        # Middleware ordering lock: auth is outermost; the deny-list
        # never answers strangers.
        status, _ = self._request("POST", f"/api/kanban/tasks/{TASK_ID}/reclaim", cookie="", header=True)
        self.assertEqual(status, 401)

    def test_reclaim_rejected_403(self):
        status, body = self._request("POST", f"/api/kanban/tasks/{TASK_ID}/reclaim", cookie=self._cookie)
        self.assertEqual(status, 403)
        self.assertIn(b"worker control operations are disabled", body)

    def test_run_terminate_rejected_403(self):
        status, body = self._request("POST", "/api/kanban/runs/1/terminate", cookie=self._cookie)
        self.assertEqual(status, 403)
        self.assertIn(b"worker control operations are disabled", body)

    def test_reassign_reclaim_first_rejected_403(self):
        status, body = self._request(
            "POST", f"/api/kanban/tasks/{TASK_ID}/reassign",
            cookie=self._cookie, json_body={"profile": "virtuajimmy", "reclaim_first": True},
        )
        self.assertEqual(status, 403)
        self.assertIn(b"worker control operations are disabled", body)

    def test_plain_reassign_and_reads_still_work(self):
        # Unaffected ops must reach their HANDLERS (not the guard): an
        # unknown synthetic task yields the upstream 409/404-class
        # status, and the board read answers 200 -- proving the guard
        # rejects only the three refused operations.
        status, _ = self._request(
            "POST", f"/api/kanban/tasks/{TASK_ID}/reassign",
            cookie=self._cookie, json_body={"profile": "virtuajimmy"},
        )
        self.assertIn(status, (200, 404, 409))
        status, _ = self._request("GET", "/api/kanban/board", cookie=self._cookie, header=False)
        self.assertIn(status, (200, 400))

    def test_csrf_guard_precedes_guard(self):
        # Cookie without the session marker header: CSRF gate (403) fires
        # before the op-specific guard; body differs from the deny-list
        # reject to prove WHICH 403 answered.
        status, body = self._request(
            "POST", f"/api/kanban/tasks/{TASK_ID}/reclaim",
            cookie=self._cookie, header=False,
        )
        self.assertEqual(status, 403)
        self.assertNotIn(b"worker control operations", body)

    def test_dependant_call_marked_and_upstream_preserved(self):
        # fastapi 0.133.1 keeps APIRoute in fastapi.routing (top level
        # only exports APIRouter), matching server.py's import.
        from fastapi.routing import APIRoute

        route = next(
            r for r in self.app.routes
            if isinstance(r, APIRoute) and r.path == "/api/kanban/tasks/{task_id}/reassign"
        )
        self.assertTrue(getattr(route.dependant.call, "_worker_control_guarded", False))
        self.assertTrue(callable(route.dependant.call._worker_control_upstream))


class TestGuardAttachesToRealRouter(unittest.TestCase):
    """If build_app cannot attach (renamed upstream endpoint) it must
    refuse to boot. Cheap static proxy for that: the fail-closed branch
    exists AND main() goes through build_app (no path serves the app
    without it)."""

    def test_main_uses_build_app(self):
        src = SERVER.read_text()
        self.assertIn("app = build_app(", src[src.index("def main("):])


if __name__ == "__main__":
    unittest.main(verbosity=2)
