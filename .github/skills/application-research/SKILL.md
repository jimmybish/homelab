---
name: application-research
description: 'Use when: researching a new application before deployment, identifying Docker images, finding default ports, locating compose examples, checking LinuxServer reverse-proxy-confs, or documenting application requirements. ALWAYS invoke before defining variables or configuration for a new service.'
---

# Application Research

**ALWAYS start by researching the application before defining any variables or configuration.**

## When to Use

- Before deploying any new Docker service
- When evaluating a self-hosted application
- When you need to identify default ports, volumes, environment variables, or dependencies
- Before creating an Ansible role, Docker Compose file, or any deployment configuration

## Research Checklist

### 1. Find Official Documentation

- Look for official Docker Hub page (e.g., `https://hub.docker.com/r/<vendor>/<app>`)
- Check the official project documentation or GitHub repository
- Review installation guides and deployment examples

### 2. Locate Docker Compose Examples

- Search for official `docker-compose.yaml` examples
- Check the project's GitHub repository (often in `/docker/` or `/examples/` directories)
- Look for community-maintained examples on GitHub or forums

### 3. Identify Default Configuration

- **Default ports:** What port(s) does the application listen on?
- **Volume mounts:** What directories need persistence? (config, data, logs)
- **Environment variables:** What are the required/recommended env vars?
- **Dependencies:** Does it require a database, cache, or other services?
- **User/permissions:** Does it need specific PUID/PGID settings?
- **Proxy configuration:** Check `https://github.com/linuxserver/reverse-proxy-confs` for pre-built NGINX configs

### 4. Understand Application Architecture

- Is it a single container or multi-container stack?
- Does it have a web interface?
- Does it need to communicate with external services?
- Are there any special networking requirements?

### 5. Review Best Practices

- Security recommendations (authentication, SSL requirements)
- Performance tuning (memory limits, CPU constraints)
- Backup requirements (what data needs to be backed up?)
- Update/upgrade procedures

### 6. Check for Security Vulnerabilities

- **CVE databases:** Search for known CVEs against the application and its Docker image
  - Search `https://www.cvedetails.com` or `https://nvd.nist.gov` for the application name
  - Check `https://hub.docker.com` for the image's vulnerability scan results (if available)
- **GitHub Security Advisories:** Review the project's GitHub Security tab for disclosed vulnerabilities
- **Open issues:** Search the project's issue tracker for security-labelled issues
- **End-of-life status:** Confirm the project is actively maintained and receiving security patches
- **Default credentials:** Identify any default usernames/passwords that must be changed on first deploy
- **Document findings:** Note any unpatched CVEs, required mitigations, or version constraints

## Documentation Sources

- Docker Hub: `https://hub.docker.com`
- GitHub: Search for "docker-compose" in project repositories
- LinuxServer.io: `https://docs.linuxserver.io` (excellent documentation for many apps)
- **LinuxServer Reverse Proxy Configs:** `https://github.com/linuxserver/reverse-proxy-confs` (pre-built NGINX configs for 100+ apps)
- Awesome-Selfhosted: `https://github.com/awesome-selfhosted/awesome-selfhosted`
- Project wikis and official documentation sites
- **CVE Details:** `https://www.cvedetails.com` (search by vendor/product)
- **NVD:** `https://nvd.nist.gov` (National Vulnerability Database)
- **GitHub Advisory Database:** `https://github.com/advisories`

## Document Findings

After research, document findings in this format (e.g. as comments in `vars/main.yaml`):

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
# - Known CVEs: none / list any unpatched or relevant CVEs
# - Default Credentials: none / describe any that must be changed
# - Security Notes: <any mitigations, version pins, or hardening required>
```

## Example Research Process

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

**⚠️ Never guess configuration values - always reference official documentation or working examples.**
