---
name: docker-compose-templating
description: 'Use when: creating or modifying Docker Compose Jinja2 templates for Ansible roles, deciding between single-container and multi-container stack patterns, configuring ports, volumes, networks, or environment variables in docker_compose.yaml.j2 files.'
---

# Docker Compose Templating

Patterns for generating `docker_compose.yaml.j2` Jinja2 templates used by Ansible roles to deploy Docker services.

## When to Use

- Creating a new `docker_compose.yaml.j2` template for an Ansible role
- Deciding between single-container vs multi-container stack layout
- Configuring port mappings, volumes, networks, or environment variables
- Adding a database or cache sidecar to an existing service

## Single-Container Services

```yaml
services:
  <service>:
    image: <image>:{{ <service>_version }}
    container_name: <service>
    restart: unless-stopped
    user: "{{ docker_user_puid }}"
    environment:
      - PUID={{ docker_user_puid }}
      - PGID={{ docker_user_pgid }}
      - TZ={{ timezone }}
    ports:
      - '{{ <service>_port }}:<internal_port>'  # REQUIRED if service has web interface
    volumes:
      - {{ <service>_folder }}:/config
    # No explicit network needed - default Docker bridge network is sufficient
```

## Multi-Container Services

Use the `<service>-stack` folder pattern:

```yaml
# vars/main.yaml defines:
# <service>_parent_folder: "{{ docker_storage_folder }}/<service>-stack"
# <service>_folder: "{{ <service>_parent_folder }}/<service>"
# <service>_database_folder: "{{ <service>_parent_folder }}/database"

services:
  <service>:
    image: <image>:{{ <service>_version }}
    container_name: <service>
    restart: unless-stopped
    user: "{{ docker_user_puid }}"
    environment:
      - PUID={{ docker_user_puid }}
      - PGID={{ docker_user_pgid }}
      - TZ={{ timezone }}
      - DATABASE_URL=postgresql://db:5432/appdb  # Use container names for internal communication
    ports:
      - '{{ <service>_port }}:<internal_port>'
    volumes:
      - {{ <service>_folder }}:/config  # Points to <service>-stack/<service>/
    networks:
      - <service>_net
    depends_on:
      - db

  db:
    image: postgres:latest
    container_name: <service>_db
    restart: unless-stopped
    environment:
      - POSTGRES_DB=appdb
    volumes:
      - {{ <service>_database_folder }}:/var/lib/postgresql/data  # Points to <service>-stack/database/
    networks:
      - <service>_net
    # NO port mapping - internal access only

networks:
  <service>_net:
    driver: bridge
```

## Best Practices

- **Single containers:** Default Docker network is sufficient, no explicit network needed
- **Multi-container stacks:** MUST define an explicit Docker network for isolation
- **ALWAYS expose ports if service has a web interface** (required for proxy access)
- For multi-container stacks, use internal container names (e.g., `http://postgres:5432`) for inter-service communication
- Use `depends_on` to define container startup order
- Only backend services without web interfaces can skip port exposure
