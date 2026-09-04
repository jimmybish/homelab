---
name: pfsense-api-inspection
description: 'Use when: troubleshooting network/routing/DNS/VPN issues that may involve pfSense rules, NAT port forwards, active states, or dropped packets. Covers read-only inspection of firewall config and logs via the pfSense REST API v2. All operations are GET-only and non-mutating.'
---

# pfSense API Inspection (read-only)

Read-only pfSense investigation via its REST API v2. Use this when troubleshooting connectivity problems where you need to see the current firewall rules, NAT port forwards, active state table, or firewall log without touching the config.

## Access & Guardrails

- **Only GET requests are permitted in this inspection workflow.** Do not assume the API key itself is read-only; other repository automation uses it for mutating DNS operations.
- Do **not** attempt POST/PUT/PATCH/DELETE. If a mutating change is genuinely required, ask the user to make it in the pfSense UI or delegate it to the appropriate managed automation.
- Never log the API key. Never commit it. Use `pfsense_api_key` from Ansible Vault only.
- **Base URL:** `https://{{ groups['pfsense_host'][0] }}` — resolves to `router` in inventory.
- **Auth header:** `X-API-Key: {{ pfsense_api_key }}`
- The LAN interface uses a self-signed certificate, so the request task sets `validate_certs: false`.

## Secure Request Workflow

Keep the decrypted key inside Ansible and suppress the authenticated request task's output. Create this temporary one-shot playbook:

```yaml
---
- hosts: localhost
  gather_facts: false
  tasks:
    - name: Validate that the requested operation is read-only
      ansible.builtin.assert:
        that:
          - pfsense_endpoint is defined
          - pfsense_endpoint | length > 0
          - pfsense_method | default('GET') == 'GET'
        fail_msg: "Only GET requests are allowed by this inspection workflow"

    - name: Query the pfSense API without exposing credentials
      ansible.builtin.uri:
        url: "https://{{ groups['pfsense_host'][0] }}/api/v2/{{ pfsense_endpoint }}"
        method: GET
        headers:
          X-API-Key: "{{ pfsense_api_key }}"
        return_content: true
        validate_certs: false
      register: pfsense_response
      no_log: true

    - name: Display the API response
      ansible.builtin.debug:
        var: pfsense_response.json
```

Save it as `/tmp/pfsense_get.yaml`, then pass only the relative endpoint:

```bash
cd ansible
ansible-playbook -i inventory.yaml /tmp/pfsense_get.yaml \
  --vault-password-file ~/ansible_key \
  -e 'pfsense_endpoint=firewall/rules'
```

Delete `/tmp/pfsense_get.yaml` after the investigation. Never use Ansible `debug` to print `pfsense_api_key`, export it to the shell, place it on a command line, or write it to a temporary file.

## Useful GET Endpoints

All paths are relative to `https://router/api/v2/`.

### Firewall Rules

```bash
ansible-playbook -i inventory.yaml /tmp/pfsense_get.yaml --vault-password-file ~/ansible_key -e 'pfsense_endpoint=firewall/rules'
```

Returns every rule across all interfaces. Fields worth inspecting per rule:
- `interface` — string or list (e.g. `lan`, `wan`, `openvpn`, `opt3`, `opt4`)
- `type` — `pass`, `block`, `reject`
- `disabled` — filter these out for the active picture
- `ipprotocol` — `inet` (IPv4), `inet6` (IPv6), `inet46` (both). Missing v6 rules are a common gotcha.
- `protocol`, `source`, `destination`, `destination_port`, `descr`

### NAT Port Forwards

```bash
ansible-playbook -i inventory.yaml /tmp/pfsense_get.yaml --vault-password-file ~/ansible_key -e 'pfsense_endpoint=firewall/nat/port_forwards'
```

Key fields per entry:
- `interface`, `ipprotocol`, `protocol`
- `destination`, `destination_port` — what the outside sees
- `target`, `local_port` — where the traffic is DNAT'd to
- `disabled`, `descr`

### Active State Table

```bash
ansible-playbook -i inventory.yaml /tmp/pfsense_get.yaml --vault-password-file ~/ansible_key -e 'pfsense_endpoint=firewall/states?limit=5000'
```

Each entry represents a live flow through pfSense. Fields:
- `interface`, `direction`, `protocol`
- `source`, `destination` (each `ip:port`)
- `state` — e.g. `ESTABLISHED:ESTABLISHED`, `SINGLE:NO_TRAFFIC`, `NO_TRAFFIC:SINGLE`
- `packets_in`/`packets_out`, `bytes_in`/`bytes_out`, `age`, `expires_in`

The state table is gold for confirming whether traffic is reaching pfSense at all. If there's no state for the flow you expect, traffic isn't arriving.

### Firewall Logs

```bash
ansible-playbook -i inventory.yaml /tmp/pfsense_get.yaml --vault-password-file ~/ansible_key -e 'pfsense_endpoint=status/logs/firewall?limit=2000'
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

1. Report the finding clearly to the user (which rule/NAT, which fields, why)
2. Ask them to make the change in the pfSense UI, or elevate the API key (which is a separate manual step)
3. Do not attempt to bypass the read-only restriction

## Related

- `known-issues.md` covers concrete scenarios where this skill has been used (e.g. wg-easy handshake failure over carrier NAT64).
- The Ansible role tasks `tasks/pfsense_dns_*.yaml` are the only place where write access to the API is (currently) exercised — those use a different code path and don't count as ad-hoc troubleshooting.
