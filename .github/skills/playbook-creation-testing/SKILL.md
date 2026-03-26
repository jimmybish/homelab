---
name: playbook-creation-testing
description: 'Use when: creating deploy playbooks, updating inventory with new service hosts, adding services to master_playbook.yaml, testing and verifying Ansible role deployments, running playbooks with common flags, or troubleshooting playbook execution. Covers the full playbook lifecycle from creation through testing to master playbook integration.'
---

# Playbook Creation & Testing

Full lifecycle for creating deploy playbooks, updating inventory, testing deployments, and integrating into the master playbook.

## When to Use

- Creating a new `deploy_<service>.yaml` playbook
- Adding a service host to `inventory.yaml`
- Testing a newly created Ansible role
- Adding a service to `master_playbook.yaml`
- Running playbooks with specific flags or troubleshooting failures

## 1. Update Inventory

Add the service host to `inventory.yaml`:

```yaml
<service>_host:
  hosts:
    <hostname>:
```

## 2. Create Playbook

Create `deploy_<service>.yaml` in the `ansible/` directory:

```yaml
---
- name: Install <Service>
  hosts: <service>_host
  become: true
  roles:
    - <service>
```

## 3. Test the Playbook

**CRITICAL: Test your role thoroughly before adding to master playbook!**

```bash
ansible-playbook -i inventory.yaml deploy_<service>.yaml --vault-password-file ~/ansible_key
```

### Verification Steps

1. **Check service is running:**
   ```bash
   ssh <host> 'sudo docker ps | grep <service>'
   ```

2. **Verify port is listening:**
   ```bash
   ssh <host> 'ss -tlnp | grep <port>'
   ```

3. **Test web interface (if applicable):**
   - Access `https://<service>.{{ internal_domain }}` in browser
   - Verify page loads correctly
   - Check for any errors in browser console

4. **Verify DNS resolution:**
   ```bash
   nslookup <service>.{{ internal_domain }}
   ```

5. **Check Homepage integration (if applicable):**
   - Visit Homepage dashboard
   - Verify service appears with correct link
   - **Verify YAML indentation is correct:**
     ```bash
     ssh <homepage_host> 'cat {{ homepage_folder }}/config/services.yaml | head -30'
     ```
   - Check that list items use 2-space indentation (not 4 spaces)
   - **Check Homepage logs for YAML errors:**
     ```bash
     ssh <homepage_host> 'sudo docker logs homepage --tail 20'
     ```

6. **Test idempotency:**
   ```bash
   # Run the playbook a second time
   ansible-playbook -i inventory.yaml deploy_<service>.yaml --vault-password-file ~/ansible_key
   
   # Expected: All tasks "ok" with no "changed" tasks
   ```

7. **Check logs for errors:**
   ```bash
   ssh <host> 'sudo docker logs <service> --tail 50'
   ```

**Only proceed to master playbook integration if all tests pass!**

## 4. Add to Master Playbook

Add your new playbook to `master_playbook.yaml`:

```yaml
- name: Deploy <Service>
  ansible.builtin.import_playbook: deploy_<service>.yaml
```

**Placement guidelines:**
- Add after `deploy_homepage.yaml` for most services
- Services that depend on others should be placed after their dependencies
- Consider the logical deployment order (e.g., databases before apps that use them)

## Testing Checklist

- [ ] **Research completed** — Official documentation reviewed, default ports identified
- [ ] **Source URLs documented** — Added to vars/main.yaml for future reference
- [ ] **Playbook created** — deploy_<service>.yaml created in ansible/ directory
- [ ] **Master playbook updated** — Service added to master_playbook.yaml in correct order
- [ ] Role deploys successfully
- [ ] Variables defined based on official documentation (not guessed)
- [ ] Docker compose template uses official image and follows official examples
- [ ] Container(s) start and remain running
- [ ] Health check passes (ports listening)
- [ ] Web interface accessible via proxy (if applicable) — check body content, not just HTTP status
- [ ] DNS alias resolves correctly (if applicable)
- [ ] Homepage widget displays correctly (if applicable)
- [ ] **Homepage YAML indentation is correct** (2 spaces, not 4)
- [ ] **Homepage container runs without YAML errors**
- [ ] Firewall rules created correctly (if external access required)
- [ ] Docker network created and containers connected
- [ ] Service survives container restart
- [ ] Re-running playbook is idempotent (no unnecessary changes)
- [ ] Configuration matches official best practices
- [ ] **Master playbook runs successfully** — Verify full deployment via master_playbook.yaml

## Running Playbooks

All playbooks are run from the `ansible/` directory.

### Base Command

```bash
cd ~/homelab/ansible
ansible-playbook -i inventory.yaml <playbook>.yaml --vault-password-file ~/ansible_key
```

### Common Flags

| Flag | Purpose | Example |
|------|---------|---------|
| `-i inventory.yaml` | Specify inventory file | Always required |
| `--vault-password-file ~/ansible_key` | Decrypt vault secrets | Required for most deploy playbooks |
| `--limit '<pattern>'` | Restrict to specific hosts | `--limit 'docker-1'` |
| `--limit '!<host>'` | Exclude specific hosts | `--limit '!mgmt'` |
| `--tags '<tag>'` | Run only tagged tasks | `--tags 'homepage_config'` |
| `--skip-tags '<tag>'` | Skip tagged tasks | `--skip-tags 'homepage'` |
| `--check --diff` | Dry run — show what would change | Safe preview, no changes applied |
| `-v` / `-vv` / `-vvv` | Increase verbosity | `-vvv` for full debug output |
| `--extra-vars "key=val"` | Pass extra variables | `--extra-vars "target=docker-1"` |

### Deployment Playbooks

```bash
# Deploy all services in order
ansible-playbook -i inventory.yaml master_playbook.yaml --vault-password-file ~/ansible_key

# Deploy a single service
ansible-playbook -i inventory.yaml deploy_grafana.yaml --vault-password-file ~/ansible_key
ansible-playbook -i inventory.yaml deploy_arr.yaml --vault-password-file ~/ansible_key
ansible-playbook -i inventory.yaml deploy_plex.yaml --vault-password-file ~/ansible_key
# ... same pattern for all deploy_*.yaml playbooks
```

### Maintenance Playbooks

```bash
# Update all Ubuntu hosts (dist-upgrade + reboot if changed, runs serial: 1)
ansible-playbook -i inventory.yaml update_ubuntu.yaml --vault-password-file ~/ansible_key

# Update all Ubuntu hosts except mgmt
ansible-playbook -i inventory.yaml update_ubuntu.yaml --vault-password-file ~/ansible_key --limit '!mgmt'

# Pull latest Docker images and recreate containers across all Ubuntu hosts
ansible-playbook -i inventory.yaml update_docker_containers.yaml --vault-password-file ~/ansible_key

# Update Docker containers on a single host
ansible-playbook -i inventory.yaml update_docker_containers.yaml --vault-password-file ~/ansible_key --limit 'docker-1'

# Stop all containers on specified hosts (interactive prompt)
ansible-playbook -i inventory.yaml stop_containers.yaml --vault-password-file ~/ansible_key

# Shutdown specified hosts (interactive prompt)
ansible-playbook -i inventory.yaml shutdown_hosts.yaml --vault-password-file ~/ansible_key

# Initial OS setup (users, packages, SSH, firewall)
ansible-playbook -i inventory.yaml os_setup.yaml --vault-password-file ~/ansible_key
```

### Homepage-Specific Operations

```bash
# Update all Homepage service entries without full redeployment
ansible-playbook -i inventory.yaml master_playbook.yaml --vault-password-file ~/ansible_key --tags homepage_config

# Rebuild Homepage infrastructure + all service entries
ansible-playbook -i inventory.yaml master_playbook.yaml --vault-password-file ~/ansible_key --tags homepage

# Update Homepage entry for a single service
ansible-playbook -i inventory.yaml deploy_grafana.yaml --vault-password-file ~/ansible_key --tags homepage
```

### Host Groups (from inventory)

| Group | Hosts | Used By |
|-------|-------|---------|
| `ubuntu` | mgmt, docker-1, docker-2, jellyfin-lxc, proxy | update_ubuntu, update_docker_containers |
| `proxy_host` | proxy | deploy_proxy |
| `homepage_host` | docker-2 | deploy_homepage |
| `grafana_host` | docker-1 | deploy_grafana |
| `plex_host` | jellyfin-lxc | deploy_plex |
| `jellyfin_host` | jellyfin-lxc | deploy_jellyfin |
| `jellyseerr_host` | proxy | deploy_jellyseerr |
| `paperless_host` | docker-2 | deploy_paperless |
| `n8n_host` | docker-2 | deploy_n8n |
| `maintainerr_host` | docker-2 | deploy_maintainerr |
| `pfsense_host` | router | DNS API operations (delegated) |

### Troubleshooting

```bash
# Dry run to preview changes
ansible-playbook -i inventory.yaml deploy_grafana.yaml --vault-password-file ~/ansible_key --check --diff

# Run with maximum verbosity
ansible-playbook -i inventory.yaml deploy_grafana.yaml --vault-password-file ~/ansible_key -vvv

# Test connectivity to all hosts
ansible -i inventory.yaml all -m ping

# Test connectivity to a single host
ansible -i inventory.yaml docker-1 -m ping

# List hosts in a group
ansible -i inventory.yaml ubuntu --list-hosts

# Run ad-hoc command on a host
ansible -i inventory.yaml docker-1 -m shell -a 'docker ps' --become
```
