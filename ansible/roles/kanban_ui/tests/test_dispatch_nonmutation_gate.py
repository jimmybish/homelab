#!/usr/bin/env python3
"""Persisted-state non-mutation gate regression (PR#13 review finding
4331497811, verify_session.sh step 10b).

The retired check hashed the live ``GET /api/kanban/board`` render of the
SHARED homelab board before/after the refused ``POST /api/kanban/dispatch``
and required byte-equality. Copilot correctly flagged that this cannot
prove non-mutation:

  * the pinned render embeds per-read computed fields (``_task_dict`` in
    plugins/kanban/dashboard/plugin_api.py adds ``age`` via
    ``kanban_db.task_age`` -- ``created_age_seconds`` is recomputed from
    wall-clock every read -- and ``current_run_started_ats``), so two
    renders of an UNCHANGED database legitimately differ once the clock
    crosses a second boundary between them; and
  * the gateway's embedded dispatcher
    (gateway/kanban_watchers_dispatcher.py, 60s interval) ticks EVERY
    live board and legitimately writes claim extensions, reclaim
    bookkeeping and task_events on unrelated cards.

  A correctly refused dispatch can therefore fail the deployment gate.

The shipped step 10b instead fingerprints the PERSISTED state of a
disposable gate board read directly from its SQLite file (tasks,
task_runs, task_events, task_comments, task_links, kanban_notify_subs,
each with a deterministic ORDER BY), holds the board's ``.dispatch.lock``
flock across the window, and tears the board down on every exit path.

Harness (stdlib-only, like the sibling suites):
  * a SQLite database with the pinned table shapes and the script's OWN
    seed/fingerprint code extracted verbatim from verify_session.sh;
  * a pinned-render model proving the presentation hash flakes while the
    persisted fingerprint stays stable (the finding reproduced);
  * a pinned ``release_stale_claims``-shaped mutation (CAS UPDATE of the
    claim columns to ready + ``reclaimed``/``failed`` bookkeeping events)
    proving the fingerprint catches a genuine dispatcher write -- the
    same expectation the task-gated fault-injection run asserts live.

Round-6d additions (review findings 1-3):
  * the shipped GATE_INJECT_PY heredoc is EXECUTED against stub
    pinned-monolith (b3aa561f) and split-module hermes_cli trees, so the
    fault injection is proven to run on the layout the repo actually
    pins, not just pattern-matched;
  * the shipped GATE_LOCK_PY publication protocol is executed (fresh
    create, gateway-created adoption, fail-closed shapes) and a FORCED
    gateway tick is made to land inside the create->link interval via an
    os.link hook, proving the pinned PermissionError->acquired degrade
    can never fire against the published lock (the old root-create->
    chown sequence is modelled as the degrading control);
  * the gate_teardown root fallback is executed in bash against near-
    miss slugs (kanban-ui-verify-1-production, suffix/traversal/
    half-grammar variants) and a live locked sibling board; the retired
    -mmin +60 find sweep must stay deleted from the script.

    python3 ansible/roles/kanban_ui/tests/test_dispatch_nonmutation_gate.py

Exit 0 = all pass. Executed with plain python3 (no fastapi needed).
"""
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERIFIER = HERE.parent / "files" / "verify_session.sh"
TASKS = HERE.parent / "tasks" / "main.yaml"
VARS = HERE.parent / "vars" / "main.yaml"


def _extract_heredoc(text, tag):
    """Return the body of the <<'TAG' ... TAG heredoc in the script.
    Tolerates redirects following the opening tag line."""
    m = re.search(r"<<'%s'[^\n]*\n(.*?\n)%s\n" % (tag, tag), text, re.S)
    assert m, f"heredoc {tag} not found in verify_session.sh"
    return m.group(1)


SCRIPT = VERIFIER.read_text()
SEED_SRC = _extract_heredoc(SCRIPT, "GATE_SEED_PY")
FP_SRC = _extract_heredoc(SCRIPT, "GATE_FP_PY")
LOCK_SRC = _extract_heredoc(SCRIPT, "GATE_LOCK_PY")
INJECT_SRC = _extract_heredoc(SCRIPT, "GATE_INJECT_PY")

# The shipped gate_teardown root-fallback guard (round-6d finding 2):
# extracted verbatim so tests EXECUTE it in bash rather than trusting a
# substring of its regex.
_m = re.search(r'if \[\[ "\$GATE_SLUG" =~.*?\n  fi\n', SCRIPT, re.S)
assert _m, "gate_teardown slug guard block not found in verify_session.sh"
TEARDOWN_GUARD = _m.group(0)

# Pinned table shapes (subset of the live schema actually referenced by
# the seed and fingerprint code; column sets match kanban.db exactly for
# the columns touched).
SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT,
    status TEXT NOT NULL, priority INTEGER DEFAULT 0, created_by TEXT,
    created_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER,
    workspace_kind TEXT NOT NULL DEFAULT 'scratch', workspace_path TEXT,
    branch_name TEXT, project_id TEXT, claim_lock TEXT, claim_expires INTEGER,
    tenant TEXT, result TEXT, idempotency_key TEXT,
    consecutive_failures INTEGER DEFAULT 0, worker_pid INTEGER,
    last_failure_error TEXT, max_runtime_seconds INTEGER,
    last_heartbeat_at INTEGER, current_run_id INTEGER,
    block_kind TEXT, block_recurrences INTEGER DEFAULT 0,
    worker_started_at INTEGER
);
CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL);
CREATE TABLE task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
    author TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
    run_id INTEGER, kind TEXT NOT NULL, payload TEXT,
    created_at INTEGER NOT NULL);
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
    profile TEXT, step_key TEXT, status TEXT NOT NULL, claim_lock TEXT,
    claim_expires INTEGER, worker_pid INTEGER, max_runtime_seconds INTEGER,
    last_heartbeat_at INTEGER, started_at INTEGER NOT NULL, ended_at INTEGER,
    outcome TEXT, summary TEXT, metadata TEXT, error TEXT,
    worker_started_at INTEGER);
CREATE TABLE kanban_notify_subs (
    task_id TEXT NOT NULL, platform TEXT NOT NULL, chat_id TEXT NOT NULL,
    chat_type TEXT, thread_id TEXT NOT NULL DEFAULT '', user_id TEXT,
    notifier_profile TEXT, delivery_metadata TEXT, created_at INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    last_ping_event_id INTEGER NOT NULL DEFAULT 0,
    delivery_mode TEXT NOT NULL DEFAULT 'notify', user_id_alt TEXT,
    PRIMARY KEY (task_id, platform, chat_id, thread_id));
"""


def make_gate_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def run_seed(path, claim_expires=None):
    """Execute the SHIPPED seed source (verbatim from the script)."""
    claim_expires = claim_expires or (int(time.time()) - 3600)
    proc = subprocess.run(
        [sys.executable, "-", path, "1780000000", str(claim_expires)],
        input=SEED_SRC, text=True, capture_output=True)
    assert proc.returncode == 0, f"seed failed: {proc.stderr}"


def run_fingerprint(path):
    """Execute the SHIPPED fingerprint source (verbatim from the script)."""
    proc = subprocess.run(
        [sys.executable, "-", path], input=FP_SRC, text=True,
        capture_output=True)
    assert proc.returncode == 0, f"fingerprint failed: {proc.stderr}"
    return proc.stdout.strip()


class _TempDb(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="kbui-gate-")
        self.addCleanup(self.dir.cleanup)
        self.db = os.path.join(self.dir.name, "kanban.db")
        make_gate_db(self.db)


class TestSeedAndFingerprintDeterminism(_TempDb):
    def test_fingerprint_is_deterministic_across_reads(self):
        run_seed(self.db)
        fp1 = run_fingerprint(self.db)
        fp2 = run_fingerprint(self.db)
        self.assertRegex(fp1, r"^[0-9a-f]{64}$")
        self.assertEqual(fp1, fp2)

    def test_fingerprint_stable_across_wal_checkpoint_and_reopen(self):
        # The seed writes through WAL; the fingerprint reopens read-only.
        # A checkpoint between reads must not perturb the logical digest
        # (byte-level file hashing WOULD flake here; the logical digest
        # is byte-independent).
        run_seed(self.db)
        fp1 = run_fingerprint(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        fp2 = run_fingerprint(self.db)
        self.assertEqual(fp1, fp2)


class TestPresentationDriftModel(_TempDb):
    """The exact flake path Copilot flagged, reproduced on a DB that
    NOTHING writes between the two reads."""

    def _pinned_task_dict(self, conn, now):
        # Model of pinned _task_dict: asdict(task) + age (task_age) +
        # current_run_started_at. Only the drift-relevant fields matter.
        rows = []
        for r in conn.execute(
                "SELECT id,status,created_at,started_at,completed_at,"
                "current_run_id FROM tasks ORDER BY id"):
            d = dict(r)
            d["age"] = {
                "created_age_seconds": now - r["created_at"],
                "started_age_seconds": (now - r["started_at"])
                if r["started_at"] else None,
                "time_to_complete_seconds": None,
            }
            cur = conn.execute(
                "SELECT started_at FROM task_runs WHERE task_id=? "
                "ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
            d["current_run_started_at"] = cur["started_at"] if cur else None
            rows.append(d)
        return rows

    def test_identical_db_renders_differently_over_time_hash_flakes(self):
        run_seed(self.db)
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        render1 = json.dumps(
            self._pinned_task_dict(conn, int(time.time())), sort_keys=True)
        time.sleep(1.1)  # cross a wall-clock second (no DB writes at all)
        render2 = json.dumps(
            self._pinned_task_dict(conn, int(time.time())), sort_keys=True)
        conn.close()
        # Sensitivity of the RETIRED check: identical persisted state,
        # different bytes -> sha mismatch -> false gate failure.
        self.assertNotEqual(render1, render2,
                            "presentation model must drift over time")
        self.assertNotEqual(hashlib.sha256(render1.encode()).hexdigest(),
                            hashlib.sha256(render2.encode()).hexdigest())
        # The shipped persisted-state fingerprint is immune.
        self.assertEqual(run_fingerprint(self.db),
                         run_fingerprint(self.db))


class TestMutationDetection(_TempDb):
    """Sensitivity: the mutation a real dispatch_once would make MUST
    change the fingerprint (the live fault-injection run asserts the
    identical expectation against the pinned dispatcher itself)."""

    def _mutate_like_release_stale_claims(self):
        # Mirrors pinned kanban_db.release_stale_claims + _record_reclaim
        # + _record_task_failure for a foreign expired claim: CAS UPDATE
        # to ready clearing claim columns, run ended 'reclaimed', and the
        # two bookkeeping events.
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        now = int(time.time())
        row = conn.execute(
            "SELECT id, claim_lock, worker_pid FROM tasks "
            "WHERE status='running' AND claim_expires < ?", (now,)).fetchone()
        assert row, "fixture must present an expired foreign claim"
        with conn:
            conn.execute(
                "UPDATE tasks SET status='ready', claim_lock=NULL, "
                "claim_expires=NULL, worker_pid=NULL, worker_started_at=NULL "
                "WHERE id=? AND status='running' AND claim_lock IS ?",
                (row["id"], row["claim_lock"]))
            conn.execute(
                "UPDATE task_runs SET status='reclaimed', ended_at=?, "
                "outcome='crashed', error=? WHERE task_id=?",
                (now, f"stale_lock={row['claim_lock']}", row["id"]))
            conn.execute(
                "INSERT INTO task_events (task_id,run_id,kind,payload,"
                "created_at) VALUES (?,?,?,?,?)",
                (row["id"], 1, "reclaimed",
                 json.dumps({"stale_lock": row["claim_lock"],
                             "host_local": False, "retry_status": "ready"}),
                 now))
            conn.execute(
                "INSERT INTO task_events (task_id,kind,created_at) "
                "VALUES (?,?,?)", (row["id"], "failed", now))
            conn.execute(
                "UPDATE tasks SET consecutive_failures=1 WHERE id=?",
                (row["id"],))
        conn.close()

    def test_reclaim_shaped_mutation_changes_fingerprint(self):
        run_seed(self.db)
        before = run_fingerprint(self.db)
        self._mutate_like_release_stale_claims()
        after = run_fingerprint(self.db)
        self.assertNotEqual(before, after)

    def test_unrelated_board_activity_changes_fingerprint(self):
        # Legit worker chatter (a comment on a foreign card) must also be
        # visible: the fingerprint is the full persisted state, so any
        # write -- even one the reclaim path never touches -- trips it.
        run_seed(self.db)
        before = run_fingerprint(self.db)
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute(
                "INSERT INTO task_comments (task_id,author,body,created_at)"
                " VALUES ('kanban-ui-gate','worker','heartbeat note',?)",
                (int(time.time()),))
        conn.close()
        self.assertNotEqual(before, run_fingerprint(self.db))

    def test_repair_seed_restores_canonical_fingerprint(self):
        # The seed runs AFTER the board dispatch lock is taken. If a
        # legitimate gateway tick already reclaimed the sentinel in the
        # create->lock gap, re-seeding must deterministically restore the
        # canonical fixture so the before-fingerprint is reproducible.
        run_seed(self.db)
        canonical = run_fingerprint(self.db)
        self._mutate_like_release_stale_claims()
        self.assertNotEqual(canonical, run_fingerprint(self.db))
        run_seed(self.db)
        self.assertEqual(canonical, run_fingerprint(self.db))


class TestFixtureShape(unittest.TestCase):
    """The shipped seed presents the exact preconditions the pinned
    reclaim path selects on."""

    def test_seed_provides_expired_foreign_claim_and_sticky_parked_row(self):
        d = tempfile.mkdtemp(prefix="kbui-gate-fixture-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        db = os.path.join(d, "kanban.db")
        make_gate_db(db)
        run_seed(db)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        now = int(time.time())
        sel = conn.execute(
            "SELECT id, claim_lock FROM tasks WHERE status='running' "
            "AND claim_expires IS NOT NULL AND claim_expires < ?",
            (now,)).fetchall()
        self.assertEqual([r["id"] for r in sel], ["kanban-ui-gate"])
        self.assertFalse(
            sel[0]["claim_lock"].startswith(f"{os.uname().nodename}:"),
            "claim_lock must be foreign to the host so reclaim proceeds "
            "past the live-worker extension branch")
        # Sticky-blocked parked row pins recompute_ready: last event is
        # the worker-initiated block, so a tick never promotes it.
        ev = conn.execute(
            "SELECT kind FROM task_events WHERE task_id="
            "'kanban-ui-gate-parked' ORDER BY created_at DESC, id DESC "
            "LIMIT 1").fetchone()
        self.assertEqual(ev["kind"], "blocked")
        # The 'claimed' event's source_status keeps _retry_status_for_run
        # deterministic (ready) so the CAS target is reproducible.
        cl = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id="
            "'kanban-ui-gate' AND kind='claimed'").fetchone()["payload"])
        self.assertEqual(cl["source_status"], "ready")
        conn.close()


class TestVerifierSourceLocks(unittest.TestCase):
    """The finding's required properties stay true in the shipped script:
    403 + documented rejection body remain the primary refusal checks;
    no live-presentation hash is compared for the dispatch leg; the
    persisted-state fingerprint is isolated and torn down."""

    def test_primary_refusal_checks_retained(self):
        self.assertIn('dispatcher-pass dispatch expected 403', SCRIPT)
        self.assertIn("'dispatcher passes are disabled'", SCRIPT)

    def test_live_board_hash_removed(self):
        self.assertNotIn("board_sha", SCRIPT,
                         "the retired live /board sha oracle must stay gone")
        self.assertNotIn("refused dispatch pass mutated board state", SCRIPT)

    def test_fingerprint_reads_persisted_db_directly(self):
        self.assertIn("gate_fingerprint", SCRIPT)
        self.assertIn("?mode=ro", SCRIPT,
                      "fingerprint must read the SQLite file, not an endpoint")

    def test_dispatch_lock_held_and_real_contention_preserved(self):
        self.assertIn('exec 9<>"$GATE_DB.dispatch.lock"', SCRIPT)
        self.assertIn("flock -x 9", SCRIPT)
        # Round-6d finding 3: the lock is published by GATE_LOCK_PY with
        # final ownership (stage + fchown + os.link). The retired
        # root-create-then-chown sequence must stay gone: any chown of
        # the published lock file would mean the root-owned window is
        # back.
        self.assertNotIn('chown "$KANBAN_UI_BOARD_UID:$KANBAN_UI_BOARD_GID" '
                         '"$GATE_DB.dispatch.lock"', SCRIPT)
        self.assertIn('"$GATE_DB.dispatch.lock" "$KANBAN_UI_BOARD_UID"',
                      SCRIPT)

    def test_teardown_on_every_exit_path_and_slug_grammar_guard(self):
        self.assertRegex(SCRIPT, r"trap 'gate_teardown; rm -rf \"\$WORK\"' EXIT")
        # Teardown only ever rm -rf's the exact-grammar slug inside the
        # boards root (never a glob, never an arbitrary path). The guard
        # itself is EXECUTED (not merely pattern-matched) by
        # TestStaleSweepRemoved.test_teardown_guard_only_touches_own_board.
        self.assertIn("kanban-ui-verify-[0-9]+-[0-9]+$", SCRIPT)
        self.assertRegex(
            SCRIPT,
            r"rm -rf -- \"\$\{KANBAN_UI_KANBAN_HOME:\?\}/kanban/boards/"
            r"\$GATE_SLUG\"")
        # API teardown targets the disposable board, hard-delete mode.
        self.assertIn("/api/kanban/boards/%s?delete=true", SCRIPT)

    def test_injection_mode_inverts_expectation(self):
        self.assertIn('_dispatch_once_locked', SCRIPT)
        self.assertIn(
            "fault injection mutated the gate board but the "
            "persisted-state gate did NOT detect it", SCRIPT)
        # Round-6d finding 1: BOTH dispatcher and connect imports must be
        # layout-tolerant; the retired unconditional split import must
        # stay gone (the pinned b3aa561f tree only ships the monolith).
        self.assertIn("pinned b3aa561f monolith layout", SCRIPT)
        self.assertIn("connect() lives in the monolith", SCRIPT)
        self.assertNotIn("import kanban_db_dispatch as kbd", SCRIPT)

    def test_anonymous_and_contrast_checks_untouched(self):
        # Round-5 legs this card must NOT regress: anon 401 at the session
        # gate, and the plain-reassign over-rejection contrast.
        self.assertIn("anon dispatch expected 401", SCRIPT)
        self.assertIn("plain reassign over-rejected", SCRIPT)


class TestInjectionRunsOnPinnedLayout(unittest.TestCase):
    """Round-6d finding 1: EXECUTE the shipped GATE_INJECT_PY heredoc
    against stub trees for BOTH layouts, not a source substring.

    The repo pins Hermes 0.20.0 at b3aa561f (ansible/roles/hermes/vars/
    main.yaml), whose hermes_cli/ ships ONLY kanban_db.py. The retired
    helper imported hermes_cli.kanban_db_dispatch unconditionally, so on
    the declared deployment revision injection died at the first import
    (the live PASS had been produced by a newer-than-pin checkout). The
    positive control here is the red/green pair: the retired import lines
    MUST fail on the pinned-layout tree, the shipped heredoc MUST
    succeed and must actually mutate the DB (fingerprint changes).
    """

    def _make_repo(self, layout):
        d = tempfile.mkdtemp(prefix="kbui-inject-repo-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        pkg = os.path.join(d, "hermes_cli")
        os.makedirs(pkg)
        with open(os.path.join(pkg, "__init__.py"), "w") as f:
            f.write("")
        def write(name, body):
            with open(os.path.join(pkg, name), "w") as f:
                f.write(body)
        mutation = '''
def _dispatch_once_locked(conn, spawn_fn=None, board=None):
    with conn:
        conn.execute(
            "INSERT INTO task_events (task_id,run_id,kind,payload,"
            "created_at) VALUES (?,?,?,?,?)",
            ("kanban-ui-gate", 1, "reclaimed",
             '{"stale_lock": "forced"}', 1790000000))
    return "tick"
'''
        connector = '''
import os, sqlite3
def connect(db_path=None, *, board=None):
    return sqlite3.connect(os.environ["KBUI_TEST_DB"], timeout=30)
'''
        broken = '''
def connect(*a, **k):
    raise AssertionError("wrong module selected")
def _dispatch_once_locked(*a, **k):
    raise AssertionError("wrong module selected")
'''
        if layout == "pinned_monolith":  # exactly the b3aa561f shape
            write("kanban_db.py", connector + mutation)
        elif layout == "split":
            # kanban_db.py exists but is a decoy: selecting it instead of
            # the split modules must blow up loudly.
            write("kanban_db.py", broken)
            write("kanban_db_connect.py", connector)
            write("kanban_db_dispatch.py", mutation)
        else:  # 'old_helper_on_pinned' handled by callers
            raise AssertionError(layout)
        return d

    def _run_inject(self, repo, db):
        env = dict(os.environ)
        env["KANBAN_UI_HERMES_REPO"] = repo
        env["KBUI_TEST_DB"] = db
        proc = subprocess.run(
            [sys.executable, "-", "gate-board"], input=INJECT_SRC,
            text=True, capture_output=True, env=env)
        return proc

    def _fresh_db(self):
        d = tempfile.mkdtemp(prefix="kbui-inject-db-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        db = os.path.join(d, "kanban.db")
        make_gate_db(db)
        run_seed(db)
        return db

    def test_pinned_monolith_layout_injection_runs_and_mutates(self):
        repo = self._make_repo("pinned_monolith")
        db = self._fresh_db()
        before = run_fingerprint(db)
        proc = self._run_inject(repo, db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("injected tick completed", proc.stdout)
        self.assertNotEqual(before, run_fingerprint(db),
                            "injected tick must actually write the board")

    def test_split_layout_injection_runs_and_selects_split_modules(self):
        repo = self._make_repo("split")
        db = self._fresh_db()
        before = run_fingerprint(db)
        proc = self._run_inject(repo, db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("injected tick completed", proc.stdout)
        self.assertNotIn("wrong module", proc.stderr + proc.stdout)
        self.assertNotEqual(before, run_fingerprint(db))

    def test_retired_unconditional_split_import_fails_on_pinned_tree(self):
        # RED control: the exact retired import lines (pre-round-6d) die
        # on the pinned b3aa561f layout, proving the shipped fallback is
        # what makes injection executable on the declared revision rather
        # than lucking into a newer checkout. The pinned monolith has no
        # kanban_db_dispatch submodule, so 'from hermes_cli import
        # kanban_db_dispatch' raises ImportError and the retired helper
        # never reaches the tick call.
        retired = (
            "import os, sys\n"
            "sys.path.insert(0, os.environ[\"KANBAN_UI_HERMES_REPO\"])\n"
            "from hermes_cli import kanban_db_dispatch as kbd\n"
            "try:\n"
            "    from hermes_cli import kanban_db_connect as kbc\n"
            "    _connect = kbc.connect\n"
            "except Exception:\n"
            "    from hermes_cli import kanban_db as _kb\n"
            "    _connect = _kb.connect\n")
        repo = self._make_repo("pinned_monolith")
        db = self._fresh_db()
        env = dict(os.environ)
        env["KANBAN_UI_HERMES_REPO"] = repo
        env["KBUI_TEST_DB"] = db
        proc = subprocess.run([sys.executable, "-", "gate-board"],
                              input=retired, text=True,
                              capture_output=True, env=env)
        self.assertNotEqual(proc.returncode, 0,
                            "retired split-only import must fail on the "
                            "pinned monolith layout")
        self.assertIn("ImportError", proc.stderr)
        self.assertNotIn("injected tick completed", proc.stdout)


class _PinnedTickLockModel:
    """Verbatim behavioural model of pinned _dispatch_tick_lock
    (hermes_cli/kanban_db.py @ b3aa561f): open('a+b'); non-blocking
    fcntl.flock LOCK_EX; ANY OSError from the open degrades to
    acquired=True. Returns (acquired, handle) plus a 'degraded' flag so
    tests can assert the degrade path never fires against the shipped
    publication protocol."""

    @staticmethod
    def acquire(path):
        import fcntl
        try:
            handle = open(path, "a+b")
        except OSError:
            return True, None, True  # degraded: PermissionError -> acquired
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True, handle, False
        except BlockingIOError:
            handle.close()
            return False, None, False
        except OSError:
            handle.close()
            return True, None, True


class TestLockPublicationProtocol(unittest.TestCase):
    """Round-6d finding 3: EXECUTE the shipped GATE_LOCK_PY heredoc."""

    def _run_publish(self, final, uid=None, gid=None):
        env_argv = ["gate-lock", final,
                    str(uid if uid is not None else os.getuid()),
                    str(gid if gid is not None else os.getgid())]
        # Run the heredoc body with sys.argv replaced, in-process so the
        # os module patch in the forced-tick test reaches it.
        saved_argv = sys.argv
        sys.argv = env_argv
        try:
            from io import StringIO
            buf = StringIO()
            from contextlib import redirect_stdout, redirect_stderr
            errbuf = StringIO()
            with redirect_stdout(buf), redirect_stderr(errbuf):
                try:
                    exec(compile(LOCK_SRC, "<GATE_LOCK_PY>", "exec"),
                         {"__name__": "__gate_lock__"})
                    rc = 0
                except SystemExit as e:
                    rc = e.code or 0
                except BaseException:
                    import traceback
                    traceback.print_exc(file=errbuf)
                    rc = 1
            return rc, buf.getvalue(), errbuf.getvalue()
        finally:
            sys.argv = saved_argv

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="kbui-lock-")
        self.addCleanup(self.dir.cleanup)
        self.final = os.path.join(self.dir.name, "kanban.db.dispatch.lock")

    def test_fresh_publish_creates_board_owned_0644_lock(self):
        rc, out, err = self._run_publish(self.final)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "created")
        st = os.stat(self.final)
        self.assertEqual(st.st_uid, os.getuid())
        self.assertEqual(st.st_gid, os.getgid())
        import stat as statmod
        self.assertEqual(statmod.S_IMODE(st.st_mode), 0o644)
        # No staging litter left in the boards dir.
        self.assertEqual(
            [n for n in os.listdir(self.dir.name)
             if n.startswith(".dispatch.lock.stage-")], [])

    def test_gateway_created_lock_is_adopted_not_unlinked(self):
        # Simulate the legitimate tick that created the lock itself in
        # the pre-link window: pre-create as the board owner.
        with open(self.final, "a+b") as fh:
            os.chmod(self.final, 0o644)
            inode_before = os.fstat(fh.fileno()).st_ino
        rc, out, err = self._run_publish(self.final)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "adopted")
        self.assertEqual(os.stat(self.final).st_ino, inode_before,
                         "adoption must never replace the lock inode")

    def test_publish_fails_closed_on_nonregular_and_wrong_owner(self):
        os.makedirs(self.final)  # directory at the lock path
        rc, _, err = self._run_publish(self.final)
        self.assertNotEqual(rc, 0, "must fail closed on a non-regular lock")
        self.assertIn("not a regular file", err)
        import shutil
        shutil.rmtree(self.final)
        # Foreign-owned PRE-EXISTING lock at the final path: the adopt
        # leg must reject it (st.st_uid != requested uid). Stat-based,
        # so it is meaningful under any euid; only the fixture chown
        # needs privilege.
        with open(self.final, "w") as fh:
            fh.write("")
        try:
            os.chown(self.final, 65534, 65534)
        except OSError:
            # EPERM unprivileged, or EINVAL inside a user namespace where
            # 65534 is not mapped.
            self.skipTest("cannot stage a foreign-owned fixture here")
        os.chmod(self.final, 0o644)
        rc, _, err = self._run_publish(self.final)  # uid=os.getuid()
        self.assertNotEqual(rc, 0, "must fail closed on foreign ownership")
        self.assertIn("AssertionError", err)

    def test_publish_fails_closed_on_unopenable_owner_lock_shape(self):
        # A pre-existing board-owner lock WITHOUT owner-write (e.g.
        # 0400) would make the gateway's own a+b open raise
        # PermissionError -> pinned degrade -> acquired despite flock.
        # The adopt branch must reject it (owner-write bit required);
        # 0600/0644 shapes are legitimately adopted instead. The check
        # is stat-based, so it is meaningful under any euid.
        for mode, should_pass in ((0o400, False), (0o600, True),
                                  (0o644, True)):
            if os.path.exists(self.final):
                os.chmod(self.final, 0o644)
                os.unlink(self.final)
            with open(self.final, "w") as fh:
                fh.write("")
            os.chmod(self.final, mode)
            rc, out, err = self._run_publish(self.final)
            if should_pass:
                self.assertEqual(rc, 0, f"mode {oct(mode)}: {err}")
                self.assertEqual(out.strip(), "adopted")
            else:
                self.assertNotEqual(
                    rc, 0,
                    f"mode {oct(mode)} must fail closed (degrade shape)")

    def test_forced_gateway_tick_inside_publication_window(self):
        # THE concurrency regression (finding 3): a gateway tick is
        # FORCED to land inside the create->link interval by hooking
        # os.link at the exact instant the final path is still absent
        # and the staging file already carries final ownership. The
        # pinned tick model then does its ordinary a+b open: absent
        # final path means NO PermissionError, so the tick legitimately
        # acquires, writes, releases; the shipped protocol adopts the
        # gateway-created lock and the verifier's later flock still
        # contends for real.
        import fcntl
        tick_ran = {"n": 0}
        real_link = os.link

        def hooked_link(src, dst, *a, **k):
            # Gateway tick arrives mid-publication, as the board owner.
            acquired, handle, degraded = _PinnedTickLockModel.acquire(dst)
            tick_ran["n"] += 1
            self.assertFalse(
                degraded,
                "the pinned PermissionError->acquired degrade must NOT "
                "fire: the final path is absent during publication, so "
                "the gateway open is ordinary, never root-blocked")
            self.assertTrue(acquired)
            # The tick does its legitimate reclaim write...
            db = os.path.join(self.dir.name, "kanban.db")
            if not os.path.exists(db):
                make_gate_db(db)
            conn = sqlite3.connect(db, timeout=30)
            with conn:
                conn.execute(
                    "INSERT INTO task_events (task_id,run_id,kind,"
                    "payload,created_at) VALUES (?,?,?,?,?)",
                    ("kanban-ui-gate", None, "reclaimed",
                     '{"forced": "window-tick"}', 1790000001))
            conn.close()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            return real_link(src, dst, *a, **k)

        os.link = hooked_link
        try:
            rc, out, err = self._run_publish(self.final)
        finally:
            os.link = real_link
        self.assertEqual(rc, 0, err)
        self.assertEqual(tick_ran["n"], 1, "tick must have landed in-window")
        self.assertEqual(out.strip(), "adopted",
                         "gateway-created lock must be adopted")
        # Post-publication, a held verifier flock makes the next tick
        # LOSE contention (the real path, not the degrade path).
        fd = os.open(self.final, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            acquired, handle, degraded = _PinnedTickLockModel.acquire(
                self.final)
            self.assertFalse(acquired,
                             "held lock must cause a real contention loss")
            self.assertFalse(degraded)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_pinned_degrade_model_documents_the_retired_root_window(self):
        # Control documenting WHY the old root-create->chown order was
        # exploitable: an unreadable-by-gateway (modelled: un-openable)
        # lock file makes the pinned tick open() raise OSError, which
        # the pinned code maps to acquired=True -> a tick writes despite
        # the verifier's flock. The shipped protocol never exposes the
        # final path in such a state.
        if os.geteuid() == 0:
            self.skipTest("mode 000 does not deny root an open")
        with open(self.final, "w") as fh:
            fh.write("")
        os.chmod(self.final, 0o000)
        try:
            acquired, handle, degraded = _PinnedTickLockModel.acquire(
                self.final)
            self.assertTrue(acquired and degraded,
                            "model must reproduce the pinned degrade")
            if handle:
                handle.close()
        finally:
            os.chmod(self.final, 0o644)


class TestStaleSweepRemoved(unittest.TestCase):
    """Round-6d finding 2: the opportunistic root sweep is gone, and the
    shipped gate_teardown guard is EXECUTED (bash) against adversarial
    slugs rather than pattern-asserted."""

    def _run_guard(self, home, slug):
        env = dict(os.environ)
        env["KANBAN_UI_KANBAN_HOME"] = home
        env["GATE_SLUG"] = slug
        script = "set -u\n" + TEARDOWN_GUARD
        return subprocess.run(["bash", "-c", script], env=env,
                              text=True, capture_output=True)

    def test_opportunistic_sweep_stays_deleted(self):
        self.assertNotIn("-mmin +60", SCRIPT)
        self.assertNotIn("-exec rm -rf", SCRIPT)
        self.assertNotRegex(SCRIPT, r"find .*kanban-ui-verify")

    def test_teardown_guard_only_touches_own_board(self):
        home = tempfile.mkdtemp(prefix="kbui-home-")
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        boards = os.path.join(home, "kanban", "boards")
        os.makedirs(boards)
        own = "kanban-ui-verify-4242-1780000000"
        # A live sibling verifier board holding its dispatch lock, and
        # near-miss / adversarial names the retired glob admitted.
        siblings = [
            own,
            "kanban-ui-verify-1-production",   # retired glob admitted this
            "kanban-ui-verify-4242",           # missing epoch segment
            "kanban-ui-verify-4242-1780000000-extra",
            "kanban-ui-verify-4242-../etc",
            "kanban-ui-verify-",
            "kanban-ui-verify--",
            "homelab",                         # real user board
        ]
        held_fds = []
        for name in siblings:
            d = os.path.join(boards, name.replace("/", "_"))
            os.makedirs(d, exist_ok=True)
            make_gate_db(os.path.join(d, "kanban.db"))
            if name == "kanban-ui-verify-1-production":
                # an ACTIVELY LOCKED old board (the 'legit verifier ran
                # >60min' scenario the age test used to vaporise). Keep
                # the fd open for the whole test: flock is per-fd, so a
                # closed handle would silently release the lock.
                lock = os.path.join(d, "kanban.db.dispatch.lock")
                fh = open(lock, "a+b")
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                held_fds.append(fh)
        self.addCleanup(lambda: [fh.close() for fh in held_fds])
        # Guard run for our own slug.
        proc = self._run_guard(home, own)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.isdir(os.path.join(boards, own)),
                         "own exact-grammar board must be removed")
        # Every other board survives, including the locked one.
        for name in siblings:
            if name == own:
                continue
            d = os.path.join(boards, name.replace("/", "_"))
            self.assertTrue(os.path.isdir(d),
                            f"guard must never remove {name!r}")
        self.assertEqual(len(held_fds), 1, "locked sibling fixture set up")
        # Each adversarial slug, used AS the gate slug, is refused.
        for bad in ("kanban-ui-verify-1-production",
                    "kanban-ui-verify-4242",
                    "kanban-ui-verify-4242-1780000000-extra",
                    "kanban-ui-verify-../etc",
                    "kanban-ui-verify-",
                    "homelab",
                    "kanban-ui-verify-1-2; rm -rf /nonexistent-ok"):
            proc = self._run_guard(home, bad)
            self.assertEqual(proc.returncode, 0)
            self.assertIn("REFUSING", proc.stderr,
                          f"slug {bad!r} must be refused by the guard")
        # Nothing at all was deleted by the refused runs (own was
        # already removed by the legitimate guard run above).
        for name in siblings:
            if name == own:
                continue
            self.assertTrue(os.path.isdir(os.path.join(boards,
                                    name.replace("/", "_"))))

    def test_exact_grammar_accepts_script_generated_slug(self):
        # The slug the script itself constructs (kanban-ui-verify-$$-epoch)
        # must pass the guard: model bash's $$ + date for a real pid/time.
        home = tempfile.mkdtemp(prefix="kbui-home-")
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        boards = os.path.join(home, "kanban", "boards")
        os.makedirs(boards)
        script = ('set -u\nGATE_SLUG="kanban-ui-verify-$$-$(date +%s)"\n'
                  'mkdir -p "$KANBAN_UI_KANBAN_HOME/kanban/boards/$GATE_SLUG"\n'
                  + TEARDOWN_GUARD +
                  'test ! -e "$KANBAN_UI_KANBAN_HOME/kanban/boards/$GATE_SLUG"'
                  ' || exit 9\n')
        env = dict(os.environ)
        env["KANBAN_UI_KANBAN_HOME"] = home
        proc = subprocess.run(["bash", "-c", script], env=env,
                              text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("REFUSING", proc.stderr)


# Live-execution hooks: when run ON the Hermes host (the kanban_ui
# deploy target) against the REAL pinned checkout, the shipped
# injection helper is additionally executed against the actual
# b3aa561f tree, not just stub layouts. Everywhere else the class
# skips cleanly, so the suite stays portable (mgmt/CI: SKIP).
HERMES_CHECKOUT = os.environ.get(
    "KANBAN_UI_HERMES_CHECKOUT", "/home/jimmy/.hermes/hermes-agent")
HERMES_VENV_PY = os.environ.get(
    "KANBAN_UI_HERMES_PY", os.path.join(HERMES_CHECKOUT, "venv", "bin",
                                        "python"))
PINNED_COMMIT = "b3aa561faffd64f05436e429a6415d175e534ec9"


@unittest.skipUnless(
    os.path.exists(os.path.join(HERMES_CHECKOUT, "hermes_cli",
                                "kanban_db.py"))
    and os.path.exists(HERMES_VENV_PY),
    "pinned hermes-agent checkout + venv not available on this host")
class TestLivePinnedLayout(unittest.TestCase):
    """Round-6d finding 1, real-tree leg: execute the shipped
    GATE_INJECT_PY heredoc against the ACTUAL pinned b3aa561f tree
    extracted from the deployment checkout (git archive; the working
    tree sits at a NEWER HEAD that already has the split modules --
    which is exactly how the pre-round-6d live PASS misled: the venv
    is an editable install whose finder always resolves hermes_cli to
    the live checkout, so 'pinned' runs silently loaded newer code.
    -S skips .pth init and the probe asserts the resolved file paths,
    so a PASS here cannot be the live tree in disguise)."""

    def _git(self, *args):
        return subprocess.run(
            ["git", "-c", "safe.directory=" + HERMES_CHECKOUT,
             "-C", HERMES_CHECKOUT] + list(args),
            text=True, capture_output=True)

    def setUp(self):
        rc = self._git("cat-file", "-e", PINNED_COMMIT + "^{commit}")
        if rc.returncode != 0:
            self.skipTest("pinned commit not present in checkout")
        import glob
        sp = glob.glob(os.path.join(HERMES_CHECKOUT, "venv", "lib",
                                    "python*", "site-packages"))
        if not sp:
            self.skipTest("venv site-packages not found")
        self.site_packages = sp[0]
        self.pinned = tempfile.mkdtemp(prefix="kbui-pinned-")
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.pinned, ignore_errors=True))
        arch = subprocess.run(
            ["git", "-c", "safe.directory=" + HERMES_CHECKOUT,
             "-C", HERMES_CHECKOUT, "archive", PINNED_COMMIT],
            capture_output=True, check=True).stdout
        import io
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(arch)) as tf:
            tf.extractall(self.pinned)
        self.assertTrue(os.path.exists(
            os.path.join(self.pinned, "hermes_cli", "kanban_db.py")))
        self.assertFalse(os.path.exists(
            os.path.join(self.pinned, "hermes_cli",
                         "kanban_db_dispatch.py")),
            "pinned tree unexpectedly contains split modules")
        # Isolated kanban home so nothing here can touch real boards.
        self.home = tempfile.mkdtemp(prefix="kbui-live-home-")
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.home, ignore_errors=True))
        self.board = "kanban-ui-verify-%d-%d" % (
            os.getpid(), int(time.time()))
        self.bdir = os.path.join(self.home, "kanban", "boards",
                                 self.board)
        os.makedirs(self.bdir)
        self.db = os.path.join(self.bdir, "kanban.db")
        conn = sqlite3.connect(self.db)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

    def _env(self, repo_path):
        e = dict(os.environ)
        e.update({
            "HOME": "/tmp",
            "HERMES_HOME": "/tmp/kanban-ui-verify-nohome",
            "HERMES_KANBAN_HOME": self.home,
            "KANBAN_UI_HERMES_REPO": repo_path,
            "PYTHONPATH": self.site_packages,
        })
        return e

    def _run_pinned_py(self, src, args, repo_path):
        return subprocess.run([HERMES_VENV_PY, "-S"] + args, input=src,
                              text=True, capture_output=True,
                              env=self._env(repo_path))

    def test_injection_executes_and_mutates_on_real_pinned_tree(self):
        run_seed(self.db)
        before = run_fingerprint(self.db)
        # Probe first: the -S invocation must resolve hermes_cli to the
        # extracted pinned tree, and the split modules must be absent.
        probe = (
            "import os, sys\n"
            'sys.path.insert(0, os.environ["KANBAN_UI_HERMES_REPO"])\n'
            "import hermes_cli.kanban_db as m1\n"
            "print('kanban_db', m1.__file__)\n"
            "try:\n"
            "    import hermes_cli.kanban_db_dispatch as m2\n"
            "    print('split', m2.__file__)\n"
            "except ImportError:\n"
            "    print('split-absent')\n")
        p = self._run_pinned_py(probe, ["-"], self.pinned)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("kanban_db " + os.path.join(
            self.pinned, "hermes_cli", "kanban_db.py"), p.stdout)
        self.assertIn("split-absent", p.stdout)
        # The shipped helper, unmodified, on the declared revision.
        p = self._run_pinned_py(INJECT_SRC, ["-", self.board],
                                self.pinned)
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        self.assertIn("injected tick completed", p.stdout)
        self.assertNotEqual(before, run_fingerprint(self.db),
                            "real-tick injection must change the "
                            "persisted fingerprint (positive control)")

    def test_retired_split_only_import_dies_on_real_pinned_tree(self):
        retired = (
            "import os, sys\n"
            "sys.path.insert(0, os.environ[\"KANBAN_UI_HERMES_REPO\"])\n"
            "from hermes_cli import kanban_db_dispatch as kbd\n"
            'print("retired-import-survived")\n')
        p = self._run_pinned_py(retired, ["-", self.board], self.pinned)
        self.assertNotEqual(p.returncode, 0,
                            "retired split-only import survived the "
                            "pinned tree -- tree or probe is wrong")
        self.assertIn("ImportError", p.stderr)
        self.assertNotIn("retired-import-survived", p.stdout)


class TestPlaybookWiring(unittest.TestCase):
    def test_production_task_passes_gate_anchors(self):
        text = TASKS.read_text()
        for var in ("KANBAN_UI_KANBAN_HOME", "KANBAN_UI_HERMES_REPO",
                    "KANBAN_UI_HERMES_PY", "KANBAN_UI_BOARD_UID",
                    "KANBAN_UI_BOARD_GID"):
            self.assertIn(var, text)

    def test_injection_task_is_flag_gated_and_injects(self):
        text = TASKS.read_text()
        self.assertIn("KANBAN_UI_VERIFY_TEST_INJECT: \"dispatch-once\"", text)
        self.assertIn("kanban_ui_verify_fault_inject | bool", text)
        inj = text.index("Verify the non-mutation gate detects a real dispatch tick")
        when_block = text[inj:inj + 2000]
        self.assertIn("not ansible_check_mode", when_block)
        # Default-off in vars: production never reaches the injection leg.
        vars_text = VARS.read_text()
        self.assertRegex(vars_text,
                         r"kanban_ui_verify_fault_inject:\s*false")

    def test_script_hard_requires_the_new_env(self):
        for var in ("KANBAN_UI_KANBAN_HOME", "KANBAN_UI_HERMES_REPO",
                    "KANBAN_UI_HERMES_PY", "KANBAN_UI_BOARD_UID",
                    "KANBAN_UI_BOARD_GID"):
            self.assertIn(f': "${{{var}:?}}"', SCRIPT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
