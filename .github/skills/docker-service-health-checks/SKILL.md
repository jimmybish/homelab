---
name: docker-service-health-checks
description: 'Use when: verifying Docker service ports are open and listening after deployment, adding health check assertions to Ansible roles, or validating that a deployed container is actually running and reachable.'
---

# Docker Service Health Checks

Ansible task pattern for verifying that a Docker service is running and its ports are listening after deployment.

## When to Use

- After deploying a Docker service via Ansible
- To assert that expected ports are open and listening
- As a post-deployment validation step in `tasks/main.yaml`

## MANDATORY GATE: a 200 through SWAG is not proof

A bare HTTP 2xx is NEVER sufficient evidence that a service is healthy.
Reverse proxies (SWAG internal and external) happily return 200s, redirects,
or cached error pages for a dead or mis-proxied upstream. Every deployment
verification must assert content, not just status:

- **Assert response BODY markers**: the response contains something only the
  real service emits (product name in the HTML title, API envelope field,
  `/api/version`-style payload). Check the direct URL AND the proxied URL —
  a healthy origin behind a broken proxy config still fails users.
- **Assert response HEADERS**: expected `Server`/`Content-Type`/service
  headers are present and the proxy didn't substitute its own defaults.
- **Check metrics where an exporter exists**: `up`, CPU, memory, disk, recent
  logs (Grafana MCP). Do not assume health from a single signal.
- A verification that only saw `200`/`301`/`302` without body+header content
  assertions has NOT verified the service. State that honestly rather than
  claiming success.

In Ansible, use `uri` with `return_content: true` plus `until`/`failed_when`
on extracted body fields (e.g. `result.json.status == "ok"` or body contains
the expected marker) rather than `status_code: 200` alone.

## Task Pattern

```yaml
- name: Bring containers up
  community.docker.docker_compose_v2:
    project_src: "{{ <service>_folder }}"
    state: present

- name: Check if service ports are open and listening
  community.general.listen_ports_facts:

- name: Assert service ports are listening
  ansible.builtin.assert:
    that:
      - <service>_port | int in (ansible_facts.tcp_listen | map(attribute='port') | list)
    fail_msg: "<service> port is not open and listening!"
    success_msg: "<service> port is open and listening."
```

## Notes

- Place this after the `community.docker.docker_compose_v2` task in `tasks/main.yaml` that starts the service containers
- For multi-port services, add each port to the `that` list as a separate assertion
- For services with dynamic or ephemeral ports, determine the actual bound port first and store it in a variable before asserting
- Uses the `community.general.listen_ports_facts` module to gather TCP/UDP listening ports
- The assertion will fail the playbook if the service didn't start correctly

## Container-to-Host Dependencies

When a container calls another service on the same Docker host, verify DNS and
the endpoint from inside the container. A host FQDN that resolves to `127.0.1.1`
works on the host but points at the container's own loopback namespace.

Preserve the service FQDN by adding an explicit Compose mapping when needed:

```yaml
extra_hosts:
  - "service.example.internal:host-gateway"
```

After deployment, resolve the FQDN and perform any authenticated health or model
discovery request from inside the application container. Host-level checks alone
do not validate the container network path.
