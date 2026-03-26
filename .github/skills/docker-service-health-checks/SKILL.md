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

- Place this after the `docker_compose_v2` task that brings containers up
- For multi-port services, add each port to the `that` list as a separate assertion
- Uses the `community.general.listen_ports_facts` module to gather TCP/UDP listening ports
- The assertion will fail the playbook if the service didn't start correctly
