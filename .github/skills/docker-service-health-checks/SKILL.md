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
