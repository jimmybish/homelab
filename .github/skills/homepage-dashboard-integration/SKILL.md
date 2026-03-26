---
name: homepage-dashboard-integration
description: 'Use when: adding a service to the Homepage dashboard, creating homepage_service.yaml.j2 templates with correct YAML indentation, using blockinfile to insert service entries, or understanding the homepage/homepage_config tagging strategy for targeted playbook execution.'
---

# Homepage Dashboard Integration

Patterns for adding services to the Homepage dashboard via Ansible, including template creation, indentation rules, blockinfile tasks, and the tagging strategy.

## When to Use

- A service has a web interface and needs to appear on the Homepage dashboard
- Creating a `homepage_service.yaml.j2` template for an Ansible role
- Adding or updating a `blockinfile` task for Homepage integration
- Understanding when to use `--tags homepage` vs `--tags homepage_config`

## Service Template

Create `templates/homepage_service.yaml.j2`:

**Before adding a widget**, check whether the service has a supported Homepage service widget at `https://gethomepage.dev/widgets/`. If no widget exists for the service, omit the `widget` block entirely.

**CRITICAL: Use correct YAML indentation (2 spaces for list items, 6 spaces for properties)**

**With widget** (only if listed at `https://gethomepage.dev/widgets/`):

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

**Without widget** (service not listed on Homepage widgets page):

```yaml
  - <Service Name>:
      icon: <service>.png
      href: {{ internal_<service>_url }}
      description: Service description
```

### Indentation Rules

- List items (starting with `-`): 2 spaces from section level
- Service properties (`icon`, `href`, etc.): 6 spaces from section level
- Widget properties: 8 spaces from section level
- **NEVER use 4 spaces** — this will break the YAML structure

## Ansible Task

Add to `tasks/main.yaml` after DNS configuration:

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
  tags:
    - homepage
    - homepage_config
```

## Important Notes

- **DO** add `notify: Restart Homepage` — ensures Homepage restarts when deploying a single service
- **DO NOT** add a check to skip if the block already exists — `blockinfile` will automatically update the block if the content changes
- This allows the role to update Homepage entries when you add new services or modify descriptions
- The `blockinfile` module is idempotent and will only trigger changes when the block content actually differs
- All web interfaces MUST appear on Homepage for easy access
- When running `master_playbook.yaml --tags homepage_config`, handlers are flushed and one final restart occurs

## Tagging Strategy

All roles that integrate with Homepage use standardized tags to enable targeted playbook execution.

### Tag Definitions

**`homepage`** — Applied to all Homepage infrastructure and setup tasks
- Homepage role: All tasks (directory setup, Docker deployment, networking, DNS, proxy)
- Other roles: Homepage service integration tasks (blockinfile operations)

**`homepage_config`** — Applied to Homepage configuration file deployment tasks
- Homepage role: Config file deployment tasks (bookmarks, docker, services, settings, widgets)
- Other roles: Homepage service integration tasks (same as `homepage` tag)

### When to Use Tagged Execution

**Use `--tags homepage` when:**
- Adding a new service and want to update Homepage immediately
- Modifying Homepage service entries (descriptions, icons, widgets)
- Troubleshooting Homepage integration issues
- Re-deploying Homepage after configuration changes
- Updating all service entries after Homepage template changes

**Use `--tags homepage_config` when:**
- Only updating Homepage config files (settings, widgets, bookmarks)
- Making changes to Homepage base template without re-running service integrations
- Faster execution when infrastructure is already in place

**Use full playbook (no tags) when:**
- Initial deployment of new services
- Major infrastructure changes
- You want to ensure everything is in sync
