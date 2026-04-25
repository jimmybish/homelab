"""Sonarr MCP Server — tools for viewing downloads and deleting shows via the Sonarr API."""

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


if __name__ == "__main__":
    mcp.run(transport="sse")
