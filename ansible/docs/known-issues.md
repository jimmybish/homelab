# Known Issues

Quick-reference runbook for recurring issues. When responding to an alert or troubleshooting a service, check here first before deep-diving.

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
