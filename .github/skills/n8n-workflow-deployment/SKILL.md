---
name: n8n-workflow-deployment
description: 'Use when: creating N8N workflow Jinja2 templates, deploying workflows and credentials via the N8N API through Ansible, using community nodes, or configuring N8N node parameters in JSON. Covers all the gotchas around N8N v2 node types, expression escaping, credential formats, and API constraints.'
---

# N8N Workflow Deployment via Ansible

Patterns for creating N8N workflow JSON templates as Jinja2 files and deploying them via the N8N REST API through Ansible tasks.

## When to Use

- Creating a new N8N workflow template (`.json.j2`)
- Deploying workflows or credentials to N8N via Ansible
- Adding community nodes to N8N
- Troubleshooting N8N node parameter errors

## Key Files

- Workflow templates: `ansible/roles/n8n/templates/<workflow_name>.json.j2`
- Reusable deploy task: `ansible/roles/n8n/tasks/deploy_workflow.yaml`
- Credential & workflow orchestration: `ansible/roles/n8n/tasks/main.yaml` (bottom section)
- Variables: `ansible/group_vars/all/vars.yaml` (n8n_api_key, discord vars, etc.)

## Workflow Template Rules

### Forbidden Fields

The N8N API rejects these as read-only on POST/PUT:
- `triggerCount` — NEVER include
- `tags` — NEVER include
- `staticData` — NEVER include

### Minimal Template Structure

```json
{
  "name": "My Workflow",
  "nodes": [...],
  "connections": {...},
  "pinData": {},
  "settings": {
    "executionOrder": "v1"
  }
}
```

### Escaping N8N Expressions in Jinja2

N8N expressions use `{{ }}` which conflicts with Jinja2. Escape them:

```
"value": "={{ '{{' }} $json.fieldName {{ '}}' }}"
```

This renders to `={{ $json.fieldName }}` in the final JSON.

For Ansible variables (rendered at deploy time), use them directly:

```
"value": "{{ n8n_copilot_path }}"
```

### Node ID Convention

Use descriptive kebab-case IDs: `"id": "discord-trigger-1"`, `"id": "exec-copilot-1"`

## N8N v2 Node Gotchas

### ExecuteCommand Node is Disabled by Default

N8N v2.0 blocks `n8n-nodes-base.executeCommand` and `n8n-nodes-base.localFileTrigger` via `NODES_EXCLUDE`. The docker-compose must include:

```yaml
- NODES_EXCLUDE=[]
```

### ResourceLocator Fields (guildId, channelId, etc.)

Many N8N v2 nodes use `resourceLocator` type for ID fields. Plain strings cause "Parameter 'X' is required" errors. Use this format:

```json
"guildId": {
  "__rl": true,
  "mode": "id",
  "value": "{{ n8n_discord_server_id }}"
}
```

For expression-based values:
```json
"channelId": {
  "__rl": true,
  "mode": "id",
  "value": "={{ '{{' }} $json.channel_id {{ '}}' }}"
}
```

### Set Node (Edit Fields) — v3.4

```json
{
  "parameters": {
    "assignments": {
      "assignments": [
        {
          "id": "field-id",
          "name": "fieldName",
          "value": "={{ '{{' }} $json.source {{ '}}' }}",
          "type": "string"
        }
      ]
    },
    "options": {}
  },
  "type": "n8n-nodes-base.set",
  "typeVersion": 3.4
}
```

### Filter Node — v2

```json
{
  "parameters": {
    "conditions": {
      "options": {
        "caseSensitive": true,
        "leftValue": "",
        "typeValidation": "strict"
      },
      "conditions": [
        {
          "id": "filter-id",
          "leftValue": "={{ '{{' }} $json.field {{ '}}' }}",
          "rightValue": "expected_value",
          "operator": {
            "type": "string",
            "operation": "equals"
          }
        }
      ],
      "combinator": "and"
    },
    "options": {}
  },
  "type": "n8n-nodes-base.filter",
  "typeVersion": 2
}
```

### Discord Nodes

**There is NO built-in Discord Trigger.** The built-in `n8n-nodes-base.discord` is action-only (send messages). For triggers, use the community node `n8n-nodes-discord-trigger`.

**Discord Trigger (community node):**
- Type: `n8n-nodes-discord-trigger.discordTrigger`
- Credential type: `discordBotTriggerApi` (NOT `discordBotApi`)
- Credential requires: `clientId`, `token`, `apiKey`, `baseUrl`, `allowedHttpRequestDomains: "all"`
- The `pattern` parameter defaults to `"start"` which requires a `value`. Set `"pattern": "every"` to trigger on all messages
- `guildIds` and `channelIds` are `multiOptions` loaded dynamically — they cause validation errors when set via API. Leave them empty and use a Filter node instead
- Output fields use **camelCase**: `channelId`, `guildId`, `authorId`, `authorName`, `content` (NOT snake_case)

**Discord Send (built-in):**
- Type: `n8n-nodes-base.discord`, typeVersion: 2
- Credential type: `discordBotApi`
- Uses `resource: "message"`, `operation: "send"` (NOT `resource: "channel"`, `operation: "sendMessage"`)
- `guildId` and `channelId` require `resourceLocator` format (see above)

## Discord Message Character Limit

Discord enforces a **2000 character limit** per message. All workflows that post to Discord must:

1. **Instruct Copilot** to stay under the limit by appending to the prompt:
   ```
   [IMPORTANT: Keep your reply under 1800 characters. Be concise.]
   ```
   Use 1800 (not 2000) so that the hard truncation at 1900 is a safety net, not the primary constraint.
2. **Truncate output** in the response parser as a safety net:
   ```javascript
   if (responseText.length > 1900) {
     responseText = responseText.substring(0, 1900) + '...';
   }
   ```
   Use 1900 (not 2000) to leave headroom for any wrapper text or formatting.

3. **Only keep the final message** — Copilot's `--output-format json` emits `assistant.message` events for every turn (intermediate "investigating..." messages AND the final response). Track message blocks and keep only the last one:
   ```javascript
   let responseText = '';
   let lastBlock = '';
   // ...
   if (obj.type === 'assistant.message' && obj.data && obj.data.content) {
     responseText += obj.data.content;
   } else if (obj.type === 'result' && obj.sessionId) {
     sessionId = obj.sessionId;
   } else if (responseText) {
     lastBlock = responseText;  // Save current block before resetting
     responseText = '';
   }
   // After loop:
   responseText = responseText || lastBlock;
   ```
   This handles tool events that occur after the final response (e.g., memory saves) without losing the response text.

## Community Node Installation

Community nodes MUST be installed in `/home/node/.n8n/nodes/` (NOT `/home/node/.n8n/`):

```yaml
- name: Install community node
  community.docker.docker_container_exec:
    container: n8n
    command: npm install <package-name> --prefix /home/node/.n8n/nodes
  register: node_install
  changed_when: "'added' in node_install.stdout"

- name: Restart n8n if community node was installed
  community.docker.docker_compose_v2:
    project_src: "{{ n8n_folder }}"
    state: restarted
  when: node_install.changed
```

N8N must be restarted after installing a community node for it to register the new node types and credential types.

## Credential Deployment via API

### Create Credential

```yaml
- name: Create credential
  ansible.builtin.uri:
    url: "{{ internal_n8n_url }}/api/v1/credentials"
    method: POST
    headers:
      X-N8N-API-KEY: "{{ n8n_api_key }}"
      Content-Type: "application/json"
    body:
      name: "Credential Name"
      type: "<credential_type>"
      data:
        field1: "{{ vault_var }}"
    body_format: json
    validate_certs: false
    status_code: [200, 201]
  delegate_to: localhost
```

### Update Credential

Use `PATCH` method with the credential ID. Same body format as create.

### Important: Credential type must be recognized

If using a community node's credential type, the community node must be installed and N8N restarted BEFORE creating the credential. Otherwise the API returns `"req.body.type is not a known type"`.

## Workflow Deployment Pattern

Workflows are deployed via the reusable `deploy_workflow.yaml` include:

```yaml
- name: List existing N8N workflows
  ansible.builtin.uri:
    url: "{{ internal_n8n_url }}/api/v1/workflows"
    method: GET
    headers:
      X-N8N-API-KEY: "{{ n8n_api_key }}"
    validate_certs: false
    return_content: true
  register: n8n_workflows
  delegate_to: localhost

- name: Deploy N8N workflows
  ansible.builtin.include_tasks:
    file: deploy_workflow.yaml
  loop:
    - name: "Workflow Name"
      template: "templates/my_workflow.json.j2"
  loop_control:
    loop_var: workflow
```

Templates are rendered via `lookup('template', ...)` on the Ansible controller — NOT via `ansible.builtin.template` to a remote file (which would require `lookup('file')` from localhost and fail).

## Workflow Activation via API

Webhook-triggered workflows must be **active** to receive requests. The N8N API does NOT support setting `active` via PUT or PATCH (it's read-only). Use the dedicated endpoint:

```yaml
- name: Activate workflow
  ansible.builtin.uri:
    url: "{{ internal_n8n_url }}/api/v1/workflows/{{ workflow_id }}/activate"
    method: POST
    headers:
      X-N8N-API-KEY: "{{ n8n_api_key }}"
    validate_certs: false
    status_code: [200]
  delegate_to: localhost
```

To deactivate: `POST /api/v1/workflows/{id}/deactivate`

## N8N Docker Compose Environment

Required environment variables for N8N v2 behind a reverse proxy:

```yaml
- NODES_EXCLUDE=[]              # Re-enable ExecuteCommand node
- N8N_PROXY_HOPS=1              # Trust X-Forwarded-For from reverse proxy
- N8N_BLOCK_ENV_ACCESS_IN_NODE=false  # Allow env var access in Code nodes
```

## NGINX Proxy Configuration

Do NOT add manual WebSocket headers to the N8N proxy config. SWAG's `proxy.conf` already handles this correctly via `$connection_upgrade`. Adding `proxy_set_header Connection "upgrade"` unconditionally breaks SSE/push connections and causes "Lost connection to the server" errors in the N8N UI.

## SSH from N8N Container

The N8N container can SSH to mgmt host for Copilot CLI execution:
- SSH key and config mounted at `/home/node/.ssh:ro`
- SSH config includes `LogLevel ERROR` to suppress known_hosts warnings (since `UserKnownHostsFile /dev/null`)
- Copilot CLI must be referenced by full path (`/home/jimmy/.local/bin/copilot`) as non-interactive SSH doesn't load user PATH
