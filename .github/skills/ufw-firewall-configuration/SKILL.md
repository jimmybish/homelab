---
name: ufw-firewall-configuration
description: 'Use when: opening ports via UFW in Ansible tasks, configuring firewall rules for Docker services, or allowing TCP/UDP traffic through the host firewall on Debian-based systems.'
---

# UFW Firewall Configuration

Ansible task pattern for opening ports via UFW on Debian-based hosts.

## When to Use

- A Docker service exposes a port that needs to be accessible
- A service has a web interface (firewall rules are REQUIRED for proxy access)
- You need to allow TCP or UDP traffic through the host firewall

## Task Pattern

```yaml
- name: Allow service port through UFW
  community.general.ufw:
    rule: allow
    port: "{{ <service>_port }}"
    proto: tcp
  when: ansible_os_family == "Debian"
```

## Notes

- If the service has a web interface, firewall rules are REQUIRED since ports must be exposed for proxy access
- Use `proto: tcp` for HTTP/HTTPS services (most common)
- Use `proto: udp` for UDP-based services (e.g., DNS, SNMP)
- The `when: ansible_os_family == "Debian"` guard ensures this only runs on Debian/Ubuntu hosts
- For services with multiple ports, add one task per port
