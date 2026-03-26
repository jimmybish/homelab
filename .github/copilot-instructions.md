This is a homelab infrastructure-as-code repository managed with Ansible. It deploys and configures Docker-based services across Proxmox VMs and LXC containers.

## Key Facts

- All Ansible work lives under `ansible/` — playbooks, roles, inventory, and group_vars
- Services are deployed as Docker containers via Ansible roles
- Secrets are encrypted with Ansible Vault (key at `~/ansible_key`)
- No `ansible.cfg` exists — **always** pass `-i inventory.yaml` when running playbooks
- Standard run command: `ansible-playbook -i inventory.yaml <playbook>.yaml --vault-password-file ~/ansible_key`
- Reverse proxy is SWAG with two instances: `swag_internal` (LAN) and `swag_external` (internet)
- DNS is managed via pfSense API
- Homepage dashboard aggregates all services
- Proxmox hosts the VMs and LXC containers

## Working Style

- Keep answers short, factual, and actionable
- Read existing code before suggesting changes
- Use Context7 to look up documentation when unsure about module parameters, APIs, or syntax
- Use skills from `.github/skills/` when performing tasks that match their descriptions

## Agent Delegation

Treat each agent as a subject matter expert. When a task involves their domain, delegate the work to them or consult them for advice before proceeding.

| Agent | Domain | When to invoke |
|-------|--------|---------------|
| `ansible_admin` | Ansible playbooks, roles, inventory, Docker deployments, automation | Writing/editing playbooks, creating roles, troubleshooting Ansible, managing containers |
| `proxmox_admin` | Proxmox VE, VMs, LXC containers, cluster resources | Creating/managing VMs or containers, adjusting resources, Proxmox troubleshooting |
| `grafana_admin` | Grafana, Prometheus, Loki, alerting, observability | Querying dashboards, exploring metrics/logs, checking alerts, managing incidents |
| `home_assistant_admin` | Home Assistant, smart home devices, automations | Checking device state, calling services, creating automations, managing dashboards |

- **Delegate** when the task is clearly within an agent's domain — let them handle it end-to-end
- **Consult** when you need domain expertise to inform a broader task — ask the agent for advice, then continue
- **Improve** when a task hits issues or requires workarounds — update the relevant `.github/agents/*.agent.md` or `.github/skills/*/SKILL.md` with the learning so future runs are smoother
