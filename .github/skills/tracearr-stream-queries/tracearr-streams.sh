#!/usr/bin/env bash
# Query active streams from Tracearr's public API.
# Usage: ./scripts/tracearr-streams.sh [--summary | --json]
#   --summary  (default) Human-readable one-line-per-stream output
#   --json     Raw JSON response
set -euo pipefail

ANSIBLE_DIR="$(cd "$(dirname "$0")/../../../ansible" && pwd)"
VAULT_KEY="${HOME}/ansible_key"

# Decrypt the API key from Ansible Vault
TRACEARR_KEY=$(ansible -i "${ANSIBLE_DIR}/inventory.yaml" tracearr_host \
  -m debug -a "msg={{ homepage_tracearr_key }}" \
  --vault-password-file "${VAULT_KEY}" 2>/dev/null \
  | grep -oP '"msg": "\K[^"]+')

# Decrypt the Tracearr URL
TRACEARR_URL=$(ansible -i "${ANSIBLE_DIR}/inventory.yaml" tracearr_host \
  -m debug -a "msg={{ internal_tracearr_url }}" \
  --vault-password-file "${VAULT_KEY}" 2>/dev/null \
  | grep -oP '"msg": "\K[^"]+')

RESPONSE=$(curl -sk "${TRACEARR_URL}/api/v1/public/streams" \
  -H "Authorization: Bearer ${TRACEARR_KEY}")

MODE="${1:---summary}"

if [[ "${MODE}" == "--json" ]]; then
  echo "${RESPONSE}"
else
  echo "${RESPONSE}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
s = d['summary']
print(f'Active streams: {s[\"total\"]} (direct play: {s[\"directPlays\"]}, transcode: {s[\"transcodes\"]}, bandwidth: {s[\"totalBitrate\"]})')
for stream in d['data']:
    if stream.get('showTitle'):
        title = f'{stream[\"showTitle\"]} S{stream[\"seasonNumber\"]:02d}E{stream[\"episodeNumber\"]:02d} - {stream[\"mediaTitle\"]}'
    elif stream.get('artistName'):
        title = f'{stream[\"artistName\"]} - {stream[\"mediaTitle\"]}'
    else:
        title = f'{stream[\"mediaTitle\"]} ({stream.get(\"year\", \"\")})'
    pct = int(stream['progressMs'] / stream['durationMs'] * 100) if stream['durationMs'] else 0
    mins_in = int(stream['progressMs'] / 60000)
    mins_total = int(stream['durationMs'] / 60000)
    print(f'  {stream[\"username\"]}: {title}')
    print(f'    {stream[\"state\"]} | {pct}% ({mins_in}m/{mins_total}m) | {stream[\"resolution\"]} {stream[\"videoDecision\"]} | {stream[\"device\"]} ({stream[\"platform\"]})')
if not d['data']:
    print('  No active streams')
"
fi
