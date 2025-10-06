# n8n Ansible Role

This role deploys n8n workflow automation platform to a Docker host.

## Overview

n8n is a workflow automation tool that allows you to connect various services and automate tasks. This role deploys n8n as a Docker container without Traefik, suitable for internal network access through an nginx reverse proxy.

## Requirements

- Docker and Docker Compose installed on the target host
- `docker_user` defined in group_vars
- `internal_domain` configured for internal network access

## Variables

### Role Variables (vars/main.yaml)
- `n8n_version`: Docker image version (default: "latest")
- `n8n_port`: Port to expose n8n on (default: 5678)
- `n8n_configure_proxy`: Enable/disable automatic proxy configuration (default: true)
- `n8n_configure_homepage`: Enable/disable automatic homepage bookmark (default: true)

### Group Variables Required
- `n8n_folder`: Base directory for n8n data (default: "/etc/docker-storage/n8n")
- `internal_n8n_url`: Internal URL for accessing n8n (e.g., "https://n8n.internal-domain.home")
- `timezone`: Timezone for n8n (e.g., "Australia/Hobart")
- `docker_user_puid`: User ID for Docker containers
- `docker_user_pgid`: Group ID for Docker containers
- `docker_user`: Username for Docker containers

## Features

- Single container deployment (n8n without Traefik)
- Data persistence via Docker volumes
- Local file support through mounted directory
- Configured for internal network access
- **Automatic nginx proxy configuration** (optional, enabled by default)
- WebSocket support for real-time workflow updates

## Directory Structure

The role creates the following directories:
- `{{ n8n_folder }}` - Main n8n directory
- `{{ n8n_folder }}/local-files` - Directory for file-based workflows

## Deployment

Deploy using the playbook:
```bash
ansible-playbook -i inventory.yaml deploy_n8n.yaml --ask-vault-pass
```

Or deploy to specific host:
```bash
ansible-playbook -i inventory.yaml deploy_n8n.yaml --limit docker-2 --ask-vault-pass
```

## Access

After deployment, n8n will be accessible at:
- Internal: `{{ internal_n8n_url }}` (requires nginx proxy configuration)
- Direct: `http://<docker-2-ip>:5678` (not recommended for production)

## Proxy Configuration

By default, the n8n role will automatically:
1. Deploy nginx configuration to the proxy_host
2. Configure routing for `https://n8n.{{ internal_domain }}`
3. Restart SWAG to apply changes

To disable automatic proxy configuration:
```yaml
n8n_configure_proxy: false
```

## Homepage Configuration

By default, the n8n role will automatically:
1. Add an n8n bookmark to the homepage bookmarks.yaml
2. Create an "Automation" section if it doesn't exist
3. Restart the Homepage container to apply changes

To disable automatic homepage configuration:
```yaml
n8n_configure_homepage: false
```

## Next Steps

After deploying n8n:
1. Access n8n at `https://n8n.{{ internal_domain }}`
2. Create your first n8n user account
3. Configure any required integrations
4. Build your first automation workflow!

## Notes

- This deployment uses Docker volumes for persistent data storage
- The `/files` directory inside n8n maps to `{{ n8n_folder }}/local-files` on the host
- n8n is configured to run with the Docker service account user
- The deployment expects HTTPS access via reverse proxy (N8N_PROTOCOL=https)
