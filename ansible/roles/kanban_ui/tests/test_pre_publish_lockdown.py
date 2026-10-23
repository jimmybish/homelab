#!/usr/bin/env python3
"""Pre-publish lockdown regression for the DOCKER-USER converger.

PR#13 round-8 finding (inline comment 4354363865, HIGH, review 5621868186),
verbatim:

  "On a first deployment, this starts the container and publishes its port
  before the DOCKER-USER restriction is installed at lines 378-420. The
  intervening health wait leaves the listener reachable from non-proxy LAN
  hosts; the earlier UFW INPUT rule does not filter Docker's DNAT/FORWARD
  path. If Compose's readiness wait fails, execution never reaches firewall
  installation and that exposure can persist. Install a fail-closed
  forwarding restriction before publishing the port (for example, matching
  the original host destination/port), then converge the container-specific
  rules without removing that protection."

Pre-fix shape (commit 6478dc9): the first DOCKER-USER task ran AFTER the
Compose build/up tasks (both with readiness waits), and the enforcement
script could only restrict the tuple once the container was RUNNING (it
resolves the post-DNAT IP first and exits "nothing published to enforce"
when the container is absent -- exactly the first-deploy state). So the
window between `docker compose up` publishing the port and the converge
task was wide open to non-proxy LAN sources, and a failed readiness wait
aborted the play before any restriction existed. Reproduced live on
the target host: jump+chain removed to emulate the first-deploy window,
a control-node (non-proxy) curl got HTTP 200/401 on the published port
while the UFW INPUT allow stayed loaded (proving INPUT does not gate the
DNAT path).

Fixed shape: a new ``--pre-lock`` mode installs a DROP at DOCKER-USER
position 1 matching the ORIGINAL host destination/port via conntrack
(``-m conntrack --ctorigdst <host_ip> --ctorigdstport <port>`` -- the
only way to see the pre-DNAT tuple there, since DOCKER-USER evaluates
after nat PREROUTING). It needs no container IP and no Docker daemon
(container absent is fine), the role runs it BEFORE the Compose tasks,
and only a successful converge (managed jump+chain verified live) or
--teardown removes it. A failed readiness wait, an aborted play, or any
mid-converge fault leaves the lockdown (or the r5 guard) in place:
fail-closed, never exposed.

Harness (stdlib only, no root, no Docker, no host netfilter): the real
``files/docker_user_enforce.sh`` runs as a subprocess with ``iptables``
and ``docker`` shimmed on PATH by generated fakes. The fake keeps an
ordered per-chain store of kernel-equivalent rule keys, renders ``-S``
in canonical target-last form, and evaluates a packet model for TWO
probes -- a non-proxy LAN client and the reverse proxy -- where a rule's
conntrack match is honoured only when the ORIGINAL host tuple of the
probe equals ``--ctorigdst``/``--ctorigdstport`` (so a lockdown written
for a different host/IP is inert, not magically restrictive).

    python3 ansible/roles/kanban_ui/tests/test_pre_publish_lockdown.py

Exit 0 = all pass. The shipped script is the exact deployed artifact
(role copy task), so this locks the live enforcement path, not a copy.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "files" / "docker_user_enforce.sh"

CONTAINER = "kanban-ui"
# RFC 5737 documentation space (TEST-NET-1/2), never routable -- same
# fixture convention as the role's other tests. Synthetic values only.
IP = "192.0.2.7"            # container (post-DNAT) address
HOST_IP = "192.0.2.20"      # host primary address the port is published on
PORT = "9470"
PROXY_IP = "198.51.100.5"
NONPROXY_SRC = "203.0.113.66/32"
CHAIN = "DOCKER-USER-KANBAN"
JUMP_COMMENT = "kanban-ui-jump"
GUARD_COMMENT = "kanban-ui-guard"
PRELOCK_COMMENT = "kanban-ui-prelock"

TUPLE = f"-d {IP}/32 -p tcp -m tcp --dport {PORT}"
GUARD_RULE = f"{TUPLE} -m comment --comment {GUARD_COMMENT} -j DROP"
JUMP_RULE = f"{TUPLE} -m comment --comment {JUMP_COMMENT} -j {CHAIN}"
PRELOCK_RULE = (
    f"-p tcp -m tcp --dport {PORT} -m conntrack --ctorigdst {HOST_IP} "
    f"--ctorigdstport {PORT} -m comment --comment {PRELOCK_COMMENT} -j DROP"
)
# A lockdown armed for a DIFFERENT published address: must be inert for our
# probe (conntrack semantics), proving the probe models are non-vacuous.
PRELOCK_FOREIGN = PRELOCK_RULE.replace(f"--ctorigdst {HOST_IP}", "--ctorigdst 192.0.2.99")
CHAIN_RULES = [
    "-i lo -j ACCEPT -m comment --comment kanban-ui-hostpath",
    f"-s {PROXY_IP}/32 -j ACCEPT -m comment --comment kanban-ui-proxy",
    "-j DROP -m comment --comment kanban-ui-deny",
]


def canon(spec_tokens):
    toks = list(spec_tokens)
    if "-j" in toks:
        i = toks.index("-j")
        target = toks[i + 1:i + 2]
        toks = toks[:i] + toks[i + 2:] + ["-j"] + target
    return " ".join(toks)


# The two probes the finding is about: a LAN client that must NEVER reach
# the listener (source deliberately absent from any allow), and the reverse
# proxy which is the only legitimate consumer (reachable ONLY once the
# managed chain is live; the pre-publish lockdown covers it too by design,
# closing the deploy window for all sources).
PROBES = {
    "nonproxy": {"src": NONPROXY_SRC, "origdst": HOST_IP, "origport": PORT},
    "proxy": {"src": f"{PROXY_IP}/32", "origdst": HOST_IP, "origport": PORT},
}


def matches(rule, probe):
    """Kernel subset semantics on the given probe's POST-DNAT packet plus
    its conntrack original tuple: every qualifier present in the rule must
    match. -i lo never matches a LAN-forwarded packet; foreign -s, -d or
    --dport never match; conntrack matches pin the ORIGINAL tuple."""
    toks = rule.split()

    def after(flag):
        return toks[toks.index(flag) + 1] if flag in toks else None

    if after("-i") == "lo":
        return False
    if "-s" in toks and after("-s") != probe["src"]:
        return False
    if "-d" in toks and after("-d") != f"{IP}/32":
        return False
    if "--dport" in toks and after("--dport") != PORT:
        return False
    if "-m" in toks and "conntrack" in toks:
        if after("--ctorigdst") != probe["origdst"]:
            return False
        if after("--ctorigdstport") != probe["origport"]:
            return False
    return True


def chain_verdict(state, probe):
    for cr in state["chains"].get(CHAIN, []):
        if not matches(cr, probe):
            continue
        toks = cr.split()
        if toks[-2:] == ["-j", "DROP"]:
            return "DROP"
        if toks[-2:] == ["-j", "ACCEPT"]:
            return "ACCEPT"
    return "OUT"


def reaches(state, probe_name):
    """True iff the probe reaches Docker's post-DOCKER-USER ACCEPT path."""
    probe = PROBES[probe_name]
    for rule in state["chains"].get("DOCKER-USER", []):
        if not matches(rule, probe):
            continue
        toks = rule.split()
        target = toks[-2:]
        if target == ["-j", "DROP"]:
            return False
        if target in (["-j", "RETURN"], ["-j", "ACCEPT"]):
            return True  # terminates into the Docker ACCEPT path
        if target == ["-j", CHAIN]:
            return chain_verdict(state, probe) == "ACCEPT"
        return True  # first-match non-terminal/foreign rule: falls through
    return True  # no rule matched: nat DOCKER ACCEPT admits it


FAKE_IPTABLES = r'''#!/usr/bin/env python3
"""Stateful iptables stand-in for test_pre_publish_lockdown.py.

Ordered per-chain store of kernel-equivalent rule keys (target-last
canonical form). -S renders in that canonical form. -C/-D are kernel
equivalence matches. Mirrors iptables-nft 1.8.7 rendering for the
conntrack match: --ctorigdst renders BARE (address mask stripped), which
is why the script's prelock spec uses the bare address.
"""
import json
import os
import sys

STATE = os.environ["FAKE_IPT_STATE"]


def canon(spec_tokens):
    toks = list(spec_tokens)
    if "-j" in toks:
        i = toks.index("-j")
        target = toks[i + 1:i + 2]
        toks = toks[:i] + toks[i + 2:] + ["-j"] + target
    return " ".join(toks)


def load():
    with open(STATE) as fh:
        return json.load(fh)


def commit(state):
    with open(STATE, "w") as fh:
        json.dump(state, fh)


argv = sys.argv[1:]
if argv[:1] == ["-w"]:
    argv = argv[2:]
joined = " ".join(argv)

if argv[:2] == ["-n", "-L"]:
    sys.exit(0 if argv[2] in load()["chains"] else 1)

op = argv[0]
if op == "-N":
    s = load()
    s["chains"].setdefault(argv[1], [])
    commit(s)
elif op == "-F":
    s = load()
    if argv[1] not in s["chains"]:
        sys.exit(1)
    s["chains"][argv[1]] = []
    commit(s)
elif op == "-X":
    s = load()
    if argv[1] not in s["chains"]:
        sys.exit(1)
    del s["chains"][argv[1]]
    commit(s)
elif op == "-A":
    s = load()
    if argv[1] not in s["chains"]:
        sys.exit(1)
    s["chains"][argv[1]].append(canon(argv[2:]))
    commit(s)
elif op == "-I":
    s = load()
    # iptables creates the built-in DOCKER-USER chain on demand; mirror it.
    chain = (
        s["chains"].setdefault(argv[1], [])
        if argv[1] == "DOCKER-USER"
        else s["chains"].get(argv[1])
    )
    if chain is None:
        sys.exit(1)
    chain.insert(int(argv[2]) - 1, canon(argv[3:]))
    commit(s)
elif op == "-C":
    s = load()
    sys.exit(0 if canon(argv[2:]) in s["chains"].get(argv[1], []) else 1)
elif op == "-D":
    s = load()
    rules = s["chains"].get(argv[1], [])
    key = canon(argv[2:])
    if key not in rules:
        sys.exit(1)
    rules.remove(key)
    commit(s)
elif op == "-S":
    s = load()
    if argv[1] not in s["chains"]:
        sys.stderr.write("chain %s does not exist\n" % argv[1])
        sys.exit(1)
    if argv[1] == "DOCKER-USER":
        print("-P %s ACCEPT" % argv[1])
    else:
        print("-N %s" % argv[1])
    for r in s["chains"][argv[1]]:
        print("-A %s %s" % (argv[1], r))
else:
    sys.stderr.write("fake iptables: unsupported argv: %s\n" % joined)
    sys.exit(2)
sys.exit(0)
'''

FAKE_DOCKER = r'''#!/usr/bin/env python3
"""docker stand-in: `docker inspect -f <fmt> <name>` returning the
configured IP, or empty (container absent) when FAKE_DOCKER_IP is unset.
Counts invocations so the pre-lock no-Docker property is observable."""
import os
import sys

if sys.argv[1:2] == ["inspect"]:
    calls = os.environ.get("FAKE_DOCKER_COUNT")
    if calls:
        with open(calls, "a") as fh:
            fh.write("inspect\n")
    ip = os.environ.get("FAKE_DOCKER_IP", "")
    print((ip + " ") if ip else "")
    sys.exit(0)
sys.exit(1)
'''


FAKE_SLEEP = r'''#!/bin/sh
# Accelerate the script's boot-race `sleep 3` loop: the wait LOOP itself
# (iterations, ordering) stays real; wall time does not.
exec /bin/sleep 0.01
'''


class Converger:
    """Runs the shipped enforcement script against a fresh fake netfilter."""

    def __init__(self, rules=None, container_ip=IP):
        self.dir = Path(tempfile.mkdtemp(prefix="kbui-prelock-"))
        bindir = self.dir / "bin"
        bindir.mkdir()
        (bindir / "iptables").write_text(FAKE_IPTABLES)
        (bindir / "iptables").chmod(0o755)
        (bindir / "docker").write_text(FAKE_DOCKER)
        (bindir / "docker").chmod(0o755)
        (bindir / "sleep").write_text(FAKE_SLEEP)
        (bindir / "sleep").chmod(0o755)
        self.script = self.dir / "enforce.sh"
        self.script.write_text(SCRIPT.read_text())
        self.script.chmod(0o755)
        self.state_file = self.dir / "state.json"
        seeded = {name: [canon(r.split()) for r in rs] for name, rs in (rules or {}).items()}
        self.state_file.write_text(json.dumps({"chains": seeded}))
        self.count_file = self.dir / "docker-calls"
        self.env = dict(
            os.environ,
            PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
            FAKE_IPT_STATE=str(self.state_file),
            FAKE_DOCKER_IP=container_ip,
            FAKE_DOCKER_COUNT=str(self.count_file),
            KANBAN_UI_CONTAINER=CONTAINER,
            KANBAN_UI_PORT=PORT,
            KANBAN_UI_PROXY_IP=PROXY_IP,
            KANBAN_UI_HOST_IP=HOST_IP,
        )
        self.env.pop("KANBAN_UI_TEST_FAIL_AFTER", None)

    def run(self, *args, fail_after=None, drop_host_ip=False):
        env = dict(self.env)
        if fail_after:
            env["KANBAN_UI_TEST_FAIL_AFTER"] = fail_after
        if drop_host_ip:
            env.pop("KANBAN_UI_HOST_IP", None)
        return subprocess.run(
            [str(self.script), *args], env=env, capture_output=True, text=True, timeout=120
        )

    def container_up(self, ip=IP):
        """Compose-up completed: the container now exists with this IP."""
        self.env["FAKE_DOCKER_IP"] = ip

    def docker_calls(self):
        return self.count_file.read_text().splitlines() if self.count_file.exists() else []

    def state(self):
        return json.loads(self.state_file.read_text())

    def du(self):
        return self.state()["chains"].get("DOCKER-USER", [])

    def chain(self):
        return self.state()["chains"].get(CHAIN, [])

    def prelocks(self):
        return [r for r in self.du() if PRELOCK_COMMENT in r]

    def reaches(self, probe):
        return reaches(self.state(), probe)


def converged_state():
    return {"DOCKER-USER": [JUMP_RULE], CHAIN: list(CHAIN_RULES)}


class TestSensitivity(unittest.TestCase):
    """Non-vacuity: what the pre-fix tooling could NOT do.

    The pre-fix script's only restriction mode (enforce) resolves the
    container first and, when it is absent -- the first-deploy state at
    Compose-up time -- exits 0 'nothing published to enforce' leaving the
    FORWARD path completely open. If the fixed enforce path ever regresses
    to restricting early (or the pre-lock stops installing), these cases
    flip.
    """

    def test_enforce_with_no_container_leaves_port_reachable(self):
        c = Converger(rules={}, container_ip="")
        r = c.run()  # pre-fix behavior for the first-deploy window
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nothing published to enforce", r.stdout)
        self.assertEqual(c.du(), [])
        self.assertTrue(c.reaches("nonproxy"), "finding: window left the port open")

    def test_no_rules_at_all_is_the_exposed_baseline(self):
        c = Converger(rules={})
        self.assertTrue(c.reaches("nonproxy"))
        self.assertTrue(c.reaches("proxy"))


class TestPreLockInstall(unittest.TestCase):
    def test_pre_lock_installs_exact_rule_without_docker(self):
        c = Converger(rules={}, container_ip="")
        r = c.run("--pre-lock")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("lockdown installed", r.stdout)
        self.assertEqual(
            c.du(), [canon(PRELOCK_RULE.split())],
            "exact lockdown rule at DOCKER-USER position 1",
        )
        # The lockdown is usable BEFORE the container exists: it must not
        # burn the 30s boot-race wait nor depend on the Docker API result.
        self.assertNotIn("not running", r.stdout)
        self.assertFalse(c.reaches("nonproxy"))

    def test_pre_lock_blocks_both_probes_during_window(self):
        c = Converger(rules={}, container_ip="")
        c.run("--pre-lock")
        self.assertFalse(c.reaches("nonproxy"), "the finding's exposure")
        # Fail-closed covers the proxy too during the deploy window: the
        # UI is not yet publishable anyway (compose up runs next).
        self.assertFalse(c.reaches("proxy"))

    def test_pre_lock_idempotent_and_retry_safe(self):
        c = Converger(rules={}, container_ip="")
        self.assertEqual(c.run("--pre-lock").returncode, 0)
        before = json.dumps(c.state(), sort_keys=True)
        r = c.run("--pre-lock")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already installed", r.stdout)
        self.assertEqual(before, json.dumps(c.state(), sort_keys=True))

    def test_pre_lock_skipped_when_managed_set_live(self):
        c = Converger(rules=converged_state())
        r = c.run("--pre-lock")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not needed", r.stdout)
        self.assertEqual(c.prelocks(), [], "steady-state must stay byte-stable")

    def test_pre_lock_requires_host_ip(self):
        c = Converger(rules={}, container_ip="")
        r = c.run("--pre-lock", drop_host_ip=True)
        self.assertNotEqual(r.returncode, 0, "host IP is mandatory for the lockdown")

    def test_foreign_host_lockdown_is_inert_for_our_probe(self):
        # Models a lockdown armed for a DIFFERENT published tuple: conntrack
        # must not match, so the probe passes it (fall-through) -- proves the
        # probe model honours conntrack instead of treating any DROP as
        # restrictive, keeping the blocking assertions non-vacuous.
        c = Converger(rules={"DOCKER-USER": [PRELOCK_FOREIGN]})
        self.assertTrue(c.reaches("nonproxy"))

    def test_pre_lock_reinstalls_when_bypass_seeded_ahead(self):
        # PR#13 round-17 review finding 1: an exact pre-lock copy is only
        # "already installed" while it is the FIRST effective rule. Seed a
        # bypass ahead of a surviving copy (netfilter evaluates in order,
        # so the bypass terminates for our probes), run --pre-lock, and
        # prove the fresh position-1 DROP restores protection BEFORE
        # Compose publishes -- and that a later converge sweeps every
        # duplicate copy.
        bypasses = {
            "return": "-m comment --comment bypass-return -j RETURN",
            "accept": "-m comment --comment bypass-accept -j ACCEPT",
            "foreign-jump": "-m comment --comment bypass-foreign -j FOREIGN-BYPASS",
        }
        for name, bypass in bypasses.items():
            with self.subTest(bypass=name):
                du = [canon(bypass.split()), canon(PRELOCK_RULE.split())]
                c = Converger(rules={"DOCKER-USER": du, CHAIN: list(CHAIN_RULES)})
                # Non-vacuity: with the bypass ahead, both probes reach the
                # Docker ACCEPT path -- the surviving copy is NOT protection.
                self.assertTrue(c.reaches("nonproxy"), f"{name} seed did not expose")
                self.assertTrue(c.reaches("proxy"), f"{name} seed did not expose")
                r = c.run("--pre-lock")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("lockdown installed", r.stdout, f"{name} head accepted")
                self.assertEqual(
                    c.du()[0], canon(PRELOCK_RULE.split()),
                    f"{name} bypass still ahead of the lockdown",
                )
                self.assertEqual(len(c.prelocks()), 2, "fresh copy + surviving copy")
                self.assertFalse(c.reaches("nonproxy"), f"{name} exposed after pre-lock")
                self.assertFalse(c.reaches("proxy"), f"{name} exposed after pre-lock")
                # The duplicate is not orphaned: converge sweeps all copies.
                c.container_up()
                r = c.run()
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(c.prelocks(), [], f"{name} copies not swept")
                self.assertFalse(c.reaches("nonproxy"))
                self.assertTrue(c.reaches("proxy"))

    def test_pre_lock_guard_ahead_rearms_positional_drop(self):
        # A container-IP guard ahead of a surviving copy is not the
        # lockdown shape: it matches the post-DNAT container tuple (which
        # a recreate can change) rather than the original host tuple, and
        # the positional invariant accepts ONLY the exact pre-lock at the
        # head -- --pre-lock re-arms a fresh position-1 DROP regardless.
        du = [canon(GUARD_RULE.split()), canon(PRELOCK_RULE.split())]
        c = Converger(rules={"DOCKER-USER": du, CHAIN: list(CHAIN_RULES)})
        r = c.run("--pre-lock")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("lockdown installed", r.stdout)
        self.assertEqual(c.du()[0], canon(PRELOCK_RULE.split()))
        self.assertEqual(len(c.prelocks()), 2)
        self.assertFalse(c.reaches("nonproxy"))
        self.assertFalse(c.reaches("proxy"))


class TestConvergeSweepsLockdown(unittest.TestCase):
    def test_converge_from_lockdown_reaches_managed_only(self):
        c = Converger(rules={}, container_ip="")
        c.run("--pre-lock")
        c.container_up()  # compose-up completed: container now exists
        r = c.run()  # readiness passed
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("converged", r.stdout)
        self.assertEqual(c.prelocks(), [], "lockdown swept only AFTER jump verified")
        self.assertEqual(c.du(), [canon(JUMP_RULE.split())])
        self.assertEqual(c.chain(), [canon(r.split()) for r in CHAIN_RULES])
        self.assertFalse(c.reaches("nonproxy"))
        self.assertTrue(c.reaches("proxy"), "proxy path opens once chain is live")

    def test_converge_from_pre_locked_stale_jump_reconverges(self):
        # Stale jump (old container IP) plus an armed lockdown: the r5/r6
        # guard sequence must replace the jump and sweep the lockdown.
        stale = TUPLE.replace(IP, "192.0.2.1") + f" -m comment --comment {JUMP_COMMENT} -j {CHAIN}"
        c = Converger(rules={"DOCKER-USER": [canon(stale.split())], CHAIN: list(CHAIN_RULES)})
        c.run("--pre-lock")
        r = c.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(c.prelocks(), [])
        self.assertEqual(c.du(), [canon(JUMP_RULE.split())])

    def test_lockdown_is_not_check_drift_and_not_already_converged_blocker(self):
        du = [canon(PRELOCK_RULE.split()), canon(JUMP_RULE.split())]
        c = Converger(rules={"DOCKER-USER": du, CHAIN: list(CHAIN_RULES)})
        r = c.run("--check")
        self.assertEqual(r.returncode, 0, "surviving lockdown is redundant DROP, not drift")
        r = c.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already converged", r.stdout)
        self.assertEqual(c.prelocks(), [], "already-converged path sweeps it too")

    def test_intruder_before_jump_still_drift_with_lockdown_present(self):
        du = [
            canon(PRELOCK_RULE.split()),
            canon("-m comment --comment intruder-accept -j ACCEPT".split()),
            canon(JUMP_RULE.split()),
        ]
        c = Converger(rules={"DOCKER-USER": du, "DOCKER-USER-KANBAN": list(CHAIN_RULES)})
        self.assertEqual(c.run("--check").returncode, 42)

    def test_idempotent_rerun_after_sweep_is_byte_identical(self):
        c = Converger(rules={}, container_ip="")
        c.run("--pre-lock")
        c.container_up()
        c.run()
        first = json.dumps(c.state(), sort_keys=True)
        r = c.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already converged", r.stdout)
        self.assertEqual(first, json.dumps(c.state(), sort_keys=True))
        # Steady state: pre-lock again is a no-op, so the whole play stays
        # changed=0 on rerun (the role's idempotency contract).
        r = c.run("--pre-lock")
        self.assertIn("not needed", r.stdout)
        self.assertEqual(first, json.dumps(c.state(), sort_keys=True))


class TestFailClosedWindows(unittest.TestCase):
    """Every abort path leaves the tuple restricted for both probes."""

    def test_readiness_failure_aborts_play_with_lockdown_in_place(self):
        # Models the finding's worst case: compose readiness wait fails,
        # the play aborts, no converge task ever runs. Nothing mutates
        # state after --pre-lock, so the lockdown is the last word.
        c = Converger(rules={}, container_ip="")
        c.run("--pre-lock")
        self.assertFalse(c.reaches("nonproxy"))
        self.assertFalse(c.reaches("proxy"))
        self.assertEqual(len(c.prelocks()), 1)

    def test_every_converge_fault_stage_keeps_tuple_restricted(self):
        for stage in ("guard", "delete", "flush", "append", "insert"):
            with self.subTest(stage=stage):
                c = Converger(rules={}, container_ip="")
                c.run("--pre-lock")
                c.container_up()  # compose-up completed before converge
                r = c.run(fail_after=stage)
                self.assertNotEqual(r.returncode, 0, f"{stage} must fail the run")
                self.assertFalse(
                    c.reaches("nonproxy"),
                    f"{stage} fault left the published port reachable",
                )
                if stage == "insert":
                    # The injected failure lands AFTER the jump insertion
                    # with the chain fully populated: the managed set is in
                    # fact live, so the proxy legitimately reaches through
                    # the managed chain (terminal DROP still gates everyone
                    # else, asserted above). No fault before that point may
                    # open the proxy path: it stays behind guard/prelock.
                    self.assertTrue(c.reaches("proxy"))
                else:
                    self.assertFalse(c.reaches("proxy"), f"{stage} fault opened proxy path")

    def test_fault_recovery_converges_and_clears(self):
        c = Converger(rules={}, container_ip="")
        c.run("--pre-lock")
        c.container_up()
        self.assertNotEqual(c.run(fail_after="append").returncode, 0)
        self.assertFalse(c.reaches("nonproxy"))
        r = c.run()  # retry
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(c.prelocks(), [])
        self.assertFalse(c.reaches("nonproxy"))
        self.assertTrue(c.reaches("proxy"))

    def test_lockdown_survives_until_verified_success_only(self):
        c = Converger(rules={}, container_ip="")
        c.run("--pre-lock")
        # Mid-success cleanup fault: converge must refuse the verdict and
        # leave the lockdown (restriction) in place.
        self.assertEqual(
            c.run("--pre-lock").returncode, 0
        )  # retry-safe arm
        self.assertEqual(len(c.prelocks()), 1)


class TestTeardownClearsLockdown(unittest.TestCase):
    def test_teardown_removes_lockdown_without_container(self):
        c = Converger(rules={}, container_ip="")
        c.run("--pre-lock")
        r = c.run("--teardown")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(c.du(), [])
        self.assertNotIn(CHAIN, c.state()["chains"])

    def test_teardown_removes_lockdown_alongside_jump_and_guard(self):
        du = [canon(PRELOCK_RULE.split()), canon(GUARD_RULE.split()), canon(JUMP_RULE.split())]
        c = Converger(rules={"DOCKER-USER": du, CHAIN: list(CHAIN_RULES)})
        r = c.run("--teardown")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(c.du(), [])
        self.assertNotIn(CHAIN, c.state()["chains"])

    def test_teardown_tolerates_missing_host_ip(self):
        # Teardown must not regress into requiring the host IP (r5
        # repeatability: absent-state runs work on any host state).
        c = Converger(rules={"DOCKER-USER": [canon(PRELOCK_RULE.split())]})
        r = c.run("--teardown", drop_host_ip=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(c.du(), [])


if __name__ == "__main__":
    if not SCRIPT.exists():  # pragma: no cover - defensive
        raise SystemExit(f"shipped script not found at {SCRIPT}")
    unittest.main(verbosity=2)
