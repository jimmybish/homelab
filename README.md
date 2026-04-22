## Jimmy's Home Lab
All the self-hosted thangs, mostly controlled by Ansible.

**This is a work in progress - not all deployed apps are via Ansible yet, not all roles are tested, and there's probably smarter ways of doing things. Always learning!**

### GitHub Copilot Agents

I use GitHub Copilot with custom agents and reusable skills to manage the homelab.

**Agents** have domain-specific knowledge and tool access, defined in [.github/agents](.github/agents):

- **ansible_admin** — Deploys and manages services via Ansible. Follows a [new service runbook](ansible/docs/new-service-runbook.md) for consistency: researches official docs, uses LinuxServer.io images where available, and wires up reverse proxies, Homepage entries, and health checks automatically.
- **grafana_admin** — Queries dashboards, explores Prometheus/Loki metrics, checks alerts, and manages incidents via MCP.
- **home_assistant_admin** — Manages Home Assistant entities, automations, scenes, and dashboards via MCP.
- **proxmox_admin** — Manages VMs, LXC containers, and cluster resources via SSH.
- **plexibot** — Plex ecosystem specialist (Plex, Tautulli, Arr stack, Tracearr, Loki logs). Sandboxed — cannot delegate to other agents. Also available as a Discord bot via n8n.

**Skills** are step-by-step instructions in [.github/skills](.github/skills) that Copilot loads on demand when a task matches their domain. They encode repeatable patterns — like scaffolding an Ansible role, templating a Docker Compose file, or wiring up a SWAG proxy config — so each deployment follows the same conventions without re-explaining the process. Current skills cover: application research, role scaffolding, Docker deployment tasks, Compose templating, vault secrets, Homepage integration, NGINX/SWAG proxy configs, pfSense DNS management, UFW firewall rules, service health checks, playbook creation/testing, n8n workflow deployment, change logging, and chat room communication formatting.

### Change Logging

All agents are required to log every change they make using the `change-logging` skill. Logs are written to the `changelogs/` directory (gitignored) in a CAB-style change ticket format covering: summary, files changed with before/after values, commands run on remote hosts, and a rollback plan.

- **File location:** `changelogs/week-YYYY-MM-DD/log_YYMMDD.md` (week folder = most recent Sunday, log file = today's date)
- **Automation:** An n8n workflow creates the weekly folder every Sunday at midnight
- **Scope:** All agents — not just infrastructure ones

**MCP Servers** provide tool access to external services:

- [Context7](https://context7.com/) — Up-to-date library and framework documentation lookup
- [Grafana MCP](https://github.com/grafana/mcp-grafana) — Dashboards, Prometheus/Loki queries, alerting, incidents, and on-call management
- [Home Assistant MCP](https://www.home-assistant.io/integrations/mcp_server/) — Smart home entity state, service calls, and automations
- [Playwright MCP](https://github.com/microsoft/playwright-mcp) — Browser automation for UI testing and validation
- [Chrome DevTools MCP](https://github.com/nicholasgriffintn/chrome-devtools-mcp) — Browser inspection and debugging

### Discord Integration

Two Discord bots interface with the homelab via [n8n](https://n8n.io/) workflows:

- **VirtuaJimmy** — General-purpose Copilot bot. Responds to @mentions in any channel it's in, creates threaded conversations with session persistence, and automatically investigates Grafana alerts.
- **Plexibot** — Plex-focused bot using the `--agent plexibot` flag. Responds to @mentions, scoped to Plex/Arr/Tracearr/log queries only.

Both bots use mention-based routing — each only responds when @mentioned directly, and thread replies continue within the same Copilot session. Supporting workflows handle "still thinking" status updates and stale thread cleanup across both channels.

### Deployed Services
- Docker
- [Homepage](https://gethomepage.dev/)
- **Media:**
  - [Jellyfin](https://hub.docker.com/r/linuxserver/jellyfin), [Plex](https://hub.docker.com/r/linuxserver/plex), [Tautulli](https://tautulli.com/)
  - [Jellyseerr](https://hub.docker.com/r/fallenbagel/jellyseerr), [Overseerr](https://hub.docker.com/r/linuxserver/overseerr)
  - [Maintainerr](https://github.com/jorenn92/maintainerr), [JellyPlex-Watched](https://github.com/luigi311/JellyPlex-Watched), [Tracearr](https://github.com/connorgallopo/tracearr)
- **Arr Suite (media automation):**
  - [Sonarr](https://hub.docker.com/r/linuxserver/sonarr), [Radarr](https://hub.docker.com/r/linuxserver/radarr), [Prowlarr](https://hub.docker.com/r/linuxserver/prowlarr), [Lidarr](https://hub.docker.com/r/linuxserver/lidarr)
  - [SABnzbd](https://hub.docker.com/r/linuxserver/sabnzbd), [qBittorrent](https://hub.docker.com/r/linuxserver/qbittorrent)
- **Productivity:** [Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx), [n8n](https://n8n.io/)
- **Monitoring:** [Grafana](https://grafana.com/), [Prometheus](https://prometheus.io/), [Loki](https://grafana.com/oss/loki/), [InfluxDB](https://www.influxdata.com/), [Alloy](https://grafana.com/oss/alloy/) (all hosts — collects logs via journald/Docker and metrics via built-in node exporter, pushed to Loki/Prometheus via remote write)
- **Infrastructure:** [SWAG](https://docs.linuxserver.io/general/swag/) (internal & external reverse proxies)

### Hardware
- pfSense router (Intel Celeron J4125 Mini PC, 4x 2.5gbe)
- 2x Intel NUC 12 Pro — Proxmox HA cluster
- TP-Link Omada (24-port POE switch + APs)
- QNAP TS-664 NAS + TR-004 (6x 8TB + 4x 4TB, RAID 5)

![image](images/Network-rack.jpg)

### Software
Services run as Docker Compose stacks on VMs and LXC containers across the Proxmox hosts, with a couple of storage-heavy or Coral TPU services on the QNAP NAS.

- Default OS: Ubuntu Server 22.04 for both VMs and LXCs.
- LXCs built from [community scripts](https://community-scripts.github.io/ProxmoxVE/scripts?id=ubuntu). Jellyfin and Plex are privileged with [hardware acceleration](https://github.com/community-scripts/ProxmoxVE/blob/main/misc/hw-acceleration.sh).
- VMs built from a cloud-init template.
- Proxmox vGPUs configured via [this blog post](https://www.derekseaman.com/2024/07/proxmox-ve-8-2-windows-11-vgpu-vt-d-passthrough-with-intel-alder-lake.html).
- Home Assistant runs the [official HA OS image](https://community-scripts.github.io/ProxmoxVE/scripts?id=haos-vm), not managed by Ansible.

### Proxy Structure
Two linuxserver/SWAG containers — one for external traffic, one for internal. Both use DNS01 verification against Cloudflare for wildcard LetsEncrypt certificates. Currently a single NGINX proxy host serves all services.

### Variables & Secrets
- Main config: `group_vars/all/vars.yaml`. Per-role config: `roles/[role]/vars/main.yaml`.
- Secrets encrypted with `ansible-vault` (password file in home directory).
- Proxy tasks use the first host in each inventory group as the upstream target.

### Running the Playbook

1. Install `dnspython`: `pipx inject ansible dnspython`
2. Create an `ansible-vault` password file.
3. Configure variables in `group_vars/all/vars.yaml` (encrypt secrets with vault).
4. Run:
```
ansible-playbook master_playbook.yaml -i inventory.yaml --vault-password-file ~/ansible_key
```

### Monitoring
- Grafana, Prometheus, Loki, and InfluxDB are deployed together as a monitoring stack.
- Alloy runs on all Ubuntu hosts, shipping Docker container logs and system journal to Loki.
- Node Exporter runs on all Ubuntu hosts. Prometheus scrapes via FQDNs.
- cAdvisor outputs Docker metrics via Prometheus.
- InfluxDB receives metrics from Proxmox hosts and Home Assistant.
