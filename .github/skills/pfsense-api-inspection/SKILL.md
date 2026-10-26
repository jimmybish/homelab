---
name: pfsense-api-inspection
description: 'Use when: troubleshooting network/routing/DNS/VPN issues that may involve pfSense rules, NAT port forwards, active states, or dropped packets. Covers read-only inspection of firewall config and logs via the pfSense REST API v2. All operations are GET-only and non-mutating.'
---

# pfSense API Inspection (read-only)

Read-only pfSense investigation via its REST API v2. Use this when troubleshooting connectivity problems where you need to see the current firewall rules, NAT port forwards, active state table, or firewall log without touching the config.

## Access & Guardrails

- **Only GET requests are permitted in this inspection workflow.** Do not assume the API key itself is read-only; other repository automation uses it for mutating DNS operations.
- Do **not** attempt POST/PUT/PATCH/DELETE. If a mutating change is genuinely required, do not make it — file a kanban card for `virtuajimmy` on the `homelab` board instead (see When to Escalate).
- **API key:** read from the file `/run/secrets/pfsense_api_key` (mounted read-only into the sandbox). Never log it, echo it, `cat` the secret file to stdout, export it to the shell environment, place it on a command line, or commit it.
- **Base URL:** read from the environment variable `PFSENSE_API_BASE` at run time (provisioned by the operator from the approved config source: the Ansible vault variable `internal_pfsense_url` in `ansible/group_vars/all/vars.yaml`). Never hard-code or print the hostname; never commit it into this skill or any repo file. If the variable is unset, stop and report rather than guessing the endpoint.
- **Auth header:** `X-API-Key: <contents of /run/secrets/pfsense_api_key>`
- The LAN interface uses a self-signed certificate, so always pass `-k` to curl.

## Secure Request Workflow

Pass the header via a curl `--config -` heredoc so the key never appears on the command line or in shell history. Examples use `${PFSENSE_API_BASE:?}` so an unset base URL fails loudly instead of hitting a guessed endpoint. Never substitute the literal hostname into commands, cards, or logs:

```bash
curl -sk --config - "${PFSENSE_API_BASE:?PFSENSE_API_BASE unset}/api/v2/firewall/rules" <<EOF
header = "X-API-Key: $(cat /run/secrets/pfsense_api_key)"
EOF
```

Responses use the envelope `{"code":..., "status":..., "response_id":..., "message":..., "data":...}` — check `status` first; `data` holds the payload. Pipe through `python3 -m json.tool` (with `head`) for readability. On 401/403, stop and report — do not retry with other keys or endpoints.

## Useful GET Endpoints

All paths are relative to `${PFSENSE_API_BASE}/api/v2/`.

### Firewall Rules

```bash
curl -sk --config - "${PFSENSE_API_BASE:?PFSENSE_API_BASE unset}/api/v2/firewall/rules" <<EOF
header = "X-API-Key: $(cat /run/secrets/pfsense_api_key)"
EOF
```

Returns every rule across all interfaces. Fields worth inspecting per rule:
- `interface` — string or list (e.g. `lan`, `wan`, `openvpn`, `opt3`, `opt4`)
- `type` — `pass`, `block`, `reject`
- `disabled` — filter these out for the active picture
- `ipprotocol` — `inet` (IPv4), `inet6` (IPv6), `inet46` (both). Missing v6 rules are a common gotcha.
- `protocol`, `source`, `destination`, `destination_port`, `descr`

### NAT Port Forwards

```bash
curl -sk --config - "${PFSENSE_API_BASE:?PFSENSE_API_BASE unset}/api/v2/firewall/nat/port_forwards" <<EOF
header = "X-API-Key: $(cat /run/secrets/pfsense_api_key)"
EOF
```

Key fields per entry:
- `interface`, `ipprotocol`, `protocol`
- `destination`, `destination_port` — what the outside sees
- `target`, `local_port` — where the traffic is DNAT'd to
- `disabled`, `descr`

### Active State Table

```bash
curl -sk --config - "${PFSENSE_API_BASE:?PFSENSE_API_BASE unset}/api/v2/firewall/states?limit=5000" <<EOF
header = "X-API-Key: $(cat /run/secrets/pfsense_api_key)"
EOF
```

Each entry represents a live flow through pfSense. Fields:
- `interface`, `direction`, `protocol`
- `source`, `destination` (each `ip:port`)
- `state` — e.g. `ESTABLISHED:ESTABLISHED`, `SINGLE:NO_TRAFFIC`, `NO_TRAFFIC:SINGLE`
- `packets_in`/`packets_out`, `bytes_in`/`bytes_out`, `age`, `expires_in`

The state table is gold for confirming whether traffic is reaching pfSense at all. If there's no state for the flow you expect, traffic isn't arriving.

### Firewall Logs

```bash
curl -sk --config - "${PFSENSE_API_BASE:?PFSENSE_API_BASE unset}/api/v2/status/logs/firewall?limit=2000" <<EOF
header = "X-API-Key: $(cat /run/secrets/pfsense_api_key)"
EOF
```

Returns raw pf log lines. Only **default-deny** and **rules with logging enabled** appear here. A missing log entry doesn't necessarily mean no drop — but combined with an absent state entry it does.

Log line format (pf CSV):

```
Aug 21 18:11:44 router filterlog[3904]: 1,6,,1000000103,igc0,match,block,in,4,0x0,,235,36374,0,none,6,tcp,40,<src_ip>,<dst_ip>,<sport>,<dport>,0,...
```

Key positions:
- Field 5: interface (e.g. `igc0` = WAN, `igc1` = LAN)
- Field 7: action (`block`, `pass`)
- Field 8: direction (`in`, `out`)
- Field 17: protocol (`tcp`, `udp`, `icmp`)
- Fields 19,20: src IP, dst IP
- Fields 21,22: src port, dst port

## Typical Investigation Patterns

### "Is external traffic reaching a service behind pfSense?"

1. Fetch `firewall/nat/port_forwards`, confirm the WAN forward exists and is not disabled
2. Fetch `firewall/states`, grep for the destination port or target IP
   - **State exists, bytes bidirectional** → traffic is flowing, problem is downstream (host, container, app)
   - **State exists, `NO_TRAFFIC` on out side** → DNAT worked, target isn't replying
   - **No state at all** → traffic isn't arriving at pfSense
3. Fetch `status/logs/firewall`, grep for the destination port or source IP → look for explicit blocks

### "Is a client's DNS query being blocked?"

1. Check `firewall/rules` for the source interface — look for `dst=127.0.0.1 dport=53` (the standard DNS redirect NAT) and confirm it's not disabled
2. Check `status/logs/firewall` for `block` entries mentioning port 53 and the client's IP

### "Does the WAN accept IPv6 for a given port?"

pfSense NAT is IPv4-only. IPv6 must be handled by a separate pass rule on WAN with `ipprotocol: inet6`. Grep the rules dump:

```python
import json, sys
d = json.load(sys.stdin)
for r in d['data']:
    if r.get('disabled'): continue
    if r.get('ipprotocol') == 'inet6' and 'wan' in str(r.get('interface')):
        print(r.get('protocol'), r.get('destination_port'), r.get('descr'))
```

If nothing prints, no v6 traffic will get through the WAN.

### JSON Field Gotchas

- `interface` can be a **string** or a **list** — always normalise
- `protocol` and `destination_port` are frequently `None` — guard formatters against it
- Prefer `str(...)` when printing rule fields for consistent output

## When to Escalate

If read-only inspection reveals a rule needs to be added or changed:

1. Report the finding clearly (which rule/NAT, which fields, why)
2. File a kanban card for `virtuajimmy` on the `homelab` board using the board's structured `kanban_create` tool, passing title, body, and assignee as structured tool arguments. Never build a shell command from the finding text: model- or user-derived strings embedded in shell syntax are a command-injection vector even when quoted, and the CLI form bypasses the structured board interface. pfSense config is Ansible-managed and out of Dexter's scope
3. Do not attempt mutations through the API yourself, and do not elevate the key

## Related

- Write access to the pfSense API lives only in the Ansible repo (`tasks/pfsense_dns_*.yaml`) owned by `virtuajimmy` — that code path is not part of this workflow.
