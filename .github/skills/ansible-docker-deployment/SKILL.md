---
name: ansible-docker-deployment
description: 'Use when: writing the main tasks/main.yaml for an Ansible role that deploys a Docker service, setting up Docker prerequisites, creating service directories, deploying docker-compose templates, running docker-compose up, or creating handlers/main.yaml with restart handlers for Docker services, Homepage, or SWAG proxy instances. Covers the standard task ordering and handler patterns for Docker-based Ansible roles.'
---

# Ansible Docker Deployment Tasks

Standard task sequence for `tasks/main.yaml` in Ansible roles that deploy Docker-based services. Covers Docker prerequisites, directory setup, and compose deployment.

## When to Use

- Writing the core `tasks/main.yaml` for a new Docker service role
- Setting up Docker prerequisites in a playbook
- Creating service directories (single or multi-container)
- Deploying a docker-compose template and bringing containers up

## Task Order

Follow this order in `tasks/main.yaml`:

1. Docker Prerequisites
2. Directory Setup
3. Firewall Configuration *(use `ufw-firewall-configuration` skill)*
4. Deploy Configuration Files & Run Compose
5. Health Check *(use `docker-service-health-checks` skill)*
6. Proxy Configuration *(use `nginx-swag-proxy-config` skill)*
7. DNS Configuration *(use `pfsense-dns-management` skill)*
8. Homepage Integration *(use `homepage-dashboard-integration` skill)*

## 1. Docker Prerequisites

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

## 2. Directory Setup

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

## 3. Deploy Configuration Files & Run Compose

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

## 4. Create Handlers (`handlers/main.yaml`)

Every role that deploys a Docker service should include a handler to restart it. Roles that configure proxy or Homepage integration should also include the corresponding delegated handlers.

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

- name: Restart Swag Internal
  community.docker.docker_compose_v2:
    project_src: "{{ proxy_folder }}"
    services:
      - swag_internal
    state: restarted
  delegate_to: "{{ groups['proxy_host'][0] }}"

- name: Restart Swag External
  community.docker.docker_compose_v2:
    project_src: "{{ proxy_folder }}"
    services:
      - swag_external
    state: restarted
  delegate_to: "{{ groups['proxy_host'][0] }}"
```

### Handler Notes

- **Start/Restart service:** Include in every role. Triggered by compose template changes via `notify`.
- **Restart Homepage:** Include if the role has Homepage integration (`notify: Restart Homepage` from blockinfile task).
- **Restart Swag Internal:** Include if the role deploys an internal proxy config. Most web-facing services use this.
- **Restart Swag External:** Include only if the role deploys an external proxy config (public internet access).
- Only include the handlers your role actually notifies — omit unused ones.
