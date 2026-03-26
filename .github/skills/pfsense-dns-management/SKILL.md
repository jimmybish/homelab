---
name: pfsense-dns-management
description: 'Use when: managing DNS host overrides and aliases in pfSense via its API, creating or updating DNS entries for services, determining proxied vs non-proxied alias parents, or cleaning up stale DNS aliases. Covers the full pfSense DNS resolver API workflow.'
---

# pfSense DNS Management

Multi-step Ansible workflow for managing DNS host overrides and service aliases in pfSense via the REST API (v2).

## When to Use

- A service needs a DNS entry (e.g., `<service>.{{ internal_domain }}`)
- Ensuring a deployment host has a DNS host override in pfSense
- Creating or updating service DNS aliases
- Cleaning up stale aliases that point to the wrong parent host
- Changing a service between proxied and non-proxied DNS routing

## Strategy

- ALL roles must ensure their deployment target host has a DNS host override in pfSense
- If the host override doesn't exist, it will be created automatically
- Services create an alias that points to EITHER the proxy host OR their deployment host
- The `<service>_is_proxied` variable (default: `true`) determines the alias parent:
  - `true`: Alias points to proxy host (most services — Grafana, arr suite, Homepage, etc.)
  - `false`: Alias points to deployment host (services like Plex that aren't proxied)

## Variable Configuration

Add to `vars/main.yaml`:

```yaml
# DNS Configuration
<service>_configure_dns: true
<service>_is_proxied: true  # Set to false for services NOT behind reverse proxy (e.g., Plex)
```

## Task Pattern (All 9 Steps)

Add to `tasks/main.yaml` after proxy configuration:

```yaml
# Step 1: Get current DNS overrides from pfSense
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

# Step 2: Ensure deployment host override exists
- name: Find deployment host override entry in pfSense
  ansible.builtin.set_fact:
    deployment_host_override: >-
      {{ pfsense_dns_overrides.json.data
         | selectattr('host', 'equalto', inventory_hostname.split('.')[0])
         | selectattr('domain', 'equalto', internal_domain)
         | list
         | first
         | default({}) }}
  when:
    - <service>_configure_dns | default(false)
    - pfsense_dns_overrides.json.data is defined

- name: Create host override for deployment target if missing
  ansible.builtin.uri:
    url: "https://{{ groups['pfsense_host'][0] }}/api/v2/services/dns_resolver/host_override"
    method: POST
    headers:
      X-API-Key: "{{ pfsense_api_key }}"
      Content-Type: "application/json"
    body_format: json
    body:
      host: "{{ inventory_hostname.split('.')[0] }}"
      domain: "{{ internal_domain }}"
      ip: ["{{ ansible_default_ipv4.address }}"]
      descr: "{{ inventory_hostname.split('.')[0] | title }} host"
    validate_certs: false
    status_code: [200, 201]
  delegate_to: localhost
  register: host_override_creation
  when:
    - <service>_configure_dns | default(false)
    - deployment_host_override | length == 0

- name: Apply DNS resolver changes after host override creation
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
    - host_override_creation is succeeded

# Step 3: Refresh DNS overrides if we created the host override
- name: Refresh DNS resolver host overrides after creation
  ansible.builtin.uri:
    url: "https://{{ groups['pfsense_host'][0] }}/api/v2/services/dns_resolver/host_overrides"
    method: GET
    headers:
      X-API-Key: "{{ pfsense_api_key }}"
    validate_certs: false
    return_content: true
  register: pfsense_dns_overrides
  delegate_to: localhost
  when:
    - <service>_configure_dns | default(false)
    - host_override_creation is succeeded

- name: Re-find deployment host override entry in pfSense
  ansible.builtin.set_fact:
    deployment_host_override: >-
      {{ pfsense_dns_overrides.json.data
         | selectattr('host', 'equalto', inventory_hostname.split('.')[0])
         | selectattr('domain', 'equalto', internal_domain)
         | list
         | first
         | default({}) }}
  when:
    - <service>_configure_dns | default(false)
    - host_override_creation is succeeded

# Step 4: Find proxy host override (for proxied services)
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
    - <service>_is_proxied | default(true)
    - pfsense_dns_overrides.json.data is defined

# Step 5: Determine parent host override based on is_proxied variable
- name: Set parent host override for service alias
  ansible.builtin.set_fact:
    <service>_parent_override: "{{ proxy_host_override if (<service>_is_proxied | default(true)) else deployment_host_override }}"
  when:
    - <service>_configure_dns | default(false)

# Step 5.5: Find and delete any existing aliases in incorrect locations
- name: Find all existing aliases for service across all host overrides
  ansible.builtin.set_fact:
    <service>_all_existing_aliases: >-
      {{ pfsense_dns_overrides.json.data
         | selectattr('aliases', 'defined')
         | map(attribute='aliases')
         | flatten
         | selectattr('host', 'equalto', '<service>')
         | selectattr('domain', 'equalto', internal_domain)
         | list }}
  when:
    - <service>_configure_dns | default(false)
    - pfsense_dns_overrides.json.data is defined

- name: Identify aliases to delete (not under correct parent)
  ansible.builtin.set_fact:
    <service>_aliases_to_delete: >-
      {{ <service>_all_existing_aliases
         | rejectattr('parent_id', 'equalto', <service>_parent_override.id | default(0))
         | list }}
  when:
    - <service>_configure_dns | default(false)
    - <service>_all_existing_aliases is defined

- name: Delete incorrect service DNS aliases
  ansible.builtin.uri:
    url: "https://{{ groups['pfsense_host'][0] }}/api/v2/services/dns_resolver/host_override/alias"
    method: DELETE
    headers:
      X-API-Key: "{{ pfsense_api_key }}"
      Content-Type: "application/json"
    body_format: json
    body:
      parent_id: "{{ item.parent_id }}"
      id: "{{ item.id }}"
      apply: false
    validate_certs: false
    status_code: [200, 204]
  delegate_to: localhost
  loop: "{{ <service>_aliases_to_delete }}"
  register: <service>_delete_results
  when:
    - <service>_configure_dns | default(false)
    - <service>_aliases_to_delete is defined
    - <service>_aliases_to_delete | length > 0

- name: Apply DNS resolver changes after deletion
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
    - <service>_delete_results is changed

# Step 6: Check if service alias already exists
- name: Check if service alias exists under parent host override
  ansible.builtin.set_fact:
    <service>_alias_exists: >-
      {{ <service>_parent_override.aliases | default([])
         | selectattr('host', 'equalto', '<service>')
         | selectattr('domain', 'equalto', internal_domain)
         | list
         | length > 0 }}
  when:
    - <service>_configure_dns | default(false)
    - <service>_parent_override is defined
    - <service>_parent_override | length > 0

# Step 7: Display DNS configuration status
- name: Display service DNS alias status
  ansible.builtin.debug:
    msg: >-
      {% if <service>_parent_override | length == 0 %}
      Warning: Parent host override not found in pfSense DNS
      {% elif <service>_alias_exists %}
      <service> DNS alias exists under {{ <service>_parent_override.host }}.{{ internal_domain }}
      {% else %}
      <service> DNS alias missing - will be created under {{ <service>_parent_override.host }}.{{ internal_domain }}
      {% endif %}
  when: <service>_configure_dns | default(false)

# Step 8: Create service alias under parent host override
- name: Create service DNS alias under parent host override
  ansible.builtin.uri:
    url: "https://{{ groups['pfsense_host'][0] }}/api/v2/services/dns_resolver/host_override/alias"
    method: POST
    headers:
      X-API-Key: "{{ pfsense_api_key }}"
      Content-Type: "application/json"
    body_format: json
    body:
      parent_id: "{{ <service>_parent_override.id }}"
      host: "<service>"
      domain: "{{ internal_domain }}"
      descr: "<Service description>"
    validate_certs: false
    status_code: [200, 201]
  delegate_to: localhost
  register: <service>_alias_result
  when:
    - <service>_configure_dns | default(false)
    - <service>_parent_override | length > 0
    - not <service>_alias_exists

# Step 9: Apply DNS changes in pfSense
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
    - (<service>_alias_result is succeeded or host_override_creation is succeeded)
```
