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
chokepoint BOTH update paths share) so running->ready raises a refusal
the endpoints map -- PATCH 400 + actionable detail, bulk per-id refusal
-- and static/app.js disables the corresponding controls.

PR#13 round-4 finding 4321367930: the refuse CHANNEL is checkout-
contract dependent. Newer checkouts (live host) expose upstream's own
``_StatusRejected`` (mapped to 400 via _map_errors); the pinned release
revision b3aa561f has NO _StatusRejected, where the old mandatory lookup
SystemExit'd the container at boot. The guard now prefers _StatusRejected
when present and otherwise raises fastapi.HTTPException(400) (update_task
has no catch-all there, bulk_update records per-id) -- identical refusal
semantics on both shapes. Fail-closed anchor: the guard still refuses to
boot when the _set_status_direct chokepoint itself is absent. See
TestBootChannelMatrix for hermetic per-shape boot cases.

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
from types import SimpleNamespace

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
    closure): the wrapper consults the predicate, raises through the
    channel-selected refusal factory, is swapped into the module global,
    and the attach fails CLOSED on router drift."""

    def test_chokepoint_swapped_into_plugin_module(self):
        src = SERVER.read_text()
        self.assertIn("plugin._set_status_direct = _set_status_direct_guarded", src)
        self.assertIn("_set_status_direct_guarded._worker_control_guarded = True", src)
        self.assertIn("_set_status_direct_guarded._worker_control_upstream = _upstream_set_status", src)

    def test_wrapper_uses_predicate_and_channel_refusal(self):
        src = SERVER.read_text()
        wrap = src[src.index("def _set_status_direct_guarded"):]
        wrap = wrap[: wrap.index("plugin._set_status_direct =")]
        self.assertIn("worker_requeue_denied(getattr(current, \"status\", None), new_status)", wrap)
        # Round-4 finding 4321367930: refusal goes through the channel
        # factory (either upstream _StatusRejected or HTTPException(400)),
        # never a hard-coded mandatory class.
        self.assertIn("raise _requeue_refusal()", wrap)
        # Current status read through upstream's own accessor, and the
        # refused path never calls the upstream write.
        self.assertIn("plugin.kanban_db.get_task(conn, task_id)", wrap)
        self.assertIn("return _upstream_set_status(conn, task_id, new_status)", wrap)
        # Round-5 finding 4323915048: the read, the predicate, and the
        # upstream write all sit INSIDE the guard's early write
        # transaction. Static ordering locks: hold opens before the
        # read, and the read/refusal/upstream-call are indented under
        # the with-block (a flat read-then-call is the racy shape).
        self.assertIn("with _guard_write_txn.hold(conn):", wrap)
        body = wrap[wrap.index("with _guard_write_txn.hold(conn):"):]
        self.assertLess(
            body.index("plugin.kanban_db.get_task(conn, task_id)"),
            body.index("worker_requeue_denied("),
        )
        self.assertLess(
            body.index("plugin.kanban_db.get_task(conn, task_id)"),
            body.index("return _upstream_set_status(conn, task_id, new_status)"),
        )
        for needle in (
            "plugin.kanban_db.get_task(conn, task_id)",
            "worker_requeue_denied(getattr(current,",
            "raise _requeue_refusal()",
            "return _upstream_set_status(conn, task_id, new_status)",
        ):
            self.assertTrue(
                any(line.startswith("            ") and needle in line
                    for line in body.splitlines()),
                f"{needle!r} not indented under the hold() block",
            )

    def test_guard_early_write_txn_shim_contract(self):
        # Round-5 finding 4323915048 static locks: the guard takes
        # upstream's own BEGIN IMMEDIATE before the status read via a
        # reentrancy-aware shim over kanban_db.write_txn, the shim is
        # installed on the mounted kanban_db, the wrapper carries the
        # handle, re-wrapping a previous shim is unwrapped (idempotent
        # reattach), and a missing write_txn anchor fails the boot.
        src = SERVER.read_text()
        self.assertIn("_upstream_write_txn = getattr(plugin.kanban_db, \"write_txn\", None)", src)
        self.assertIn("while getattr(_upstream_write_txn, \"_kanban_ui_reentrant_shim\", False):", src)
        self.assertIn("_upstream_write_txn = _upstream_write_txn._upstream", src)
        self.assertIn("if not callable(_upstream_write_txn):", src)
        shim = src[src.index("class _ReentrantGuardWriteTxn"):]
        shim = shim[: shim.index("_guard_write_txn = _ReentrantGuardWriteTxn(")]
        self.assertIn("_kanban_ui_reentrant_shim = True", shim)
        self.assertIn("self._local = threading.local()", shim)
        # Signature-transparent pass-through (newer checkouts pass
        # allow_nested= and other kwargs that belong to upstream).
        self.assertIn("with self._upstream(conn, *args, **kwargs) as tx:", shim)
        self.assertIn("self._local.held = conn", shim)
        # Join only for THIS thread holding THIS exact connection; every
        # other caller gets upstream's own context manager back verbatim.
        self.assertIn("return contextlib.nullcontext(conn)", shim)
        self.assertIn("return self._upstream(conn, *args, **kwargs)", shim)
        self.assertIn("plugin.kanban_db.write_txn = _guard_write_txn", src)
        self.assertIn("_set_status_direct_guarded._worker_control_write_txn = _guard_write_txn", src)
        # Both new fail-closed anchors (write_txn anchor + chokepoint)
        # live in the same attach block.
        block = src[src.index('_upstream_set_status = getattr'):]
        block = block[: block.index("def _set_status_direct_guarded")]
        self.assertGreaterEqual(block.count("raise SystemExit"), 3)

    def test_attach_failure_refuses_to_boot(self):
        src = SERVER.read_text()
        block = src[src.index('_upstream_set_status = getattr'):]
        block = block[: block.index("def _set_status_direct_guarded")]
        # Fail-closed anchor: a missing/non-callable chokepoint refuses
        # to boot on its own, independent of any refuse channel.
        self.assertIn("if not callable(_upstream_set_status):", block)
        self.assertIn("raise SystemExit", block)
        # Channel A is preferred only when it is a real Exception class;
        # channel B is fastapi's HTTPException(400); if even that cannot
        # be imported the boot is refused (fail closed, no silent pass).
        self.assertIn("issubclass(_status_rejected_cls, Exception)", block)
        self.assertIn("_status_rejected_cls(WORKER_REQUEUE_REJECT_MSG)", block)
        self.assertIn("status_code=400, detail=WORKER_REQUEUE_REJECT_MSG", block)
        self.assertGreaterEqual(block.count("raise SystemExit"), 2)

    def test_main_uses_build_app(self):
        src = SERVER.read_text()
        self.assertIn("app = build_app(", src[src.index("def main("):])


# --------------------------------------------------------------------------
# PR#13 round-5 finding 4323915048: check-to-use atomicity locks. The
# guard's status read must be taken INSIDE the write transaction (early
# BEGIN IMMEDIATE through upstream's own write_txn), kanban_db.write_txn
# must carry the reentrancy shim so the pinned helper's own
# `with write_txn(conn)` joins instead of nesting a forbidden BEGIN, and
# a checkout without a callable write_txn must refuse to boot.
# --------------------------------------------------------------------------

class TestAtomicGuardContract(unittest.TestCase):
    def test_read_and_mutation_inside_hold(self):
        src = SERVER.read_text()
        wrap = src[src.index("def _set_status_direct_guarded"):]
        wrap = wrap[: wrap.index("_set_status_direct_guarded.__name__")]
        self.assertIn("with _guard_write_txn.hold(conn):", wrap)
        # Read, predicate, refusal, and the upstream write are all INSIDE
        # the hold (indented under it), never before the lock is taken.
        body = wrap[wrap.index("with _guard_write_txn.hold(conn):"):]
        self.assertIn("current = plugin.kanban_db.get_task(conn, task_id)", body)
        self.assertIn("worker_requeue_denied(", body)
        self.assertIn("raise _requeue_refusal()", body)
        self.assertIn("return _upstream_set_status(conn, task_id, new_status)", body)

    def test_write_txn_shim_installed_and_anchored(self):
        src = SERVER.read_text()
        self.assertIn("plugin.kanban_db.write_txn = _guard_write_txn", src)
        self.assertIn("_set_status_direct_guarded._worker_control_write_txn = _guard_write_txn", src)
        block = src[src.index("_upstream_write_txn = getattr"):][: src.index("def _set_status_direct_guarded")]
        # Fail-closed: no callable kanban_db.write_txn -> no early lock -> boot refused.
        self.assertIn("if not callable(_upstream_write_txn):", block)
        self.assertIn("raise SystemExit", block)
        # Double-build safety: an existing shim is unwrapped, never re-wrapped.
        self.assertIn('_kanban_ui_reentrant_shim', block)
        self.assertIn("_upstream_write_txn = _upstream_write_txn._upstream", block)

    def test_shim_joins_only_own_thread_and_connection(self):
        # Hermetic shim behaviour (no fastapi needed): hold() enters the
        # upstream CM exactly once; a nested same-thread/same-connection
        # call JOINS (nullcontext, no second BEGIN); other threads and
        # other connections get the upstream CM back untouched.
        import contextlib
        import threading as _threading

        begins = []

        def fake_upstream(conn):
            @contextlib.contextmanager
            def cm(c):
                begins.append(c)
                yield c
            return cm(conn)

        shim = server._ReentrantGuardWriteTxn(fake_upstream)
        conn_a, conn_b = object(), object()

        with shim.hold(conn_a):
            self.assertIs(type(shim(conn_a)), contextlib.nullcontext)  # joins
            self.assertIsNot(type(shim(conn_b)), contextlib.nullcontext)  # other conn
            joined_on_b = {}

            def other_thread():
                joined_on_b["type"] = type(shim(conn_a))

            t = _threading.Thread(target=other_thread)
            t.start(); t.join()
            self.assertIsNot(joined_on_b["type"], contextlib.nullcontext)  # other thread
        self.assertEqual(begins, [conn_a])  # upstream entered exactly once
        # Outside any hold the shim is fully transparent.
        self.assertIsNot(type(shim(conn_a)), contextlib.nullcontext)
        with shim(conn_a) as c:
            self.assertIs(c, conn_a)
        self.assertEqual(begins, [conn_a, conn_a])

    def test_shim_rollbacks_on_hold_body_exception(self):
        import contextlib

        events = []

        def fake_upstream(conn):
            @contextlib.contextmanager
            def cm(c):
                try:
                    yield c
                except Exception:
                    events.append("rollback")
                    raise
                events.append("commit")
            return cm(conn)

        shim = server._ReentrantGuardWriteTxn(fake_upstream)
        conn = object()
        with self.assertRaises(ValueError):
            with shim.hold(conn):
                raise ValueError("boom")
        self.assertEqual(events, ["rollback"])
        # And the hold is cleared even on the failure path.
        self.assertIs(getattr(shim._local, "held", shim._UNHELD), shim._UNHELD)
        with shim.hold(conn):
            pass
        self.assertEqual(events, ["rollback", "commit"])


# --------------------------------------------------------------------------
# Boot-channel matrix (PR#13 round-4 finding 4321367930): the startup
# guard must BOOT against both real plugin contracts and still fail
# closed when the chokepoint is gone. Hermetic: a stub plugin module is
# written to a temp dir and loaded through the exact same
# spec_from_file_location path build_app uses (server.PLUGIN repointed);
# no DB, no network, no real checkout. Needs fastapi (skipped where
# absent; executed inside the deployed container).
# --------------------------------------------------------------------------

# The sibling guards (reassign, bulk reclaim-first) attach BEFORE the
# status guard, so the stub router carries their two endpoints or the
# boot would SystemExit on router drift for the wrong reason.
_STUB_BASE = '''
from types import SimpleNamespace
from typing import Optional

from fastapi import APIRouter

router = APIRouter()

_TASK = None
def _stub_write_txn(conn):
    # Stand-in for kanban_db.write_txn (context manager, BEGIN IMMEDIATE
    # upstream). The r5 atomicity shim attaches over it.
    from contextlib import contextmanager
    @contextmanager
    def _cm(c):
        yield c
    return _cm(conn)
kanban_db = SimpleNamespace(get_task=lambda conn, tid: _TASK, write_txn=_stub_write_txn)


@router.post("/tasks/{task_id}/reassign")
def reassign_task_endpoint(payload: Optional[dict] = None):
    return {"ok": True}


@router.post("/tasks/bulk")
def bulk_update(payload: Optional[dict] = None):
    return {"results": []}
'''

_STUB_PINNED = _STUB_BASE + '''

def _set_status_direct(conn, task_id, new_status):
    # b3aa561f shape: helper present, NO _StatusRejected anywhere.
    return True
'''

_STUB_NEWER = _STUB_PINNED + '''

class _StatusRejected(Exception):
    pass
'''

_STUB_NEWER_NOCLASS = _STUB_PINNED + '''

_StatusRejected = "not-a-class"
'''

_STUB_NO_HELPER = _STUB_BASE + '''

class _StatusRejected(Exception):
    pass
'''


class TestBootChannelMatrix(unittest.TestCase):
    """build_app boots on the pinned contract (channel B), prefers
    _StatusRejected when present (channel A), and always refuses to boot
    when the _set_status_direct chokepoint itself is absent."""

    @classmethod
    def setUpClass(cls):
        if not _fastapi_available():
            raise unittest.SkipTest("fastapi not importable here (run inside the container)")
        os.environ["KANBAN_UI_COOKIE_INSECURE"] = "1"

    def _build_with_stub(self, source: str):
        import sys

        d = tempfile.mkdtemp(prefix="kbui-stub-plugin-")
        (Path(d) / "plugin_api.py").write_text(source)
        saved_plugin = server.PLUGIN
        try:
            server.PLUGIN = Path(d)
            app = server.build_app("synthetic-test-secret-0123456789abcdef", "", "", "")
        finally:
            server.PLUGIN = saved_plugin
        return app, sys.modules["kanban_plugin_api"]

    def test_pinned_shape_boots_and_refuses_via_http_exception(self):
        # b3aa561f contract: guard must ATTACH (no SystemExit) and the
        # refusal must be fastapi's HTTPException(400) carrying the
        # actionable message.
        from fastapi import HTTPException

        _, plugin = self._build_with_stub(_STUB_PINNED)
        wrapper = plugin._set_status_direct
        self.assertTrue(getattr(wrapper, "_worker_control_guarded", False))
        plugin._TASK = SimpleNamespace(status="running")
        with self.assertRaises(HTTPException) as cm:
            wrapper(None, "t_x", "ready")
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.detail, server.WORKER_REQUEUE_REJECT_MSG)
        # Allowed transitions still reach the upstream write.
        plugin._TASK = SimpleNamespace(status="todo")
        self.assertTrue(wrapper(None, "t_x", "ready"))

    def test_newer_shape_prefers_plugin_status_rejected(self):
        _, plugin = self._build_with_stub(_STUB_NEWER)
        wrapper = plugin._set_status_direct
        self.assertTrue(getattr(wrapper, "_worker_control_guarded", False))
        plugin._TASK = SimpleNamespace(status="running")
        with self.assertRaises(plugin._StatusRejected) as cm:
            wrapper(None, "t_x", "ready")
        self.assertIn("hermes kanban reclaim", str(cm.exception))

    def test_non_class_status_rejected_falls_back_to_http_exception(self):
        # A _StatusRejected that is not an Exception subclass is not a
        # usable channel; the guard must fall back to channel B rather
        # than raise something the endpoints cannot map.
        from fastapi import HTTPException

        _, plugin = self._build_with_stub(_STUB_NEWER_NOCLASS)
        plugin._TASK = SimpleNamespace(status="running")
        with self.assertRaises(HTTPException):
            plugin._set_status_direct(None, "t_x", "ready")

    def test_missing_chokepoint_refuses_to_boot_even_with_error_class(self):
        # Fail-closed anchor: the chokepoint itself is mandatory. The
        # stub HAS _StatusRejected (so the old code would have booted
        # the wrong way round) but NO _set_status_direct -> SystemExit.
        d = tempfile.mkdtemp(prefix="kbui-stub-plugin-")
        (Path(d) / "plugin_api.py").write_text(_STUB_NO_HELPER)
        saved_plugin = server.PLUGIN
        try:
            server.PLUGIN = Path(d)
            with self.assertRaises(SystemExit) as cm:
                server.build_app("synthetic-test-secret-0123456789abcdef", "", "", "")
        finally:
            server.PLUGIN = saved_plugin
        self.assertIn("_set_status_direct", str(cm.exception))


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
