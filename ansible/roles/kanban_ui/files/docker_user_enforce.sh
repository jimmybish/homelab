#!/bin/bash
# kanban-ui DOCKER-USER enforcement (PR#13 round-4 finding 4321368046).
# Converges the service-scoped pre-Docker forwarding filter for the published
# kanban-ui port: a dedicated DOCKER-USER-KANBAN chain (ACCEPT in-lo, ACCEPT
# the SWAG proxy source, terminal DROP) plus a jump at the head of
# DOCKER-USER matching ONLY the kanban container's post-DNAT tuple, so no
# other container, network, or host service is affected.
#
# Invoked by:
#   1. the kanban_ui Ansible role (deploy convergence + drift check), and
#   2. kanban-ui-docker-user.service (Type=oneshot After=docker.service
#      PartOf=docker.service), Docker's documented mitigation for user rules
#      lost when the daemon recreates DOCKER-USER at startup (boot, and
#      every daemon restart/upgrade re-fires the unit):
#      https://docs.docker.com/engine/network/packet-filtering-firewalls/
#
# Configuration arrives via the environment (never interpolated into shell
# syntax by the caller): KANBAN_UI_CONTAINER, KANBAN_UI_PORT,
# KANBAN_UI_PROXY_IP. The container address is resolved from the live Docker
# API on every run, so a recreate that reassigns the IP self-heals.
#
# Modes:
#   enforce (default): idempotent converge; exit 0 always unless an
#     iptables operation itself fails (a missing container is not a failure:
#     nothing is published, nothing to enforce). On drift: ensure the chain
#     exists, remove any stale jump variant, populate the chain (terminal
#     DROP last), then insert the jump at DOCKER-USER position 1 — the jump
#     is only ever present while the full ACCEPT/DROP set is live, so there
#     is no window where it points at an empty (fail-open) chain.
#   --check: exit 0 when every managed rule is live, 42 on drift, 44 when
#     the container is not running (nothing published, nothing enforced).
#   --teardown: remove every jump into the chain (any destination variant)
#     then flush + unregister the chain. Designed to run via
#     ansible.builtin.script pushed from the role's files/ (PR#13 round-5
#     finding 4323915133): the absent-state playbook path deletes the
#     installed copy at /usr/local/sbin, so repeatable cleanup must not
#     depend on it. Fully rc-tolerant: missing chain, no jumps, and even a
#     missing DOCKER-USER chain (no Docker on the host) exit 0 while still
#     removing any leftover rules it can find.
set -uo pipefail

: "${KANBAN_UI_CONTAINER:?KANBAN_UI_CONTAINER must be set}"
: "${KANBAN_UI_PORT:?KANBAN_UI_PORT must be set}"
: "${KANBAN_UI_PROXY_IP:?KANBAN_UI_PROXY_IP must be set}"
case "$KANBAN_UI_PORT" in (*[!0-9]*|'') echo "invalid port" >&2; exit 2 ;; esac
case "$KANBAN_UI_PROXY_IP" in
  (*[!0-9.]*|'') echo "invalid proxy ip" >&2; exit 2 ;;
esac

# -w serialises against Docker's own concurrent rule writes (observed live
# on this host: per-command iptables-nft calls race with the daemon during
# container bring-up). 30s covers daemon-start bursts.
ipt() { iptables -w 30 "$@"; }

CHAIN=DOCKER-USER-KANBAN
JUMP_COMMENT=kanban-ui-jump
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
  if ipt -n -L "${CHAIN}" >/dev/null 2>&1; then
    ipt -F "${CHAIN}" && ipt -X "${CHAIN}"
    removed=1
  fi
  if [ "$removed" = 1 ]; then
    echo "torn down ${CHAIN} (jumps + chain)"
  else
    echo "no ${CHAIN} rules present"
  fi
  exit 0
fi

resolve_ip() {
  docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' \
    "$KANBAN_UI_CONTAINER" 2>/dev/null | awk '{print $1}'
}

ip=$(resolve_ip)
if [ -z "$ip" ]; then
  # Boot-race tolerance (systemd path): the container (restart policy
  # unless-stopped) comes up alongside the daemon; give it up to 30s.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 3
    ip=$(resolve_ip)
    [ -n "$ip" ] && break
  done
fi
if [ -z "$ip" ]; then
  if [ "${1:-}" = "--check" ]; then
    echo "container $KANBAN_UI_CONTAINER not running; nothing enforced" >&2
    exit 44
  fi
  echo "container $KANBAN_UI_CONTAINER not running; nothing published to enforce"
  exit 0
fi

jump_spec() {
  echo "-d ${ip}/32 -p tcp -m tcp --dport ${KANBAN_UI_PORT} -m comment --comment ${JUMP_COMMENT}"
}

all_rules_live() {
  local r
  # -C prints the rule on success; stdout must stay quiet (the Ansible task
  # uses only the exit code).
  # shellcheck disable=SC2086  # spec is deliberately word-split into argv
  ipt -C DOCKER-USER $(jump_spec) -j "${CHAIN}" >/dev/null 2>&1 || return 1
  for r in "${RULES[@]}"; do
    # shellcheck disable=SC2086  # rule is deliberately word-split into argv
    ipt -C ${r} >/dev/null 2>&1 || return 1
  done
  return 0
}

if [ "${1:-}" = "--check" ]; then
  all_rules_live && exit 0
  exit 42
fi

if all_rules_live; then
  # Exact managed state already live: touch nothing (byte-stable rerun).
  echo "DOCKER-USER-KANBAN already converged for ${ip}:${KANBAN_UI_PORT} (proxy ${KANBAN_UI_PROXY_IP})"
  exit 0
fi

# Chain must exist before anything jumps to it.
ipt -n -L "${CHAIN}" >/dev/null 2>&1 || ipt -N "${CHAIN}" || exit 1

# Remove every existing jump into our chain (stale IP variants included);
# re-added below only after the chain is fully populated, so there is no
# window where transit traffic meets a partially built chain.
while read -r r; do
  [ -n "$r" ] || continue
  # shellcheck disable=SC2086  # rule spec must be separate argv words
  ipt -D DOCKER-USER ${r#-A DOCKER-USER } || true
done < <(ipt -S DOCKER-USER | grep -- "-j ${CHAIN}$" || true)

# Populate the chain; -A order guarantees the terminal DROP is last.
ipt -F "${CHAIN}" || exit 1
for r in "${RULES[@]}"; do
  # shellcheck disable=SC2086  # rule is deliberately word-split into argv
  ipt -A ${r} || exit 1
done

# Managed jump at the head of DOCKER-USER (Docker guidance: user rules
# precede everything else there), added LAST so it never sees an empty chain.
# shellcheck disable=SC2086  # spec is deliberately word-split into argv
ipt -I DOCKER-USER 1 $(jump_spec) -j "${CHAIN}" || exit 1

echo "converged DOCKER-USER-KANBAN for ${ip}:${KANBAN_UI_PORT} (proxy ${KANBAN_UI_PROXY_IP})"
