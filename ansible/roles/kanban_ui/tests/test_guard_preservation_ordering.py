#!/usr/bin/env python3
"""Guard-preservation ordering regression for the fail-closed converger.

PR#13 round-6 finding (inline comment 4331497704, HIGH, review 5593170781),
verbatim:

  "A failed run after jump deletion can leave the temporary DROP guard as
  the only forwarding restriction. On retry, this loop deletes that guard
  before inserting its replacement, briefly reopening the published port to
  non-proxy traffic. If insertion fails, the script exits with neither guard
  nor jump and access remains open. Keep surviving guards until the
  replacement jump and chain verify successfully; the final cleanup already
  removes all guard copies."

Pre-fix shape (commit f745099, lines 201-207): the reconvergence block
deleted every surviving ``kanban-ui-guard`` DROP FIRST and only then inserted
its replacement. Two exposures: (a) if that single ``-I`` failed the script
exited 1 with NEITHER jump nor guard on the published tuple, and (b) even on
the success path the delete->insert gap briefly left the tuple uncovered.

Fixed shape: surviving guards are KEPT while the fresh guard is inserted
(stacked DROPs on an equal matcher are harmless), and every guard copy is
removed only by the comment-matched cleanup AFTER ``all_rules_live`` verifies
the replacement jump + chain -- the same sweep ``--teardown`` already does.

Harness (stdlib only, no root, no Docker, no host netfilter): the real
``files/docker_user_enforce.sh`` runs as a subprocess with ``iptables`` and
``docker`` shimmed on PATH by generated fakes. The fake keeps an ordered,
per-chain rule store in JSON, honours the script's exact argv shapes
(`-w 30`, ``-I chain pos spec``, ``-S``, ``-C``, ``-D`` ...), can fail any
operation by substring match, and appends a snapshot (op, guard count, jump
count, tuple-restricted) after EVERY mutation. Because kernel state only
changes at those operations, the snapshot stream covers every instant the
port could be exposed -- the same property the live fault matrix re-proves
against real netfilter.

    python3 ansible/roles/kanban_ui/tests/test_guard_preservation_ordering.py

Exit 0 = all pass. Runs anywhere the role's other stdlib tests run (control
node); the shipped script is the exact deployed artifact (role copy task),
so this locks the live enforcement path, not a copy of it.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "files" / "docker_user_enforce.sh"

CONTAINER = "kanban-ui"
# RFC 5737 documentation space (TEST-NET-1/2), never routable -- same
# fixture convention as the role's other tests (e.g. test_log_redaction.py
# PEER = "192.0.2.7"). Synthetic values only; the harness never routes them.
IP = "192.0.2.7"
HOST_IP = "192.0.2.20"
PORT = "9470"
PROXY_IP = "198.51.100.5"
CHAIN = "DOCKER-USER-KANBAN"
JUMP_COMMENT = "kanban-ui-jump"
GUARD_COMMENT = "kanban-ui-guard"

# PR#13 round-18 finding 4373043501: the jump/guard matcher is pinned to
# the ORIGINAL host tuple via conntrack, independent of the container IP.
GUARD_RULE = (
    f"-p tcp -m tcp --dport {PORT} -m conntrack --ctdir ORIGINAL --ctorigdst {HOST_IP} --ctorigdstport {PORT}"
    f" -m comment --comment {GUARD_COMMENT} -j DROP"
)
JUMP_RULE = (
    f"-p tcp -m tcp --dport {PORT} -m conntrack --ctdir ORIGINAL --ctorigdst {HOST_IP} --ctorigdstport {PORT}"
    f" -m comment --comment {JUMP_COMMENT} -j {CHAIN}"
)
CHAIN_RULES = [
    "-i lo -j ACCEPT -m comment --comment kanban-ui-hostpath",
    f"-s {PROXY_IP}/32 -j ACCEPT -m comment --comment kanban-ui-proxy",
    "-j DROP -m comment --comment kanban-ui-deny",
]

# The pre-fix (f745099) guard-replacement loop, reconstructed verbatim and
# spliced back in for the SENSITIVITY case: proves the harness reproduces the
# finding instead of passing vacuously.
PREFIX_ANCHOR = "ipt -I DOCKER-USER 1 $(guard_spec) || exit 1"
PREFIX_LOOP = (
    "while read -r r; do\n"
    "  [ -n \"$r\" ] || continue\n"
    "  # shellcheck disable=SC2086  # rule spec must be separate argv words\n"
    "  ipt -D DOCKER-USER ${r#-A DOCKER-USER } || exit 1\n"
    f'done < <(ipt -S DOCKER-USER | grep -- "--comment ${{GUARD_COMMENT}} " || true)\n'
)

FAKE_IPTABLES = r'''#!/usr/bin/env python3
"""Stateful iptables stand-in for test_guard_preservation_ordering.py."""
import json
import os
import sys

STATE = os.environ["FAKE_IPT_STATE"]
LOG = os.environ["FAKE_IPT_LOG"]
FAIL = [p for p in os.environ.get("FAKE_IPT_FAIL", "").split("&&") if p]
TUPLE_D = "-d " + os.environ["FAKE_IPT_IP"] + "/32"
TUPLE_P = "--dport " + os.environ["FAKE_IPT_PORT"]
# PR#13 round-18 finding 4373043501: managed jump/guard now pin the
# ORIGINAL host tuple via conntrack instead of the post-DNAT container -d.
CT_D = "--ctorigdst " + os.environ["FAKE_IPT_HOST"]
CHAIN = os.environ["FAKE_IPT_CHAIN"]
GUARD = os.environ["FAKE_IPT_GUARD"]
JUMP = os.environ["FAKE_IPT_JUMP"]


def load():
    with open(STATE) as fh:
        return json.load(fh)


def is_guard(rule):
    return GUARD in rule and "-j DROP" in rule and CT_D in rule and TUPLE_P in rule


def is_jump(rule):
    return (
        rule.endswith("-j " + CHAIN)
        and JUMP in rule
        and CT_D in rule
        and TUPLE_P in rule
    )


def restricted(state):
    """True iff the published tuple cannot reach Docker's ACCEPT path."""
    du = state["chains"].get("DOCKER-USER", [])
    if any(is_guard(r) for r in du):
        return True
    if any(is_jump(r) for r in du):
        ch = state["chains"].get(CHAIN, [])
        # A jump into a chain lacking the terminal deny FAILS OPEN: count the
        # chain as restricting only when its last rule is the terminal DROP
        # (rule text ends with the deny comment, iptables -S order).
        if ch and ch[-1].endswith("-m comment --comment kanban-ui-deny"):
            return True
    return False


def snapshot(op, state):
    du = state["chains"].get("DOCKER-USER", [])
    with open(LOG, "a") as fh:
        fh.write(json.dumps({
            "op": op,
            "guards": sum(1 for r in du if is_guard(r)),
            "jumps": sum(1 for r in du if is_jump(r)),
            "restricted": restricted(state),
        }) + "\n")


def commit(op, state):
    with open(STATE, "w") as fh:
        json.dump(state, fh)
    snapshot(op, state)


argv = sys.argv[1:]
if argv[:1] == ["-w"]:
    argv = argv[2:]
joined = " ".join(argv)

if FAIL and all(p in joined for p in FAIL):
    sys.exit(1)  # emulate a genuine iptables/xtables failure, state untouched

if argv[:2] == ["-n", "-L"]:
    sys.exit(0 if argv[2] in load()["chains"] else 1)

op = argv[0]
if op == "-N":
    s = load()
    s["chains"].setdefault(argv[1], [])
    commit(joined, s)
elif op == "-F":
    s = load()
    if argv[1] not in s["chains"]:
        sys.exit(1)
    s["chains"][argv[1]] = []
    commit(joined, s)
elif op == "-X":
    s = load()
    if argv[1] not in s["chains"]:
        sys.exit(1)
    del s["chains"][argv[1]]
    commit(joined, s)
elif op == "-A":
    s = load()
    if argv[1] not in s["chains"]:
        sys.exit(1)
    s["chains"][argv[1]].append(" ".join(argv[2:]))
    commit(joined, s)
elif op == "-I":
    s = load()
    # iptables creates the built-in DOCKER-USER chain on demand; mirror that.
    chain = s["chains"].setdefault(argv[1], []) if argv[1] == "DOCKER-USER" else s["chains"].get(argv[1])
    if chain is None:
        sys.exit(1)
    chain.insert(int(argv[2]) - 1, " ".join(argv[3:]))
    commit(joined, s)
elif op == "-C":
    s = load()
    sys.exit(0 if " ".join(argv[2:]) in s["chains"].get(argv[1], []) else 1)
elif op == "-D":
    s = load()
    rules = s["chains"].get(argv[1], [])
    spec = " ".join(argv[2:])
    if spec not in rules:
        sys.exit(1)
    rules.remove(spec)
    commit(joined, s)
elif op == "-S":
    s = load()
    if argv[1] not in s["chains"]:
        sys.stderr.write("chain %s does not exist\n" % argv[1])
        sys.exit(1)
    for r in s["chains"][argv[1]]:
        print("-A %s %s" % (argv[1], r))
else:
    sys.stderr.write("fake iptables: unsupported argv: %s\n" % joined)
    sys.exit(2)
sys.exit(0)
'''

FAKE_DOCKER = r'''#!/usr/bin/env python3
"""docker stand-in: only `docker inspect -f <fmt> <name>` is used."""
import os
import sys

if sys.argv[1:2] == ["inspect"]:
    ip = os.environ.get("FAKE_DOCKER_IP", "")
    print((ip + " ") if ip else "")
    sys.exit(0)
sys.exit(1)
'''


def prefix_variant(src: str) -> str:
    """Splice the pre-fix delete-before-insert loop back in (sensitivity)."""
    assert PREFIX_ANCHOR in src, "guard insertion anchor moved; update the harness"
    out = src.replace(PREFIX_ANCHOR, PREFIX_LOOP + PREFIX_ANCHOR, 1)
    assert out != src
    return out


class Converger:
    """Runs a docker_user_enforce.sh variant against a fresh fake netfilter."""

    def __init__(self, src: str, rules: dict | None = None, fail: str = ""):
        self.dir = Path(tempfile.mkdtemp(prefix="kbui-guard-ord-"))
        bindir = self.dir / "bin"
        bindir.mkdir()
        (bindir / "iptables").write_text("#!/usr/bin/env python3\n" + FAKE_IPTABLES[len("#!/usr/bin/env python3\n"):])
        (bindir / "iptables").chmod(0o755)
        (bindir / "docker").write_text(FAKE_DOCKER)
        (bindir / "docker").chmod(0o755)
        self.script = self.dir / "enforce.sh"
        self.script.write_text(src)
        self.script.chmod(0o755)
        self.state_file = self.dir / "state.json"
        self.state_file.write_text(json.dumps({"chains": rules if rules is not None else {}}))
        self.log_file = self.dir / "log.jsonl"
        self.env = dict(
            os.environ,
            PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
            FAKE_IPT_STATE=str(self.state_file),
            FAKE_IPT_LOG=str(self.log_file),
            FAKE_IPT_FAIL=fail,
            FAKE_IPT_IP=IP,
            FAKE_IPT_PORT=PORT,
            FAKE_IPT_CHAIN=CHAIN,
            FAKE_IPT_GUARD=GUARD_COMMENT,
            FAKE_IPT_JUMP=JUMP_COMMENT,
            FAKE_IPT_HOST=HOST_IP,
            FAKE_DOCKER_IP=IP,
            KANBAN_UI_CONTAINER=CONTAINER,
            KANBAN_UI_PORT=PORT,
            KANBAN_UI_PROXY_IP=PROXY_IP,
            KANBAN_UI_HOST_IP=HOST_IP,
        )
        self.env.pop("KANBAN_UI_TEST_FAIL_AFTER", None)

    def run(self, *args, fail_after: str | None = None):
        env = dict(self.env)
        if fail_after:
            env["KANBAN_UI_TEST_FAIL_AFTER"] = fail_after
        return subprocess.run(
            [str(self.script), *args], env=env, capture_output=True, text=True, timeout=120
        )

    def state(self) -> dict:
        return json.loads(self.state_file.read_text())

    def du(self) -> list:
        return self.state()["chains"].get("DOCKER-USER", [])

    def chain(self) -> list:
        return self.state()["chains"].get(CHAIN, [])

    def guards(self) -> list:
        return [r for r in self.du() if GUARD_COMMENT in r and "-j DROP" in r]

    def jumps(self) -> list:
        return [r for r in self.du() if r.endswith(f"-j {CHAIN}")]

    def log(self) -> list:
        if not self.log_file.exists():
            return []
        return [json.loads(line) for line in self.log_file.read_text().splitlines() if line]


def converged_rules() -> dict:
    """Kernel state as left by a successful converge, minus the transient guard."""
    return {"DOCKER-USER": [JUMP_RULE], CHAIN: list(CHAIN_RULES)}


def failed_run_state() -> dict:
    """State after a mid-script failure: guard live, jump gone, chain built."""
    return {"DOCKER-USER": [GUARD_RULE], CHAIN: list(CHAIN_RULES)}


SRC = SCRIPT.read_text()


class TestSensitivity(unittest.TestCase):
    """The harness must reproduce the finding on the pre-fix script."""

    def test_prefix_script_loses_guard_and_jump_on_failed_insert(self):
        c = Converger(prefix_variant(SRC), failed_run_state(),
                      fail=f"-I DOCKER-USER 1&&{GUARD_COMMENT}")
        r = c.run()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(c.jumps(), [], "retry must have deleted the (already absent) jump")
        self.assertEqual(c.guards(), [], "pre-fix deleted the surviving guard before re-inserting")
        self.assertFalse(
            any(e["restricted"] for e in c.log()),
            "pre-fix retry must leave the published tuple UNRESTRICTED end to end",
        )

    def test_prefix_script_exposes_transient_window_even_when_insert_succeeds(self):
        c = Converger(prefix_variant(SRC), failed_run_state())
        r = c.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(
            any(not e["restricted"] for e in c.log()),
            "pre-fix delete->insert gap must be observable as an unrestricted snapshot",
        )


class TestGuardPreservation(unittest.TestCase):
    """The shipped script keeps guards until jump+chain verify."""

    def test_failed_guard_insert_keeps_surviving_guard(self):
        c = Converger(SRC, failed_run_state(), fail=f"-I DOCKER-USER 1&&{GUARD_COMMENT}")
        r = c.run()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertGreaterEqual(len(c.guards()), 1, "guard must survive the failed replacement")
        for e in c.log():
            self.assertTrue(e["restricted"], f"tuple exposed at op: {e}")

    def test_never_unrestricted_on_success_from_failed_run_state(self):
        c = Converger(SRC, failed_run_state())
        r = c.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        for e in c.log():
            self.assertTrue(e["restricted"], f"tuple exposed at op: {e}")
        self.assertEqual(len(c.jumps()), 1)
        self.assertEqual(c.guards(), [], "final cleanup must remove all guard copies")
        self.assertEqual(c.chain(), list(CHAIN_RULES), "-A order guarantees terminal DROP last")

    def test_guard_survives_until_jump_inserted(self):
        """Ordering lock: at the instant the jump is inserted, a guard is live."""
        c = Converger(SRC, failed_run_state())
        self.assertEqual(c.run().returncode, 0)
        jump_ops = [e for e in c.log() if e["op"].startswith("-I DOCKER-USER 1") and JUMP_COMMENT in e["op"]]
        self.assertEqual(len(jump_ops), 1, c.log())
        self.assertGreaterEqual(jump_ops[0]["guards"], 1,
                                "guard must still be present when the replacement jump lands")
        guard_ops = [i for i, e in enumerate(c.log()) if e["op"].startswith("-I DOCKER-USER 1") and GUARD_COMMENT in e["op"]]
        self.assertEqual(len(guard_ops), 1)
        # the guard insert happens BEFORE the jump insert (fail-closed order)
        self.assertLess(guard_ops[0], c.log().index(jump_ops[0]))

    def test_every_fault_stage_leaves_a_live_guard(self):
        # Seeds must force the FULL reconvergence path: from a fully
        # converged state the script correctly short-circuits ("already
        # converged", exit 0) before any fault stage is reachable.
        drift = {"DOCKER-USER": [JUMP_RULE], CHAIN: [CHAIN_RULES[0]]}  # half-built chain
        for stage in ("guard", "delete", "flush", "append", "insert"):
            for seed, label in ((drift, "drift-start"), (failed_run_state(), "stale-guard-start")):
                with self.subTest(stage=stage, seed=label):
                    c = Converger(SRC, dict((k, list(v)) for k, v in seed.items()))
                    r = c.run(fail_after=stage)
                    self.assertEqual(r.returncode, 1, f"{stage}/{label}: {r.stdout}{r.stderr}")
                    self.assertGreaterEqual(len(c.guards()), 1,
                                            f"{stage}/{label}: fail-closed requires a live guard")
                    for e in c.log():
                        self.assertTrue(e["restricted"], f"{stage}/{label} exposed at {e}")

    def test_retry_after_fault_removal_converges(self):
        c = Converger(SRC, failed_run_state(), fail=f"-I DOCKER-USER 1&&{GUARD_COMMENT}")
        self.assertEqual(c.run().returncode, 1)
        self.assertEqual(len(c.guards()), 1)
        c.env["FAKE_IPT_FAIL"] = ""  # fault removed
        r = c.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(c.jumps()), 1)
        self.assertEqual(c.guards(), [])
        self.assertEqual(c.chain(), list(CHAIN_RULES))
        for e in c.log():
            self.assertTrue(e["restricted"], f"tuple exposed at op: {e}")

    def test_stacked_guards_all_removed_on_success(self):
        state = {"DOCKER-USER": [GUARD_RULE, GUARD_RULE], CHAIN: list(CHAIN_RULES)}
        c = Converger(SRC, state)
        self.assertEqual(c.run().returncode, 0)
        self.assertEqual(c.guards(), [], "final cleanup must sweep EVERY guard copy")

    def test_idempotent_rerun_is_byte_identical(self):
        c = Converger(SRC, {})
        r1 = c.run()
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertIn("converged", r1.stdout)
        dump1 = json.dumps(c.state(), sort_keys=True)
        r2 = c.run()
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("already converged", r2.stdout)
        self.assertEqual(dump1, json.dumps(c.state(), sort_keys=True),
                         "rerun must not touch kernel state")
        # ...and a third run from a guard-stacked but converged state still
        # returns to the identical byte state (guard sweep only).
        c.state_file.write_text(json.dumps({"chains": {
            "DOCKER-USER": [JUMP_RULE, GUARD_RULE], CHAIN: list(CHAIN_RULES)}}))
        r3 = c.run()
        self.assertEqual(r3.returncode, 0, r3.stderr)
        self.assertEqual(dump1, json.dumps(c.state(), sort_keys=True))

    def test_teardown_removes_jump_guard_and_chain(self):
        state = {"DOCKER-USER": [JUMP_RULE, GUARD_RULE, GUARD_RULE], CHAIN: list(CHAIN_RULES)}
        c = Converger(SRC, state)
        r = c.run("--teardown")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(c.du(), [])
        self.assertNotIn(CHAIN, c.state()["chains"])

    def test_check_mode_ignores_transient_guard(self):
        c = Converger(SRC, converged_rules())
        self.assertEqual(c.run("--check").returncode, 0)
        c.state_file.write_text(json.dumps({"chains": {
            "DOCKER-USER": [GUARD_RULE], CHAIN: list(CHAIN_RULES)}}))
        self.assertEqual(c.run("--check").returncode, 42, "missing jump must read as drift")


if __name__ == "__main__":
    if not SCRIPT.exists():  # pragma: no cover - defensive
        sys.exit(f"missing script under test: {SCRIPT}")
    unittest.main(verbosity=2)
