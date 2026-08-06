#!/usr/bin/env bash
# Onboards a new host: adds it to existing inventory.yaml groups, bootstraps
# the virtuajimmy agent user via the host's original build credentials, then
# runs os_setup.yaml against it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANSIBLE_DIR="$(cd "$SCRIPT_DIR/ansible" && pwd)"
INVENTORY_FILE="$ANSIBLE_DIR/inventory.yaml"
VAULT_KEY="$HOME/ansible_key"

read -rp "Hostname or IP: " HOST
[[ -z "$HOST" ]] && { echo "Hostname/IP is required." >&2; exit 1; }

read -rp "Build user (existing SSH account on the host): " BUILD_USER
[[ -z "$BUILD_USER" ]] && { echo "User is required." >&2; exit 1; }

read -rp "Path to private key for that user: " KEY_PATH
[[ -z "$KEY_PATH" ]] && { echo "Key path is required." >&2; exit 1; }
KEY_PATH="${KEY_PATH/#\~/$HOME}"
[[ -f "$KEY_PATH" ]] || { echo "Key file not found: $KEY_PATH" >&2; exit 1; }
chmod 600 "$KEY_PATH"

read -rp "Role(s) to add under in inventory.yaml (space/comma separated) [ubuntu]: " ROLES_INPUT
ROLES_INPUT="${ROLES_INPUT:-ubuntu}"
IFS=', ' read -ra ROLES <<< "$ROLES_INPUT"

mapfile -t VALID_GROUPS < <(grep -E '^[A-Za-z0-9_]+:$' "$INVENTORY_FILE" | sed 's/:$//')
for role in "${ROLES[@]}"; do
  if ! printf '%s\n' "${VALID_GROUPS[@]}" | grep -qx "$role"; then
    echo "ERROR: group '$role' does not exist in inventory.yaml. This script only adds to existing groups." >&2
    echo "Valid groups: ${VALID_GROUPS[*]}" >&2
    exit 1
  fi
done

echo
echo "Host:  $HOST"
echo "User:  $BUILD_USER"
echo "Key:   $KEY_PATH"
echo "Roles: ${ROLES[*]}"
read -rp "Proceed with inventory update + os_setup run? [y/N] " CONFIRM
[[ "$CONFIRM" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# Insert the host under each requested group's existing "hosts:" block, preserving file formatting.
for role in "${ROLES[@]}"; do
  python3 - "$INVENTORY_FILE" "$role" "$HOST" << 'PYEOF'
import re
import sys

inventory_path, group, host = sys.argv[1], sys.argv[2], sys.argv[3]

with open(inventory_path) as f:
    lines = f.readlines()

group_header = f"{group}:\n"
start = next((i for i, line in enumerate(lines) if line == group_header), None)
if start is None:
    print(f"ERROR: group '{group}' not found", file=sys.stderr)
    sys.exit(1)

end = len(lines)
for i in range(start + 1, len(lines)):
    stripped = lines[i].rstrip("\n")
    if stripped and not stripped.startswith(" ") and not stripped.startswith("#"):
        end = i
        break

hosts_idx = next((i for i in range(start + 1, end) if re.match(r'^\s{2}hosts:\s*$', lines[i])), None)
if hosts_idx is None:
    print(f"ERROR: no 'hosts:' key found under group '{group}'", file=sys.stderr)
    sys.exit(1)

host_pattern = re.compile(rf'^\s*{re.escape(host)}:\s*$')
if any(host_pattern.match(lines[i]) for i in range(hosts_idx + 1, end)):
    print(f"SKIP: '{host}' already present under group '{group}'")
    sys.exit(0)

indent = "    "
for i in range(hosts_idx + 1, end):
    m = re.match(r'^(\s+)\S', lines[i])
    if m:
        indent = m.group(1)
        break

lines.insert(hosts_idx + 1, f"{indent}{host}:\n")
with open(inventory_path, "w") as f:
    f.writelines(lines)
print(f"ADDED: '{host}' under group '{group}'")
PYEOF
done

cd "$ANSIBLE_DIR"

echo
echo "==> Bootstrapping virtuajimmy agent user on $HOST"
ansible-playbook -i inventory.yaml deploy_agent_user.yaml \
  --limit "$HOST" \
  --tags agent_user \
  -e ansible_user="$BUILD_USER" \
  -e ansible_ssh_private_key_file="$KEY_PATH" \
  --vault-password-file "$VAULT_KEY"

echo
echo "==> Verifying agent user connectivity"
ansible "$HOST" -i inventory.yaml -m ping --vault-password-file "$VAULT_KEY"

echo
echo "==> Running os_setup.yaml on $HOST"
ansible-playbook -i inventory.yaml os_setup.yaml \
  --limit "$HOST" \
  --vault-password-file "$VAULT_KEY"

echo
echo "Onboarding complete for $HOST."
