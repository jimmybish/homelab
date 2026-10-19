#!/usr/bin/env python3
"""Security regression tests for the status-driven reclaim guard.

PR#13 review finding (comment 4319617397): the round-1 deny-list
(test_worker_control_guard.py) blocks the EXPLICIT reclaim endpoints, but
the pinned plugin's PATCH /api/kanban/tasks/{id} and POST
/api/kanban/tasks/bulk both dispatch ``status: "ready"`` through
``_drag_to`` -> ``_set_status_direct()``, which for a RUNNING task NULLs
claim_lock/claim_expires/worker_pid, ends the run as 'reclaimed', and
post-commits ``_terminate_reclaimed_worker`` -- a no-op whenever the claim
lock lacks the LOCAL host prefix (always true inside this container). A
running card dragged to Ready therefore released the claim without
signalling the host worker, letting the dispatcher spawn a duplicate
beside it. server.py now wraps ``plugin._set_status_direct`` (the single
chokepoint BOTH update paths share) so running->ready raises
``plugin._StatusRejected`` -- PATCH 400 + actionable detail, bulk per-id
refusal -- and static/app.js disables the corresponding controls.

    python3 ansible/roles/kanban_ui/tests/test_requeue_guard.py

Exit 0 = all pass. Stdlib only on the control node (same harness rules as
test_worker_control_guard.py). The end-to-end wiring class additionally
needs fastapi (and the mounted plugin router) and skips when absent; it
is executed inside the deployed container where both exist:

    docker cp test_requeue_guard.py kanban-ui:/tmp/
    docker exec -e KANBAN_UI_SERVER_PATH=/app/server.py kanban-ui \
        python /tmp/test_requeue_guard.py
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


def _fastapi_available() -> bool:
    import importlib.util as ilu

    return ilu.find_spec("fastapi") is not None


class TestRequeuePredicate(unittest.TestCase):
    """The pure (current, requested) predicate -- exactly the one op."""

    def test_running_to_ready_denied(self):
        self.assertTrue(server.worker_requeue_denied("running", "ready"))

    def test_ordinary_transitions_allowed(self):
        # Everything the finding does NOT raise must pass through: idle
        # drags, running -> other columns, blocked/scheduled reopen.
        for cur, req in (
            ("todo", "ready"), ("ready", "todo"), ("triage", "ready"),
            ("running", "todo"), ("running", "blocked"), ("running", "scheduled"),
            ("running", "review"), ("running", "done"), ("running", "archived"),
            ("blocked", "ready"), ("scheduled", "ready"), ("review", "ready"),
            ("done", "ready"), ("archived", "ready"),
        ):
            self.assertFalse(server.worker_requeue_denied(cur, req), (cur, req))

    def test_non_string_and_absent_pass_through(self):
        # An absent task (get_task -> None via getattr default) is not a
        # running claim; upstream handles not-found. Garbage never denies.
        self.assertFalse(server.worker_requeue_denied(None, "ready"))
        self.assertFalse(server.worker_requeue_denied("", "ready"))
        self.assertFalse(server.worker_requeue_denied(7, "ready"))
        self.assertFalse(server.worker_requeue_denied("running", None))
        self.assertFalse(server.worker_requeue_denied(None, None))

    def test_case_and_padding_not_silently_denied(self):
        # The predicate is exact-match on the DB's canonical lowercase
        # statuses; uppercase variants must NOT deny (upstream would
        # reject the unknown status itself -- the guard is not a parser).
        self.assertFalse(server.worker_requeue_denied("Running", "ready"))
        self.assertFalse(server.worker_requeue_denied("running", "Ready"))


class TestRejectMessage(unittest.TestCase):
    def test_message_explains_and_points_to_host_cli(self):
        msg = server.WORKER_REQUEUE_REJECT_MSG
        self.assertIn("running", msg)
        self.assertIn("ready", msg)
        self.assertIn("hermes kanban reclaim", msg)
        # No credential material may ever leak into a reject message.
        self.assertNotIn("token", msg.lower())


class TestServerWiringContract(unittest.TestCase):
    """Static locks on build_app's chokepoint swap (contract, not
    closure): the wrapper consults the predicate, raises the plugin's own
    _StatusRejected, is swapped into the module global, and the attach
    fails CLOSED on router drift."""

    def test_chokepoint_swapped_into_plugin_module(self):
        src = SERVER.read_text()
        self.assertIn("plugin._set_status_direct = _set_status_direct_guarded", src)
        self.assertIn("_set_status_direct_guarded._worker_control_guarded = True", src)
        self.assertIn("_set_status_direct_guarded._worker_control_upstream = _upstream_set_status", src)

    def test_wrapper_uses_predicate_and_plugin_error_class(self):
        src = SERVER.read_text()
        wrap = src[src.index("def _set_status_direct_guarded"):]
        wrap = wrap[: wrap.index("plugin._set_status_direct =")]
        self.assertIn("worker_requeue_denied(getattr(current, \"status\", None), new_status)", wrap)
        self.assertIn("raise _status_rejected_cls(WORKER_REQUEUE_REJECT_MSG)", wrap)
        # Current status read through upstream's own accessor, and the
        # refused path never calls the upstream write.
        self.assertIn("plugin.kanban_db.get_task(conn, task_id)", wrap)
        self.assertIn("return _upstream_set_status(conn, task_id, new_status)", wrap)

    def test_attach_failure_refuses_to_boot(self):
        src = SERVER.read_text()
        block = src[src.index('_upstream_set_status = getattr'):]
        block = block[: block.index("def _set_status_direct_guarded")]
        self.assertIn("raise SystemExit", block)
        # Both drift conditions checked: helper callable, error class a
        # real Exception subclass.
        self.assertIn("callable(_upstream_set_status)", block)
        self.assertIn("issubclass(_status_rejected_cls, Exception)", block)

    def test_main_uses_build_app(self):
        src = SERVER.read_text()
        self.assertIn("app = build_app(", src[src.index("def main("):])


class TestUiControlsDisabled(unittest.TestCase):
    """static/app.js must refuse the running -> ready gesture client-side
    (defense-in-depth) and show the affordance disabled, not removed."""

    def test_movetasks_refuses_running_ready(self):
        src = APPJS.read_text()
        move = src[src.index("async function moveTasks"):]
        move = move[: move.index("function cardMenuItems")]
        self.assertIn('if (target === "ready")', move)
        self.assertIn('t.status === "running"', move)
        self.assertIn("hermes kanban reclaim", move)

    def test_context_menu_ready_disabled_for_running(self):
        src = APPJS.read_text()
        self.assertIn("→ Ready (disabled in the WebUI)", src)
        # openCardMenu honours the disabled+title pair on menu items.
        menu = src[src.index("function openCardMenu"):]
        menu = menu[: menu.index("function closeMenus")]
        self.assertIn('...(it.disabled ? { disabled: "" } : {})', menu)
        self.assertIn("...(it.title ? { title: it.title } : {})", menu)

    def test_drawer_status_select_disables_ready_for_running(self):
        src = APPJS.read_text()
        ctl = src[src.index("function statusControl"):]
        ctl = ctl[: ctl.index("/* ---------------- bulk bar")]
        self.assertIn("readyOpt.disabled = true", ctl)
        self.assertIn("hermes kanban reclaim", ctl)


# --------------------------------------------------------------------------
# Live wiring: real FastAPI assembly + REAL board DB. Runs inside the
# deployed container (needs fastapi + the mounted plugin checkout); the
# scratch board is created, exercised, and hard-deleted by the test.
# --------------------------------------------------------------------------

TEST_BOARD = "zz-selftest-requeue"


class TestLiveRequeueWiring(unittest.TestCase):
    """End-to-end through the REAL assembled app: PATCH and bulk both
    refuse running->ready (400 / per-id refusal) while the claim columns
    stay intact, every ordinary transition still works, and the swap is
    marked on the module. Skipped when fastapi/plugin are unavailable."""

    @classmethod
    def setUpClass(cls):
        if not _fastapi_available():
            raise unittest.SkipTest("fastapi not importable here (run inside the container)")
        # Loopback-dev opt-out, same sanctioned path the sibling guard
        # harness uses: a SEPARATE process from the running server, so it
        # never weakens the live container (which has no such variable).
        os.environ["KANBAN_UI_COOKIE_INSECURE"] = "1"
        try:
            cls.app = server.build_app("synthetic-test-secret-0123456789abcdef", "", "", "")
        except Exception as e:  # plugin router unimportable outside the container
            raise unittest.SkipTest(f"plugin router not importable here: {e!r}")
        # The module object build_app imported (registered in sys.modules).
        import sys

        cls.plugin = sys.modules["kanban_plugin_api"]
        cls.kdb = cls.plugin.kanban_db
        # Guard must be attached in this assembly.
        wrapper = cls.plugin._set_status_direct
        if not getattr(wrapper, "_worker_control_guarded", False):
            raise unittest.SkipTest("chokepoint guard not attached")
        cls._upstream = wrapper._worker_control_upstream

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
        # Real ASGI never carries the query in scope["path"] (Starlette
        # routes on path only); split it out or every ?board= request 404s.
        path, _, query = path.partition("?")
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "path": path, "raw_path": path.encode(),
            "query_string": query.encode(), "root_path": "", "scheme": "https",
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
        self.cookie = raw.decode().split("kanban_ui_session=")[1].split(";")[0]

        # Scratch board (hard-delete if a stale one lingers), then one
        # task driven to running with claim fields set by hand.
        self._request("DELETE", f"/api/kanban/boards/{TEST_BOARD}?delete=true", cookie=self.cookie)
        status, b = self._request("POST", "/api/kanban/boards", cookie=self.cookie,
                                  json_body={"slug": TEST_BOARD})
        self.assertIn(status, (200, 201), f"scratch board create failed: {status} {b!r}")
        status, b = self._request("POST", f"/api/kanban/tasks?board={TEST_BOARD}",
                                  cookie=self.cookie, json_body={"title": "requeue-guard selftest"})
        self.assertEqual(status, 200, f"task create failed: {status} {b!r}")
        self.task_id = _json.loads(b)["task"]["id"]
        with self.kdb.scoped_current_board(TEST_BOARD):
            conn = self.plugin._conn(board=TEST_BOARD)
            try:
                with self.kdb.write_txn(conn):
                    conn.execute(
                        "UPDATE tasks SET status='running', claim_lock='foreignhost:1', "
                        "claim_expires=strftime('%s','now')+3600, worker_pid=999999 "
                        "WHERE id=?",
                        (self.task_id,),
                    )
            finally:
                conn.close()
        self._cleanup_done = False

    def tearDown(self):
        if getattr(self, "_cleanup_done", False):
            return
        self._cleanup_done = True
        self._request("DELETE", f"/api/kanban/boards/{TEST_BOARD}?delete=true", cookie=self.cookie)

    def _row(self):
        conn = self.plugin._conn(board=TEST_BOARD)
        try:
            return conn.execute(
                "SELECT status, claim_lock, worker_pid, current_run_id FROM tasks WHERE id=?",
                (self.task_id,)).fetchone()
        finally:
            conn.close()

    def test_guard_marked_on_module(self):
        self.assertTrue(getattr(self.plugin._set_status_direct, "_worker_control_guarded", False))
        self.assertTrue(callable(self.plugin._set_status_direct._worker_control_upstream))

    def test_patch_running_to_ready_rejected_400_claim_intact(self):
        status, body = self._request(
            "PATCH", f"/api/kanban/tasks/{self.task_id}?board={TEST_BOARD}",
            cookie=self.cookie, json_body={"status": "ready"})
        self.assertEqual(status, 400)
        self.assertIn(b"cannot requeue a running task", body)
        self.assertIn(b"hermes kanban reclaim", body)
        row = self._row()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["claim_lock"], "foreignhost:1")  # claim NOT released
        self.assertEqual(row["worker_pid"], 999999)

    def test_bulk_running_to_ready_refused_claim_intact(self):
        status, body = self._request(
            "POST", f"/api/kanban/tasks/bulk?board={TEST_BOARD}",
            cookie=self.cookie, json_body={"ids": [self.task_id], "status": "ready"})
        self.assertEqual(status, 200)  # bulk answers per-id
        import json as _json
        results = _json.loads(body)["results"]
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertIn("cannot requeue a running task", results[0]["error"])
        row = self._row()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["claim_lock"], "foreignhost:1")

    def test_running_to_todo_still_works(self):
        # The finding only raises running->ready; running->todo must
        # still reach upstream (claim cleared there by upstream itself).
        status, body = self._request(
            "PATCH", f"/api/kanban/tasks/{self.task_id}?board={TEST_BOARD}",
            cookie=self.cookie, json_body={"status": "todo"})
        self.assertEqual(status, 200, f"{status} {body!r}")
        self.assertEqual(self._row()["status"], "todo")

    def test_idle_ready_transitions_still_work(self):
        # After leaving running, ordinary drags back to ready are fine
        # (PATCH and bulk both), proving the guard is the exact pair only.
        status, _ = self._request(
            "PATCH", f"/api/kanban/tasks/{self.task_id}?board={TEST_BOARD}",
            cookie=self.cookie, json_body={"status": "todo"})
        self.assertEqual(status, 200)
        status, body = self._request(
            "PATCH", f"/api/kanban/tasks/{self.task_id}?board={TEST_BOARD}",
            cookie=self.cookie, json_body={"status": "ready"})
        self.assertEqual(status, 200, f"{status} {body!r}")
        self.assertEqual(self._row()["status"], "ready")
        # ready -> todo -> ready through the bulk path as well.
        status, body = self._request(
            "POST", f"/api/kanban/tasks/bulk?board={TEST_BOARD}",
            cookie=self.cookie, json_body={"ids": [self.task_id], "status": "todo"})
        self.assertEqual(status, 200)
        self.assertTrue(_json_ok(body), body)
        status, body = self._request(
            "POST", f"/api/kanban/tasks/bulk?board={TEST_BOARD}",
            cookie=self.cookie, json_body={"ids": [self.task_id], "status": "ready"})
        self.assertEqual(status, 200)
        self.assertTrue(_json_ok(body), body)
        self.assertEqual(self._row()["status"], "ready")


def _json_ok(body: bytes) -> bool:
    import json as _json
    results = _json.loads(body)["results"]
    return len(results) == 1 and results[0]["ok"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
