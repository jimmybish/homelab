---
name: tracearr-stream-queries
description: 'Use when: checking who is currently watching on Plex/Jellyfin/Emby, querying active streams, checking stream counts, or determining if it is safe to restart media server services. Covers the Tracearr public REST API for stream data.'
---

# Tracearr Stream Queries

How to query active streams from Tracearr's public REST API. Use this to check who's watching, what they're watching, and stream quality details.

## When to Use

- Checking if anyone is currently watching on Plex, Jellyfin, or Emby
- Getting details about active streams (user, title, quality, device)
- Determining if it's safe to restart services (no active playback)
- Answering "what's playing right now?" questions

## API Reference

### Endpoint

```
GET /api/v1/public/streams
```

This is the only public endpoint needed for stream queries. It was discovered from the [Homepage widget source](https://github.com/gethomepage/homepage/blob/main/src/widgets/tracearr/widget.js) — Tracearr's own docs don't cover API specifics yet.

### Authentication

```
Authorization: Bearer <api_key>
```

- The API key is a **public read-only** key generated in Tracearr Settings → API
- Key is stored vault-encrypted as `homepage_tracearr_key` in `ansible/group_vars/all/vars.yaml`
- Keys are prefixed with `trr_pub_`

### Base URLs

| Context | URL |
|---------|-----|
| Internal (from homelab network) | `https://tracearr.{{ internal_domain }}` |
| Direct (container-to-container) | `http://docker-2:3001` |

### How to Query

Use the pre-built script — it handles vault decryption, API auth, and output formatting internally.

**Human-readable summary (default):**
```bash
/home/jimmy/homelab/.github/skills/tracearr-stream-queries/tracearr-streams.sh
```

Example output:
```
Active streams: 2 (direct play: 2, transcode: 0, bandwidth: 18.9 Mbps)
  jimmybish: Below Deck Down Under S03E17 - Never Can Sey' Goodbye
    playing | 61% (26m/42m) | 1080p directplay | Apple TV (tvOS)
  callumski999: How I Met Your Mother S05E05 - Duel Citizenship
    playing | 3% (0m/21m) | 1080p directplay | Xbox (Xbox)
```

**Raw JSON (for programmatic use):**
```bash
/home/jimmy/homelab/.github/skills/tracearr-stream-queries/tracearr-streams.sh --json
```

### Important Notes

- **Do NOT construct curl commands manually** — always use the script above
- **`web/fetch` does NOT work** for this API — it's a webpage content extractor, not a JSON API client
- **Loki logs don't help** — Plex doesn't log session activity to stdout, so container logs won't show who's watching

## Response Format

```json
{
  "data": [
    {
      "id": "uuid",
      "serverId": "uuid",
      "serverName": "server-name",
      "username": "user",
      "mediaTitle": "Episode Title",
      "mediaType": "episode",
      "showTitle": "Show Name",
      "seasonNumber": 3,
      "episodeNumber": 17,
      "year": 2025,
      "artistName": null,
      "albumName": null,
      "durationMs": 2555177,
      "state": "playing",
      "progressMs": 168626,
      "startedAt": "2026-04-24T09:26:30.414Z",
      "isTranscode": false,
      "videoDecision": "directplay",
      "audioDecision": "directplay",
      "bitrate": 9819,
      "resolution": "1080p",
      "sourceVideoCodecDisplay": "H.264",
      "sourceAudioCodecDisplay": "EAC3",
      "audioChannelsDisplay": "Stereo",
      "device": "Apple TV",
      "player": "Apple TV",
      "product": "Plex for Apple TV",
      "platform": "tvOS"
    }
  ],
  "summary": {
    "total": 1,
    "transcodes": 0,
    "directStreams": 0,
    "directPlays": 1,
    "totalBitrate": "9.8 Mbps",
    "byServer": [
      {
        "serverId": "uuid",
        "serverName": "server-name",
        "total": 1,
        "transcodes": 0,
        "directStreams": 0,
        "directPlays": 1,
        "totalBitrate": "9.8 Mbps"
      }
    ]
  }
}
```

### Key Fields

| Field | Description |
|-------|-------------|
| `summary.total` | Total active streams across all servers |
| `summary.transcodes` | Number of streams being transcoded |
| `summary.directPlays` | Number of direct play streams |
| `summary.totalBitrate` | Aggregate bandwidth in human-readable format |
| `data[].username` | Who is watching |
| `data[].mediaTitle` | Episode/movie title |
| `data[].showTitle` | Series name (null for movies) |
| `data[].mediaType` | `episode`, `movie`, `track`, or `live` |
| `data[].state` | `playing`, `paused`, or `buffering` |
| `data[].progressMs` / `durationMs` | Playback progress and total duration in milliseconds |
| `data[].videoDecision` | `directplay`, `transcode`, or `copy` |
| `data[].resolution` | e.g., `1080p`, `4K` |
| `data[].device` | Playback device name |

### When No Streams Are Active

Returns an empty data array with zeroed summary:
```json
{
  "data": [],
  "summary": { "total": 0, "transcodes": 0, "directStreams": 0, "directPlays": 0, "totalBitrate": "0 bps", "byServer": [] }
}
```

## Common Queries

**Is anyone watching?** → Check `summary.total > 0`

**What are they watching?** → Iterate `data[]`, report `username`, `showTitle` (or `mediaTitle` for movies), `seasonNumber`/`episodeNumber`

**How far along?** → `progressMs / durationMs * 100` for percentage, or `progressMs / 60000` for minutes

**Is anything transcoding?** → Check `summary.transcodes > 0`, or filter `data[]` where `isTranscode == true`

**Safe to restart Plex?** → `summary.total == 0` means no active streams
