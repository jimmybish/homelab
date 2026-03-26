---
name: ansible-vault-secrets
description: 'Use when: encrypting secrets with Ansible Vault, adding vault-encrypted variables to group_vars, or managing sensitive configuration values like API keys, passwords, or tokens for service deployments.'
---

# Ansible Vault Secrets Management

Patterns for encrypting and storing secrets using Ansible Vault in the homelab environment.

## When to Use

- Encrypting API keys, passwords, or tokens for a service
- Adding vault-encrypted variables to `group_vars/all/vars.yaml`
- Setting up global variables for a new service deployment

## Encrypting a Secret

```bash
ansible-vault encrypt_string --vault-password-file ~/ansible_key 'secret_value' --name 'variable_name'
```

This outputs a vault-encrypted block that can be pasted directly into a vars file.

## Adding Global Variables

When deploying a new service, add its configuration to `group_vars/all/vars.yaml`:

```yaml
# <Service> Configuration
<service>_folder: "/etc/docker-storage/<service>"
internal_<service>_url: "https://<service>.{{ internal_domain }}"
homepage_<service>_key: !vault |
          $ANSIBLE_VAULT;1.1;AES256
          <encrypted_key>
```

## Notes

- The vault password file is located at `~/ansible_key`
- All playbooks that use vault secrets must include `--vault-password-file ~/ansible_key`
- Never commit plain-text credentials — always use `ansible-vault encrypt_string`
- Vault-encrypted values are safe to commit to version control
