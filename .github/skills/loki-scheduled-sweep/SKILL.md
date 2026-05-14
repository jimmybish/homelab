---
name: loki-scheduled-sweep
description: 'Use when: performing a scheduled Loki log sweep (e.g. the daily n8n Log Monitor workflow), summarising warnings and anomalies across hosts, and producing a written report on how to fix or suppress each finding. Defines the sweep methodology, the report format (markdown files in `log-sweeps/`), and a list of known-benign log patterns that must be excluded from reports.'
---

# Scheduled Loki Sweep

How to perform a scheduled Loki sweep across all hosts, work out how to address each finding at its source, and write the outcome to a markdown report on disk. Used by the n8n `Log Monitor` workflow but applicable to any ad-hoc sweep.

## When to Use

- Triggered by the n8n `Log Monitor` workflow (daily at 18:00 local time)
- A user asks for a "Loki sweep" or "log review" across the homelab
- Investigating recurring noise to decide whether it should be fixed or ignored

## Sweep Methodology

1. Query Loki across all hosts for the last **26 hours** (gives 2h of overlap with the previous daily run so nothing slips between sweeps).
2. Focus on `level=~"warning|error|critical"` plus repeating lower-level patterns that occur more than 10 times in the 26-hour window or clearly represent a new recurring issue.
3. **Apply the ignore list below before counting or reporting anything.** A finding made up entirely of ignored patterns is not a finding.
4. For each remaining finding, do real investigation, do not just describe the symptom:
   - Identify the source (host, container/unit, stream labels) and a representative log line.
   - Determine the root cause (read configs, check upstream issues, query related metrics/logs).
   - Decide on one of two outcomes:
     - **Fixable** — describe the concrete change needed (config edit, role update, package upgrade, restart, etc.) and where in the repo it lives.
     - **Not fixable by us** — explain why (upstream bug, third-party SaaS, expected noise) and recommend adding it to the ignore list in this skill, including the exact stream selector and substring/regex to match.
5. Do **not** recommend Grafana alert rules in the report. The point of the sweep is to drive the noise down, not to alert on it.

## Sweep Constraints

- Apply the ignore list before counting, ranking, or writing findings.
- Investigate the source and root cause, not just the visible symptom.
- Prefer a concrete fix when it is under our control.
- If it is not under our control, recommend an ignore-list addition with exact matching details.
- Do not recommend Grafana alert rules in the report.

## Report Output

Reports are written to disk, **not** posted to Discord.

- Folder: `log-sweeps/` at the repo root. The folder is in `.gitignore` and must stay that way (reports may contain hostnames, IPs, tokens in log lines, etc.).
- Filename: `log-sweeps/YYYY-MM-DD-HHMM.md` using the local time the sweep started.
- One file per sweep run. Do not append to or overwrite previous reports.
- If nothing remains after filtering, still write the report file with a single line: `No findings after applying the ignore list.` so we have a record the sweep ran.

### File Structure

```markdown
# Loki Sweep — <YYYY-MM-DD HH:MM local>

Window: last 26 hours (<start ISO> → <end ISO>)
Hosts queried: <count or "all">

## Findings

### 1. <short title, e.g. "vaultwarden: repeated 401s from /api/accounts/prelogin">
- **Source:** `host=...`, `container_name=...` (and any other useful labels)
- **Sample line:** `<one representative log line, fenced if long>`
- **Rate:** `<count> events over <window>` (e.g. `412 events over 26h, ~16/h`)
- **Root cause:** <what's actually happening, with evidence>
- **Action:**
  - **Fix:** <concrete change, file paths, role names, commands> — OR —
  - **Ignore:** add to `loki-scheduled-sweep` ignore list with selector `{...}` matching `<pattern>`. Justification: <one line>.

### 2. ...
```

Keep each finding tight. Prose is fine, no tables.

## Ignore List (Known Benign Patterns)

These are log patterns confirmed to be noise. Do not include them in reports, do not propose fixes for them, and do not count them when assessing severity of nearby findings.

| Source | Pattern | Why it's safe to ignore |
|---|---|---|
| `container_name="addon_core_matter_server"` | `CHIP_ERROR [chip.native.DIS] Failed to parse mDNS query` / `Failed to parse mDNS query` | Upstream CHIP SDK minimal-mDNS resolver logs `CHIP_ERROR` for any unparseable mDNS packet on `224.0.0.251:5353`, including normal Bonjour/AirPlay/Chromecast/HomeKit/Sonos/printer announcements. Matter functionality unaffected. Tracked in `project-chip/connectedhomeip#27425`. |
| `container_name="addon_d5369777_music_assistant"` | `aiosonos.api` `playbackError` `ERROR_LOST_CONNECTION` (typically `serviceId: 303` / Sonos Radio) | Sonos Radio ad streams routinely drop the connection mid-play and the Sonos group auto-recovers to the next track. Logged by `aiosonos` as a player-reported error but not actionable from our side, it's an upstream Sonos Radio ad-insertion issue. Safe to ignore unless it correlates with actual playback outages reported by users. |
| `container_name="hassio_supervisor"` | `[supervisor.addons.validate]` lines containing `deprecated` (e.g. `App config 'arch' uses deprecated values [...]` or `uses deprecated 'codenotary' field`) | Supervisor emits these at WARNING level on every addon load for third-party HA addons (Overseerr, MQTT IO, Folding@home, Google Drive Backup, Everything Presence Zone Configurator, etc.) that haven't dropped legacy `arch` values or the removed `codenotary` field. Loki tags the stream `level=error` but the log line itself is WARNING and addon functionality is unaffected. Nothing we can fix, it's on upstream addon maintainers. |

When adding a new entry: include the stream selector, the substring/regex to match, and a one-line justification with a link to the upstream issue or the changelog entry that established it as benign.

## Maintenance

- When a finding is determined to be unfixable, add it to the ignore list above in the same task that produced the report so the next sweep doesn't re-report it.
- Patterns should be removed from the ignore list if the underlying cause is later fixed upstream and the noise stops.
- The `log-sweeps/` folder is local-only and must remain in `.gitignore`.
