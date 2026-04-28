---
name: loki-scheduled-sweep
description: 'Use when: performing a scheduled Loki log sweep (e.g. the hourly n8n Log Monitor workflow), summarising warnings and anomalies across hosts, or recommending Grafana alert rules from log patterns. Defines the sweep methodology and a list of known-benign log patterns that must be excluded from reports.'
---

# Scheduled Loki Sweep

How to perform a scheduled (typically hourly) Loki sweep across all hosts, summarise findings, and recommend Grafana alert rules. Used by the n8n `Log Monitor` workflow but applicable to any ad-hoc sweep.

## When to Use

- Triggered by the n8n `Log Monitor` workflow (hourly cron)
- A user asks for a "Loki sweep" or "log review" across the homelab
- Generating Grafana alert recommendations from observed log patterns

## Sweep Methodology

1. Query Loki for the requested window (default: last 90 minutes) across all hosts.
2. Focus on `level=~"warning|error|critical"` plus any unusual repeating patterns at lower levels.
3. **Apply the ignore list below before counting or reporting anything.** A finding made up entirely of ignored patterns is not a finding.
4. For each remaining finding, capture:
   - Source (host, container/unit, log stream labels)
   - Pattern / sample line
   - Rate or count over the window
   - A recommended Grafana alert rule (LogQL + threshold) that would catch a regression
5. If nothing remains after filtering, respond with **only** the literal text `NO_ISSUES_FOUND` (no other words, no markdown).

## Ignore List (Known Benign Patterns)

These are log patterns confirmed to be noise. Do not include them in reports, do not recommend alerts for them, and do not count them when assessing severity of nearby findings.

| Source | Pattern | Why it's safe to ignore |
|---|---|---|
| `container_name="addon_core_matter_server"` | `CHIP_ERROR [chip.native.DIS] Failed to parse mDNS query` / `Failed to parse mDNS query` | Upstream CHIP SDK minimal-mDNS resolver logs `CHIP_ERROR` for any unparseable mDNS packet on `224.0.0.251:5353`, including normal Bonjour/AirPlay/Chromecast/HomeKit/Sonos/printer announcements. Matter functionality unaffected. Tracked in `project-chip/connectedhomeip#27425`. |

When adding a new entry: include the stream selector, the substring/regex to match, and a one-line justification with a link to the upstream issue or the changelog entry that established it as benign.

## Reporting Format

- Reply will be posted to Discord — use the `chat-room-communication` skill for tone and formatting.
- Keep replies under 1800 characters.
- Do not include `NO_ISSUES_FOUND` anywhere in a report that contains real findings.
- One concise bullet per finding: source, pattern, rate, recommended alert.

## Maintenance

- When a new noisy-but-benign pattern is identified (e.g. via Discord triage), add it to the ignore list above in the same PR that confirms it's benign.
- Patterns should be removed from the ignore list if the underlying cause is later fixed upstream and the noise stops.
