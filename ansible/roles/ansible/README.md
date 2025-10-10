# Ansible Role: ansible

This role installs and configures Ansible on management hosts.

## Overview

- **Service Type**: CLI tool (no web interface)
- **Installation Method**: Python virtual environment or system package
- **Target Hosts**: mgmt_hosts group
- **Official Documentation**: https://docs.ansible.com/

## Features

- Installs Ansible in a Python virtual environment for version control
- Configures Ansible with optimized settings for homelab use
- Installs required Python dependencies and Ansible collections
- Creates symlinks for easy command-line access
- Sets up bash environment for Ansible tools
- Configures SSH settings for remote host management

## Requirements

- Python 3.10 or higher
- Sudo privileges on management hosts
- SSH access to managed hosts

## Role Variables

All variables are defined in `vars/main.yaml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `ansible_package_version` | `latest` | Version of Ansible to install |
| `ansible_install_method` | `pip` | Installation method: `pip` or `package` |
| `ansible_python_version` | `3.10` | Required Python version |
| `ansible_venv_path` | `/opt/ansible-venv` | Virtual environment location |
| `ansible_config_dir` | `/etc/ansible` | Ansible configuration directory |
| `ansible_ssh_key_path` | `~/.ssh/ansible` | SSH key for Ansible connections |

## Dependencies

### Python Packages
- docker
- docker-compose
- requests
- jmespath
- netaddr
- dnspython
- pytz

### Ansible Collections
- community.general
- community.docker
- ansible.posix

## Usage

### Deploy Ansible

```bash
ansible-playbook -i inventory.yaml deploy_ansible.yaml --vault-password-file ~/ansible_key
```

### Verify Installation

After deployment, SSH into a management host and verify:

```bash
ansible --version
ansible-galaxy collection list
```

### Using Ansible

The role sets up:
- Ansible commands available via symlinks in `/usr/local/bin/`
- Virtual environment activated in `.bashrc`
- Command completion for all Ansible tools
- Configuration file at `/etc/ansible/ansible.cfg`

## Post-Installation

### SSH Key Setup

If you don't have an SSH key for Ansible, create one:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/ansible -C "ansible-automation"
```

Then copy the public key to all managed hosts:

```bash
ssh-copy-id -i ~/.ssh/ansible.pub user@managed-host
```

### Test Connection

Test Ansible connectivity to your hosts:

```bash
ansible all -m ping -i inventory.yaml
```

## Configuration

The role deploys an optimized `ansible.cfg` with:
- Smart fact gathering and caching
- SSH pipelining for performance
- YAML output format
- Timing and profiling callbacks
- Proper logging configuration

## Files Created

- `/opt/ansible-venv/` - Python virtual environment
- `/etc/ansible/ansible.cfg` - Ansible configuration
- `/usr/local/bin/ansible*` - Symlinks to Ansible commands
- `~/.bashrc` - Environment variables and completion
- `/var/log/ansible.log` - Ansible execution log

## Notes

- This role does NOT create the homelab repository structure
- It assumes you have your playbooks and inventory already set up
- SSH keys must be manually distributed to managed hosts
- The role installs Ansible but doesn't configure specific inventories
- No web interface (feature flags all set to false)

## Troubleshooting

### Virtual Environment Issues

If the virtual environment isn't activated in new shells:

```bash
source /opt/ansible-venv/bin/activate
```

Or ensure `.bashrc` changes are sourced:

```bash
source ~/.bashrc
```

### Collection Installation Failures

Manually install collections:

```bash
/opt/ansible-venv/bin/ansible-galaxy collection install community.general --force
```

### Python Dependencies Missing

Reinstall dependencies:

```bash
/opt/ansible-venv/bin/pip install -r requirements.txt
```

## Examples

### Run a playbook from management host

```bash
ansible-playbook -i /path/to/inventory.yaml playbook.yaml --vault-password-file ~/ansible_key
```

### Check host facts

```bash
ansible hostname -m setup -i inventory.yaml
```

### Run ad-hoc commands

```bash
ansible all -m shell -a "uptime" -i inventory.yaml
```

## References

- [Ansible Installation Guide](https://docs.ansible.com/ansible/latest/installation_guide/intro_installation.html)
- [Ansible Configuration](https://docs.ansible.com/ansible/latest/reference_appendices/config.html)
- [Best Practices](https://docs.ansible.com/ansible/latest/user_guide/playbooks_best_practices.html)
