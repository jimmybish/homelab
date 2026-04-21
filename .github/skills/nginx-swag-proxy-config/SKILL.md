---
name: nginx-swag-proxy-config
description: 'Use when: creating NGINX reverse proxy configurations for SWAG, deploying subdomain proxy templates, checking LinuxServer reverse-proxy-confs for pre-built configs, or configuring proxy pass rules for Docker services behind SWAG internal proxy.'
---

# NGINX / SWAG Reverse Proxy Configuration

Patterns for creating and deploying NGINX reverse proxy configurations via SWAG (Secure Web Application Gateway) for Docker services.

## Environment Overview

This environment runs **two SWAG reverse proxy instances** on the same host (`proxy_host`):

| Instance | Container Name | Config Folder | Purpose |
|----------|---------------|---------------|---------|
| **Internal** | `swag_internal` | `{{ proxy_folder }}/internal/` | LAN traffic — services accessed via `<service>.{{ internal_domain }}` |
| **External** | `swag_external` | `{{ proxy_folder }}/external/` | Externally incoming traffic from the internet |

- NGINX proxy configs are placed under `<folder>/nginx/proxy-confs/`
- Most services only need an internal proxy config
- External configs are only added for services that need public internet access. Ask the user if unsure whether external access is needed, never assume it is.

## When to Use

- A service has a web interface that needs to be accessible via `<service>.{{ internal_domain }}`
- Creating a `<service>.subdomain.conf.j2` template for an Ansible role
- Deploying a proxy config to the SWAG internal or external instance
- Adapting a LinuxServer pre-built NGINX config for your environment

## Check for Existing Configs First

**IMPORTANT: Always check LinuxServer before writing a custom config!**

- Visit: `https://github.com/linuxserver/reverse-proxy-confs`
- Search for your application name (e.g., "grafana", "sonarr", "uptime-kuma")
- If a configuration exists, use it as the basis for your template (may need minor adjustments)
- These configs are battle-tested and include proper headers, websocket support, and security settings

**Example:** For Grafana, a pre-built config exists at:
`https://github.com/linuxserver/reverse-proxy-confs/blob/master/grafana.subdomain.conf.sample`

## Subdomain Config Template

If no pre-built configuration exists, create `templates/<service>.subdomain.conf.j2`:

```nginx
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;

    server_name <service>.{{ internal_domain }};

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

## Tips for Adapting LinuxServer Configs

- Replace hardcoded IPs with `{{ <service>_proxy_ip }}`
- Replace hardcoded ports with `{{ <service>_port }}`
- Keep all the special headers and proxy settings (they're there for a reason!)
- Maintain websocket support blocks if present (required for real-time features)
- Keep security headers and authentication blocks

## Ansible Deployment Tasks

Add these to `tasks/main.yaml` after the health check.

### Internal Proxy (most services)

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
    - Restart Swag Internal
```

### External Proxy (only if public internet access is needed)

```yaml
- name: Deploy service nginx config to SWAG External
  ansible.builtin.template:
    src: <service>.subdomain.conf.j2
    dest: "{{ proxy_folder }}/external/nginx/proxy-confs/<service>.subdomain.conf"
    mode: '0644'
  delegate_to: "{{ groups['proxy_host'][0] }}"
  notify:
    - Restart Swag External
```
