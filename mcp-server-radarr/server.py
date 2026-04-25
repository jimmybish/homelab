"""Radarr MCP Server — tools for viewing downloads, listing movies, and deleting movies via the Radarr API."""

import json
import os
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

RADARR_BASE_URL = os.environ.get("RADARR_BASE_URL", "http://localhost:7878")
RADARR_API_KEY = os.environ["RADARR_API_KEY"]
API_PREFIX = "/api/v3"

mcp = FastMCP("Radarr", host="0.0.0.0", port=8852)


def _headers() -> dict[str, str]:
    return {"X-Api-Key": RADARR_API_KEY}


async def _get(path: str, params: dict[str, Any] | None = None) -> str:
    """GET request to the Radarr API, return JSON string."""
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    async with httpx.AsyncClient(base_url=RADARR_BASE_URL, timeout=30) as client:
        resp = await client.get(f"{API_PREFIX}{path}", headers=_headers(), params=clean_params)
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)


async def _delete(path: str, params: dict[str, Any] | None = None) -> str:
    """DELETE request to the Radarr API, return status."""
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    async with httpx.AsyncClient(base_url=RADARR_BASE_URL, timeout=30) as client:
        resp = await client.delete(f"{API_PREFIX}{path}", headers=_headers(), params=clean_params)
        resp.raise_for_status()
        return json.dumps({"status": "ok", "statusCode": resp.status_code})


@mcp.tool()
async def get_queue(
    page: Optional[int] = None,
    page_size: Optional[int] = None,
    include_movie: Optional[bool] = True,
) -> str:
    """Active download queue. Returns paginated list of movies currently downloading or waiting. Each item includes movie title, quality, size, progress (sizeleft), status, download client, and any error messages."""
    return await _get(
        "/queue",
        {
            "page": page,
            "pageSize": page_size,
            "includeMovie": include_movie,
        },
    )


@mcp.tool()
async def get_movie(title: Optional[str] = None) -> str:
    """List all movies in Radarr, or search by title. Each movie includes id, title, year, status, path, size on disk, quality profile, hasFile flag, and monitored status. Use the 'id' field when calling delete_movie."""
    if title:
        return await _get("/movie/lookup", {"term": title})
    return await _get("/movie")


@mcp.tool()
async def delete_movie(movie_id: int) -> str:
    """Delete a movie from Radarr. This WILL delete the movie file from disk. The movie will NOT be added to the import exclusion list, so it can be re-added later if needed. Use get_movie first to find the movie_id."""
    return await _delete(
        f"/movie/{movie_id}",
        {"deleteFiles": True, "addImportExclusion": False},
    )


if __name__ == "__main__":
    mcp.run(transport="sse")
