"""Sonarr MCP Server — tools for managing TV series, downloads, releases, and blocklisting via the Sonarr API."""

import json
import os
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

SONARR_BASE_URL = os.environ.get("SONARR_BASE_URL", "http://localhost:8989")
SONARR_API_KEY = os.environ["SONARR_API_KEY"]
API_PREFIX = "/api/v3"

mcp = FastMCP("Sonarr", host="0.0.0.0", port=8851)


def _headers() -> dict[str, str]:
    return {"X-Api-Key": SONARR_API_KEY}


async def _get(path: str, params: dict[str, Any] | None = None) -> str:
    """GET request to the Sonarr API, return JSON string."""
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    async with httpx.AsyncClient(base_url=SONARR_BASE_URL, timeout=30) as client:
        resp = await client.get(f"{API_PREFIX}{path}", headers=_headers(), params=clean_params)
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


async def _post(path: str, body: dict[str, Any] | None = None) -> str:
    """POST request to the Sonarr API, return JSON string."""
    async with httpx.AsyncClient(base_url=SONARR_BASE_URL, timeout=60) as client:
        resp = await client.post(f"{API_PREFIX}{path}", headers=_headers(), json=body or {})
        resp.raise_for_status()
        data = resp.json() if resp.content else {"status": "ok"}
        return json.dumps(data, indent=2)


async def _delete(path: str, params: dict[str, Any] | None = None) -> str:
    """DELETE request to the Sonarr API, return status."""
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    async with httpx.AsyncClient(base_url=SONARR_BASE_URL, timeout=30) as client:
        resp = await client.delete(f"{API_PREFIX}{path}", headers=_headers(), params=clean_params)
        resp.raise_for_status()
        return json.dumps({"status": "ok", "statusCode": resp.status_code})


@mcp.tool()
async def get_queue(
    page: Optional[int] = None,
    page_size: Optional[int] = None,
    include_series: Optional[bool] = True,
    include_episode: Optional[bool] = True,
) -> str:
    """Active download queue. Returns paginated list of items currently downloading or waiting. Each item includes series name, episode info, quality, size, progress (sizeleft), status, download client, and any error messages. Use include_series=true and include_episode=true for full context."""
    return await _get(
        "/queue",
        {
            "page": page,
            "pageSize": page_size,
            "includeSeries": include_series,
            "includeEpisode": include_episode,
        },
    )


@mcp.tool()
async def get_series(title: Optional[str] = None) -> str:
    """List all TV series in Sonarr, or search by title. Each series includes id, title, year, status (continuing/ended), path, seasons, episode/file counts, size on disk, and quality profile. Use the 'id' field when calling delete_series."""
    if title:
        return await _get("/series/lookup", {"term": title})
    return await _get("/series")


@mcp.tool()
async def delete_series(series_id: int) -> str:
    """Delete a TV series from Sonarr. This WILL delete all episode files from disk. The series will NOT be added to the import exclusion list, so it can be re-added later if needed. Use get_series first to find the series_id."""
    return await _delete(
        f"/series/{series_id}",
        {"deleteFiles": True, "addImportListExclusion": False},
    )


@mcp.tool()
async def get_episodes(
    series_id: int,
    season_number: Optional[int] = None,
    include_series: Optional[bool] = False,
) -> str:
    """List episodes for a TV series. Returns episode id, title, season/episode number, air date, monitored status, and whether a file exists. Use the episode 'id' field when calling search_releases or trigger_episode_search."""
    return await _get(
        "/episode",
        {
            "seriesId": series_id,
            "seasonNumber": season_number,
            "includeSeries": include_series,
        },
    )


@mcp.tool()
async def blocklist_queue_item(
    queue_item_id: int,
    remove_from_client: Optional[bool] = True,
    skip_redownload: Optional[bool] = False,
) -> str:
    """Remove a download from the queue and add it to the blocklist so Sonarr won't grab it again. The release is removed from the download client by default. Use get_queue first to find the queue_item_id."""
    return await _delete(
        f"/queue/{queue_item_id}",
        {
            "removeFromClient": remove_from_client,
            "blocklist": True,
            "skipRedownload": skip_redownload,
        },
    )


@mcp.tool()
async def search_releases(
    series_id: int,
    episode_id: int,
) -> str:
    """Interactive search — query all indexers for available releases for a specific episode. Returns a list of releases with title, quality, size, seeders, indexer, and other details. Use get_series + get_episodes first to find the IDs. Use grab_release to download a specific result."""
    return await _get(
        "/release",
        {"seriesId": series_id, "episodeId": episode_id},
    )


@mcp.tool()
async def grab_release(guid: str, indexer_id: int) -> str:
    """Grab a specific release and push it to the download client. Use search_releases first to find the guid and indexerId of the release you want to download."""
    return await _post("/release", {"guid": guid, "indexerId": indexer_id})


@mcp.tool()
async def trigger_episode_search(episode_ids: list[int]) -> str:
    """Trigger an automatic search for one or more episodes. Sonarr will search all indexers and grab the best matching release per its quality profile. Use get_episodes first to find the episode IDs."""
    return await _post("/command", {"name": "EpisodeSearch", "episodeIds": episode_ids})


@mcp.tool()
async def trigger_series_search(series_id: int) -> str:
    """Trigger an automatic search for all monitored episodes in a series. Sonarr will search all indexers and grab the best matching releases per its quality profile. Use get_series first to find the series_id."""
    return await _post("/command", {"name": "SeriesSearch", "seriesId": series_id})


if __name__ == "__main__":
    mcp.run(transport="sse")
