# Ansible Role Creation Guide

This guide provides a standardized approach to creating Ansible roles for homelab services, using the Grafana role as a reference implementation.

## Step 0: Research the Application

**ALWAYS start by researching the application before defining any variables or configuration.**

### Research Checklist:

1. **Find Official Documentation:**
   - Look for official Docker Hub page (e.g., `https://hub.docker.com/r/<vendor>/<app>`)
   - Check the official project documentation or GitHub repository
   - Review installation guides and deployment examples

2. **Locate Docker Compose Examples:**
   - Search for official `docker-compose.yaml` examples
   - Check the project's GitHub repository (often in `/docker/` or `/examples/` directories)
   - Look for community-maintained examples on GitHub or forums

3. **Identify Default Configuration:**
   - **Default ports:** What port(s) does the application listen on?
   - **Volume mounts:** What directories need persistence? (config, data, logs)
   - **Environment variables:** What are the required/recommended env vars?
   - **Dependencies:** Does it require a database, cache, or other services?
   - **User/permissions:** Does it need specific PUID/PGID settings?
   - **Proxy configuration:** Check `https://github.com/linuxserver/reverse-proxy-confs` for pre-built NGINX configs

4. **Understand Application Architecture:**
   - Is it a single container or multi-container stack?
   - Does it have a web interface?
   - Does it need to communicate with external services?
   - Are there any special networking requirements?

5. **Review Best Practices:**
   - Security recommendations (authentication, SSL requirements)
   - Performance tuning (memory limits, CPU constraints)
   - Backup requirements (what data needs to be backed up?)
   - Update/upgrade procedures

### Example Research Process:

For deploying "Uptime Kuma" monitoring tool:

```bash
# 1. Check Docker Hub
https://hub.docker.com/r/louislam/uptime-kuma

# 2. Find official docker-compose example
https://github.com/louislam/uptime-kuma/blob/master/docker/docker-compose.yml

# 3. Check for existing NGINX reverse proxy config
https://github.com/linuxserver/reverse-proxy-confs
# Search for "uptime-kuma" → Found: uptime-kuma.subdomain.conf.sample

# 4. Identify from documentation:
#    - Default port: 3001
#    - Volume needed: /app/data
#    - Single container, no dependencies
#    - Has web interface → needs proxy configuration
#    - Pre-built proxy config available (LinuxServer)
#    - No special env vars required
```

### Documentation Sources:

- Docker Hub: `https://hub.docker.com`
- GitHub: Search for "docker-compose" in project repositories
- LinuxServer.io: `https://docs.linuxserver.io` (excellent documentation for many apps)
- **LinuxServer Reverse Proxy Configs:** `https://github.com/linuxserver/reverse-proxy-confs` (pre-built NGINX configs for 100+ apps)
- Awesome-Selfhosted: `https://github.com/awesome-selfhosted/awesome-selfhosted`
- Project wikis and official documentation sites

**⚠️ Never guess configuration values - always reference official documentation or working examples.**

## Prerequisites

Before creating a new role, ensure you understand:
- The service you're deploying (research completed in Step 0)
- Whether it has a web interface (if yes, ports must be exposed and proxy configured)
- What DNS aliases are needed
- Which inventory group(s) will use this role

## Role Structure

```
roles/
  └── <service_name>/
      ├── handlers/
      │   └── main.yaml          # Service restart handlers
      ├── tasks/
      │   └── main.yaml          # Main task list
      ├── templates/
      │   ├── docker_compose.yaml.j2         # Docker Compose file
      │   ├── <service>.subdomain.conf.j2    # NGINX proxy config (if web interface)
      │   └── homepage_service.yaml.j2       # Homepage dashboard entry (optional)
      └── vars/
          └── main.yaml          # Role-specific variables
```

## Step-by-Step Role Creation

### 1. Research Complete - Document Findings

Before proceeding, document your research findings:

```yaml
# Research Notes for <service>
# Source: <official documentation URL>
# Docker Hub: <hub.docker.com link>
# Example compose: <GitHub link>
# NGINX Proxy Config: https://github.com/linuxserver/reverse-proxy-confs (check if available)

# Default Configuration:
# - Image: <vendor>/<image>:<tag>
# - Default Port: <port>
# - Protocol: http/https
# - Volume Mounts: 
#   - /path/to/config
#   - /path/to/data
# - Required Environment Variables:
#   - VAR1=value
#   - VAR2=value
# - Dependencies: none/postgres/redis/etc
# - Web Interface: yes/no
# - Pre-built NGINX Config: yes/no (from LinuxServer reverse-proxy-confs)
# - Special Requirements: <any special networking, permissions, etc>
```

Save these notes as comments in your role's `vars/main.yaml` file for future reference.

### 2. Create Role Directory Structure

```bash
cd ansible/roles
mkdir -p <service>/{tasks,templates,vars,handlers}
touch <service>/tasks/main.yaml
touch <service>/vars/main.yaml
touch <service>/handlers/main.yaml
```

### 3. Define Variables (`vars/main.yaml`) - Based on Research

Use your research findings to populate accurate values:

```yaml
---
# Research Notes - <Service Name>
# Official Source: <documentation URL>
# Example Compose: <GitHub link to official docker-compose.yaml>
# Last Updated: <date>

# Version information (check Docker Hub for latest stable tag)
<service>_version: "latest"  # or specific version like "1.2.3"

# Port mappings (from official documentation)
<service>_port: 8080  # PRIMARY PORT - Use the official default port from documentation
# <service>_additional_port: 9090  # Add if app uses multiple ports

# Volume paths (use docker_storage_folder from group_vars)
# For single-container services:
<service>_folder: "{{ docker_storage_folder }}/<service>"

# For multi-container stacks, use parent folder pattern:
# <service>_parent_folder: "{{ docker_storage_folder }}/<service>-stack"
# <service>_folder: "{{ <service>_parent_folder }}/<service>"
# <service>_database_folder: "{{ <service>_parent_folder }}/database"
# <service>_cache_folder: "{{ <service>_parent_folder }}/cache"

# Feature flags
<service>_configure_homepage: true    # ALWAYS true if web interface exists
<service>_configure_proxy: true       # ALWAYS true if web interface exists
<service>_configure_dns: true         # ALWAYS true if web interface exists

# Application-specific variables (from official documentation)
# <service>_db_type: "postgres"  # Example: if database required
# <service>_max_upload_size: "50M"  # Example: app-specific setting
```

**Decision Points:**
- **Has web interface?** 
  - **YES** → Set all of: `configure_proxy: true`, `configure_dns: true`, `configure_homepage: true`
  - Web interface services MUST expose ports and MUST configure internal SWAG proxy
  - Homepage link is ALWAYS added for web interfaces
  - All services are accessed via internal network through the SWAG internal proxy

### 3. Create Docker Compose Template (`templates/docker_compose.yaml.j2`)

**For single-container services:**
```yaml
services:
  <service>:
    image: <image>:{{ <service>_version }}
    container_name: <service>
    restart: unless-stopped
    user: "{{ docker_user_puid }}"
    environment:
      - PUID={{ docker_user_puid }}
      - PGID={{ docker_user_pgid }}
      - TZ={{ timezone }}
    ports:
      - '{{ <service>_port }}:<internal_port>'  # REQUIRED if service has web interface
    volumes:
      - {{ <service>_folder }}:/config
    # No explicit network needed - default Docker bridge network is sufficient
```

**For multi-container services:**

Use the `<service>-stack` folder pattern:
```yaml
# vars/main.yaml defines:
# <service>_parent_folder: "{{ docker_storage_folder }}/<service>-stack"
# <service>_folder: "{{ <service>_parent_folder }}/<service>"
# <service>_database_folder: "{{ <service>_parent_folder }}/database"

services:
  <service>:
    image: <image>:{{ <service>_version }}
    container_name: <service>
    restart: unless-stopped
    user: "{{ docker_user_puid }}"
    environment:
      - PUID={{ docker_user_puid }}
      - PGID={{ docker_user_pgid }}
      - TZ={{ timezone }}
      - DATABASE_URL=postgresql://db:5432/appdb  # Use container names for internal communication
    ports:
      - '{{ <service>_port }}:<internal_port>'
    volumes:
      - {{ <service>_folder }}:/config  # Points to <service>-stack/<service>/
    networks:
      - <service>_net
    depends_on:
      - db

  db:
    image: postgres:latest
    container_name: <service>_db
    restart: unless-stopped
    environment:
      - POSTGRES_DB=appdb
    volumes:
      - {{ <service>_database_folder }}:/var/lib/postgresql/data  # Points to <service>-stack/database/
    networks:
      - <service>_net
    # NO port mapping - internal access only

networks:
  <service>_net:
    driver: bridge
```

**Best Practices:**
- **Single containers:** Default Docker network is sufficient, no explicit network needed
- **Multi-container stacks:** MUST define an explicit Docker network for isolation
- **ALWAYS expose ports if service has a web interface** (required for proxy access)
- For multi-container stacks, use internal container names (e.g., `http://postgres:5432`) for inter-service communication
- Use `depends_on` to define container startup order
- Only backend services without web interfaces can skip port exposure

### 4. Create Main Tasks (`tasks/main.yaml`)

Follow this task order:

#### 4.1 Docker Prerequisites
```yaml
---
- name: Ensure docker-compose is installed
  ansible.builtin.package:
    name: docker-compose
    state: present

- name: Ensure Docker service is running
  ansible.builtin.service:
    name: docker
    state: started
    enabled: true
```

#### 4.2 Directory Setup

**For single-container services:**
```yaml
- name: Setup service directory
  ansible.builtin.file:
    path: "{{ <service>_folder }}"
    state: directory
    mode: '0755'
    owner: "{{ docker_user }}"
    group: "{{ docker_user }}"
```

**For multi-container stacks:**
```yaml
- name: Setup parent directory for multi-container stack
  ansible.builtin.file:
    path: "{{ <service>_parent_folder }}"
    state: directory
    mode: '0755'
    owner: "{{ docker_user }}"
    group: "{{ docker_user }}"

- name: Setup main service directory
  ansible.builtin.file:
    path: "{{ <service>_folder }}"
    state: directory
    mode: '0755'
    owner: "{{ docker_user }}"
    group: "{{ docker_user }}"

- name: Setup database directory
  ansible.builtin.file:
    path: "{{ <service>_database_folder }}"
    state: directory
    mode: '0755'
    owner: "{{ docker_user }}"
    group: "{{ docker_user }}"
```

#### 4.3 Firewall Configuration (REQUIRED if web interface exists)
```yaml
- name: Allow service port through UFW
  community.general.ufw:
    rule: allow
    port: "{{ <service>_port }}"
    proto: tcp
  when: ansible_os_family == "Debian"
```

**Note:** If the service has a web interface, firewall rules are REQUIRED since ports must be exposed for proxy access.

#### 4.4 Deploy Configuration Files
```yaml
- name: Deploy <service> using Docker Compose
  ansible.builtin.template:
    src: "templates/docker_compose.yaml.j2"
    dest: "{{ <service>_folder }}/docker-compose.yaml"
    mode: '0644'
    owner: "{{ docker_user }}"
    group: "{{ docker_user }}"
    force: true
  notify:
    - Start <service>

- name: Run docker-compose up
  community.docker.docker_compose_v2:
    project_src: "{{ <service>_folder }}"
    state: present
    remove_orphans: true
```

#### 4.5 Health Check
```yaml
- name: Check if service ports are open and listening
  community.general.listen_ports_facts:

- name: Assert service ports are listening
  ansible.builtin.assert:
    that:
      - <service>_port | int in (ansible_facts.tcp_listen | map(attribute='port') | list)
    fail_msg: "<service> port is not open and listening!"
    success_msg: "<service> port is open and listening."
```

#### 4.6 Internal Proxy Configuration (REQUIRED if web interface exists)

**Internal Proxy:** For local network access via `<service>.{{ internal_domain }}`

**IMPORTANT: Check for existing NGINX configurations first!**

Before creating a custom proxy configuration, check if LinuxServer.io already provides one:
- Visit: `https://github.com/linuxserver/reverse-proxy-confs`
- Search for your application name (e.g., "grafana", "sonarr", "uptime-kuma")
- If a configuration exists, use it as the basis for your template (may need minor adjustments for your environment)
- These configs are battle-tested and include proper headers, websocket support, and security settings

**Example:** For Grafana, a pre-built config exists at:
`https://github.com/linuxserver/reverse-proxy-confs/blob/master/grafana.subdomain.conf.sample`

If no pre-built configuration exists, create `templates/<service>.subdomain.conf.j2`:
```nginx
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;

    server_name <service>.*;

    include /config/nginx/ssl.conf;

    client_max_body_size 0;

    location / {
        include /config/nginx/proxy.conf;
        include /config/nginx/resolver.conf;
        set $upstream_app {{ <service>_proxy_ip }};
        set $upstream_port {{ <service>_port }};
        set $upstream_proto http;
        proxy_pass $upstream_proto://$upstream_app:$upstream_port;
    }
}
```

**Tips for adapting LinuxServer reverse-proxy-confs:**
- Replace hardcoded IPs with `{{ <service>_proxy_ip }}`
- Replace hardcoded ports with `{{ <service>_port }}`
- Keep all the special headers and proxy settings (they're there for a reason!)
- Maintain websocket support blocks if present (required for real-time features)
- Keep security headers and authentication blocks

Add internal proxy configuration tasks:
```yaml
# Configure SWAG Internal Proxy (REQUIRED for web interfaces)
- name: Resolve service host IP for proxy configuration
  ansible.builtin.set_fact:
    <service>_proxy_ip: "{{ hostvars[inventory_hostname]['ansible_default_ipv4']['address'] }}"

- name: Deploy service nginx config to SWAG Internal
  ansible.builtin.template:
    src: <service>.subdomain.conf.j2
    dest: "{{ proxy_folder }}/internal/nginx/proxy-confs/<service>.subdomain.conf"
    mode: '0644'
  delegate_to: "{{ groups['proxy_host'][0] }}"
  notify:
    - Restart Swag
```

#### 4.7 DNS Alias Configuration (REQUIRED if web interface exists)

```yaml
# Check pfSense DNS Resolver Host Overrides
- name: Get DNS resolver host overrides from pfSense
  ansible.builtin.uri:
    url: "https://{{ groups['pfsense_host'][0] }}/api/v2/services/dns_resolver/host_overrides"
    method: GET
    headers:
      X-API-Key: "{{ pfsense_api_key }}"
    validate_certs: false
    return_content: true
  register: pfsense_dns_overrides
  delegate_to: localhost
  when: <service>_configure_dns | default(false)

- name: Find proxy host override entry in pfSense
  ansible.builtin.set_fact:
    proxy_host_override: >-
      {{ pfsense_dns_overrides.json.data
         | selectattr('host', 'equalto', 'proxy')
         | selectattr('domain', 'equalto', internal_domain)
         | list
         | first
         | default({}) }}
  when:
    - <service>_configure_dns | default(false)
    - pfsense_dns_overrides.json.data is defined

- name: Check if service alias exists under proxy host override
  ansible.builtin.set_fact:
    <service>_alias_exists: >-
      {{ proxy_host_override.aliases | default([])
         | selectattr('host', 'equalto', '<service>')
         | selectattr('domain', 'equalto', internal_domain)
         | list
         | length > 0 }}
  when:
    - <service>_configure_dns | default(false)
    - proxy_host_override is defined
    - proxy_host_override | length > 0

- name: Display service DNS alias status
  ansible.builtin.debug:
    msg: >-
      {% if proxy_host_override | length == 0 %}
      Warning: Proxy host override not found in pfSense DNS
      {% elif <service>_alias_exists %}
      <service> DNS alias exists under proxy.{{ internal_domain }}
      {% else %}
      <service> DNS alias missing - will be created
      {% endif %}
  when: <service>_configure_dns | default(false)

- name: Create service DNS alias under proxy host override
  ansible.builtin.uri:
    url: "https://{{ groups['pfsense_host'][0] }}/api/v2/services/dns_resolver/host_override/alias"
    method: POST
    headers:
      X-API-Key: "{{ pfsense_api_key }}"
      Content-Type: "application/json"
    body_format: json
    body:
      parent_id: "{{ proxy_host_override.id }}"
      host: "<service>"
      domain: "{{ internal_domain }}"
      descr: "<Service description>"
    validate_certs: false
    status_code: [200, 201]
  delegate_to: localhost
  register: alias_creation_result
  when:
    - <service>_configure_dns | default(false)
    - proxy_host_override | length > 0
    - not <service>_alias_exists

- name: Apply DNS resolver changes in pfSense
  ansible.builtin.uri:
    url: "https://{{ groups['pfsense_host'][0] }}/api/v2/services/dns_resolver/apply"
    method: POST
    headers:
      X-API-Key: "{{ pfsense_api_key }}"
    validate_certs: false
    status_code: [200, 201]
  delegate_to: localhost
  when:
    - <service>_configure_dns | default(false)
    - alias_creation_result is succeeded
```

#### 4.8 Homepage Integration (REQUIRED if web interface exists)

Create `templates/homepage_service.yaml.j2`:
```yaml
    - <Service Name>:
        icon: <service>.png
        href: {{ internal_<service>_url }}
        description: Service description
        widget:
            type: <service>
            url: {{ internal_<service>_url }}
            key: {{ homepage_<service>_key }}
```

Add homepage configuration task:
```yaml
# Configure Homepage Services (REQUIRED for web interfaces)
- name: Add service to Homepage section
  ansible.builtin.blockinfile:
    path: "{{ homepage_folder }}/config/services.yaml"
    marker: "# {mark} ANSIBLE MANAGED BLOCK - <service> service"
    block: "{{ lookup('template', 'homepage_service.yaml.j2') }}"
    insertafter: "^- <Section>:"
    mode: '0644'
  delegate_to: "{{ groups['homepage_host'][0] }}"
  notify:
    - Restart Homepage
  when: <service>_configure_homepage | default(true)
```

**Important Notes:**
- **DO NOT** add a check to skip if the block already exists - `blockinfile` will automatically update the block if the content changes
- This allows the role to update Homepage entries when you add new services or modify descriptions
- The `blockinfile` module is idempotent and will only trigger changes when the block content actually differs
- All web interfaces MUST appear on Homepage for easy access

### 5. Create Handlers (`handlers/main.yaml`)

```yaml
---
- name: Start <service>
  community.docker.docker_compose_v2:
    project_src: "{{ <service>_folder }}"
    state: restarted
    remove_orphans: true

- name: Restart Homepage
  community.docker.docker_compose_v2:
    project_src: "{{ homepage_folder }}"
    state: restarted
  delegate_to: "{{ groups['homepage_host'][0] }}"

- name: Restart Swag
  community.docker.docker_compose_v2:
    project_src: "{{ proxy_folder }}"
    services:
      - swag_internal
    state: restarted
  delegate_to: "{{ groups['proxy_host'][0] }}"
```

### 6. Update Global Variables

Add to `group_vars/all/vars.yaml`:
```yaml
# <Service> Configuration
<service>_folder: "/etc/docker-storage/<service>"
internal_<service>_url: "https://<service>.{{ internal_domain }}"
homepage_<service>_key: !vault |
          $ANSIBLE_VAULT;1.1;AES256
          <encrypted_key>
```

To encrypt secrets:
```bash
ansible-vault encrypt_string --vault-password-file ~/ansible_key 'secret_value' --name '<service>_key'
```

### 7. Update Inventory

Add the service host to `inventory.yaml`:
```yaml
<service>_host:
  hosts:
    <hostname>:
```

### 8. Create Playbook

Create `deploy_<service>.yaml` in the `ansible/` directory:
```yaml
---
- name: Install <Service>
  hosts: <service>_host
  become: true
  roles:
    - <service>
```

#### 8.1 Test the Playbook

**CRITICAL: Test your role thoroughly before adding to master playbook!**

Run the individual playbook to verify it works correctly:

```bash
# Run the playbook
ansible-playbook -i inventory.yaml deploy_<service>.yaml --vault-password-file ~/ansible_key

# Check the output for:
# - No failed tasks
# - All tasks completed successfully
# - Services started and healthy
```

**Verification steps:**
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

6. **Test idempotency:**
   ```bash
   # Run the playbook a second time
   ansible-playbook -i inventory.yaml deploy_<service>.yaml --vault-password-file ~/ansible_key
   
   # Expected output: All tasks should be "ok" with no "changed" tasks
   # This confirms the playbook is idempotent
   ```

7. **Check logs for errors:**
   ```bash
   ssh <host> 'sudo docker logs <service> --tail 50'
   ```

**Only proceed to Step 9 if all tests pass successfully!**

### 9. Add to Master Playbook

Add your new playbook to `master_playbook.yaml` for orchestrated deployments:

```yaml
- name: Deploy <Service>
  ansible.builtin.import_playbook: deploy_<service>.yaml
```

**Placement guidelines:**
- Add after `deploy_homepage.yaml` for most services
- Services that depend on others should be placed after their dependencies
- Consider the logical deployment order (e.g., databases before apps that use them)

**Example addition to master_playbook.yaml:**
```yaml
- name: Deploy Homepage
  ansible.builtin.import_playbook: deploy_homepage.yaml

- name: Deploy <Service>  # <-- Add your new service here
  ansible.builtin.import_playbook: deploy_<service>.yaml

- name: Deploy Arr Suite
  ansible.builtin.import_playbook: deploy_arr.yaml
```

This allows running all deployments with a single command:
```bash
ansible-playbook -i inventory.yaml master_playbook.yaml --vault-password-file ~/ansible_key
```

## Testing Checklist

After creating the role, test the following:

- [ ] **Research completed** - Official documentation reviewed, default ports identified
- [ ] **Source URLs documented** - Added to vars/main.yaml for future reference
- [ ] **Playbook created** - deploy_<service>.yaml created in ansible/ directory
- [ ] **Master playbook updated** - Service added to master_playbook.yaml in correct order
- [ ] Role deploys successfully
- [ ] Variables defined based on official documentation (not guessed)
- [ ] Docker compose template uses official image and follows official examples
- [ ] Container(s) start and remain running
- [ ] Health check passes (ports listening)
- [ ] Web interface accessible via proxy (if applicable)
- [ ] DNS alias resolves correctly (if applicable)
- [ ] Homepage widget displays correctly (if applicable)
- [ ] Firewall rules created correctly (if external access required)
- [ ] Docker network created and containers connected
- [ ] Service survives container restart
- [ ] Re-running playbook is idempotent (no unnecessary changes)
- [ ] Configuration matches official best practices
- [ ] **Master playbook runs successfully** - Verify full deployment via master_playbook.yaml

## Common Patterns

### Single-Container Service (Most Common)

For standalone services (like n8n, Uptime Kuma, etc.):
```yaml
services:
  <service>:
    image: <vendor>/<service>:{{ <service>_version }}
    container_name: <service>
    restart: unless-stopped
    ports:
      - '{{ <service>_port }}:<internal_port>'
    environment:
      - PUID={{ docker_user_puid }}
      - PGID={{ docker_user_pgid }}
      - TZ={{ timezone }}
    volumes:
      - <service>_data:/data
    # Uses default Docker bridge network - no explicit network needed

volumes:
  <service>_data:
```

### Multi-Container Stack with Internal Communication

For services with databases or dependencies (like Grafana with Loki/Prometheus):

**Folder Structure Pattern:** Use `<service>-stack` as parent folder with subfolders for each container:
- `<service>-stack/docker-compose.yaml`
- `<service>-stack/<service>/` (main service data)
- `<service>-stack/database/` (database data)
- `<service>-stack/cache/` (cache data if applicable)

```yaml
services:
  app:
    image: <vendor>/app:{{ app_version }}
    container_name: app
    restart: unless-stopped
    ports:
      - '{{ app_port }}:3000'  # Only web interface exposed
    environment:
      - DATABASE_URL=postgresql://db:5432/appdb  # Container name for internal DNS
    volumes:
      - {{ <service>_folder }}:/config  # Uses <service>_parent_folder/<service>
    depends_on:
      - db
    networks:
      - <service>_net

  db:
    image: postgres:latest
    container_name: app_db
    restart: unless-stopped
    # NO port mapping - internal-only access via Docker network
    environment:
      - POSTGRES_DB=appdb
    volumes:
      - {{ <service>_database_folder }}:/var/lib/postgresql/data  # Uses <service>_parent_folder/database
    networks:
      - <service>_net

networks:
  <service>_net:  # Explicit network REQUIRED for multi-container isolation
    driver: bridge
```

**Key Differences:**
- Single container: No explicit network needed, uses Docker's default bridge
- Multi-container: Explicit network REQUIRED for proper isolation and internal DNS resolution

**Folder Organization:**
- Single container: Use `docker_storage_folder/<service>` (e.g., `/etc/docker-storage/n8n`)
- Multi-container with bind mounts: Use `docker_storage_folder/<service>-stack` as parent with subfolders:
  - Example: `/etc/docker-storage/grafana-stack/{grafana/,prometheus/,loki/}`
  - Example: `/etc/docker-storage/jellyfin-stack/{jellyfin/,jellyseerr/}`
  - Benefits: Groups related services, simplifies backups, clearer organization
- Multi-container with Docker named volumes: Use `docker_storage_folder/<service>` (no `-stack` suffix needed):
  - Example: `/etc/docker-storage/paperless/` (uses named volumes: data, media, pgdata, redisdata)
  - Rationale: Named volumes are managed by Docker, not visible on disk, so the folder only contains docker-compose.yaml and minimal config
  - Benefits: Simpler folder structure when volumes are abstracted away by Docker

### Service Requiring Both Internal and External Access

```yaml
services:
  api:
    ports:
      - '{{ <service>_api_port }}:8080'  # External API access
    networks:
      - <service>_net
      - web_proxy_net  # If accessed by external services

  web:
    # No ports needed - accessed via proxy only
    networks:
      - <service>_net
```

## Decision Tree

```
Does it have a web interface?
├─ YES → REQUIRED CONFIGURATION:
│        ✓ Set configure_proxy: true
│        ✓ Set configure_dns: true
│        ✓ Set configure_homepage: true
│        ✓ Expose port in docker-compose
│        ✓ Add firewall rules
│        ✓ Create internal NGINX proxy config (proxy_folder/internal/)
│        ✓ Create DNS alias under proxy host
│        ✓ Add to Homepage services.yaml
│        ✓ Access via internal network at <service>.{{ internal_domain }}
│
└─ NO  → Backend service only:
         ✓ Set configure_proxy: false
         ✓ Set configure_dns: false
         ✓ Set configure_homepage: false
         ✓ NO port exposure needed
         ✓ NO proxy configuration
         ✓ Communication via Docker network only

Does it have multiple containers?
├─ YES → MUST define explicit Docker network (<service>_net)
│        Use container names for internal communication (e.g., postgres:5432)
│        Only main web container needs port exposure
│        Backend containers should NOT expose ports
│        
│        Folder structure depends on volume strategy:
│        
│        ├─ Using bind mounts (host directories):
│        │  MUST use <service>-stack parent folder structure
│        │  Example vars:
│        │    <service>_parent_folder: "{{ docker_storage_folder }}/<service>-stack"
│        │    <service>_folder: "{{ <service>_parent_folder }}/<service>"
│        │    <service>_db_folder: "{{ <service>_parent_folder }}/database"
│        │  Example: grafana-stack/{grafana/,prometheus/,loki/}
│        │
│        └─ Using Docker named volumes:
│           Use simple folder: <service>_folder: "{{ docker_storage_folder }}/<service>"
│           No subfolders needed - volumes managed by Docker
│           Example: paperless/ (contains docker-compose.yaml only)
│           Example vars:
│             <service>_folder: "{{ docker_storage_folder }}/<service>"
│
└─ NO  → Default Docker bridge network is sufficient
         No explicit network definition needed
         Use simple path: <service>_folder: "{{ docker_storage_folder }}/<service>"
```

## Best Practices

1. **ALWAYS research first** - Never guess ports, volumes, or configuration values
2. **Use official examples** - Base your docker-compose template on official documentation
3. **Check LinuxServer reverse-proxy-confs** - Use pre-built NGINX configs when available (https://github.com/linuxserver/reverse-proxy-confs)
4. **Document your sources** - Add research notes and URLs to vars/main.yaml
4. **Verify defaults** - Cross-reference Docker Hub, GitHub, and official docs
5. **Use explicit Docker networks for multi-container stacks** - Required for isolation and internal DNS
6. **Single containers can use default network** - No need to define explicit networks
7. **Web interfaces ALWAYS need ports exposed** - Required for proxy access, non-negotiable
8. **Use internal URLs for inter-container communication** - Faster and more reliable (multi-container only)
9. **Internal proxy is REQUIRED for all web interfaces** - No exceptions
10. **Homepage links are REQUIRED for all web interfaces** - Provides centralized access
11. **DNS aliases match web interfaces** - If it has a web UI, it gets a DNS alias
12. **Keep special proxy headers** - If using LinuxServer configs, maintain websocket and security headers
13. **Document service dependencies** - Comment multi-container relationships
14. **Test idempotency** - Run playbook multiple times to ensure it's safe
15. **Use Ansible vault for secrets** - Never commit plain-text credentials
16. **Follow naming conventions** - Keep variable names consistent across roles
17. **Add health checks** - Verify service is actually working, not just deployed
18. **Let blockinfile handle updates** - Don't add conditional checks to skip existing blocks; blockinfile automatically updates content when it changes and is idempotent

## Example: Complete Minimal Web Service Role

**Example: Deploying Uptime Kuma (based on real research)**

**Step 0 - Research findings:**
- Official: `https://github.com/louislam/uptime-kuma`
- Docker Hub: `https://hub.docker.com/r/louislam/uptime-kuma`
- Official compose: `https://github.com/louislam/uptime-kuma/blob/master/docker/docker-compose.yml`
- Default port: 3001 (from official docs)
- Volume needed: `/app/data` (from official compose)
- No database required, single container
- Has web interface

**vars/main.yaml:**
```yaml
---
# Research Notes - Uptime Kuma
# Official Source: https://github.com/louislam/uptime-kuma
# Example Compose: https://github.com/louislam/uptime-kuma/blob/master/docker/docker-compose.yml
# Default Port: 3001 (official documentation)

uptime_kuma_version: "1"
uptime_kuma_port: 3001  # Official default port

# ALL web interfaces MUST have these set to true
uptime_kuma_configure_homepage: true
uptime_kuma_configure_proxy: true
uptime_kuma_configure_dns: true
```

**What gets created automatically:**
- ✅ Port exposed on host (3001)
- ✅ Firewall rule allowing port 3001
- ✅ Internal NGINX proxy config at `{{ proxy_folder }}/internal/nginx/proxy-confs/uptime_kuma.subdomain.conf`
- ✅ DNS alias `uptime_kuma.{{ internal_domain }}` under proxy host
- ✅ Homepage entry for easy access
- ✅ Health check confirming port is listening

**Run:**
```bash
ansible-playbook -i inventory.yaml deploy_uptime_kuma.yaml --vault-password-file ~/ansible_key
```

**Access:** `https://uptime-kuma.{{ internal_domain }}`

---

## Quick Reference: Common Applications

Here are some commonly deployed applications with their default configurations for quick reference:

| Application | Default Port | Image | Volume Mounts | Web Interface | Notes |
|------------|--------------|-------|---------------|---------------|-------|
| Uptime Kuma | 3001 | louislam/uptime-kuma:1 | /app/data | Yes | Monitoring tool |
| Portainer | 9000 (HTTP), 9443 (HTTPS) | portainer/portainer-ce | /data | Yes | Docker management |
| Nextcloud | 80 | nextcloud:latest | /var/www/html | Yes | Requires DB (postgres/mysql) |
| Vaultwarden | 80 | vaultwarden/server | /data | Yes | Bitwarden alternative |
| Heimdall | 80 (HTTP), 443 (HTTPS) | linuxserver/heimdall | /config | Yes | Dashboard |
| Jellyfin | 8096 | jellyfin/jellyfin | /config, /cache, /media | Yes | Media server |
| Plex | 32400 | plexinc/pms-docker | /config, /media | Yes | Media server |
| Sonarr | 8989 | linuxserver/sonarr | /config, /tv | Yes | TV show management |
| Radarr | 7878 | linuxserver/radarr | /config, /movies | Yes | Movie management |
| Prowlarr | 9696 | linuxserver/prowlarr | /config | Yes | Indexer manager |
| Lidarr | 8686 | linuxserver/lidarr | /config, /music | Yes | Music management |
| qBittorrent | 8080 (WebUI), 6881 (TCP) | linuxserver/qbittorrent | /config, /downloads | Yes | Torrent client |
| Traefik | 80, 443, 8080 | traefik:latest | /etc/traefik | Yes (8080) | Reverse proxy |
| Nginx Proxy Manager | 80, 443, 81 | jc21/nginx-proxy-manager | /data, /letsencrypt | Yes (81) | Proxy manager |
| Pi-hole | 53 (DNS), 80 (Web) | pihole/pihole | /etc/pihole, /etc/dnsmasq.d | Yes | DNS/Ad blocker |
| Home Assistant | 8123 | homeassistant/home-assistant | /config | Yes | Home automation |
| Grafana | 3000 | grafana/grafana | /var/lib/grafana | Yes | Monitoring/visualization |
| Prometheus | 9090 | prom/prometheus | /prometheus | Yes | Metrics database |
| Loki | 3100 | grafana/loki | /loki | No | Log aggregation |
| n8n | 5678 | n8nio/n8n | /home/node/.n8n | Yes | Workflow automation |

**Note:** Always verify these defaults against current official documentation, as they may change over time.

---

*This guide is designed to be used as a reference by GitHub Copilot and human developers when creating new Ansible roles.*
