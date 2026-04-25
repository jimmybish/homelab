---
name: mcp-server-creation
description: 'Use when: creating a new MCP (Model Context Protocol) server to expose a service API as tools for Copilot/LLM consumption. Covers the Python server source, Dockerfile, Ansible role for deployment, client configuration, and agent integration. Follow this skill end-to-end for each new MCP server.'
---

# MCP Server Creation

How to create, deploy, and integrate a read-only MCP server that wraps an existing service's REST API as MCP tools. Each server is a Python Docker container deployed via Ansible, accessible over SSE transport.

## When to Use

- Exposing a homelab service's API as MCP tools for Copilot agents
- Creating a new `mcp-server-<service>/` directory with server source
- Deploying an MCP server container via Ansible
- Registering a new MCP server in VS Code and Copilot CLI configs

## Architecture Overview

```
mcp-server-<service>/          ← Source code (repo root)
  ├── server.py                ← FastMCP server with @mcp.tool() functions
  ├── requirements.txt         ← Python deps
  └── Dockerfile               ← Container build

ansible/roles/mcp_<service>/   ← Ansible role
  ├── vars/main.yaml
  ├── tasks/main.yaml
  ├── handlers/main.yaml
  └── templates/docker_compose.yaml.j2
```

Transport: **SSE** (HTTP-based, Docker container on homelab host).
Clients connect via `http://<host>:<port>/sse`.

## Step 1: Research the Target API

Before writing any code, fully understand the API you're wrapping:

- Find the API documentation (Swagger/OpenAPI, source code, or manual testing)
- List every GET endpoint, its path, query parameters, and response shape
- Identify the authentication mechanism (Bearer token, API key header, etc.)
- Identify the base URL from the homelab network (prefer `localhost:<port>` if on same host)
- Note which parameters are optional vs required

## Step 2: Create the Python MCP Server

### Directory: `mcp-server-<service>/`

### `server.py`

```python
"""<Service> MCP Server — read-only tools wrapping the <Service> API."""

import json
import os
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("<SERVICE>_BASE_URL", "http://localhost:<port>")
API_KEY = os.environ["<SERVICE>_API_KEY"]
API_PREFIX = "/api/v1"  # adjust per service

mcp = FastMCP("<Service>", host="0.0.0.0", port=<mcp_port>)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}"}


async def _get(path: str, params: dict[str, Any] | None = None) -> str:
    """GET request to the API, return JSON string."""
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        resp = await client.get(f"{API_PREFIX}{path}", headers=_headers(), params=clean_params)
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


@mcp.tool()
async def get_example(some_param: Optional[str] = None) -> str:
    """Clear, descriptive docstring — this becomes the tool description visible to LLMs."""
    return await _get("/example", {"someParam": some_param})


if __name__ == "__main__":
    mcp.run(transport="sse")
```

### Critical API Notes (MCP SDK v1.27.0+)

- **`host` and `port` go in the `FastMCP()` constructor**, NOT in `run()`
- `run()` only accepts `transport` and `mount_path`
- Always use `host="0.0.0.0"` for Docker containers (bind to all interfaces)
- Always use `transport="sse"` for remote Docker deployments

### Tool Design Guidelines

- **One tool per API endpoint** — don't combine multiple endpoints into one tool
- **Return raw JSON as string** — let the LLM interpret the data, don't pre-format
- **Use `Optional` parameters** — filter `None` values before passing to the API
- **Write descriptive docstrings** — these are the only thing the LLM sees to decide when to call the tool
- **Use snake_case for parameters** — even if the API uses camelCase (map in the `_get` call)
- **Strip None values** before sending to the API: `{k: v for k, v in params.items() if v is not None}`

### `requirements.txt`

```
mcp[cli]>=1.0.0
httpx>=0.27.0
uvicorn>=0.30.0
```

### `Dockerfile`

```dockerfile
FROM python:3.12-slim

RUN groupadd -r mcp && useradd -r -g mcp -d /app -s /sbin/nologin mcp

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

USER mcp

EXPOSE <mcp_port>

CMD ["python", "server.py"]
```

- Always run as non-root (`USER mcp`)
- Copy `requirements.txt` before `server.py` for better layer caching
- Use `python:3.12-slim` as the base image

## Step 3: Create the Ansible Role

Use the standard `ansible-role-scaffolding` and `ansible-docker-deployment` skill patterns, with these MCP-specific adaptations:

### `vars/main.yaml`

```yaml
---
# MCP <Service> Server
# Wraps: <API documentation URL>
# Last Updated: <date>

mcp_<service>_version: "latest"
mcp_<service>_port: <port>  # Pick a free port — check existing roles for conflicts

# Target API connection
mcp_<service>_api_base_url: "http://localhost:<target_port>"

# Volume paths — stateless, just needs compose + build context
mcp_<service>_folder: "{{ docker_storage_folder }}/mcp-<service>"

# Feature flags — MCP servers have no web UI
mcp_<service>_configure_dns: false
mcp_<service>_is_proxied: false
mcp_<service>_configure_proxy: false
mcp_<service>_configure_homepage: false
```

MCP servers are **always**: no DNS, no proxy, no homepage. They're internal tools, not user-facing web services.

### `templates/docker_compose.yaml.j2`

```yaml
services:
  mcp-<service>:
    build:
      context: "{{ mcp_<service>_folder }}/build"
      dockerfile: Dockerfile
    image: mcp-<service>:{{ mcp_<service>_version }}
    container_name: mcp-<service>
    restart: unless-stopped
    environment:
      - <SERVICE>_API_KEY={{ <vault_api_key_var> }}
      - <SERVICE>_BASE_URL={{ mcp_<service>_api_base_url }}
    network_mode: host
```

- **`network_mode: host`** — simplest networking. The MCP server can reach the target service at `localhost:<port>` and clients can reach the MCP server at `<host>:<mcp_port>`.
- **No volumes** — MCP servers are stateless.
- **`build` + `image`** — builds locally from the synced source code, no registry needed.

### `tasks/main.yaml`

```yaml
---
# 1. Docker Prerequisites
- name: Ensure docker-compose is installed
  ansible.builtin.package:
    name: docker-compose
    state: present

- name: Ensure Docker service is running
  ansible.builtin.service:
    name: docker
    state: started
    enabled: true

# 2. Directory Setup
- name: Setup mcp-<service> service directory
  ansible.builtin.file:
    path: "{{ mcp_<service>_folder }}"
    state: directory
    mode: '0755'
    owner: "{{ docker_user }}"
    group: "{{ docker_user }}"

- name: Setup mcp-<service> build context directory
  ansible.builtin.file:
    path: "{{ mcp_<service>_folder }}/build"
    state: directory
    mode: '0755'
    owner: "{{ docker_user }}"
    group: "{{ docker_user }}"

# 3. Firewall
- name: Allow mcp-<service> port through UFW
  community.general.ufw:
    rule: allow
    port: "{{ mcp_<service>_port }}"
    proto: tcp
  when: ansible_os_family == "Debian"

# 4. Sync build context and deploy
- name: Copy MCP server source to build context
  ansible.builtin.copy:
    src: "{{ item }}"
    dest: "{{ mcp_<service>_folder }}/build/{{ item | basename }}"
    mode: '0644'
    owner: "{{ docker_user }}"
    group: "{{ docker_user }}"
  loop:
    - "{{ playbook_dir }}/../mcp-server-<service>/server.py"
    - "{{ playbook_dir }}/../mcp-server-<service>/requirements.txt"
    - "{{ playbook_dir }}/../mcp-server-<service>/Dockerfile"
  notify:
    - Start mcp_<service>

- name: Deploy mcp-<service> docker-compose
  ansible.builtin.template:
    src: "templates/docker_compose.yaml.j2"
    dest: "{{ mcp_<service>_folder }}/docker-compose.yaml"
    mode: '0644'
    owner: "{{ docker_user }}"
    group: "{{ docker_user }}"
    force: true
  notify:
    - Start mcp_<service>

- name: Build and run mcp-<service>
  community.docker.docker_compose_v2:
    project_src: "{{ mcp_<service>_folder }}"
    state: present
    build: always
    remove_orphans: true

# 5. Health Check
- name: Wait for mcp-<service> to start
  ansible.builtin.pause:
    seconds: 5

- name: Check if mcp-<service> port is open and listening
  community.general.listen_ports_facts:

- name: Assert mcp-<service> port is listening
  ansible.builtin.assert:
    that:
      - mcp_<service>_port | int in (ansible_facts.tcp_listen | map(attribute='port') | list)
    fail_msg: "mcp-<service> port {{ mcp_<service>_port }} is not open and listening!"
    success_msg: "mcp-<service> port {{ mcp_<service>_port }} is open and listening."
```

Key differences from standard Docker service roles:
- **No proxy, DNS, or homepage phases** — skip phases 6-8
- **`build: always`** on `docker_compose_v2` — ensures source changes trigger rebuilds
- **`copy` task syncs source** — uses `playbook_dir/../mcp-server-<service>/` path to locate files relative to the playbook

### `handlers/main.yaml`

```yaml
---
- name: Start mcp_<service>
  community.docker.docker_compose_v2:
    project_src: "{{ mcp_<service>_folder }}"
    state: restarted
    remove_orphans: true
    build: always
```

Only one handler needed — no Homepage or SWAG restarts.
Include `build: always` so notified restarts also rebuild from updated source.

## Step 4: Client Registration

After deploying, register the MCP server in **both** config files:

### `.vscode/mcp.json` (VS Code Copilot Chat)

Add to the `servers` object:
```json
"<service>": {
    "type": "sse",
    "url": "http://<host>:<mcp_port>/sse"
}
```

### `~/.copilot/mcp-config.json` (Copilot CLI)

Add to the `mcpServers` object:
```json
"<service>": {
    "type": "sse",
    "url": "http://<host>:<mcp_port>/sse"
}
```

### Playbook, Inventory, Master Playbook

Follow standard `playbook-creation-testing` skill:
- Create `ansible/deploy_mcp_<service>.yaml`
- Add `mcp_<service>_host` group to `ansible/inventory.yaml`
- Add `internal_mcp_<service>_url` to `ansible/group_vars/all/vars.yaml`
- Add `import_playbook` to `ansible/master_playbook.yaml` after the service it wraps

## Step 5: Agent Integration

If an agent should use the MCP tools:

1. Add `'<service>/*'` to the agent's `tools` frontmatter list
2. Add the MCP server to the agent's Service Reference table
3. Add a "When to use which" section mapping user questions to specific MCP tools
4. Remove any shell scripts or manual API calls the MCP tools replace

## Step 6: Validation

### Automated test script

Run from the Ansible control host (requires `uvx`):

```bash
uvx --with httpx --with "mcp[cli]" python3 -c "
import asyncio, json
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client

async def test():
    async with sse_client('http://<host>:<port>/sse') as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f'Tools: {len(tools.tools)}')
            for t in tools.tools:
                print(f'  {t.name}')
            
            # Call each tool and check it returns valid JSON
            for tool in tools.tools:
                try:
                    result = await session.call_tool(tool.name, {})
                    data = json.loads(result.content[0].text)
                    keys = list(data.keys()) if isinstance(data, dict) else f'array[{len(data)}]'
                    print(f'PASS {tool.name}: {keys}')
                except Exception as e:
                    print(f'FAIL {tool.name}: {e}')

asyncio.run(test())
"
```

### Checklist

- [ ] All tools listed by `list_tools()`
- [ ] Each tool returns valid JSON (no 401/403/500 errors)
- [ ] Response keys match the target API's documented schema
- [ ] Playbook is idempotent (second run = 0 changed)
- [ ] MCP server entry added to `.vscode/mcp.json`
- [ ] MCP server entry added to `~/.copilot/mcp-config.json`
- [ ] Agent updated with MCP tool access (if applicable)
- [ ] Any replaced shell scripts or skills deleted

## Gotchas & Lessons Learned

### Docker BuildKit Cache

**Problem:** After updating `server.py`, `docker compose build` may use cached layers even though the file content changed. `docker rmi` does NOT clear BuildKit's build cache.

**Fix:** When source changes aren't picked up:
```bash
ssh <host> 'sudo docker builder prune -f'
ssh <host> 'cd <service_folder> && sudo docker compose build --no-cache && sudo docker compose up -d'
```

The Ansible role's `build: always` flag handles normal rebuilds, but BuildKit cache poisoning on first deploy requires manual pruning.

### FastMCP API (v1.27.0+)

- `host` and `port` are **constructor parameters**: `FastMCP("Name", host="0.0.0.0", port=8850)`
- `run()` only takes `transport` and `mount_path`: `mcp.run(transport="sse")`
- Passing `host`/`port` to `run()` raises `TypeError: FastMCP.run() got an unexpected keyword argument`
- Context7 docs may show a different API — always verify against the installed version

### Variable Scoping

Role vars from other roles (e.g., `tracearr_port` from the `tracearr` role) are **not available** in your MCP role. Define your own variables for the target service URL. Don't reference variables from other roles.

### Port Selection

Check all existing `*_port` variables across roles before picking a port. Use `grep -r '_port:' ansible/roles/*/vars/main.yaml` to get the full list.

## Reference Implementation

See `mcp-server-tracearr/` and `ansible/roles/mcp_tracearr/` for a complete working example wrapping the Tracearr Public API with 8 tools.
