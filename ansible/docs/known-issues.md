# Known Issues

Quick-reference runbook for recurring issues. When responding to an alert or troubleshooting a service, check here first before deep-diving.

---

## Sparky "Node Exporter Scrape Failing" (formerly "Host Down")

**Alert:** `Node Exporter Scrape Failing` (Grafana, uid `host-down-1`, severity: critical) and `Sparky Unreachable (No Logs)` (uid `sparky-unreachable-1`, severity: critical)
**Symptoms:** Repeated flapping firing/resolving of the scrape-failure alert for `sparky.jimmynrose.id.au:9100` / `job=node_exporter`, often many times per hour, while Sparky remains reachable over SSH and its Docker workloads keep running.

**Root cause:** Alloy on Sparky embeds `prometheus.exporter.unix` and self-scrapes it, then relabels the result to look like an external `<host>:9100` target — there is no actual listener being probed. Under Sparky's vLLM workload (which by design uses most of the host's memory), Alloy's own process periodically stalls long enough that it can't service its internal scrape request before the timeout, producing `up=0` with zero samples. Successful scrapes complete in ~35ms; failed ones return nothing after exactly the configured `scrape_timeout` — that bimodal pattern is the signature of this issue (as opposed to a real network/host outage, which produces sustained connection failures, not alternating fast/timeout results).

**A Discord-linked "fix" that did NOT work:** an earlier response diagnosed this as a missing `node_exporter` systemd unit/package and installed the Ubuntu `prometheus-node-exporter` apt package standalone. This did not help, because Alloy was never scraping that package — it was always scraping its own embedded exporter. Prometheus/Grafana evidence (raw `scrape_duration_seconds`, `node_exporter_build_info` build tags) directly disproved that diagnosis: alerts kept firing identically after the package install. Don't repeat this fix — check the actual Alloy config (`ansible/roles/alloy/templates/alloy_config.alloy.j2`) and `alloy_node_exporter_mode` before assuming a missing package.

### Fix (already applied for Sparky)

Sparky is configured (via `ansible/group_vars/spark/vars.yaml` → `alloy_node_exporter_mode: "standalone"`) to run the Ubuntu `prometheus-node-exporter` package as its own systemd service, with Alloy scraping it over loopback (`127.0.0.1:9100`) instead of running the exporter embedded in its own process. This means a stalled/busy Alloy process can no longer block metrics collection. The `job`/`instance` relabeling is unchanged, so the alert rule and dashboards required no changes.

If this recurs on Sparky (or starts on another host), redeploy with:
```
ansible-playbook -i inventory.yaml os_setup.yaml --vault-password-file ~/ansible_key --limit <host>
```
after setting `alloy_node_exporter_mode: "standalone"` in that host's `group_vars`.

### Alert design — two separate signals

Because a scrape failure doesn't necessarily mean the host is down, this alert space is split into two rules:

- **`Node Exporter Scrape Failing`** (`host-down-1`) — fires when Prometheus can't scrape any non-cAdvisor job/instance. This can mean the host is unreachable, *or* that the exporter/agent on that host is too busy to respond in time. Treat it as "metrics collection is broken here", not "the host is down".
- **`Sparky Unreachable (No Logs)`** (`sparky-unreachable-1`) — Loki-based, fires only if Sparky sends zero log lines (journal or Docker) in 5 minutes (`absent_over_time({host="sparky"}[5m])`). Log shipping kept working throughout every occurrence of the scrape-failure issue, so this is a much stronger true host-down signal for Sparky specifically. It's intentionally scoped to Sparky only (the one host that has shown this failure mode) rather than generalized fleet-wide.

**Triage:** if `Node Exporter Scrape Failing` fires for Sparky but `Sparky Unreachable (No Logs)` stays Normal, the host is alive and it's a scrape/agent issue, not an outage.

---

## N8N Discord Trigger Reconnect Loop

**Alert:** `N8N Discord Trigger Reconnect Loop` (Grafana, severity: warning)
**Symptoms:** Discord bot (VirtuaJimmy / Plexibot) stops responding — no 👀 reaction, no replies. Loki shows repeated `removing trigger node` → `Connected to IPC server` log lines from the `n8n` container.
**Root cause:** The Discord trigger community node (`n8n-nodes-discord-trigger`) uses IPC to communicate between the bot process and N8N trigger nodes. Rapid workflow updates (e.g. multiple playbook runs) or activation/deactivation cycles cause the IPC state to desynchronize — the bot receives Discord messages but the trigger node's listener is no longer registered to process them.

### Fix

1. Restart the N8N container:
   ```
   ssh docker-2 'docker restart n8n'
   ```
2. Wait ~15 seconds for bots to reconnect, then verify in logs:
   ```
   ssh docker-2 'docker logs --tail 20 n8n 2>&1 | grep -E "ready and listening|Activated workflow"'
   ```
   Expected: both bots report "ready and listening for messages" and all workflows show "Activated".
3. Test by tagging @VirtuaJimmy in the Alerts channel — expect a 👀 reaction within a few seconds.

### Prevention

- Avoid running `deploy_n8n.yaml` repeatedly in quick succession — each run triggers a workflow deactivate/reactivate cycle.
- The `active: true` flag on the "Discord VirtuaJimmy" workflow entry (added 2026-04-22) ensures the workflow is always re-activated after deployment.

---

## Container Down (During Deployment)

**Alert:** `Container Down` (Grafana, severity: critical)
**Symptoms:** Alert fires for a specific container on a host, but a `deploy_*.yaml` playbook was recently run against that host.
**Root cause:** Ansible playbooks restart containers during deployment (docker-compose up). The `container_last_seen` metric goes stale during the restart window, triggering the alert after the 2-minute `for` duration.

### Triage

1. **Check if a playbook was recently run.** If the alert fires within minutes of a deployment, it's almost certainly transient — wait 2-3 minutes for the container to come back and the alert to auto-resolve.
2. **If no recent deployment**, SSH to the host and check:
   ```
   ssh <host> 'docker ps -a --filter name=<container>'
   ```
   - If the container is restarting (status `Restarting`), check for crash loops:
     ```
     ssh <host> 'docker logs --tail 50 <container> 2>&1'
     ```
   - If the container is stopped/exited, try restarting it:
     ```
     ssh <host> 'cd /opt/docker/<service> && docker compose up -d'
     ```
3. **If the container won't start**, check disk space (`df -h`) and Docker daemon health (`systemctl status docker`).

### Host-to-service reference

| Host | Services |
|------|----------|
| `docker-1` | Sonarr, Radarr, Lidarr, Prowlarr, SABnzbd, qBittorrent, Grafana |
| `docker-2` | Homepage, Paperless, N8N, Maintainerr, Tracearr, JellyPlex-Watched |
| `proxy` | SWAG (internal + external), Overseerr, Jellyseerr |
| `jellyfin-lxc` | Plex, Jellyfin |

---

## Proxmox-2 rpool Uncorrectable I/O Failure

**Symptoms:** The first sign is that **all hosts on proxmox-2 stop sending metrics to Grafana**. Shortly after that, Proxmox-2 becomes completely unresponsive. The console shows a hung ZFS task stack (`zio_wait`, `zvol_write_task`, `dmu_tx_hold_write_by_dnode`) followed by repeated messages like `WARNING: Pool 'rpool' has encountered an uncorrectable I/O failure and has been suspended.` The node usually needs a hard power cycle before it responds again.

**Status:** This is a real storage-path failure, not a benign reboot or a thermal pad issue. The 990 Pro temperature trace showed the NVMe was not hot when metrics stopped, and the Asus NUC 12 Pro chassis has identical built-in thermal pads on Proxmox-1 and Proxmox-2.

### Ruled Out

- **Thermal pad differences**: Both NUCs use the same chassis and built-in thermal pads.
- **NVMe overheating**: The temperature graph for Proxmox-2 was only around the mid-40s to low-60s °C when metrics stopped, well below throttle territory.
- **A simple deployment restart**: The node is fully hung and stops serving metrics before the hard power-off.
- **ZFS replication deadlock as the primary explanation**: The console evidence points to an uncorrectable I/O suspension on `rpool`, not just a stuck replication job.

### Yet To Be Implemented

- **Disable NVMe APST / power-state management** by setting `nvme_core.default_ps_max_latency_us=0` on the Proxmox boot cmdline.
- **Update Samsung 990 Pro firmware** on both nodes to the newest available release.
- **Enable persistent journaling** so the last boot's kernel and ZFS messages survive a hard reset.
- **Enable crash capture** with `kdump` or equivalent so a future hang/panic can be inspected.
- **Add storage-path alerting** for NVMe media errors, I/O timeouts, and repeated ZFS suspend events.

### Data Collection Checklist

When the issue is active, collect the following before power-cycling if possible. The metric blackout itself is the earliest signal, so note the exact time Grafana stopped receiving samples from proxmox-2:

1. `zpool status -v rpool` and `zpool events -v`
2. `smartctl -a /dev/nvme0n1` and `nvme smart-log /dev/nvme0`
3. `dmesg | grep -E 'error|fail|nvme|zfs|I/O|timeout|hung'`
4. `journalctl -b -1 -p err --no-pager` if persistent journaling is enabled
5. Grafana/Prometheus temperature graphs for both Proxmox nodes
6. The exact console text showing the first `rpool` suspend message and the hung-task stack

### Next Steps

1. Keep the 68°C alert in place so we still get early warning if temperatures do rise.
2. Apply the NVMe APST change and firmware update before the next heavy write window.
3. Capture the commands above during the next failure so we can confirm whether the trigger is power-state handling, firmware, or another NVMe path issue.

---

## High Error Rate in Logs

**Alert:** `High Error Rate in Logs` (Grafana, severity: warning)
**Symptoms:** Alert fires with labels `host` and `container_name` identifying the source. The alert query already filters out known noise (Loki internals, Grafana provisioning loggers, `context canceled`).

### Triage

1. **Identify the source** from the alert labels: `host` and `container_name`.
2. **Query Loki for the actual error lines** — the alert only counts errors, not the content. Use the `grafana_admin` agent or query directly:
   ```logql
   {container_name="<container>", host="<host>"} | logfmt | level=~"(?i)error|fatal|panic"
   ```
3. **Common false positives:**
   - **Loki/Grafana self-feedback**: The alert query excludes most internal Loki noise, but new internal callers may slip through. If the errors are from `loki` or `grafana` containers and reference internal Go packages (`caller=*.go`), consider adding an exclusion to the alert query in `provisioning_alertrules.yaml.j2`.
   - **Transient network errors**: Brief DNS or connectivity blips can cause error spikes across multiple containers. If errors cluster around the same timestamp and resolve quickly, no action needed.
   - **Container startup errors**: Some services log errors during initialization (e.g. waiting for a database). If the container just restarted, wait a minute and check if errors stop.
4. **If errors are persistent and real**, check the specific service:
   - Look for config changes, failed updates, or resource exhaustion.
   - Check `docker logs --tail 100 <container>` on the host for full context.
   - Restart the container if the errors suggest a stuck state.

---

## WireGuard (wg-easy) Handshake Fails on Mobile Carriers

**Alert / Symptom:** WireGuard client on iOS/Android connects fine from home Wi-Fi but from cellular data (LTE/5G) it loops handshake timeouts. Pings and browsing over the tunnel all fail. `docker exec wg-easy wg show wg0` shows the peer with no `endpoint`, no `latest handshake`, and no `transfer` counters. `tcpdump -i wg0` inside the container shows zero packets.

### Root Cause

Australian mobile carriers (Telstra confirmed, likely others) run their LTE/5G APN as **IPv6-only with 464XLAT/NAT64**. The phone wraps the WireGuard endpoint's IPv4 address into an IPv6 address using the carrier's PLAT prefix (e.g. `2001:8004::/32` for Telstra), sends the UDP packet over v6, and the carrier's PLAT gateway translates it back to v4 at the edge.

The PLAT gateway silently drops UDP traffic on non-standard ports. WireGuard's default **UDP 51820 is blocked**. Only well-known UDP ports (`53`, `443`, `500`, `4500`) survive translation.

### Fix

Move the client-facing WireGuard port to **UDP 4500** (IPsec NAT-T port, always allowed by carrier PLATs) while keeping the container's internal listen port at 51820.

**pfSense NAT Port Forward:**
- Interface: WAN
- Protocol: UDP
- Destination: WAN address
- Destination port: `4500`
- Redirect target IP: proxy host (e.g. `192.168.0.200`)
- Redirect target port: **`51820`** (translate external 4500 → internal 51820)
- Description: `wg-easy WireGuard (carrier-friendly)`

**wg-easy admin UI:**
- WireGuard Host: `vpn.<domain>` (unchanged)
- WireGuard Port: `4500` — this only changes the `Endpoint =` line in generated client configs. The container keeps `ListenPort = 51820` internally.

**iOS client:** Delete the old tunnel, download the new profile from wg-easy (endpoint should read `:4500`), scan the QR code, connect. Handshake completes within seconds.

### Verification

- On pfSense (via API): `firewall/states` should show a UDP flow to `<proxy>:4500` with `bytes_out > 0` (bidirectional traffic)
- On the proxy host: `docker exec wg-easy wg show wg0` shows peer with recent `latest handshake` and non-zero `transfer`
- Inside the container: `tcpdump -i wg0` shows client traffic when you use apps on the phone

### Notes

- The **existing role does not automate this**. The pfSense NAT rule and the wg-easy UI port setting are manual steps. If we ever add multi-carrier VPN support, bake `wg_easy_client_port: 4500` into the role and drive both the container's `WG_PORT` and a shared NAT task.
- Home Wi-Fi over IPv4 works on either port, so home users won't notice — this only bites when a client uses cellular data.
- Also check DNS: iOS on carrier IPv6 will refuse to use a bare IPv4 DNS server if `Allowed IPs` is a split tunnel. Use full tunnel (`0.0.0.0/0, ::/0`) plus `DNS = 192.168.0.1` in the client config.
