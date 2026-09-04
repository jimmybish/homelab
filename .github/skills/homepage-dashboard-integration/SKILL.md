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

Create `ansible/templates/homepage/<service>_service.yaml.j2`. Templates are
playbook-level because both the service role and the centralized Homepage
rebuild use them.

Template checklist:

1. Check whether the service has a supported Homepage widget at `https://gethomepage.dev/widgets/`.
2. If no widget exists for the service, omit the `widget` block entirely.
3. Keep the YAML indentation exactly as shown below.

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

**CRITICAL: Each Homepage section (Slop, Media, Smart Home, Infra, ARR, etc.) must use ONE consistent indentation style throughout. Mixing 2-space and 4-space list items in the same section triggers `bad indentation of a sequence entry` and prevents `services.yaml` from loading at all — Homepage shows no services.**

Before creating a new template, check the target section's existing entries and match them exactly:

- **Media section** currently uses 2-space list items / 6-space properties
- **Slop, Smart Home, Infra, ARR sections** currently use 4-space list items / 8-space properties

Both styles are valid YAML; the requirement is consistency within a section.

**2-space style** (for the Media section):

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

**4-space style** (for Slop, Smart Home, Infra, ARR):

```yaml
    - <Service Name>:
        icon: <service>.png
        href: {{ internal_<service>_url }}
        description: Service description
```

After adding a new service, verify the merged `services.yaml` on the Homepage host and check `docker logs homepage --tail 20` for `YAMLException` errors.

## Role Integration Task

Add to `tasks/main.yaml` after DNS configuration:

```yaml
# Configure Homepage Services (REQUIRED for web interfaces)
- name: Add <Service Name> to Homepage <Section> section
  ansible.builtin.include_tasks:
    file: "{{ playbook_dir }}/tasks/homepage_add_service.yaml"
  vars:
    _homepage_service_name: <Service Name>
    _homepage_marker: <service> service
    _homepage_template: <service>_service.yaml.j2
    _homepage_insertafter: "^- <Section>:"
  when: <service>_configure_homepage | default(true)
  tags:
    - homepage
    - homepage_config

- name: Notify Homepage restart for <service>
  ansible.builtin.debug:
    msg: "<service> homepage entry updated"
  changed_when: _homepage_service_result is changed
  notify:
    - Restart Homepage
  when: <service>_configure_homepage | default(true)
  tags:
    - homepage
    - homepage_config
```

## Centralized Rebuild Registration

Every service integration MUST also be registered in
`ansible/tasks/homepage_populate_all_services.yaml`. Homepage can replace its
base `services.yaml` during deployment; this registry restores all managed
service blocks afterward. A role-only integration will disappear after such a
rebuild.

Add the service under its target section using the same marker, template,
insertion point, condition, and tags as the role task:

```yaml
- name: "Homepage config: <Service Name>"
  ansible.builtin.include_tasks:
    file: homepage_add_service.yaml
  vars:
    _homepage_service_name: <Service Name>
    _homepage_marker: <service> service
    _homepage_template: <service>_service.yaml.j2
    _homepage_insertafter: "^- <Section>:"
  when: <service>_configure_homepage | default(true)
  tags:
    - homepage
    - homepage_config
```

## Important Notes

- **DO** use `homepage_add_service.yaml` in both the role and rebuild registry
- **DO** keep marker, template, insertion point, condition, and tags identical in both locations
- **DO** add the bridge notification task — ensures Homepage restarts when deploying a single service
- **DO NOT** add a check to skip if the block already exists — the shared `blockinfile` task updates it idempotently
- This allows the role to update Homepage entries when you add new services or modify descriptions
- The shared task's `blockinfile` operation only triggers changes when the block content differs
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
- Updating every affected service entry after a shared Homepage template change

**Use `--tags homepage_config` when:**
- Only updating Homepage config files (settings, widgets, bookmarks)
- Making changes to Homepage base template without re-running service integrations
- Faster execution when infrastructure is already in place

**Use full playbook (no tags) when:**
- Initial deployment of new services
- Major infrastructure changes
- You want to ensure everything is in sync
