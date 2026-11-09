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

### NVMe Baseline for Both Proxmox Nodes (2026-10-21)

User-provided `smartctl -i /dev/nvme0n1` output confirms that both nodes were running firmware **`4B2QJXD7`** at the capture times below. These are pre-update readings; no firmware update was performed as part of recording them. The shared model and firmware do not establish that both nodes' failures have the same root cause.

| Field | proxmox-1 | proxmox-2 |
|-------|-----------|-----------|
| Capture time (AEDT, UTC+11) | 2026-10-21 10:57:07 | 2026-10-21 10:58:05 |
| Model number | Samsung SSD 990 PRO 2TB | Samsung SSD 990 PRO 2TB |
| Serial number | `S7DNNU0Y674859F` | `S7DNNU0Y674865L` |
| Firmware version | `4B2QJXD7` | `4B2QJXD7` |
| Namespace 1 utilization at capture | 740,095,266,816 bytes (740 GB) | 1,182,099,603,456 bytes (1.18 TB) |
| Namespace 1 IEEE EUI-64 | `002538 4651a40dcb` | `002538 4651a40dd1` |

Additional fields were identical on both drives:

| Field | Value |
|-------|-------|
| PCI vendor/subsystem ID | `0x144d` |
| IEEE OUI identifier | `0x002538` |
| Total NVM capacity | 2,000,398,934,016 bytes (2.00 TB) |
| Unallocated NVM capacity | 0 bytes |
| Controller ID | 1 |
| NVMe version | 2.0 |
| Number of namespaces | 1 |
| Namespace 1 size/capacity | 2,000,398,934,016 bytes (2.00 TB) |
| Namespace 1 formatted LBA size | 512 bytes |

### NVMe Firmware Tracking

The baseline above is historical. Current firmware per node:

| Node | Drive serial | Firmware | Updated | Verified |
|------|--------------|----------|---------|----------|
| proxmox-1 | `S7DNNU0Y674859F` | **`8B2QJXD7`** (Samsung, Dec 2025: "To improve read-operation stability") | 2026-11-08, from `4B2QJXD7` | Prometheus `node_nvme_info{firmware_revision="8B2QJXD7"}` from 15:17 AEDT; host rebooted 15:15:20 AEDT (last pre-update sample 14:24 AEDT) |
| proxmox-2 | `S7DNNU0Y674865L` | `4B2QJXD7` (Samsung, Dec 2024) | Not yet updated | Prometheus `node_nvme_info` on 2026-11-08 |

Check current values at any time with `node_nvme_info{instance=~"proxmox-[12].*"}` in Prometheus or `smartctl -i /dev/nvme0n1` on the node.

**Why proxmox-1 was updated:** proxmox-1 hung during the 04:00 PBS backup on 2026-10-18 (~05:46 AEDT) and 2026-10-21 (~05:54 AEDT). Loki retained kernel `nvme nvme0: I/O tag … timeout` READ/WRITE/FLUSH aborts and a controller reset just before logging stopped, each time while an LXC on `local-zfs` was being uploaded. Backups on 2026-10-19 and 2026-10-20 completed. The backup task status `Error: unexpected status` is a consequence of the hang, not its cause.

**What to watch:** while proxmox-2 stays on `4B2QJXD7`, the two nodes give a direct comparison. If proxmox-1 completes nightly backups with no NVMe timeouts and proxmox-2 still has storage hangs, that supports firmware as a factor. If proxmox-1 hangs again with NVMe timeouts on `8B2QJXD7`, firmware alone is not the fix. Note that nightly backups also succeeded on some nights before the update, so a few clean nights is not conclusive.

**Updating a drive:** Samsung's ISO cannot be written with Etcher/DD for these NUCs. Its El Torito and MBR boot records are BIOS-only, and NUC 12 Pro firmware is UEFI-only with no CSM. Format the USB as GPT + FAT32 and copy the ISO contents onto it (it contains `\EFI\BOOT\BOOTX64.EFI`), or use Rufus with the ISO selected (GPT, UEFI non-CSM, FAT32, ISO Image mode). Boot it from the F10 menu. Disable Secure Boot temporarily if it is rejected.

### Ruled Out

- **Thermal pad differences**: Both NUCs use the same chassis and built-in thermal pads.
- **NVMe overheating**: The temperature graph for Proxmox-2 was only around the mid-40s to low-60s °C when metrics stopped, well below throttle territory.
- **A simple deployment restart**: The node is fully hung and stops serving metrics before the hard power-off.
- **ZFS replication deadlock as the primary explanation**: The console evidence points to an uncorrectable I/O suspension on `rpool`, not just a stuck replication job.

### Yet To Be Implemented

- **Disable NVMe APST / power-state management** by setting `nvme_core.default_ps_max_latency_us=0` on the Proxmox boot cmdline. Verified 2026-11-08 (read-only):
  - **proxmox-2: done.** In both the running and configured cmdline; `/sys/module/nvme_core/parameters/default_ps_max_latency_us` = `0`. Applied by the `proxmox` role on 2026-06-14 (backup `/etc/kernel/cmdline.82807.2026-06-14@13:39:13~`), active since the 2026-07-29 reboot. Boots via systemd-boot.
  - **proxmox-1: done, verified after reboot at 2026-11-08 15:43:20 AEDT.** The running cmdline includes `nvme_core.default_ps_max_latency_us=0` and `/sys/module/nvme_core/parameters/default_ps_max_latency_us` reads `0`. Kernel 6.17.13-2-pve, NVMe firmware `8B2QJXD7`, `rpool` ONLINE. Before this it had sysfs value `100000`: proxmox-1 boots via **GRUB** on its ESP (`09C0-9F7F`), and GRUB ignores `/etc/kernel/cmdline`, so the role's original task never applied. The `proxmox` role now also deploys `/etc/default/grub.d/zz-nvme-apst.cfg`, which appends the parameter to `GRUB_CMDLINE_LINUX` (normal and recovery entries), then runs `proxmox-boot-tool refresh` (`update-grub` in GRUB mode). Applied with `deploy_proxmox.yaml --tags nvme_apst` on 2026-11-08; rerunning with that tag prints each node's APST state.
  - `nvme-cli` is not installed on either node, so the APST feature (`nvme get-feature /dev/nvme0 -f 0x0c -H`) has not been read from the controller itself.
  - Because proxmox-1 changed firmware and APST together (APST disabled from 2026-11-08 15:43), the two nodes now have the same APST state but different firmware. A stable proxmox-1 from here on can't be attributed to firmware or APST alone, since both changed on the same day.
- **Update Samsung 990 Pro firmware** on both nodes to the newest available release. proxmox-1 is done (`8B2QJXD7`, 2026-11-08); proxmox-2 is still on `4B2QJXD7`. See NVMe Firmware Tracking above.
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

1. Keep NVMe temperature alerting in place for early warning. As of 2026-10-23 it evaluates only the **Composite** sensor (hwmon `temp1`, which Samsung's 0-70°C operating spec is measured against): `NVMe Drive Temperature High` warns above 70°C for 10 minutes and recovers below 65°C; `NVMe Drive Temperature Critical` fires when the composite reaches the drive-reported warning threshold (~82°C) or the drive asserts its SMART temperature alarm. The previous 68°C rule alerted on the hottest of all sensors, so daily backup-load peaks on Sensor 2 caused firing/recovery noise.
2. Apply the NVMe APST change and firmware update before the next heavy write window. Firmware is updated on proxmox-1 only; decide whether to update proxmox-2 now or keep it on `4B2QJXD7` for a while as a comparison.
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
