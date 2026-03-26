---
name: ansible-role-scaffolding
description: 'Use when: creating a new Ansible role directory structure, defining role variables, setting up the standard role skeleton with tasks/templates/vars/handlers, or determining variable naming conventions and feature flags for a service deployment.'
---

# Ansible Role Scaffolding

Standard directory structure, variable patterns, and conventions for creating Ansible roles that deploy Docker-based homelab services.

## When to Use

- Creating a brand new Ansible role for a service
- Setting up the directory skeleton for a role
- Defining `vars/main.yaml` with correct naming conventions
- Determining feature flags (proxy, DNS, homepage) for a service

## Prerequisites

Before creating a new role, ensure you understand:
- The service you're deploying (use the `application-research` skill first)
- Whether it has a web interface, and if so, whether it should be routed through the reverse proxy (ask the user if not obvious)
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

## Create Role Directory Structure

```bash
cd ansible/roles
mkdir -p <service>/{tasks,templates,vars,handlers}
touch <service>/tasks/main.yaml
touch <service>/vars/main.yaml
touch <service>/handlers/main.yaml
```

## Define Variables (`vars/main.yaml`)

Use research findings (from the `application-research` skill) to populate accurate values:

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

## Decision Points

- **Has web interface?**
  - **YES** → Set all of: `configure_proxy: true`, `configure_dns: true`, `configure_homepage: true`
  - Web interface services MUST expose ports and MUST configure internal SWAG proxy
  - Homepage link is ALWAYS added for web interfaces
  - All services are accessed via internal network through the SWAG internal proxy
