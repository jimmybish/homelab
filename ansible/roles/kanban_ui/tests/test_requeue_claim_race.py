#!/usr/bin/env python3
"""Deterministic check-to-use race regression for the running->ready guard.

PR#13 round-5 finding (comment 4323915048): the guard's status read used
to run BEFORE the pinned upstream helper (b3aa561f
plugins/kanban/dashboard/plugin_api.py:1042 -> kanban_db.write_txn at
hermes_cli/kanban_db.py:2801) opened its BEGIN IMMEDIATE. The host
dispatcher's claim_task (kanban_db.py:4226 -- its own BEGIN IMMEDIATE CAS
``ready -> running`` setting claim_lock/claim_expires/worker_pid) could
commit in that window: the guard saw 'ready', the helper then re-read the
freshly-committed claim inside its own txn and its unconditional UPDATE
NULLed the claim columns and ended the run 'reclaimed' -> duplicate
dispatch beside the live worker. Both update paths -- PATCH
/api/kanban/tasks/{id} and POST /api/kanban/tasks/bulk -- funnel through
the single _set_status_direct chokepoint, so both were exposed.

server.py now acquires BEGIN IMMEDIATE (through upstream's own
kanban_db.write_txn, wrapped in a thread+connection reentrancy shim so
the pinned helper's internal write_txn and trailing recompute_ready join
the open txn instead of hitting SQLite's forbidden nested BEGIN) BEFORE
the status read, and holds it across the mutation.

Harness: a real WAL-mode SQLite database with two connections (UI vs
dispatcher thread), a stub plugin whose helper mirrors the pinned
b3aa561f transaction shape, and threading.Event handshakes -- the
dispatcher is injected at EXACTLY the guard's historical read position
(get_task hook), no sleeps race against timing. The pre-fix wrapper is
reconstructed verbatim to prove the harness reproduces the clobber
(sensitivity), then the shipped guard is proven immune through BOTH
chokepoint entry shapes (PATCH-flow and bulk-flow, mirroring
update_task:896/905 and _bulk_apply_one:1271/1285 resolving the module
global at call time).

    python3 ansible/roles/kanban_ui/tests/test_requeue_claim_race.py

Exit 0 = all pass. Needs fastapi (build_app); skipped elsewhere, and
executed inside the deployed container exactly like the sibling
boot-channel matrix:

    docker cp test_requeue_claim_race.py kanban-ui:/tmp/
    docker exec -e KANBAN_UI_SERVER_PATH=/app/server.py kanban-ui \
        python /tmp/test_requeue_claim_race.py
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
SERVER = Path(os.environ.get("KANBAN_UI_SERVER_PATH", str(HERE.parent / "files" / "app" / "server.py")))

os.environ.setdefault("KANBAN_UI_HERMES_REPO", tempfile.mkdtemp(prefix="kbui-race-repo-"))
os.environ.setdefault("KANBAN_UI_TOKEN_FILE", str(SERVER))  # exists; never read by import

_spec = importlib.util.spec_from_file_location("kanban_ui_server", SERVER)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


def _fastapi_available() -> bool:
    import importlib.util as ilu

    return ilu.find_spec("fastapi") is not None


SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    claim_lock TEXT,
    claim_expires TEXT,
    worker_pid INTEGER,
    current_run_id INTEGER
);
CREATE TABLE task_events (
    task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER
);
"""

# Stub plugin: pinned b3aa561f helper transaction shape (helper opens its
# own write_txn boundary FIRST, reads prev INSIDE it, unconditional
# WHERE id UPDATE, post-txn recompute_ready) plus the two endpoint routes
# the sibling guards require at attach time. update_task_flow /
# bulk_apply_one_flow mirror update_task:896/905 and
# _bulk_apply_one:1271/1285 resolving _set_status_direct as a MODULE
# GLOBAL at call time -- that is what makes the build_app swap
# authoritative for both paths. kanban_db carries a real BEGIN IMMEDIATE
# boundary so build_app's r5 shim attaches over something honest; the
# harness swaps in richer accessors after the boot.
_STUB = '''
from typing import Optional

from fastapi import APIRouter

router = APIRouter()


def _write_boundary(conn):
    # b3aa561f kanban_db.write_txn shape: BEGIN IMMEDIATE boundary,
    # COMMIT on success, ROLLBACK on exception.
    class _Txn:
        def __enter__(self):
            conn.execute("BEGIN IMMEDIATE")
            return conn

        def __exit__(self, exc_type, exc, tb):
            if exc_type is not None:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                return False
            conn.execute("COMMIT")
            return False

    return _Txn()


class _KdbStub:
    write_txn = staticmethod(_write_boundary)


# Shared across module re-execs (build_app loads the stub fresh each
# boot): a second build over the SAME mounted kanban_db must find the
# previous shim installed and unwrap it, not stack a shim-of-a-shim.
import sys as _sys
import types as _types

_registry = _sys.modules.setdefault("_kbui_race_shared_registry", _types.ModuleType("_kbui_race_shared_registry"))
kanban_db = getattr(_registry, "kdb", None)
if kanban_db is None:
    kanban_db = _KdbStub()
    _registry.kdb = kanban_db


def _set_status_direct(conn, task_id, new_status):
    # b3aa561f shape (plugin_api.py:1042): txn opens FIRST, prev re-read
    # INSIDE, unconditional UPDATE, post-txn recompute_ready.
    with kanban_db.write_txn(conn):
        prev = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?",
            (task_id,)).fetchone()
        if prev is None:
            return False
        was_running = prev["status"] == "running"
        cur = conn.execute(
            "UPDATE tasks SET status = ?, "
            "  claim_lock = CASE WHEN ? = 'running' THEN claim_lock ELSE NULL END, "
            "  claim_expires = CASE WHEN ? = 'running' THEN claim_expires ELSE NULL END, "
            "  worker_pid = CASE WHEN ? = 'running' THEN worker_pid ELSE NULL END "
            "WHERE id = ?",
            (new_status, new_status, new_status, new_status, task_id),
        )
        if cur.rowcount != 1:
            return False
        run_id = None
        if was_running and new_status != "running" and prev["current_run_id"]:
            run_id = kanban_db._end_run(conn, task_id)
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, 'status', ?, ?)",
            (task_id, run_id, "ready", 0),
        )
    if new_status in {"done", "ready"}:
        kanban_db.recompute_ready(conn)
    return True


def update_task_flow(conn, task_id, new_status):
    # PATCH /tasks/{id} path: resolves the module global at call time.
    return _set_status_direct(conn, task_id, "ready")


def bulk_apply_one_flow(conn, task_id, new_status):
    # POST /tasks/bulk path: identical chokepoint via the module global.
    return _set_status_direct(conn, task_id, "ready")


@router.post("/tasks/{task_id}/reassign")
def reassign_task_endpoint(payload: Optional[dict] = None):
    return {"ok": True}


@router.post("/tasks/bulk")
def bulk_update(payload: Optional[dict] = None):
    return {"results": []}
'''


class _Kdb:
    """kanban_db stand-in with real accessor semantics; the get_task hook
    is the deterministic injection point (fires exactly at the guard's
    historical read position, once per armed pass)."""

    def __init__(self, write_txn):
        self.get_task_hook = None  # callable -> invoked AFTER the row read
        self.write_txn = write_txn  # replaced with the installed shim by setUp
        self.recompute_ready_calls = []

    def get_task(self, conn, task_id):
        row = conn.execute(
            "SELECT id, status, claim_lock, claim_expires, worker_pid, current_run_id "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        # Upstream get_task returns a Task dataclass; the guard reads
        # current.status via getattr -- mirror the attribute shape, not a
        # dict, or the predicate would never see 'running'.
        task = None if row is None else SimpleNamespace(**dict(row))
        hook, self.get_task_hook = self.get_task_hook, None
        if hook is not None:
            hook()
        return task

    def recompute_ready(self, conn):
        # Pinned helper calls this post-txn for done/ready. Inside the
        # guard hold it must JOIN the open txn; recording + a trivial
        # read proves the connection never lost its transaction midway.
        self.recompute_ready_calls.append("called")
        conn.execute("SELECT 1 FROM tasks LIMIT 1")

    def _end_run(self, conn, task_id, **kw):
        conn.execute("UPDATE tasks SET current_run_id = NULL WHERE id = ?", (task_id,))
        return 7


class _RaceHarness(unittest.TestCase):
    """Shared fixture: build_app on the stub (fastapi required), fresh
    WAL db + UI/dispatcher connections per test."""

    @classmethod
    def setUpClass(cls):
        if not _fastapi_available():
            raise unittest.SkipTest("fastapi not importable here (run inside the container)")
        os.environ["KANBAN_UI_COOKIE_INSECURE"] = "1"

    def setUp(self):
        d = tempfile.mkdtemp(prefix="kbui-race-plugin-")
        (Path(d) / "plugin_api.py").write_text(_STUB)
        saved_plugin = server.PLUGIN
        try:
            server.PLUGIN = Path(d)
            self.app = server.build_app("synthetic-test-secret-0123456789abcdef", "", "", "")
        finally:
            server.PLUGIN = saved_plugin
        self.plugin = sys.modules["kanban_plugin_api"]
        self.wrapper = self.plugin._set_status_direct
        self.assertTrue(getattr(self.wrapper, "_worker_control_guarded", False),
                        "guard must be attached for this race suite to mean anything")
        self.shim = getattr(self.wrapper, "_worker_control_write_txn", None)
        self.assertTrue(self.shim is not None
                        and getattr(self.shim, "_kanban_ui_reentrant_shim", False),
                        "r5 atomicity shim must be attached to the guarded wrapper")
        # The build installed the shim on the stub's kanban_db object;
        # hand the harness accessor object the SAME shim so the pinned
        # helper's internal write_txn call reaches the join logic.
        self.assertIs(self.plugin.kanban_db.write_txn, self.shim)
        self.kdb = _Kdb(write_txn=self.shim)
        self.plugin.kanban_db = self.kdb

        self.dbfile = str(Path(d) / "board.db")
        self.ui = sqlite3.connect(self.dbfile, isolation_level=None, check_same_thread=False)
        self.ui.row_factory = sqlite3.Row
        self.ui.execute("PRAGMA journal_mode=WAL")
        self.ui.execute("PRAGMA busy_timeout=5000")
        self.disp = sqlite3.connect(self.dbfile, isolation_level=None, check_same_thread=False)
        self.disp.row_factory = sqlite3.Row
        self.disp.execute("PRAGMA journal_mode=WAL")
        self.disp.execute("PRAGMA busy_timeout=80")  # contention = fast, deterministic error
        self.ui.executescript(SCHEMA)

    def tearDown(self):
        for c in (self.ui, self.disp):
            try:
                c.close()
            except Exception:
                pass

    def _seed(self, task_id, status, run=None):
        self.ui.execute(
            "INSERT INTO tasks (id, status, claim_lock, claim_expires, worker_pid, current_run_id) "
            "VALUES (?, ?, NULL, NULL, NULL, ?)",
            (task_id, status, run))

    def _row(self, task_id, conn=None):
        c = conn or self.ui
        r = c.execute(
            "SELECT status, claim_lock, worker_pid, current_run_id FROM tasks WHERE id = ?",
            (task_id,)).fetchone()
        return dict(r)

    def _dispatcher_claim(self, task_id, results, errors):
        """Host dispatcher's atomic ready->running CAS, mirroring
        claim_task's write_txn + guarded UPDATE shape on its own
        connection."""
        try:
            self.disp.execute("BEGIN IMMEDIATE")
            cur = self.disp.execute(
                "UPDATE tasks SET status='running', claim_lock='host:4242', "
                "claim_expires='2000000000', worker_pid=4242, current_run_id=99 "
                "WHERE id=? AND status='ready' AND claim_lock IS NULL",
                (task_id,))
            results["rowcount"] = cur.rowcount
            self.disp.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            errors.append(str(exc))
            try:
                self.disp.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass

    def _wait_until(self, pred, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return True
            time.sleep(0.02)
        return pred()


class TestHarnessSensitivity(_RaceHarness):
    """Pre-fix wrapper shape (read BEFORE the helper opens its txn) must
    reproduce the exact clobber the finding describes, or this suite is
    proving nothing."""

    def test_old_shape_clobbers_fresh_host_claim(self):
        self._seed("t_race1", "ready")
        read_done = threading.Event()
        claim_committed = threading.Event()

        def hook():
            read_done.set()          # historical racy position: no lock held
            self.assertTrue(claim_committed.wait(10), "dispatcher never committed")

        def old_shape_guard(conn, task_id):
            current = self.kdb.get_task(conn, task_id)  # read outside any txn: the bug
            if server.worker_requeue_denied(getattr(current, "status", None), "ready"):
                outcomes["refused"] = True
                return None
            return self.wrapper._worker_control_upstream(conn, task_id, "ready")

        outcomes = {}
        self.kdb.get_task_hook = hook
        results, errors = {}, []

        def dispatcher():
            self.assertTrue(read_done.wait(10))  # commit exactly in the window
            self._dispatcher_claim("t_race1", results, errors)
            claim_committed.set()

        t_disp = threading.Thread(target=dispatcher)
        t_disp.start()
        old_shape_guard(self.ui, "t_race1")  # blocks in the hook until claim commits
        t_disp.join(10)
        self.assertFalse(t_disp.is_alive())

        # Dispatcher won the window with no write lock held by the guard...
        self.assertFalse(errors, f"pre-fix window must be lock-free: {errors}")
        self.assertEqual(results.get("rowcount"), 1, "dispatcher claim must have committed")
        # ...the stale 'ready' read slipped past the predicate, and the
        # helper clobbered the fresh claim + worker PID -- the finding.
        row = self._row("t_race1")
        self.assertEqual(row["status"], "ready")
        self.assertIsNone(row["claim_lock"])
        self.assertIsNone(row["worker_pid"])
        self.assertNotIn("refused", outcomes)


class TestGuardedRacePatchesAndBulk(_RaceHarness):
    """The shipped guard: dispatcher locked out at the old window (both
    flows), fresh claim never clobbered, refusal still exact."""

    def _assert_locked_out(self, flow, task_id):
        self._seed(task_id, "ready", run=7)
        read_done = threading.Event()
        proceed = threading.Event()
        outcomes = {}

        def hook():
            read_done.set()  # read is now INSIDE the guard's write txn
            self.assertTrue(proceed.wait(10))

        def run_guard():
            try:
                outcomes["value"] = flow(self.ui, task_id, "ready")
            except Exception as exc:
                outcomes["raised"] = exc

        self.kdb.get_task_hook = hook
        results, errors = {}, []
        t_guard = threading.Thread(target=run_guard)
        t_guard.start()
        self.assertTrue(read_done.wait(10), "guard never reached the (now under-lock) read")
        t_disp = threading.Thread(target=self._dispatcher_claim, args=(task_id, results, errors))
        t_disp.start()
        # The dispatcher attempts BEGIN IMMEDIATE and must NOT be able to
        # commit while the guard hold is open: contention settles as a
        # lock error, never as rowcount==1.
        self.assertTrue(self._wait_until(lambda: bool(results) or bool(errors)),
                        "dispatcher neither committed nor hit the write lock")
        self.assertEqual(results, {}, "claim must NOT have committed during the hold")
        self.assertTrue(errors and "locked" in " ".join(errors).lower(),
                        f"expected SQLITE_BUSY from the dispatcher, got {errors}")
        proceed.set()
        t_guard.join(10)
        t_disp.join(10)
        self.assertFalse(t_guard.is_alive() or t_disp.is_alive(), "threads hung")

        # The guarded mutation proceeded cleanly from its under-lock read.
        self.assertNotIn("raised", outcomes)
        self.assertIs(True, outcomes.get("value"))
        row = self._row(task_id)
        self.assertEqual(row["status"], "ready")   # ready -> ready no-op transition
        self.assertIsNone(row["claim_lock"])        # never any committed claim to clobber
        self.assertIsNone(row["worker_pid"])
        # recompute_ready JOINED the open txn (a nested BEGIN would have
        # raised 'cannot start a transaction within a transaction' and
        # landed in outcomes['raised']).
        self.assertIn("called", self.kdb.recompute_ready_calls)

    def test_patch_flow_dispatcher_locked_out_at_old_read_position(self):
        self._assert_locked_out(self.plugin.update_task_flow, "t_race2a")

    def test_bulk_flow_dispatcher_locked_out_at_old_read_position(self):
        self._assert_locked_out(self.plugin.bulk_apply_one_flow, "t_race2b")

    def test_committed_claim_before_guard_is_refused_claim_intact(self):
        # Other half of the interleaving: claim fully committed BEFORE the
        # guard's early BEGIN IMMEDIATE -> the under-lock read sees
        # 'running' and refuses; claim + worker PID + run pointer all
        # untouched (no clobber, no duplicate dispatch).
        for flow, task_id in (
            (self.plugin.update_task_flow, "t_race3a"),
            (self.plugin.bulk_apply_one_flow, "t_race3b"),
        ):
            self._seed(task_id, "ready")
            results, errors = {}, []
            self._dispatcher_claim(task_id, results, errors)
            self.assertEqual(results.get("rowcount"), 1, "fixture claim must commit")
            self.assertFalse(errors)
            with self.assertRaises(Exception) as cm:  # channel B: fastapi HTTPException
                flow(self.ui, task_id, "ready")
            self.assertEqual(getattr(cm.exception, "status_code", None), 400)
            self.assertEqual(cm.exception.detail, server.WORKER_REQUEUE_REJECT_MSG)
            row = self._row(task_id)
            self.assertEqual(row["status"], "running")
            self.assertEqual(row["claim_lock"], "host:4242")
            self.assertEqual(row["worker_pid"], 4242)
            self.assertEqual(row["current_run_id"], 99)  # run NOT ended 'reclaimed'
            # Refusal rolled back inside the txn: zero event rows written.
            self.assertEqual(
                self.ui.execute("SELECT count(*) AS n FROM task_events WHERE task_id = ?",
                                (task_id,)).fetchone()["n"], 0)

    def test_idle_ready_promotion_still_works_under_the_early_lock(self):
        # Non-running cards keep flowing: the early BEGIN IMMEDIATE must
        # not change ordinary drag behaviour on either flow.
        self._seed("t_race4", "todo")
        self.assertIs(True, self.plugin.update_task_flow(self.ui, "t_race4", "ready"))
        self.assertEqual(self._row("t_race4")["status"], "ready")
        self.assertIs(True, self.plugin.bulk_apply_one_flow(self.ui, "t_race4", "ready"))


class TestShimBehaviour(_RaceHarness):
    """The write_txn shim is transparent for every caller except the
    guard-holding thread/connection pair."""

    def test_join_only_for_held_thread_and_connection(self):
        import contextlib

        other = sqlite3.connect(self.dbfile, isolation_level=None, check_same_thread=False)
        other.execute("PRAGMA journal_mode=WAL")
        try:
            with self.shim.hold(self.ui):
                joined = self.shim(self.ui)
                self.assertIsInstance(joined, contextlib.nullcontext)
                # Different connection: still upstream's own boundary.
                self.assertIsNot(type(self.shim(other)), contextlib.nullcontext)
                # Same connection, DIFFERENT thread: must NOT join (the
                # thread-local marker never leaks) -- BEGIN on the
                # connection already writing raises, not a silent join.
                box = {}

                def foreign():
                    try:
                        with self.shim(self.ui):
                            box["joined"] = True
                    except sqlite3.OperationalError as exc:
                        box["error"] = str(exc)

                t = threading.Thread(target=foreign)
                t.start()
                t.join(10)
                self.assertNotIn("joined", box)
                self.assertIn("error", box)
            # Outside any hold the shim is invisible: upstream boundary.
            self.assertIsNot(type(self.shim(self.ui)), contextlib.nullcontext)
        finally:
            other.close()

    def test_hold_commits_and_rolls_back_like_upstream(self):
        self._seed("t_race5", "todo")
        with self.shim.hold(self.ui):
            self.ui.execute("UPDATE tasks SET status='ready' WHERE id='t_race5'")
            self.assertTrue(self.ui.in_transaction)
        self.assertFalse(self.ui.in_transaction)
        self.assertEqual(self._row("t_race5")["status"], "ready")
        # Exception inside the hold rolls back (upstream rollback-on-raise).
        with self.assertRaises(RuntimeError):
            with self.shim.hold(self.ui):
                self.ui.execute("UPDATE tasks SET status='blocked' WHERE id='t_race5'")
                raise RuntimeError("boom")
        self.assertFalse(self.ui.in_transaction)
        self.assertEqual(self._row("t_race5")["status"], "ready")

    def test_double_boot_unwraps_previous_shim(self):
        # A second build_app over the same mounted kanban_db must not
        # stack shims (build_app unwraps via _upstream/_kanban_ui_reentrant_shim).
        d = tempfile.mkdtemp(prefix="kbui-race-plugin-")
        (Path(d) / "plugin_api.py").write_text(_STUB)
        saved_plugin = server.PLUGIN
        try:
            server.PLUGIN = Path(d)
            server.build_app("synthetic-test-secret-0123456789abcdef", "", "", "")
            second = sys.modules["kanban_plugin_api"]
            shim2 = second._set_status_direct._worker_control_write_txn
            self.assertTrue(getattr(shim2, "_kanban_ui_reentrant_shim", False))
            # Chained unwrap terminates at a real function, not another shim.
            u = shim2._upstream
            self.assertFalse(getattr(u, "_kanban_ui_reentrant_shim", False))
        finally:
            server.PLUGIN = saved_plugin


if __name__ == "__main__":
    unittest.main(verbosity=2)
