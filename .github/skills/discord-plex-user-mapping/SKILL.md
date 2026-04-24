---
name: discord-plex-user-mapping
description: 'Use when: a Discord user tells Plexibot their Plex username to register a mapping, or when Plexibot needs to look up which Plex account belongs to a Discord user. Covers reading, adding, and updating entries in the user mapping file.'
---

# Discord → Plex User Mapping

Maps Discord users to their Plex usernames so Plexibot can correlate stream activity with Discord users.

## When to Use

- A Discord user tags Plexibot and says something like "my Plex username is X"
- Plexibot needs to check who a Discord user is on Plex (e.g., "am I watching anything?")
- Cross-referencing Tracearr stream data with Discord users

## Data File

**Path:** `/home/jimmy/homelab/.github/skills/discord-plex-user-mapping/user_map.csv`

This file is **gitignored** and must be created manually on first use.

### Format

CSV with 3 columns, pipe-delimited:

```
discord_handle|discord_user_id|plex_username
```

- `discord_handle` — Discord display name (e.g., `jimmybish`)
- `discord_user_id` — Discord numeric user ID (e.g., `123456789012345678`)
- `plex_username` — Plex username as it appears in Tracearr stream data

### Example

```
jimmybish|284751038901649408|jimmybish
callum|518293746102938475|callumski999
```

### Creating the File

If the file doesn't exist, create it with the header:

```bash
echo 'discord_handle|discord_user_id|plex_username' > /home/jimmy/homelab/.github/skills/discord-plex-user-mapping/user_map.csv
```

## Operations

### Look Up a Plex Username by Discord User

Search by Discord handle or user ID:

```bash
grep -i 'jimmybish' /home/jimmy/homelab/.github/skills/discord-plex-user-mapping/user_map.csv
```

Or by Discord user ID (more reliable — handles are not unique):

```bash
grep '284751038901649408' /home/jimmy/homelab/.github/skills/discord-plex-user-mapping/user_map.csv
```

### Add a New Mapping

Append a new line:

```bash
echo 'newuser|987654321012345678|their_plex_name' >> /home/jimmy/homelab/.github/skills/discord-plex-user-mapping/user_map.csv
```

### Update an Existing Mapping

Use `sed` to replace in-place by Discord user ID:

```bash
sed -i 's/^.*|284751038901649408|.*$/jimmybish|284751038901649408|new_plex_name/' /home/jimmy/homelab/.github/skills/discord-plex-user-mapping/user_map.csv
```

## Integration with Tracearr Streams

When a Discord user asks "am I watching anything?" or "what am I streaming?":

1. Look up their Plex username from `user_map.csv` using their Discord user ID
2. Run the Tracearr streams script: `/home/jimmy/homelab/.github/skills/tracearr-stream-queries/tracearr-streams.sh --json`
3. Filter the JSON `data[]` array for entries where `username` matches their Plex username
4. Report back what they're watching (or "nothing right now")

If the user isn't in the mapping file, ask them to register by providing their Plex username.

## Important Notes

- The CSV file is **not committed to git** — it contains Discord user IDs which are semi-private
- Discord user IDs are the reliable key — Discord handles can change
- Plex usernames must match exactly what Tracearr reports (case-sensitive)
- When a user registers, confirm back: "Got it — I've linked Discord user `X` to Plex account `Y`"
- The Discord handle and user ID are injected into every prompt by the n8n workflow as `[Discord user: <handle>, Discord user ID: <id>]` — always use these values when adding/updating mappings
