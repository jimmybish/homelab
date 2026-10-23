#!/usr/bin/env python3
"""Managed-rule ORDERING regression for the DOCKER-USER converger.

PR#13 round-8 finding (inline comment 4354363842, HIGH, review 5621868186),
verbatim:

  "iptables -C checks whether a rule exists, not where it runs. If a RETURN
  or ACCEPT precedes the managed jump, these checks still pass, so --check
  and the already-converged path report success while non-proxy traffic
  bypasses the restriction. Extra or reordered rules in the private chain
  can likewise change its behavior without failing these checks. Verify that
  the managed jump is first and that the private chain contains exactly the
  intended ordered rules; otherwise reconverge under the temporary DROP
  guard."

Pre-fix shape (commit 1e043d2, all_rules_live): verification used only
``iptables -C`` membership tests for the managed jump and the three chain
rules. A RETURN (or ACCEPT, or any foreign rule) sitting BEFORE the managed
jump in DOCKER-USER is invisible to ``-C``, so ``--check`` and the
already-converged early-exit both reported success while evaluated-first
rules let non-proxy traffic bypass the restriction. Likewise a chain with an
extra ACCEPT or a reordered rule passed every ``-C``.

Fixed shape (managed_state_live): the check phase compares the live
``iptables -S`` dump against the EXPECTED ORDERED ruleset -- the managed
jump must be the FIRST DOCKER-USER rule once exact transient-guard copies
are excluded, and the private chain must contain exactly the RULES entries
in order, no extras, no reorder. Matching is per-rule canonicalised by
BUNDLING each known option with its operand (option=operand) and sorting
the pairs -- never bare tokens (PR#13 round-15 review finding, blocking:
a bare token sort let ``--comment DROP -j kanban-ui-guard`` collide with
the real guard ``--comment kanban-ui-guard -j DROP``, so a bypass rule
could masquerade as the guard we skip or as the managed jump). Pair
bundling still normalises ``iptables -S`` rendering token order (verified
live on iptables-nft 1.8.7: rules installed as ``-j ACCEPT -m comment
--comment X`` render as ``-m comment --comment X -j ACCEPT``). Any
mismatch fails --check (42) and drives reconvergence under the temporary
DROP guard, reusing the untouched r5/r6 fail-closed sequence.

Harness (stdlib only, no root, no Docker, no host netfilter): the real
``files/docker_user_enforce.sh`` runs as a subprocess with ``iptables`` and
``docker`` shimmed on PATH by generated fakes. The fake keeps an ordered
per-chain store of kernel-equivalent rule keys, renders ``-S`` in the
canonical target-last form (deliberately DIFFERENT token order from the
inserted specs, so the script must normalise), implements ``-C``/``-D`` as
kernel-equivalent matches (token order within a rule irrelevant, like the
real kernel), and adds an evaluation model for the non-proxy probe so
"bypass" is an observable property, not a comment.

    python3 ansible/roles/kanban_ui/tests/test_firewall_rule_ordering.py

Exit 0 = all pass. The shipped script is the exact deployed artifact (role
copy task), so this locks the live enforcement path, not a copy of it.
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
IP = "192.0.2.7"
HOST_IP = "192.0.2.20"
PORT = "9470"
PROXY_IP = "198.51.100.5"
PROBE_SRC = "203.0.113.66/32"
CHAIN = "DOCKER-USER-KANBAN"
JUMP_COMMENT = "kanban-ui-jump"
GUARD_COMMENT = "kanban-ui-guard"

TUPLE = f"-d {IP}/32 -p tcp -m tcp --dport {PORT}"
GUARD_RULE = f"{TUPLE} -m comment --comment {GUARD_COMMENT} -j DROP"
JUMP_RULE = f"{TUPLE} -m comment --comment {JUMP_COMMENT} -j {CHAIN}"
CHAIN_RULES = [
    "-i lo -j ACCEPT -m comment --comment kanban-ui-hostpath",
    f"-s {PROXY_IP}/32 -j ACCEPT -m comment --comment kanban-ui-proxy",
    "-j DROP -m comment --comment kanban-ui-deny",
]
# The finding's injections: a RETURN matching everything, BEFORE the jump.
RETURN_RULE = "-m comment --comment intruder-return -j RETURN"
# Unqualified ACCEPT (no source pin): an administrator wide-open rule.
ACCEPT_RULE = "-m comment --comment intruder-accept -j ACCEPT"
FOREIGN_JUMP = "-s 198.51.100.77/32 -m comment --comment foreign-jump -j DOCKER-USER-OTHER"
# Extra chain rule that DOES match the non-proxy probe (unqualified ACCEPT
# inserted before the terminal DROP -- a probe-matching bypass, exactly the
# "extra rules in the private chain" case from the finding).
EXTRA_CHAIN_ACCEPT = "-p tcp -m tcp --dport 9470 -m comment --comment intruder-extra -j ACCEPT"

# --- Round-15 blocking finding: comment <-> jump-target operand swaps -----
# Each SWAPPED_* rule swaps the VALUES bound to --comment and -j relative to
# a managed rule, and the foreign chain it jumps to is registered so the
# rule is kernel-valid. Under a bare-token (multiset) canonicaliser every
# swap has the SAME key as its managed twin, so the verifier is blind to it.
FK_TARGET_CHAIN_RULE = "-m comment --comment foreign-ok -j ACCEPT"
# Multiset twin of the transient guard (-j DROP / --comment kanban-ui-guard).
SWAPPED_GUARD = f"{TUPLE} -m comment --comment DROP -j {GUARD_COMMENT}"
# Multiset twin of the managed jump (-j CHAIN / --comment kanban-ui-jump).
SWAPPED_JUMP = f"{TUPLE} -m comment --comment {CHAIN} -j {JUMP_COMMENT}"
# Multiset twin of the first managed chain rule (-j ACCEPT / comment
# kanban-ui-hostpath).
SWAPPED_LO = "-i lo -m comment --comment ACCEPT -j kanban-ui-hostpath"


def canon(spec: str) -> str:
    """Kernel-equivalent rule key, matching the fake's -S rendering:
    '-j TARGET' moved to the end, remaining tokens in insertion order.
    (The script itself compares fully sorted tokens; this mirrors what
    `-S` puts on the wire.)"""
    toks = spec.split()
    if "-j" in toks:
        i = toks.index("-j")
        target = toks[i + 1:i + 2]
        toks = toks[:i] + toks[i + 2:] + ["-j"] + target
    return " ".join(toks)


# The pre-fix existence-only verifier, reconstructed verbatim from commit
# 1e043d2 and spliced back in for the SENSITIVITY cases: proves the harness
# reproduces the finding instead of passing vacuously.
PREFIX_FUNCTION = '''all_rules_live() {
  local r
  # shellcheck disable=SC2086  # spec is deliberately word-split into argv
  ipt -C DOCKER-USER $(jump_spec) -j "${CHAIN}" >/dev/null 2>&1 || return 1
  for r in "${RULES[@]}"; do
    # shellcheck disable=SC2086  # rule is deliberately word-split into argv
    ipt -C ${r} >/dev/null 2>&1 || return 1
  done
  return 0
}
'''
MANAGED_ANCHOR = "managed_state_live() {"

# Round-15 sensitivity splice: revert ONLY norm_rule to the bare-token
# (multiset) sort that the round-15 review rejected, keeping everything else
# of the fixed script intact. Proves the swapped-comment/target regressions
# below are non-vacuous: under bare-token sorting every swap renders --check
# green, under pair bundling each one is drift (42).
TOKENSORT_BODY = '''norm_rule() {
  # shellcheck disable=SC2086  # $1 is deliberately word-split into tokens
  ( set -f; printf '%s\\n' $1 | LC_ALL=C sort | tr '\\n' ' ' )
}
'''
NORM_ANCHOR = "norm_rule() {"


def tokensort_variant(src: str) -> str:
    """Swap the fixed pair-bundling norm_rule for the bare-token sort."""
    assert NORM_ANCHOR in src, "norm_rule renamed; update the harness"
    start = src.index(NORM_ANCHOR)
    end = src.index("\n}\n", start) + len("\n}\n")
    out = src[:start] + TOKENSORT_BODY + src[end:]
    assert out != src
    return out

FAKE_IPTABLES = r'''#!/usr/bin/env python3
"""Stateful iptables stand-in for test_firewall_rule_ordering.py.

Ordered per-chain store of kernel-equivalent rule keys (target-last
canonical form). -S renders in that canonical form -- token order
deliberately DIFFERENT from several inserted specs, so any verifier that
compares raw text instead of normalising is caught. -C and -D are kernel
equivalence matches (token order within a rule irrelevant). Adds an
evaluation model: rules are walked in order and the first match on the
non-proxy probe tuple decides, exactly like netfilter.
"""
import json
import os
import sys

STATE = os.environ["FAKE_IPT_STATE"]
LOG = os.environ["FAKE_IPT_LOG"]
CHAIN = os.environ["FAKE_IPT_CHAIN"]
GUARD = os.environ["FAKE_IPT_GUARD"]
JUMP = os.environ["FAKE_IPT_JUMP"]
PROXY = os.environ["FAKE_IPT_PROXY"]
PROBE_SRC = os.environ["FAKE_IPT_PROBE"]
IP = os.environ["FAKE_IPT_IP"]
PORT = os.environ["FAKE_IPT_PORT"]
TUPLE_D = "-d " + IP + "/32"
TUPLE_P = "--dport " + PORT


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


def matches(rule):
    """Could this (canonical) rule match the non-proxy probe tuple? The
    probe satisfies a rule iff EVERY qualifier present in the rule matches
    the probe (kernel subset semantics): -i lo never matches a LAN probe,
    a foreign -s never matches, a foreign -d/--dport never matches, and an
    unqualified rule always matches."""
    toks = rule.split()

    def after(flag):
        return toks[toks.index(flag) + 1] if flag in toks else None

    if after("-i") == "lo":
        return False
    if "-s" in toks and after("-s") != PROBE_SRC:
        return False
    if "-d" in toks and after("-d") != TUPLE_D.split()[1]:
        return False
    if "--dport" in toks and after("--dport") != TUPLE_P.split()[1]:
        return False
    return True


def is_guard(rule):
    toks = rule.split()
    return (GUARD in toks and toks[-2:] == ["-j", "DROP"]
            and (IP + "/32") in toks and "--dport" in toks and PORT in toks)


def is_jump(rule):
    toks = rule.split()
    return toks[-2:] == ["-j", CHAIN] and JUMP in toks and (IP + "/32") in toks and PORT in toks


def chain_verdict(state):
    """What the probe does inside the private chain: DROP / ACCEPT / OUT."""
    for cr in state["chains"].get(CHAIN, []):
        if not matches(cr):
            continue
        toks = cr.split()
        if toks[-2:] == ["-j", "DROP"]:
            return "DROP"
        if toks[-2:] == ["-j", "ACCEPT"]:
            return "ACCEPT"
    return "OUT"


def restricted(state):
    """True iff the non-proxy probe cannot reach Docker's ACCEPT path."""
    for rule in state["chains"].get("DOCKER-USER", []):
        if not matches(rule):
            continue
        toks = rule.split()
        target = toks[-2:]
        if target == ["-j", "DROP"]:
            return True
        if target in (["-j", "RETURN"], ["-j", "ACCEPT"]):
            return False  # terminates before any restriction
        if target == ["-j", CHAIN]:
            return chain_verdict(state) == "DROP"
        return False  # some other first-match rule: fall-through to docker ACCEPT
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
    s["chains"][argv[1]].append(canon(argv[2:]))
    commit(joined, s)
elif op == "-I":
    s = load()
    # iptables creates the built-in DOCKER-USER chain on demand; mirror that.
    chain = s["chains"].setdefault(argv[1], []) if argv[1] == "DOCKER-USER" else s["chains"].get(argv[1])
    if chain is None:
        sys.exit(1)
    chain.insert(int(argv[2]) - 1, canon(argv[3:]))
    commit(joined, s)
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
    commit(joined, s)
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
    """Splice the pre-fix existence-only verifier back in (sensitivity).

    The body runs under the FIXED function name so the three call sites
    (``--check``, already-converged early-exit, post-reconvergence gate)
    keep working while the verification semantics revert to ``-C``
    existence-only, exactly as they behaved at commit 1e043d2.
    """
    assert MANAGED_ANCHOR in src, "ordered verifier renamed; update the harness"
    start = src.index(MANAGED_ANCHOR)
    end = src.index("\n}\n", start) + len("\n}\n")
    body = PREFIX_FUNCTION.replace("all_rules_live(", "managed_state_live(", 1)
    out = src[:start] + body + src[end:]
    assert out != src
    return out


def restricted(state: dict) -> bool:
    """Python twin of the fake's evaluation model (non-proxy probe)."""
    def matches(rule):
        toks = rule.split()

        def after(flag):
            return toks[toks.index(flag) + 1] if flag in toks else None

        if after("-i") == "lo":
            return False
        if "-s" in toks and after("-s") != PROBE_SRC:
            return False
        if "-d" in toks and after("-d") != f"{IP}/32":
            return False
        if "--dport" in toks and after("--dport") != PORT:
            return False
        return True

    for rule in state["chains"].get("DOCKER-USER", []):
        if not matches(rule):
            continue
        toks = rule.split()
        target = toks[-2:]
        if target == ["-j", "DROP"]:
            return True
        if target in (["-j", "RETURN"], ["-j", "ACCEPT"]):
            return False
        if target == ["-j", CHAIN]:
            for cr in state["chains"].get(CHAIN, []):
                if not matches(cr):
                    continue
                ct = cr.split()
                if ct[-2:] == ["-j", "DROP"]:
                    return True
                if ct[-2:] == ["-j", "ACCEPT"]:
                    return False
            return False
        return False
    return False


class Converger:
    """Runs a docker_user_enforce.sh variant against a fresh fake netfilter."""

    def __init__(self, src: str, rules: dict | None = None):
        self.dir = Path(tempfile.mkdtemp(prefix="kbui-fw-ord-"))
        bindir = self.dir / "bin"
        bindir.mkdir()
        (bindir / "iptables").write_text(FAKE_IPTABLES)
        (bindir / "iptables").chmod(0o755)
        (bindir / "docker").write_text(FAKE_DOCKER)
        (bindir / "docker").chmod(0o755)
        self.script = self.dir / "enforce.sh"
        self.script.write_text(src)
        self.script.chmod(0o755)
        self.state_file = self.dir / "state.json"
        seeded = {
            name: [canon(r) for r in rules_]
            for name, rules_ in (rules or {}).items()
        }
        self.state_file.write_text(json.dumps({"chains": seeded}))
        self.log_file = self.dir / "log.jsonl"
        self.env = dict(
            os.environ,
            PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
            FAKE_IPT_STATE=str(self.state_file),
            FAKE_IPT_LOG=str(self.log_file),
            FAKE_IPT_IP=IP,
            FAKE_IPT_PORT=PORT,
            FAKE_IPT_PROXY=PROXY_IP,
            FAKE_IPT_PROBE=PROBE_SRC,
            FAKE_IPT_CHAIN=CHAIN,
            FAKE_IPT_GUARD=GUARD_COMMENT,
            FAKE_IPT_JUMP=JUMP_COMMENT,
            FAKE_DOCKER_IP=IP,
            KANBAN_UI_CONTAINER=CONTAINER,
            KANBAN_UI_PORT=PORT,
            KANBAN_UI_PROXY_IP=PROXY_IP,
            KANBAN_UI_HOST_IP=HOST_IP,
        )
        self.env.pop("KANBAN_UI_TEST_FAIL_AFTER", None)

    def run(self, *args):
        return subprocess.run(
            [str(self.script), *args], env=self.env, capture_output=True, text=True, timeout=120
        )

    def state(self) -> dict:
        return json.loads(self.state_file.read_text())

    def du(self) -> list:
        return self.state()["chains"].get("DOCKER-USER", [])

    def chain(self) -> list:
        return self.state()["chains"].get(CHAIN, [])

    def log(self) -> list:
        if not self.log_file.exists():
            return []
        return [json.loads(line) for line in self.log_file.read_text().splitlines() if line]

    def restricted(self) -> bool:
        return restricted(self.state())

    def dump(self) -> str:
        return json.dumps(self.state(), sort_keys=True)


def converged_state() -> dict:
    """Kernel state left by a successful converge (guard removed)."""
    return {"DOCKER-USER": [JUMP_RULE], CHAIN: list(CHAIN_RULES)}


SRC = SCRIPT.read_text()


class TestReproduction(unittest.TestCase):
    """The harness reproduces the finding on the PRE-FIX verifier: an
    injected bypass before the managed jump leaves the probe unrestricted
    while --check still exits 0. This locks non-vacuity for every check
    below."""

    def _bypass_passes_check(self, intruder: str, chain: list | None = None):
        state = {"DOCKER-USER": [intruder, JUMP_RULE],
                 CHAIN: list(chain if chain is not None else CHAIN_RULES)}
        c = Converger(prefix_variant(SRC), state)
        self.assertFalse(c.restricted(), "injected rule must bypass the jump for the probe")
        r = c.run("--check")
        self.assertEqual(
            r.returncode, 0,
            f"pre-fix --check must (wrongly) report success on a bypassed tuple: {r.stdout}{r.stderr}",
        )

    def test_prefix_return_before_jump_passes_check(self):
        self._bypass_passes_check(RETURN_RULE)

    def test_prefix_accept_before_jump_passes_check(self):
        self._bypass_passes_check(ACCEPT_RULE)

    def test_prefix_extra_chain_accept_passes_check(self):
        # An ACCEPT matching the probe sits above the terminal DROP: the
        # probe bypasses, yet the -C set is complete so pre-fix --check
        # still exits 0.
        self._bypass_passes_check(
            "never-matches-probe -s 198.51.100.222/32 -j RETURN",  # keeps du shape
            chain=[CHAIN_RULES[0], EXTRA_CHAIN_ACCEPT, CHAIN_RULES[1], CHAIN_RULES[2]],
        )

    def test_prefix_reordered_chain_passes_check(self):
        # Reorder that still satisfies every -C: proof -C cannot see order.
        # (This particular reorder OVER-blocks rather than bypasses -- the
        # finding's point is that the check cannot see the difference.)
        state = {"DOCKER-USER": [JUMP_RULE], CHAIN: [CHAIN_RULES[2], CHAIN_RULES[0], CHAIN_RULES[1]]}
        c = Converger(prefix_variant(SRC), state)
        self.assertEqual(c.run("--check").returncode, 0,
                         "pre-fix --check must (wrongly) pass on a reordered chain")


class TestOrderingDetection(unittest.TestCase):
    """The shipped verifier must FAIL --check on every drift the finding
    describes, and PASS on the exact managed set regardless of -S rendering
    token order."""

    def _drift_fails_check(self, state: dict):
        c = Converger(SRC, state)
        r = c.run("--check")
        self.assertEqual(r.returncode, 42, f"--check must report drift: {r.stdout}{r.stderr}")
        return c

    def test_return_before_jump_fails_check(self):
        self._drift_fails_check({"DOCKER-USER": [RETURN_RULE, JUMP_RULE], CHAIN: list(CHAIN_RULES)})

    def test_accept_before_jump_fails_check(self):
        self._drift_fails_check({"DOCKER-USER": [ACCEPT_RULE, JUMP_RULE], CHAIN: list(CHAIN_RULES)})

    def test_foreign_jump_before_managed_jump_fails_check(self):
        self._drift_fails_check({"DOCKER-USER": [FOREIGN_JUMP, JUMP_RULE], CHAIN: list(CHAIN_RULES)})

    def test_extra_chain_accept_fails_check(self):
        self._drift_fails_check(
            {"DOCKER-USER": [JUMP_RULE], CHAIN: [CHAIN_RULES[0], EXTRA_CHAIN_ACCEPT, CHAIN_RULES[1], CHAIN_RULES[2]]}
        )

    def test_reordered_chain_fails_check(self):
        self._drift_fails_check({"DOCKER-USER": [JUMP_RULE], CHAIN: [CHAIN_RULES[2], CHAIN_RULES[0], CHAIN_RULES[1]]})

    def test_missing_chain_rule_fails_check(self):
        self._drift_fails_check({"DOCKER-USER": [JUMP_RULE], CHAIN: CHAIN_RULES[:2]})

    def test_chain_absent_fails_check(self):
        self._drift_fails_check({"DOCKER-USER": [JUMP_RULE]})

    def test_empty_docker_user_fails_check(self):
        self._drift_fails_check({CHAIN: list(CHAIN_RULES)})

    def test_guard_only_residue_fails_check(self):
        """A stale guard with no jump is NOT managed state (port dead for
        the proxy too): --check must fail so converge repairs it."""
        self._drift_fails_check({"DOCKER-USER": [GUARD_RULE], CHAIN: list(CHAIN_RULES)})

    def test_clean_converged_state_passes_check(self):
        c = Converger(SRC, converged_state())
        self.assertEqual(c.run("--check").returncode, 0)

    def test_transient_guard_at_position_one_still_passes_check(self):
        """The guard is deliberately excluded from the managed set: a run
        interrupted between guard insert and cleanup keeps --check green as
        long as the managed rules sit EXACTLY after the guard copy."""
        state = {"DOCKER-USER": [GUARD_RULE, JUMP_RULE], CHAIN: list(CHAIN_RULES)}
        c = Converger(SRC, state)
        self.assertEqual(c.run("--check").returncode, 0,
                         "guard-then-jump (mid-failure state) is not drift")

    def test_intruder_before_jump_not_masked_by_guard(self):
        state = {"DOCKER-USER": [GUARD_RULE, RETURN_RULE, JUMP_RULE], CHAIN: list(CHAIN_RULES)}
        c = Converger(SRC, state)
        self.assertEqual(c.run("--check").returncode, 42)

    def test_check_output_is_quiet(self):
        """The Ansible verify task consumes only the exit code: no stdout."""
        c = Converger(SRC, converged_state())
        self.assertEqual(c.run("--check").stdout, "")


class TestOperandSwapBinding(unittest.TestCase):
    """Round-15 blocking finding: rule identity must preserve option/operand
    BINDING. Each case swaps the values bound to --comment and -j relative
    to a managed rule (with the swapped jump target registered as a real
    foreign chain, so the rule is kernel-valid). The bare-token multiset
    canonicaliser gives every swap the SAME key as its managed twin, so the
    verifier skips/accepts it as managed state; pair bundling must reject
    all of them, and converge must repair them."""

    def swapped_guard_state(self):
        return {"DOCKER-USER": [SWAPPED_GUARD, JUMP_RULE],
                CHAIN: list(CHAIN_RULES), GUARD_COMMENT: [FK_TARGET_CHAIN_RULE]}

    def swapped_jump_state(self):
        return {"DOCKER-USER": [SWAPPED_JUMP],
                CHAIN: list(CHAIN_RULES), JUMP_COMMENT: [FK_TARGET_CHAIN_RULE]}

    def swapped_chain_state(self):
        return {"DOCKER-USER": [JUMP_RULE],
                CHAIN: [SWAPPED_LO, CHAIN_RULES[1], CHAIN_RULES[2]],
                "kanban-ui-hostpath": [FK_TARGET_CHAIN_RULE]}

    # -- Sensitivity: the multiset canonicaliser lets every swap through --

    def test_prefix_bare_token_sort_passes_swapped_guard(self):
        c = Converger(tokensort_variant(SRC), self.swapped_guard_state())
        self.assertFalse(c.restricted(),
                         "swapped-guard rule jumps to a foreign chain: probe bypasses")
        self.assertEqual(c.run("--check").returncode, 0,
                         "bare-token sort must (wrongly) skip the swap as a transient guard")

    def test_prefix_bare_token_sort_passes_swapped_jump(self):
        c = Converger(tokensort_variant(SRC), self.swapped_jump_state())
        self.assertFalse(c.restricted(),
                         "swapped-jump rule jumps to a foreign chain: probe bypasses")
        self.assertEqual(c.run("--check").returncode, 0,
                         "bare-token sort must (wrongly) accept the swap as the managed jump")

    def test_prefix_bare_token_sort_passes_swapped_chain_rule(self):
        c = Converger(tokensort_variant(SRC), self.swapped_chain_state())
        self.assertEqual(c.run("--check").returncode, 0,
                         "bare-token sort must (wrongly) accept the swapped chain rule")

    # -- Shipped verifier: every swap is drift --

    def test_swapped_guard_before_jump_fails_check(self):
        c = Converger(SRC, self.swapped_guard_state())
        self.assertFalse(c.restricted(), "must be a real bypass, not a vacuous drift case")
        self.assertEqual(c.run("--check").returncode, 42,
                         "swapped guard twin is NOT a transient guard: must not be skipped")

    def test_swapped_jump_at_position_one_fails_check(self):
        c = Converger(SRC, self.swapped_jump_state())
        self.assertFalse(c.restricted(), "must be a real bypass, not a vacuous drift case")
        self.assertEqual(c.run("--check").returncode, 42,
                         "swapped jump twin is NOT the managed jump: must not be accepted first")

    def test_swapped_chain_rule_fails_check(self):
        c = Converger(SRC, self.swapped_chain_state())
        self.assertEqual(c.run("--check").returncode, 42,
                         "swapped chain twin is NOT the managed lo ACCEPT")

    def test_swapped_rule_after_jump_fails_check(self):
        """Position check breaks at the first non-guard rule, so an exact
        swapped twin can only masquerade BEFORE the jump -- but a trailing
        swap must never be mistaken for a chain rule either: chain compare is
        exact and ordered, and a DOCKER-USER residue after the jump is not
        inspected (harmless behind the terminating jump). Lock the chain
        variant explicitly: swapped twin appended AFTER the managed rules."""
        c = Converger(SRC, {"DOCKER-USER": [JUMP_RULE],
                            CHAIN: list(CHAIN_RULES) + [SWAPPED_LO],
                            "kanban-ui-hostpath": [FK_TARGET_CHAIN_RULE]})
        self.assertEqual(c.run("--check").returncode, 42,
                         "extra swapped-twin chain rule must fail the exact-chain check")

    # -- Reconvergence repairs the swaps --

    def test_repair_swapped_guard(self):
        c = Converger(SRC, self.swapped_guard_state())
        r = c.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("converged", r.stdout)
        self.assertEqual(c.du()[0], canon(JUMP_RULE), "managed jump must end up FIRST")
        self.assertNotIn(canon(SWAPPED_GUARD), c.du()[:1])
        self.assertEqual(c.chain(), [canon(r_) for r_ in CHAIN_RULES])
        self.assertTrue(c.restricted())
        for e in c.log():
            self.assertTrue(e["restricted"], f"tuple exposed at op {e['op']}")
        self.assertEqual(c.run("--check").returncode, 0)

    def test_repair_swapped_jump(self):
        c = Converger(SRC, self.swapped_jump_state())
        r = c.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("converged", r.stdout)
        self.assertEqual(c.du()[0], canon(JUMP_RULE), "managed jump must end up FIRST")
        self.assertTrue(c.restricted())
        for e in c.log():
            self.assertTrue(e["restricted"], f"tuple exposed at op {e['op']}")
        self.assertEqual(c.run("--check").returncode, 0)

    def test_repair_swapped_chain_rule(self):
        c = Converger(SRC, self.swapped_chain_state())
        r = c.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("converged", r.stdout)
        self.assertEqual(c.chain(), [canon(r_) for r_ in CHAIN_RULES],
                         "chain must be the exact ordered managed set")
        self.assertTrue(c.restricted())
        for e in c.log():
            self.assertTrue(e["restricted"], f"tuple exposed at op {e['op']}")
        self.assertEqual(c.run("--check").returncode, 0)


class TestReconvergence(unittest.TestCase):
    """On drift the converger repairs ordering UNDER the temporary DROP
    guard, with the probe restricted at every observed instant, and leaves
    the exact managed set (jump first, chain exact) behind."""

    def _repair(self, state: dict):
        c = Converger(SRC, state)
        r = c.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("converged", r.stdout)
        self.assertNotIn("already converged", r.stdout)
        self.assertEqual(c.du()[0], canon(JUMP_RULE), "managed jump must end up FIRST")
        # Any surviving foreign rules now sit BEHIND the jump: harmless for
        # the probe (the jump terminates on the chain's terminal DROP) and
        # invisible to --check because position 1 is the managed jump.
        self.assertEqual(c.chain(), [canon(r_) for r_ in CHAIN_RULES],
                         "chain must be the exact ordered managed set")
        self.assertTrue(c.restricted(), "probe must be DROPped after repair")
        for e in c.log():
            self.assertTrue(e["restricted"], f"tuple exposed at op {e['op']}")
        # idempotent rerun + check
        r2 = c.run()
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("already converged", r2.stdout)
        self.assertEqual(c.run("--check").returncode, 0)
        return c

    def test_repair_return_before_jump(self):
        self._repair({"DOCKER-USER": [RETURN_RULE, JUMP_RULE], CHAIN: list(CHAIN_RULES)})

    def test_repair_accept_before_jump(self):
        self._repair({"DOCKER-USER": [ACCEPT_RULE, JUMP_RULE], CHAIN: list(CHAIN_RULES)})

    def test_repair_foreign_jump_before_managed_jump(self):
        self._repair({"DOCKER-USER": [FOREIGN_JUMP, JUMP_RULE], CHAIN: list(CHAIN_RULES)})

    def test_repair_extra_chain_accept(self):
        self._repair(
            {"DOCKER-USER": [JUMP_RULE], CHAIN: [CHAIN_RULES[0], EXTRA_CHAIN_ACCEPT, CHAIN_RULES[1], CHAIN_RULES[2]]}
        )

    def test_repair_reordered_chain(self):
        self._repair({"DOCKER-USER": [JUMP_RULE], CHAIN: [CHAIN_RULES[2], CHAIN_RULES[0], CHAIN_RULES[1]]})

    def test_repair_keeps_surviving_guard_until_verified(self):
        """Coherence with the r5/r6 fail-closed work: from a drifted state
        with a leftover guard, a guard stays live through the entire
        delete/flush/append/insert sequence."""
        c = Converger(SRC, {"DOCKER-USER": [GUARD_RULE, RETURN_RULE, JUMP_RULE], CHAIN: list(CHAIN_RULES)})
        r = c.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        events = c.log()
        guard_inserts = [i for i, e in enumerate(events)
                         if e["op"].startswith("-I DOCKER-USER 1") and GUARD_COMMENT in e["op"]]
        jump_inserts = [i for i, e in enumerate(events)
                        if e["op"].startswith("-I DOCKER-USER 1") and JUMP_COMMENT in e["op"]]
        self.assertEqual(len(jump_inserts), 1)
        self.assertLess(guard_inserts[0], jump_inserts[0], "guard precedes jump insert")
        self.assertGreaterEqual(events[jump_inserts[0]]["guards"], 1, "guard live when jump lands")
        self.assertEqual(c.du()[0], canon(JUMP_RULE))
        self.assertEqual(c.chain(), [canon(r_) for r_ in CHAIN_RULES])
        for e in events:
            self.assertTrue(e["restricted"], f"tuple exposed at op {e['op']}")

    def test_converged_clean_state_touches_nothing(self):
        c = Converger(SRC, converged_state())
        r = c.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already converged", r.stdout)
        self.assertEqual(c.log(), [], "exact managed state must not touch the kernel")

    def test_sensitivity_ordered_verifier_repairs_where_prefix_lets_check_pass(self):
        """End-to-end lock of the finding's scenario: on the pre-fix
        verifier --check says OK with an unrestricted probe; on the shipped
        verifier the same state is drift (42) and converge repairs it."""
        state = {"DOCKER-USER": [RETURN_RULE, JUMP_RULE], CHAIN: list(CHAIN_RULES)}
        pre = Converger(prefix_variant(SRC), dict((k, list(v)) for k, v in state.items()))
        self.assertEqual(pre.run("--check").returncode, 0)
        self.assertFalse(pre.restricted())
        post = Converger(SRC, dict((k, list(v)) for k, v in state.items()))
        self.assertEqual(post.run("--check").returncode, 42)
        self.assertFalse(post.restricted())
        self.assertEqual(post.run().returncode, 0)
        self.assertTrue(post.restricted())


if __name__ == "__main__":
    unittest.main(verbosity=2)
