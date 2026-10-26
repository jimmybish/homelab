#!/usr/bin/env python3
"""Security regression tests for the bulk reclaim-first reassign guard.

PR#13 review finding (comment 4319617432): the round-1 deny-list
(test_worker_control_guard.py) wraps ONLY the single-task reassign
endpoint's parsed body, but the pinned plugin's POST /api/kanban/tasks/
bulk accepts BulkTaskBody (ids + assignee + reclaim_first) and
_bulk_apply_one calls kanban_db.reassign_task(..., reclaim_first=True)
directly when the flag is set -- releasing a host worker's claim without
ever signalling it (the claim lock never carries the container's local
host prefix), letting the dispatcher spawn a duplicate beside the still-
running worker. server.py now wraps the bulk endpoint's dependant.call
so a validated payload with assignee set AND reclaim_first truthy is
refused 403 BEFORE bulk_update opens a board connection or mutates any
item, while ordinary bulk operations keep their exact upstream
semantics.

    python3 ansible/roles/kanban_ui/tests/test_bulk_reassign_guard.py

Exit 0 = all pass. Stdlib only on the control node (same harness rules
as test_worker_control_guard.py). The end-to-end wiring class
additionally needs fastapi (and the mounted plugin router) and skips
when absent; it is executed inside the deployed container where both
exist:

    docker cp test_bulk_reassign_guard.py kanban-ui:/tmp/
    docker exec -e KANBAN_UI_SERVER_PATH=/app/server.py kanban-ui \
        python /tmp/test_bulk_reassign_guard.py
"""
import importlib.util
import json as _json
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


def _fastapi_available() -> bool:
    import importlib.util as ilu

    return ilu.find_spec("fastapi") is not None


class TestBulkPredicate(unittest.TestCase):
    """The pure payload predicate -- exactly the branch that reaches
    reassign_task(reclaim_first=True) in _bulk_apply_one: assignee set
    AND reclaim_first truthy."""

    def test_dict_assignee_plus_reclaim_denied(self):
        self.assertTrue(
            server.worker_control_bulk_reassign_denied(
                {"ids": ["t_1"], "assignee": "virtuajimmy", "reclaim_first": True}))

    def test_dict_empty_string_assignee_plus_reclaim_denied(self):
        # Upstream maps "" through `payload.assignee or None` into
        # reassign_task(None, reclaim_first=True) -- an UNASSIGN with a
        # claim release. Same hazard, same refusal.
        self.assertTrue(
            server.worker_control_bulk_reassign_denied(
                {"ids": ["t_1"], "assignee": "", "reclaim_first": True}))

    def test_dict_truthy_nonbool_reclaim_denied(self):
        self.assertTrue(
            server.worker_control_bulk_reassign_denied(
                {"ids": ["t_1"], "assignee": "x", "reclaim_first": 1}))

    def test_ordinary_bulk_ops_allowed(self):
        # Everything the finding does NOT raise: status/priority/archive
        # batches, and the ordinary assignee batch (reclaim_first absent
        # or false) which goes through assign_task and never touches
        # reclaim_task.
        for payload in (
            {"ids": ["t_1"], "status": "ready"},
            {"ids": ["t_1"], "assignee": "virtuajimmy"},
            {"ids": ["t_1"], "assignee": "virtuajimmy", "reclaim_first": False},
            {"ids": ["t_1"], "priority": 5},
            {"ids": ["t_1"], "archive": True},
            {"ids": []},
            {},
        ):
            self.assertFalse(server.worker_control_bulk_reassign_denied(payload), payload)

    def test_reclaim_first_without_assignee_allowed(self):
        # BulkTaskBody exposes reclaim_first independently, but upstream
        # only consults it inside the `payload.assignee is not None`
        # branch -- reclaim_first alone is inert and must stay inert.
        self.assertFalse(
            server.worker_control_bulk_reassign_denied({"ids": ["t_1"], "reclaim_first": True}))
        # Explicit JSON null is upstream's "field not sent" for assignee.
        self.assertFalse(
            server.worker_control_bulk_reassign_denied(
                {"ids": ["t_1"], "assignee": None, "status": "todo", "reclaim_first": True}))

    def test_model_object_supported(self):
        class Payload:
            assignee = "virtuajimmy"
            reclaim_first = True

        self.assertTrue(server.worker_control_bulk_reassign_denied(Payload()))
        Payload.reclaim_first = False
        self.assertFalse(server.worker_control_bulk_reassign_denied(Payload()))
        Payload.reclaim_first = True
        Payload.assignee = None
        self.assertFalse(server.worker_control_bulk_reassign_denied(Payload()))

    def test_unrecognised_shape_fails_closed(self):
        for junk in (None, "assign", 42, ["reclaim_first"], object()):
            self.assertTrue(server.worker_control_bulk_reassign_denied(junk), repr(junk))

    def test_partial_model_fails_closed(self):
        # An object that carries reclaim_first but not assignee (or vice
        # versa) is drift from the pinned BulkTaskBody -- refuse blind.
        class OnlyReclaim:
            reclaim_first = False

        class OnlyAssignee:
            assignee = "x"

        self.assertTrue(server.worker_control_bulk_reassign_denied(OnlyReclaim()))
        self.assertTrue(server.worker_control_bulk_reassign_denied(OnlyAssignee()))


class TestServerWiringContract(unittest.TestCase):
    """Static locks on build_app's bulk wrapper (contract, not closure):
    it is swapped into the bulk route's dependant.call, consults the
    predicate with the same fail-closed key lookup, preserves the sync/
    async dispatch shape, and BOTH attach paths fail CLOSED."""

    def test_bulk_guard_swaps_dependant_call(self):
        src = SERVER.read_text()
        self.assertIn("_bulk_route.dependant.call = _bulk_guarded_call", src)
        self.assertIn("_bulk_guarded_call._worker_control_guarded = True", src)
        self.assertIn("_bulk_guarded_call._worker_control_upstream = _bulk_upstream_call", src)
        self.assertIn('worker_control_bulk_reassign_denied(values.get("payload", values.get("body")))', src)

    def test_route_locked_to_exact_path(self):
        src = SERVER.read_text()
        block = src[src.index('_bulk_path = '):]
        block = block[: block.index("_bulk_guarded_call.__name__")]
        self.assertIn('_bulk_path = "/api/kanban/tasks/bulk"', src)
        self.assertIn("len(_bulk_routes) != 1", block)
        self.assertIn('"POST" in (r.methods or set())', block)
        self.assertGreaterEqual(block.count("raise SystemExit"), 2)
        self.assertIn("is_coroutine_callable", block)

    def test_wrapper_preserves_sync_dispatch_shape(self):
        # The pinned bulk_update is a sync def; swapping an async wrapper
        # for a sync route (or vice versa) would break threadpool
        # dispatch. The code must branch on _bulk_upstream_is_async and
        # both branches must carry the JSONResponse 403.
        src = SERVER.read_text()
        self.assertIn("if _bulk_upstream_is_async:", src)
        guard = src[src.index("if _bulk_upstream_is_async:"):]
        guard = guard[: guard.index("_bulk_guarded_call.__name__")]
        self.assertIn("await _bulk_upstream_call(**values)", guard)
        self.assertIn("return _bulk_upstream_call(**values)", guard)
        self.assertEqual(guard.count("status_code=403"), 2)

    def test_main_uses_build_app(self):
        src = SERVER.read_text()
        self.assertIn("app = build_app(", src[src.index("def main("):])


# --------------------------------------------------------------------------
# Live wiring: real FastAPI assembly + REAL board DB. Runs inside the
# deployed container (needs fastapi + the mounted plugin checkout); the
# scratch board is created, exercised, and hard-deleted by the test.
# --------------------------------------------------------------------------

TEST_BOARD = "zz-selftest-bulkreclaim"


class TestLiveBulkWiring(unittest.TestCase):
    """End-to-end through the REAL assembled app: bulk reclaim-first
    reassign is refused 403 with ZERO mutations (claim columns and
    assignee intact via row re-read, untouched sibling unaffected),
    ordinary bulk operations still reach upstream, and the wrapper is
    marked on the route."""

    @classmethod
    def setUpClass(cls):
        if not _fastapi_available():
            raise unittest.SkipTest("fastapi not importable here (run inside the container)")
        # Loopback-dev opt-out, same sanctioned path the sibling guard
        # harnesses use: a SEPARATE process from the running server, so
        # it never weakens the live container (which has no such variable).
        os.environ["KANBAN_UI_COOKIE_INSECURE"] = "1"
        try:
            cls.app = server.build_app("synthetic-test-secret-0123456789abcdef", "", "", "")
        except Exception as e:  # plugin router unimportable outside the container
            raise unittest.SkipTest(f"plugin router not importable here: {e!r}")
        import sys

        cls.plugin = sys.modules["kanban_plugin_api"]
        cls.kdb = cls.plugin.kanban_db

    def _request(self, method, path, cookie="", header=True, json_body=None):
        import asyncio

        headers = []
        if cookie:
            headers.append((b"cookie", f"kanban_ui_session={cookie}".encode()))
        if header:
            headers.append((b"x-kanban-ui", b"1"))
        body = b"" if json_body is None else _json.dumps(json_body).encode()
        if body:
            headers.append((b"content-type", b"application/json"))
        # Real ASGI never carries the query in scope["path"]; split it out
        # or every ?board= request 404s.
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

        # Scratch board (hard-delete if a stale one lingers), then two
        # tasks: one idle (todo), one running under a FOREIGN claim --
        # exactly the shape the single-reassign guard refuses per-task.
        self._request("DELETE", f"/api/kanban/boards/{TEST_BOARD}?delete=true", cookie=self.cookie)
        status, b = self._request("POST", "/api/kanban/boards", cookie=self.cookie,
                                  json_body={"slug": TEST_BOARD})
        self.assertIn(status, (200, 201), f"scratch board create failed: {status} {b!r}")
        self.task_ids = {}
        for name, title in (("idle", "bulk-guard-idle"), ("running", "bulk-guard-running")):
            status, b = self._request("POST", f"/api/kanban/tasks?board={TEST_BOARD}",
                                      cookie=self.cookie, json_body={"title": title})
            self.assertEqual(status, 200, f"task create failed: {status} {b!r}")
            self.task_ids[name] = _json.loads(b)["task"]["id"]
        with self.kdb.scoped_current_board(TEST_BOARD):
            conn = self.plugin._conn(board=TEST_BOARD)
            try:
                with self.kdb.write_txn(conn):
                    conn.execute(
                        "UPDATE tasks SET status='running', claim_lock='foreignhost:1', "
                        "claim_expires=strftime('%s','now')+3600, worker_pid=999999 "
                        "WHERE id=?",
                        (self.task_ids["running"],),
                    )
                    # Pin the idle card to a deterministic 'todo' (fresh
                    # dashboard-created tasks land 'ready'; the mixed-batch
                    # test asserts the whole refused request mutated
                    # NOTHING, so the baseline must be exact).
                    conn.execute(
                        "UPDATE tasks SET status='todo' WHERE id=?",
                        (self.task_ids["idle"],),
                    )
            finally:
                conn.close()
        self._cleanup_done = False

    def tearDown(self):
        if getattr(self, "_cleanup_done", False):
            return
        self._cleanup_done = True
        self._request("DELETE", f"/api/kanban/boards/{TEST_BOARD}?delete=true", cookie=self.cookie)

    def _row(self, task_id):
        conn = self.plugin._conn(board=TEST_BOARD)
        try:
            return conn.execute(
                "SELECT status, assignee, claim_lock, worker_pid, current_run_id FROM tasks WHERE id=?",
                (task_id,)).fetchone()
        finally:
            conn.close()

    def test_route_wrapper_marked(self):
        # fastapi 0.133.1 keeps APIRoute in fastapi.routing (top level
        # only exports APIRouter), matching server.py's import.
        from fastapi.routing import APIRoute

        route = next(
            r for r in self.app.routes
            if isinstance(r, APIRoute) and r.path == "/api/kanban/tasks/bulk"
        )
        self.assertTrue(getattr(route.dependant.call, "_worker_control_guarded", False))
        self.assertTrue(callable(route.dependant.call._worker_control_upstream))

    def test_bulk_reclaim_first_rejected_403_claim_intact(self):
        status, body = self._request(
            "POST", f"/api/kanban/tasks/bulk?board={TEST_BOARD}",
            cookie=self.cookie,
            json_body={"ids": [self.task_ids["running"]],
                       "assignee": "zz-someone-else", "reclaim_first": True},
        )
        self.assertEqual(status, 403)
        self.assertIn(b"worker control operations are disabled", body)
        self.assertIn(b"hermes kanban reclaim", body)
        row = self._row(self.task_ids["running"])
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["claim_lock"], "foreignhost:1")  # claim NOT released
        self.assertEqual(row["worker_pid"], 999999)
        self.assertIsNone(row["assignee"])                    # never reassigned

    def test_bulk_mixed_batch_rejected_before_any_mutation(self):
        # The finding's core ask: guard BEFORE any items are mutated.
        # An idle sibling in the SAME batch must be untouched by the
        # refused reclaim-first payload (whole request 403s).
        status, body = self._request(
            "POST", f"/api/kanban/tasks/bulk?board={TEST_BOARD}",
            cookie=self.cookie,
            json_body={"ids": [self.task_ids["idle"], self.task_ids["running"]],
                       "assignee": "zz-someone-else", "reclaim_first": True},
        )
        self.assertEqual(status, 403)
        self.assertIn(b"worker control operations are disabled", body)
        idle = self._row(self.task_ids["idle"])
        self.assertEqual(idle["status"], "todo")
        self.assertIsNone(idle["assignee"])
        run = self._row(self.task_ids["running"])
        self.assertEqual(run["claim_lock"], "foreignhost:1")
        self.assertIsNone(run["assignee"])

    def test_bulk_empty_assignee_plus_reclaim_rejected_403(self):
        # "" unassigns WITH a claim release upstream -- same hazard.
        status, body = self._request(
            "POST", f"/api/kanban/tasks/bulk?board={TEST_BOARD}",
            cookie=self.cookie,
            json_body={"ids": [self.task_ids["running"]], "assignee": "", "reclaim_first": True},
        )
        self.assertEqual(status, 403)
        self.assertIn(b"worker control operations are disabled", body)
        self.assertEqual(self._row(self.task_ids["running"])["claim_lock"], "foreignhost:1")

    def test_ordinary_bulk_assignee_still_works(self):
        # Regression: the ordinary assignee batch (no reclaim_first)
        # keeps working end to end on the same endpoint.
        status, body = self._request(
            "POST", f"/api/kanban/tasks/bulk?board={TEST_BOARD}",
            cookie=self.cookie,
            json_body={"ids": [self.task_ids["idle"]], "assignee": "zz-assignee"},
        )
        self.assertEqual(status, 200, f"{status} {body!r}")
        results = _json.loads(body)["results"]
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"], body)
        self.assertEqual(self._row(self.task_ids["idle"])["assignee"], "zz-assignee")

    def test_ordinary_bulk_status_still_works(self):
        status, body = self._request(
            "POST", f"/api/kanban/tasks/bulk?board={TEST_BOARD}",
            cookie=self.cookie,
            json_body={"ids": [self.task_ids["idle"]], "status": "ready"},
        )
        self.assertEqual(status, 200, f"{status} {body!r}")
        self.assertTrue(_json.loads(body)["results"][0]["ok"], body)
        self.assertEqual(self._row(self.task_ids["idle"])["status"], "ready")

    def test_reclaim_first_without_assignee_inert_as_upstream(self):
        # reclaim_first alone never reaches reassign_task upstream; the
        # guard stays out of the way and the status half applies.
        status, body = self._request(
            "POST", f"/api/kanban/tasks/bulk?board={TEST_BOARD}",
            cookie=self.cookie,
            json_body={"ids": [self.task_ids["idle"]], "status": "ready", "reclaim_first": True},
        )
        self.assertEqual(status, 200, f"{status} {body!r}")
        self.assertTrue(_json.loads(body)["results"][0]["ok"], body)
        self.assertEqual(self._row(self.task_ids["idle"])["status"], "ready")

    def test_anonymous_bulk_gets_session_401_first(self):
        status, _ = self._request(
            "POST", f"/api/kanban/tasks/bulk?board={TEST_BOARD}",
            cookie="", header=True,
            json_body={"ids": [self.task_ids["running"]], "assignee": "x", "reclaim_first": True},
        )
        self.assertEqual(status, 401)

    def test_csrf_guard_precedes_bulk_guard(self):
        # Cookie without the session marker header: the CSRF gate (403)
        # answers, not the op guard -- proved by the differing body.
        status, body = self._request(
            "POST", f"/api/kanban/tasks/bulk?board={TEST_BOARD}",
            cookie=self.cookie, header=False,
            json_body={"ids": [self.task_ids["running"]], "assignee": "x", "reclaim_first": True},
        )
        self.assertEqual(status, 403)
        self.assertNotIn(b"worker control operations", body)

    def test_single_reassign_guard_still_attached(self):
        # Cross-suite lock: adding the bulk wrapper must not dislodge the
        # original single-reassign wrapper.
        from fastapi.routing import APIRoute

        route = next(
            r for r in self.app.routes
            if isinstance(r, APIRoute) and r.path == "/api/kanban/tasks/{task_id}/reassign"
        )
        self.assertTrue(getattr(route.dependant.call, "_worker_control_guarded", False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
