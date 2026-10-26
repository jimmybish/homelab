#!/bin/bash
# kanban-ui DOCKER-USER enforcement (PR#13 round-4 finding 4321368046).
# Converges the service-scoped pre-Docker forwarding filter for the published
# kanban-ui port: a dedicated DOCKER-USER-KANBAN chain (ACCEPT in-lo, ACCEPT
# the SWAG proxy source, terminal DROP) plus a jump at the head of
# DOCKER-USER matching ONLY the published host tuple via conntrack
# (--ctorigdst/--ctorigdstport, PR#13 round-18 finding 4373043501: no
# container-IP dependency, so the restriction survives a Compose recreate
# that reassigns the container address), so no other container, network, or
# host service is affected.
#
# Invoked by:
#   1. the kanban_ui Ansible role (deploy convergence + drift check), and
#   2. kanban-ui-docker-user.service (Type=oneshot After=docker.service
#      PartOf=docker.service, and Wants=kanban-ui-docker-user.service from
#      the docker.service.d drop-in so the daemon's own start pulls it in —
#      PR#13 r8 finding 4354363965), Docker's documented mitigation for
#      user rules
#      lost when the daemon recreates DOCKER-USER at startup (boot, and
#      every daemon restart/upgrade re-fires the unit):
#      https://docs.docker.com/engine/network/packet-filtering-firewalls/
#
# Configuration arrives via the environment (never interpolated into shell
# syntax by the caller): KANBAN_UI_CONTAINER, KANBAN_UI_PORT,
# KANBAN_UI_PROXY_IP, KANBAN_UI_HOST_IP (the published host address;
# required by every mode except --teardown). The container address is
# resolved from the live Docker API only to decide whether a container
# exists at all (nothing published -> nothing to enforce); the permanent
# matchers never embed it (PR#13 round-18 finding 4373043501), so the
# restriction holds even mid-recreate while Docker reassigns the IP.
#
# Modes:
#   --pre-lock: install the fail-closed PRE-PUBLISH lockdown BEFORE the
#     container publishes its port (PR#13 round-8 finding 4354363865): a
#     DROP at DOCKER-USER position 1 matching the ORIGINAL host
#     destination/port tuple (post-DNAT packets only expose the container
#     address, so the original tuple is matched via conntrack
#     --ctorigdst/--ctorigdstport). Needs no container IP and no Docker API:
#     the role runs this ahead of the Compose build/up tasks so the
#     readiness wait and any earlier failure can never leave the freshly
#     published port reachable from non-proxy LAN hosts. Idempotent ONLY
#     when an exact copy is the FIRST effective rule in DOCKER-USER (round-
#     17 review finding: a RETURN/ACCEPT or foreign jump pushed ahead of a
#     surviving copy must not be accepted as "already installed" -- any
#     other head rule forces a fresh position-1 insert, and duplicate
#     copies are comment-swept by converge/teardown). A live managed jump
#     (steady state) makes the lockdown unnecessary and the run is a no-op.
#     Survives every mid-play failure (including a failed Compose readiness
#     wait); cleared ONLY by a successful converge (after the managed
#     jump+chain verify live) or --teardown.
#   enforce (default): idempotent converge; exit 0 always unless an
#     iptables operation itself fails (a missing container is not a failure:
#     nothing is published, nothing to enforce). Reconvergence is
#     FAIL-CLOSED (PR#13 round-5 finding 4323915092): before the stale jump
#     is removed, a temporary service-scoped DROP matching the same
#     original host tuple (-m comment kanban-ui-guard) is inserted at
#     DOCKER-USER position 1, so the published port stays DROP'd for every
#     source THROUGHOUT the delete/flush/append/insert sequence. The guard
#     is deleted ONLY after the jump+chain set verifies live; any mid-script
#     failure exits non-zero with the guard still in place (restriction
#     stays ON, never off). Normal completion leaves no orphan guard, and
#     sweeps any pre-publish lockdown left by the --pre-lock stage.
#   --check: exit 0 when every managed rule is live IN THE EXPECTED
#     POSITION/ORDER (managed jump first in DOCKER-USER, private chain
#     exactly the ordered RULES), 42 on drift, 44 when the container is not
#     running (nothing published, nothing enforced). The transient guard is
#     deliberately NOT part of the managed live set.
#   --teardown: remove every jump into the chain (any destination variant),
#     any leftover kanban-ui-guard DROP (a fail-closed guard surviving
#     a mid-script failure must not outlive an absent-state teardown) and
#     any leftover kanban-ui-prelock DROP (the pre-publish lockdown must not
#     outlive the service either),
#     then flush + unregister the chain. Designed to run via
#     ansible.builtin.script pushed from the role's files/ (PR#13 round-5
#     finding 4323915133): the absent-state playbook path deletes the
#     installed copy at /usr/local/sbin, so repeatable cleanup must not
#     depend on it. Fully rc-tolerant: missing chain, no jumps, and even a
#     missing DOCKER-USER chain (no Docker on the host) exit 0 while still
#     removing any leftover rules it can find.
#
# Fault injection (TEST ONLY): KANBAN_UI_TEST_FAIL_AFTER=guard|delete|flush|
# append|insert makes the script exit 1 immediately after that stage
# completes, emulating a mid-script iptables failure so the fail-closed
# property can be proven live. Unset/empty in every production invocation
# (role tasks and the systemd unit never set it); an invalid value is a
# usage error (exit 2) so a typo can never half-arm.
set -uo pipefail

# The pre-lock and teardown modes never resolve the container, so they must
# not require variables only the converge path consumes. KANBAN_UI_HOST_IP
# (the published host address) is required by --pre-lock, enforce and
# --check; it is deliberately NOT required by --teardown, which matches the
# lockdown by comment alone.
if [ "${1:-}" != "--teardown" ]; then
  : "${KANBAN_UI_HOST_IP:?KANBAN_UI_HOST_IP must be set}"
  case "$KANBAN_UI_HOST_IP" in
    (*[!0-9.]*|'') echo "invalid host ip" >&2; exit 2 ;;
  esac
fi
: "${KANBAN_UI_CONTAINER:?KANBAN_UI_CONTAINER must be set}"
: "${KANBAN_UI_PORT:?KANBAN_UI_PORT must be set}"
: "${KANBAN_UI_PROXY_IP:?KANBAN_UI_PROXY_IP must be set}"
case "$KANBAN_UI_PORT" in (*[!0-9]*|'') echo "invalid port" >&2; exit 2 ;; esac
case "$KANBAN_UI_PROXY_IP" in
  (*[!0-9.]*|'') echo "invalid proxy ip" >&2; exit 2 ;;
esac
case "${KANBAN_UI_TEST_FAIL_AFTER:-}" in
  (''|guard|delete|flush|append|insert) ;;
  (*) echo "invalid KANBAN_UI_TEST_FAIL_AFTER" >&2; exit 2 ;;
esac

# -w serialises against Docker's own concurrent rule writes (observed live
# on this host: per-command iptables-nft calls race with the daemon during
# container bring-up). 30s covers daemon-start bursts.
ipt() { iptables -w 30 "$@"; }

CHAIN=DOCKER-USER-KANBAN
JUMP_COMMENT=kanban-ui-jump
GUARD_COMMENT=kanban-ui-guard
PRELOCK_COMMENT=kanban-ui-prelock
# Each entry is "<chain> <rulespec>" (no -A/-C flag) so the same string
# serves both `ipt -A ${r}` and `ipt -C ${r}` without re-quoting.
RULES=(
  "${CHAIN} -i lo -j ACCEPT -m comment --comment kanban-ui-hostpath"
  "${CHAIN} -s ${KANBAN_UI_PROXY_IP}/32 -j ACCEPT -m comment --comment kanban-ui-proxy"
  "${CHAIN} -j DROP -m comment --comment kanban-ui-deny"
)

if [ "${1:-}" = "--teardown" ]; then
  # Runs WITHOUT resolving the container IP: stale jumps (older container
  # address) must be removable after the container is already gone. Match on
  # the chain name only. Rule text from iptables -S is word-split on purpose.
  removed=0
  while read -r r; do
    [ -n "$r" ] || continue
    # shellcheck disable=SC2086  # rule spec must be separate argv words
    ipt -D DOCKER-USER ${r#-A DOCKER-USER } || true
    removed=1
  done < <(ipt -S DOCKER-USER | grep -- "-j ${CHAIN}$" || true)
  # A fail-closed guard surviving a mid-script failure must not outlive the
  # service: delete any guard DROP variant too (comment-matched, always
  # present on guard rules regardless of the resolved container IP). The
  # same applies to the pre-publish lockdown (round-8 finding 4354363865):
  # a lockdown surviving an aborted play (or an abandoned absent-state
  # run) must not orphan on a host that no longer runs kanban-ui, and
  # would otherwise black-hole any future service reusing the port tuple.
  while read -r r; do
    [ -n "$r" ] || continue
    # shellcheck disable=SC2086  # rule spec must be separate argv words
    ipt -D DOCKER-USER ${r#-A DOCKER-USER } || true
    removed=1
  done < <(ipt -S DOCKER-USER | grep -- "--comment ${GUARD_COMMENT} " || true)
  while read -r r; do
    [ -n "$r" ] || continue
    # shellcheck disable=SC2086  # rule spec must be separate argv words
    ipt -D DOCKER-USER ${r#-A DOCKER-USER } || true
    removed=1
  done < <(ipt -S DOCKER-USER | grep -- "--comment ${PRELOCK_COMMENT} " || true)
  if ipt -n -L "${CHAIN}" >/dev/null 2>&1; then
    ipt -F "${CHAIN}" && ipt -X "${CHAIN}"
    removed=1
  fi
  if [ "$removed" = 1 ]; then
    echo "torn down ${CHAIN} (jumps + guard + prelock + chain)"
  else
    echo "no ${CHAIN} rules present"
  fi
  exit 0
fi

resolve_ip() {
  docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' \
    "$KANBAN_UI_CONTAINER" 2>/dev/null | awk '{print $1}'
}

# Pre-lock mode must not depend on Docker at all: the container may not
# exist yet (first deploy), and waiting for it would delay the lockdown
# that is supposed to PRECEDE publishing. The boot-race wait is for the
# converge/check paths only, where an absent container right after a
# daemon restart is transient.
PRELOCK_MODE=0
if [ "${1:-}" = "--pre-lock" ]; then
  PRELOCK_MODE=1
fi

ip=$(resolve_ip)
if [ -z "$ip" ] && [ "$PRELOCK_MODE" = 0 ]; then
  # Boot-race tolerance (systemd path): the container (restart policy
  # unless-stopped) comes up alongside the daemon; give it up to 30s.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 3
    ip=$(resolve_ip)
    [ -n "$ip" ] && break
  done
fi
if [ -z "$ip" ] && [ "$PRELOCK_MODE" = 0 ]; then
  if [ "${1:-}" = "--check" ]; then
    echo "container $KANBAN_UI_CONTAINER not running; nothing enforced" >&2
    exit 44
  fi
  echo "container $KANBAN_UI_CONTAINER not running; nothing published to enforce"
  exit 0
fi

# Shared matcher for the jump and the transient guard. PR#13 round-18
# finding 4373043501: this used to pin the post-DNAT container address
# (-d ${ip}/32). When the existing ruleset validated, --pre-lock skipped
# its lockdown before Compose could RECREATE the container; if recreation
# reassigned the IP, the old container-IP jump stopped restricting the
# published port until convergence after the readiness waits (a failed
# wait left the restriction missing entirely). The matcher is therefore
# pinned to the ORIGINAL host destination/port via conntrack -- the exact
# token set the pre-publish lockdown already proved live on this host:
# DOCKER-USER evaluates after nat PREROUTING, so only conntrack exposes
# the pre-DNAT tuple, and the host address + published port are fixed by
# the Compose ports mapping regardless of which container address Docker
# assigns. --ctdir ORIGINAL is load-bearing, not cosmetic: conntrack
# matches the original tuple in BOTH directions, and a container->client
# REPLY packet carries the client's ephemeral port as its destination --
# when that port happens to equal the published port, a direction-blind
# --dport match would drag replies into the chain, where the source-based
# ACCEPTs never match (source is the container) and the terminal DROP
# kills the established session (the old -d container/32 matcher could
# not catch a reply at all). ORIGINAL-only keeps the old direction
# semantics: forward packets are gated by the chain, replies pass.
# Blast radius is unchanged: traffic to any other published address/port,
# or any transit flow that never DNATs to this host tuple, never matches.
# Bare --ctorigdst address (no /32) so the rendered spec round-trips
# byte-identically through `iptables -S` on iptables-nft 1.8.7.
tuple_spec() {
  echo "-p tcp -m tcp --dport ${KANBAN_UI_PORT} -m conntrack --ctdir ORIGINAL --ctorigdst ${KANBAN_UI_HOST_IP} --ctorigdstport ${KANBAN_UI_PORT}"
}

jump_spec() {
  echo "$(tuple_spec) -m comment --comment ${JUMP_COMMENT}"
}

guard_spec() {
  echo "$(tuple_spec) -m comment --comment ${GUARD_COMMENT} -j DROP"
}

# Pre-publish lockdown (PR#13 round-8 finding 4354363865): matches the
# ORIGINAL host destination/port. DOCKER-USER is evaluated AFTER nat PREROUTING,
# so by then the packet's destination already reads as the container address;
# the pre-DNAT tuple is only recoverable through conntrack. Matching the
# original host address + published port therefore covers every LAN source
# without knowing the container IP -- which is what makes it installable
# BEFORE the container exists. Bare --ctorigdst address (no /32) so the
# inserted spec is byte-identical to the `iptables -S` rendering (verified
# live on iptables-nft 1.8.7: the mask is stripped at render time), keeping
# exact-copy comparisons and deletions trivially exact.
prelock_spec() {
  echo "-p tcp -m tcp --dport ${KANBAN_UI_PORT} -m conntrack --ctorigdst ${KANBAN_UI_HOST_IP} --ctorigdstport ${KANBAN_UI_PORT} -m comment --comment ${PRELOCK_COMMENT} -j DROP"
}

# Canonical order-insensitive key for one rule spec: rule ORDER within the
# chain is compared strictly (that is the position property the finding is
# about), but OPTION ORDER WITHIN one rule is normalised away, because
# `iptables -S` rendering order of tokens has varied across backends and
# versions and the guaranteed invariant is only the kernel's rule-equivalence
# semantics (verified against live iptables-nft 1.8.7 output on this host,
# which round-trips our specs verbatim).
#
# Normalisation BUNDLES each known single-operand option with its operand
# (option=operand) BEFORE sorting, so the sort permutes whole option/operand
# PAIRS, never bare tokens (PR#13 round-15 review finding, blocking): a bare
# token sort made `--comment DROP -j kanban-ui-guard` (a jump into a foreign
# chain NAMED like the guard comment) collide with the real transient guard
# `--comment kanban-ui-guard -j DROP`, and the same collision existed between
# the managed jump's comment and target -- a bypass rule could masquerade as
# the guard we skip or as the jump we accept in first position.
#
# Only the options our managed rules actually use (and the short forms
# `iptables -S` emits) are paired; any other token stays bare. A token with
# an `=` that was not produced by bundling -- or a known option left without
# its operand (`opt=`, which no installable rule can encode) -- therefore
# cannot collide with a managed rule's key: worst case is spurious drift and
# a fail-closed reconvergence, never a false PASS. set -f in the subshell
# keeps token expansion glob-safe; specs are our own or -S output (no
# newlines, no glob metacharacters).
norm_rule() {
  # shellcheck disable=SC2086  # $1 is deliberately word-split into tokens
  (
    set -f
    # shellcheck disable=SC2086  # word-splitting is the point of $1 here
    set -- $1
    out=()
    while [ "$#" -gt 0 ]; do
      case $1 in
        (-j | -m | -p | -s | -d | -i | -o | --comment | --dport | --sport | --ctdir | --ctorigdst | --ctorigdstport)
          # Bundle the option with its operand; a trailing option with no
          # operand encodes as "opt=" (a value no real rule can carry).
          # --ctorigdst/--ctorigdstport are bundled for the same reason as
          # --comment/-j (PR#13 round-15 blocking finding): their operands
          # (an address, a port) must never be sortable as bare tokens that
          # could masquerade as a managed rule's comment or jump target.
          out+=("$1=${2-}")
          shift
          [ "$#" -gt 0 ] && shift
          ;;
        (*)
          out+=("$1")
          shift
          ;;
      esac
    done
    if [ "${#out[@]}" -gt 0 ]; then
      printf '%s\n' "${out[@]}" | LC_ALL=C sort | tr '\n' ' '
    fi
  )
}

# POSITION verification, not mere existence (PR#13 round-8 finding
# 4354363842): `iptables -C` answers "does this rule exist", so a RETURN or
# ACCEPT injected BEFORE the managed jump -- or an extra/reordered rule in
# the private chain -- left -C-only checks green while non-proxy traffic
# bypassed the restriction. This verifier compares the live `iptables -S`
# rendering against the EXPECTED ORDERED ruleset instead:
#   * DOCKER-USER: the managed jump must be the FIRST rule once exact
#     transient-guard copies (the only shape this script inserts, matched
#     token-for-token below; the guard is deliberately not part of the
#     managed live set) and exact pre-publish lockdown copies (the
#     --pre-lock artifact, likewise matched token-for-token and removed by
#     the converge sweep once the jump verifies -- a surviving lockdown is
#     redundantly DROP, never drift) are skipped. Anything evaluated ahead
#     of it -- RETURN, ACCEPT, a foreign jump -- is drift.
#   * private chain: exactly the RULES entries, in RULES order. A missing
#     rule, an inserted extra, or any reorder fails.
# -P/-N header lines from -S are skipped. Output is small (single-service
# chains), so the read loops never risk a blocked process substitution.
managed_state_live() {
  local norm_guard norm_prelock norm_jump norm_rules=() key line i seen_jump r
  norm_guard=$(norm_rule "$(guard_spec)")
  norm_prelock=$(norm_rule "$(prelock_spec)")
  norm_jump=$(norm_rule "$(jump_spec) -j ${CHAIN}")
  for r in "${RULES[@]}"; do
    norm_rules+=("$(norm_rule "${r#"${CHAIN}" }")")
  done
  seen_jump=0
  while IFS= read -r line; do
    case "$line" in
      (-P\ *|-N\ *) continue ;;
    esac
    key=$(norm_rule "${line#-A DOCKER-USER }")
    [ "$key" = "$norm_guard" ] && continue
    [ "$key" = "$norm_prelock" ] && continue
    # First non-guard rule must BE the managed jump (position check).
    # Anything ahead of the jump -- however harmless it looks -- is drift:
    # netfilter evaluates in order, and only the jump's chain guarantees
    # the terminal DROP for the published tuple.
    [ "$key" = "$norm_jump" ] || return 1
    seen_jump=1
    break
  done < <(ipt -S DOCKER-USER 2>/dev/null)
  # The jump must be present AND first: an empty DOCKER-USER, a missing
  # chain, or a guard-only residue (-S yields no jump line) must fail.
  [ "$seen_jump" = 1 ] || return 1
  i=0
  while IFS= read -r line; do
    case "$line" in
      (-P\ *|-N\ *) continue ;;
    esac
    [ "$i" -lt "${#norm_rules[@]}" ] || return 1  # extra rule beyond managed set
    key=$(norm_rule "${line#-A "${CHAIN}" }")
    [ "$key" = "${norm_rules[$i]}" ] || return 1
    i=$((i + 1))
  done < <(ipt -S "${CHAIN}" 2>/dev/null)
  [ "$i" -eq "${#norm_rules[@]}" ] || return 1  # missing rule(s)
  return 0
}

if [ "${1:-}" = "--check" ]; then
  managed_state_live && exit 0
  exit 42
fi

# Delete every DOCKER-USER rule carrying the given comment (guard or
# pre-publish lockdown copies). Used only on paths where the restriction is
# redundant (jump live and verified): a copy that refuses to delete must
# make the caller fail, never report convergence while the proxy path stays
# DROP'd. Rule text from iptables -S is word-split on purpose.
sweep_comment() {
  local comment="$1" r
  while read -r r; do
    [ -n "$r" ] || continue
    # shellcheck disable=SC2086  # rule spec must be separate argv words
    ipt -D DOCKER-USER ${r#-A DOCKER-USER } || return 1
  done < <(ipt -S DOCKER-USER | grep -- "--comment ${comment} " || true)
  if ipt -S DOCKER-USER | grep -q -- "--comment ${comment} "; then
    return 1
  fi
  return 0
}

# PRE-PUBLISH LOCKDOWN (PR#13 round-8 finding 4354363865): the role invokes
# this mode BEFORE the Compose build/up tasks, so the port is never
# published without a DROP already covering the original host tuple. Runs
# without the container being up (no boot-race wait, no Docker dependency
# in the install path). Every failure path of the play afterwards --
# including a failed Compose readiness wait, which aborts before the
# converge tasks -- leaves the lockdown in place: fail-closed. Only a
# successful converge (managed jump+chain verified live) or --teardown
# removes it.
if [ "$PRELOCK_MODE" = 1 ]; then
  if managed_state_live; then
    # Steady state: the managed jump (whose chain ends in the terminal
    # DROP, and whose matcher pins the ORIGINAL host tuple, not a
    # container address) already restricts the tuple across a Compose
    # recreate that reassigns the container IP (PR#13 round-18 finding
    # 4373043501); installing a lockdown here would make every converge
    # play permanently non-idempotent (install -> sweep -> install...).
    # Report unchanged and touch nothing. Deliberately NOT gated on the
    # resolved container IP any more: during a recreation the container
    # may be briefly absent while the old ruleset still (correctly)
    # protects, and an unresolvable IP must never blind this check.
    echo "pre-publish lockdown not needed; managed DOCKER-USER ruleset live"
    exit 0
  fi
  norm_want=$(norm_rule "$(prelock_spec)")
  # Idempotency is POSITIONAL (PR#13 round-17 review finding 1): an exact
  # copy that is no longer the FIRST effective rule is not protection.
  # Netfilter evaluates DOCKER-USER in order, so a RETURN/ACCEPT or foreign
  # jump inserted ahead of a surviving copy lets Compose publish behind the
  # bypass while a mere-existence scan reported "already installed" (the
  # High-severity exposure re-opened). Only an exact copy AT the head is a
  # no-op; any other head rule (or none) re-arms a fresh position-1 DROP.
  # Duplicate copies from repeated re-arms are harmless (all are DROPs on
  # the equal matcher) and are comment-swept by every converge success path
  # and by --teardown, so none can orphan.
  first_rule=""
  while IFS= read -r line; do
    case "$line" in
      (-P\ *|-N\ *) continue ;;
    esac
    first_rule="${line#-A DOCKER-USER }"
    break
  done < <(ipt -S DOCKER-USER 2>/dev/null)
  if [ -n "$first_rule" ] && [ "$(norm_rule "$first_rule")" = "$norm_want" ]; then
    # Retry-safe: a previous failed play already armed the lockdown at the
    # head and nothing is evaluated ahead of it.
    echo "pre-publish lockdown already installed for ${KANBAN_UI_HOST_IP}:${KANBAN_UI_PORT}"
    exit 0
  fi
  # Single xtables-locked insertion at position 1, ahead of everything
  # (including any stale jump or bypass pushed above a surviving copy), so
  # the tuple is restricted from the instant this returns until converge
  # verifies the managed set.
  # shellcheck disable=SC2046  # $(prelock_spec) is deliberately word-split into argv
  ipt -I DOCKER-USER 1 $(prelock_spec) || exit 1
  echo "pre-publish lockdown installed for ${KANBAN_UI_HOST_IP}:${KANBAN_UI_PORT}"
  exit 0
fi

if managed_state_live; then
  # Exact managed state already live: touch nothing (byte-stable rerun).
  # A leftover guard from an earlier failed run is still worth clearing —
  # the restriction is redundant while the jump is live, and a guard alone
  # (jump gone) would keep the port dead for the proxy too. A surviving
  # pre-publish lockdown is cleared on the same grounds (it DROPs the
  # proxy path as well).
  sweep_comment "$GUARD_COMMENT" || {
    # A guard that will not delete leaves the proxy path DROPped; a stale
    # restriction the role cannot clear is a failure, not "already converged".
    echo "stale guard removal did not take effect" >&2
    exit 1
  }
  sweep_comment "$PRELOCK_COMMENT" || {
    echo "stale pre-publish lockdown removal did not take effect" >&2
    exit 1
  }
  echo "DOCKER-USER-KANBAN already converged for ${ip}:${KANBAN_UI_PORT} (proxy ${KANBAN_UI_PROXY_IP})"
  exit 0
fi

# Chain must exist before anything jumps to it.
ipt -n -L "${CHAIN}" >/dev/null 2>&1 || ipt -N "${CHAIN}" || exit 1

# FAIL-CLOSED RECONVERGENCE (PR#13 round-5 finding 4323915092, hardened by
# round-6 finding 4331497704):
# From here until the final verified guard deletion, the container tuple is
# DROP'd in DOCKER-USER no matter which step fails next. Surviving guards
# from an earlier failed run are deliberately KEPT while the fresh guard is
# inserted: deleting them first opened a window (and, if the -I below then
# failed, a terminal state) with neither jump nor guard covering the port.
# Stacked duplicate guards are harmless -- every copy is a DROP on an equal
# matcher -- and the verified-success cleanup below (and --teardown) remove
# ALL guard copies by comment match, so no guard can orphan. The insertion
# is a single xtables-locked operation, atomic against Docker's own writes,
# so at every instant at least one guard or the jump restricts the tuple.
# shellcheck disable=SC2086  # spec is deliberately word-split into argv
ipt -I DOCKER-USER 1 $(guard_spec) || exit 1
[ "${KANBAN_UI_TEST_FAIL_AFTER:-}" = "guard" ] && { echo "injected failure after guard" >&2; exit 1; }

# Remove every existing jump into our chain (stale IP variants included).
# The guard above already covers the tuple, so removing the jump here opens
# no exposure window; the jump is re-added below only after the chain is
# fully populated, so it never sees a partially built (fail-open) chain.
while read -r r; do
  [ -n "$r" ] || continue
  # shellcheck disable=SC2086  # rule spec must be separate argv words
  ipt -D DOCKER-USER ${r#-A DOCKER-USER } || true
done < <(ipt -S DOCKER-USER | grep -- "-j ${CHAIN}$" || true)
[ "${KANBAN_UI_TEST_FAIL_AFTER:-}" = "delete" ] && { echo "injected failure after delete" >&2; exit 1; }

# Populate the chain; -A order guarantees the terminal DROP is last.
ipt -F "${CHAIN}" || exit 1
[ "${KANBAN_UI_TEST_FAIL_AFTER:-}" = "flush" ] && { echo "injected failure after flush" >&2; exit 1; }
for r in "${RULES[@]}"; do
  # shellcheck disable=SC2086  # rule is deliberately word-split into argv
  ipt -A ${r} || exit 1
done
[ "${KANBAN_UI_TEST_FAIL_AFTER:-}" = "append" ] && { echo "injected failure after append" >&2; exit 1; }

# Managed jump at the head of DOCKER-USER (Docker guidance: user rules
# precede everything else there), added LAST so it never sees an empty chain.
# shellcheck disable=SC2086  # spec is deliberately word-split into argv
ipt -I DOCKER-USER 1 $(jump_spec) -j "${CHAIN}" || exit 1
[ "${KANBAN_UI_TEST_FAIL_AFTER:-}" = "insert" ] && { echo "injected failure after insert" >&2; exit 1; }

# Verified success is the ONLY path that may relax anything: re-check the
# full managed set against the kernel, then remove the transient guard and
# any pre-publish lockdown. If verification or removal fails, exit non-zero
# WITH both still in place — fail-closed, the restriction stays on.
managed_state_live || exit 1
sweep_comment "$GUARD_COMMENT" || {
  # Should be unreachable; belt-and-braces so a silent delete failure can
  # never leave a "converged" verdict with an orphan guard.
  echo "guard removal did not take effect" >&2
  exit 1
}
sweep_comment "$PRELOCK_COMMENT" || {
  # Same fail-closed rule for the pre-publish lockdown: an unrestorable
  # DROP would outlive the "converged" verdict, so refuse to claim it.
  echo "pre-publish lockdown removal did not take effect" >&2
  exit 1
}

echo "converged DOCKER-USER-KANBAN for ${ip}:${KANBAN_UI_PORT} (proxy ${KANBAN_UI_PROXY_IP})"
