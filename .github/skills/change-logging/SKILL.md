---
name: change-logging
description: 'Use when: completing any task that modifies files, runs commands on remote hosts, or changes configuration. ALWAYS log changes at the end of a task. This skill is mandatory — never skip it when changes have been made.'
---

# Change Logging

Record every change made during a task in a structured change log file, similar to a CAB (Change Advisory Board) change ticket.

## When to Use

- **Always** — after any task that modifies files, runs commands on remote hosts, or changes configuration
- This includes: editing files in the repo, running Ansible playbooks, SSHing to hosts, calling APIs that mutate state, creating/deleting resources
- This does **not** include: read-only queries, searches, or informational lookups

## File Location

```
changelogs/week-YYYY-MM-DD/log_YYMMDD.md
```

- **Week folder:** `changelogs/week-YYYY-MM-DD/` — the `YYYY-MM-DD` is the most recent Sunday's date (start of the week)
- **Log file:** `log_YYMMDD.md` — the `YYMMDD` is **today's date** (e.g. `log_260422.md` for 22 April 2026)
- If the week folder doesn't exist, create it with `mkdir -p`
- If the log file already exists for today, **append** a new entry to it (add a horizontal rule `---` separator between entries)

### Computing the Week Folder Date

The week folder uses the most recent Sunday. In a terminal:
```bash
date -d "last sunday" +%Y-%m-%d  # or today's date if it IS Sunday
```

## Log Entry Format

Each entry follows this CAB-style template:

```markdown
# Change Log — YYYY-MM-DD HH:MM

**Agent:** <agent name or "copilot">
**Requested by:** <user request summary — one line>

## Summary

Brief description of what was changed and why (2-3 sentences max).

## Changes Made

| File | Change |
|------|--------|
| `path/to/file` | Description of what changed (values before → after, lines added/removed, etc.) |
| `path/to/other/file` | Description |

## Commands Executed on Remote Hosts

| Host | Command | Purpose |
|------|---------|---------|
| `hostname` | `command run` | Why it was run and the outcome |

If no remote commands were executed, write: *None — all changes were local file edits.*

## Rollback Plan

Step-by-step instructions to revert the changes, or *N/A — trivial/no-impact change* if the change is low-risk.

Examples of good rollback plans:
- "Revert `file.yaml` to previous values: `key: old_value`"
- "Run `ansible-playbook -i inventory.yaml deploy_foo.yaml` with the old template"
- "Delete the new file: `rm path/to/new/file`"
- "No rollback needed — this was a new file addition with no side effects"
```

## Rules

1. **Write to disk only** — do not git add, commit, or push the changelog
2. **One file per day** — all entries for the same day go in the same `log_YYMMDD.md` file
3. **Append, don't overwrite** — if the file exists, add `---` then the new entry at the bottom
4. **Be specific** — list actual file paths, actual values changed, actual commands run. Don't be vague
5. **Include before/after** — when changing a value, note what it was and what it became
6. **Summarise, don't dump** — for large diffs, summarise the key changes rather than pasting entire files
7. **Always include rollback** — even if it's "N/A", the section must be present
