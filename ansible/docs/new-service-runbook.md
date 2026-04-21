# New Service Role — Step-by-Step Runbook

Follow these phases **in order** when creating a new Ansible role that deploys a Docker service. Each phase references a skill — read it before executing.

Do NOT skip ahead. Complete each phase before moving to the next. If debugging derails the flow, return to this runbook and resume from the current phase.

---

## Phase 1: Research

**Skill:** `application-research`

- [ ] Find official docs, Docker Hub page, GitHub repo
- [ ] Locate official `docker-compose.yaml` examples
- [ ] Identify: default ports, volume mounts, environment variables, dependencies
- [ ] Check if it needs a database, cache, or other sidecars
- [ ] Check `https://github.com/linuxserver/reverse-proxy-confs` for a pre-built NGINX config
- [ ] Check `https://gethomepage.dev/widgets/` for a Homepage widget
- [ ] Check for known CVEs or security advisories
- [ ] Note the container's expected UID/GID (especially for database images — some like TimescaleDB HA run as non-root and cannot fix directory ownership at startup)

**Gate:** You can describe the image, port, volumes, env vars, and dependencies from memory.

---

## Phase 2: Scaffold the Role

**Skill:** `ansible-role-scaffolding`

- [ ] Create directory structure: `roles/<service>/{tasks,templates,vars,handlers}/`
- [ ] Create `vars/main.yaml` with research notes, versions, ports, folder paths, and feature flags
- [ ] Verify port doesn't conflict with existing services on the target host

**Gate:** `vars/main.yaml` exists with accurate values sourced from documentation.

---

## Phase 3: Create Templates

### Docker Compose
**Skill:** `docker-compose-templating`

- [ ] Create `templates/docker_compose.yaml.j2`
- [ ] Single-container: default bridge network. Multi-container: explicit network + `depends_on`
- [ ] Match the official compose example — don't invent settings
- [ ] Verify container UIDs match directory ownership in tasks (Phase 4)

### NGINX Proxy Config
**Skill:** `nginx-swag-proxy-config`

- [ ] If pre-built config exists at linuxserver/reverse-proxy-confs, adapt it
- [ ] Otherwise create `templates/<service>.subdomain.conf.j2`
- [ ] Add WebSocket support if the app uses real-time features (Socket.io, SSE, etc.)

### Homepage Entry
**Skill:** `homepage-dashboard-integration`

- [ ] Create `templates/homepage_service.yaml.j2`
- [ ] Use 2-space indentation for list items, 6-space for properties
- [ ] Only add a `widget` block if the service is listed at `https://gethomepage.dev/widgets/`

**Gate:** All templates created and reviewed for correctness.

---

## Phase 4: Create Tasks and Handlers

### Tasks
**Skills:** `ansible-docker-deployment`, `ufw-firewall-configuration`, `docker-service-health-checks`, `nginx-swag-proxy-config`, `pfsense-dns-management`, `homepage-dashboard-integration`

- [ ] Create `tasks/main.yaml` following the standard task order:
  1. Docker prerequisites
  2. Directory setup (match ownership to container UID/GID!)
  3. Firewall (UFW)
  4. Deploy compose template + `docker_compose_v2`
  5. Health check (assert port listening)
  6. Proxy config deployment
  7. DNS (full 9-step pfSense pattern)
  8. Homepage integration (blockinfile + tags)

### Handlers
**Skill:** `ansible-docker-deployment`

- [ ] Create `handlers/main.yaml` with handlers for: service restart, Homepage restart, SWAG restart
- [ ] Only include handlers that tasks actually notify

### Secrets
**Skill:** `ansible-vault-secrets`

- [ ] Generate and vault-encrypt any secrets (API keys, passwords, tokens)
- [ ] Add to `group_vars/all/vars.yaml`
- [ ] Add `internal_<service>_url` to group_vars

**Gate:** Role is complete. Syntax check passes: `ansible-playbook -i inventory.yaml deploy_<service>.yaml --vault-password-file ~/ansible_key --syntax-check`

---

## Phase 5: Create Playbook and Update Inventory

**Skill:** `playbook-creation-testing`

- [ ] Create `deploy_<service>.yaml` playbook
- [ ] Add `<service>_host` group to `inventory.yaml`
- [ ] Do **NOT** add to `master_playbook.yaml` yet

**Gate:** Playbook and inventory are ready. Master playbook is untouched.

---

## Phase 6: Deploy and Test

**Skill:** `playbook-creation-testing`

- [ ] Run the playbook: `ansible-playbook -i inventory.yaml deploy_<service>.yaml --vault-password-file ~/ansible_key`
- [ ] Fix any errors, then **return to this checklist** (don't skip remaining items)
- [ ] Verify containers are running: `ssh <host> 'sudo docker ps | grep <service>'`
- [ ] Verify port is listening: `ssh <host> 'ss -tlnp | grep <port>'`
- [ ] Check container logs: `ssh <host> 'sudo docker logs <service> --tail 50'`
- [ ] Test web interface through proxy — check HTTP status AND response body:
  ```bash
  curl -sk https://<service>.<domain>/ | head -20
  ```
  Verify the HTML contains the service name or expected content (not the SWAG default page). A 200 status alone is not sufficient — SWAG returns 200 with its own page when the upstream is unreachable.
- [ ] Verify DNS resolution: `nslookup <service>.<domain>`
- [ ] Check Homepage YAML indentation: `ssh <homepage_host> 'cat <homepage_folder>/config/services.yaml'`
- [ ] Check Homepage logs for YAML errors: `ssh <homepage_host> 'sudo docker logs homepage --tail 20'`
- [ ] **Idempotency test:** Run playbook a second time — expect zero `changed` tasks
- [ ] Check container logs again for runtime errors

**Gate:** All checks pass. Second run is fully idempotent.

---

## Phase 7: Master Playbook Integration

**Skill:** `playbook-creation-testing`

- [ ] Add `import_playbook` entry to `master_playbook.yaml` in the correct order
- [ ] Run full master playbook to verify no regressions (or at minimum, `--syntax-check` it)

**Gate:** Master playbook includes the new service and passes.
