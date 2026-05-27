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


async def _post(path: str, body: dict[str, Any] | None = None) -> str:
    """POST request to the Radarr API, return JSON string."""
    async with httpx.AsyncClient(base_url=RADARR_BASE_URL, timeout=60) as client:
        resp = await client.post(f"{API_PREFIX}{path}", headers=_headers(), json=body or {})
        resp.raise_for_status()
        data = resp.json() if resp.content else {"status": "ok"}
        return json.dumps(data, indent=2)


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


@mcp.tool()
async def search_releases(movie_id: int) -> str:
    """Interactive search — query all indexers for available releases for a specific movie. Returns a list of releases with title, quality, size, seeders, indexer, and other details. Use get_movie first to find the movie_id. Use grab_release to download a specific result."""
    return await _get("/release", {"movieId": movie_id})


@mcp.tool()
async def grab_release(guid: str, indexer_id: int) -> str:
    """Grab a specific release and push it to the download client. Use search_releases first to find the guid and indexerId of the release you want to download."""
    return await _post("/release", {"guid": guid, "indexerId": indexer_id})


@mcp.tool()
async def trigger_movie_search(movie_ids: list[int]) -> str:
    """Trigger an automatic search for one or more movies. Radarr will search all indexers and grab the best matching release per its quality profile. Use get_movie first to find the movie IDs."""
    return await _post("/command", {"name": "MoviesSearch", "movieIds": movie_ids})


@mcp.tool()
async def list_quality_profiles() -> str:
    """List all quality profiles configured in Radarr. Returns id and name for each profile. Use the 'id' field as quality_profile_id when calling add_movie."""
    return await _get("/qualityprofile")


@mcp.tool()
async def list_root_folders() -> str:
    """List all root folders configured in Radarr (e.g. /movies). Returns id, path, and freeSpace. Use the 'path' field as root_folder_path when calling add_movie."""
    return await _get("/rootfolder")


@mcp.tool()
async def add_movie(
    tmdb_id: int,
    quality_profile_id: Optional[int] = None,
    root_folder_path: Optional[str] = None,
    monitored: Optional[bool] = True,
    minimum_availability: Optional[str] = "released",
    search_for_movie: Optional[bool] = True,
) -> str:
    """Add a new movie to Radarr by TMDB id. The lookup payload is fetched automatically (title, year, images, etc.). If quality_profile_id or root_folder_path are omitted, the first configured one is used. minimum_availability controls when Radarr considers the movie available to grab: 'announced', 'inCinemas', 'released', or 'tba'. Set search_for_movie=true to kick off a search immediately after adding. Use get_movie (with a title) first to find the tmdbId, or list_quality_profiles / list_root_folders to pick specific values."""
    lookup_raw = await _get("/movie/lookup/tmdb", {"tmdbId": tmdb_id})
    lookup = json.loads(lookup_raw)
    if not lookup:
        return json.dumps({"error": f"No movie found for tmdbId {tmdb_id}"}, indent=2)
    movie = lookup[0] if isinstance(lookup, list) else lookup

    if quality_profile_id is None:
        profiles = json.loads(await _get("/qualityprofile"))
        if not profiles:
            return json.dumps({"error": "No quality profiles configured in Radarr"}, indent=2)
        quality_profile_id = profiles[0]["id"]

    if root_folder_path is None:
        folders = json.loads(await _get("/rootfolder"))
        if not folders:
            return json.dumps({"error": "No root folders configured in Radarr"}, indent=2)
        root_folder_path = folders[0]["path"]

    movie["qualityProfileId"] = quality_profile_id
    movie["rootFolderPath"] = root_folder_path
    movie["monitored"] = monitored
    movie["minimumAvailability"] = minimum_availability
    movie["addOptions"] = {
        "monitor": "movieOnly",
        "searchForMovie": search_for_movie,
    }

    return await _post("/movie", movie)


if __name__ == "__main__":
    mcp.run(transport="sse")
