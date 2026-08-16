"""Tracearr MCP Server — read-only tools wrapping the Tracearr Public API."""

import json
import os
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

TRACEARR_BASE_URL = os.environ.get("TRACEARR_BASE_URL", "http://localhost:3001")
TRACEARR_API_KEY = os.environ["TRACEARR_API_KEY"]
API_PREFIX = "/api/v1/public"

mcp = FastMCP("Tracearr", host="0.0.0.0", port=8850)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TRACEARR_API_KEY}"}


async def _get(path: str, params: dict[str, Any] | None = None) -> str:
    """Make a GET request to the Tracearr API and return the JSON response as a string."""
    return json.dumps(await _get_json(path, params), indent=2)


async def _get_json(path: str, params: dict[str, Any] | None = None) -> Any:
    """Make a GET request to the Tracearr API and return the decoded JSON response."""
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    async with httpx.AsyncClient(base_url=TRACEARR_BASE_URL, timeout=30) as client:
        resp = await client.get(f"{API_PREFIX}{path}", headers=_headers(), params=clean_params)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def get_health() -> str:
    """Check Tracearr server connectivity. Returns status, version, and connection status for all configured media servers (Plex, Jellyfin, Emby)."""
    return await _get("/health")


@mcp.tool()
async def get_stats(server_id: Optional[str] = None) -> str:
    """Dashboard overview statistics. Returns active stream count, total users, sessions in last 30 days, and violations in last 7 days. Optionally filter by server UUID."""
    return await _get("/stats", {"serverId": server_id})


@mcp.tool()
async def get_stats_today(
    server_id: Optional[str] = None,
    timezone: Optional[str] = None,
) -> str:
    """Today's dashboard statistics with timezone support. Returns active streams, validated plays (>= 2 min), watch time in hours, alerts in last 24h, and active users today."""
    return await _get("/stats/today", {"serverId": server_id, "timezone": timezone})


@mcp.tool()
async def get_activity(
    period: Optional[str] = None,
    server_id: Optional[str] = None,
    timezone: Optional[str] = None,
) -> str:
    """Playback activity trends and breakdowns. Returns plays over time, concurrent streams, day-of-week and hour-of-day distributions, platform usage, and quality breakdown. Period can be 'week', 'month', or 'year' (default: month)."""
    return await _get("/activity", {"period": period, "serverId": server_id, "timezone": timezone})


@mcp.tool()
async def get_streams(
    server_id: Optional[str] = None,
    summary: Optional[bool] = None,
) -> str:
    """Currently active playback sessions with codec and quality details. Set summary=true for a lightweight response that omits the per-stream data array. Each stream includes user, media info, quality (resolution, codecs, bitrate), device, and playback state."""
    return await _get("/streams", {"serverId": server_id, "summary": summary})


@mcp.tool()
async def get_users(
    server_id: Optional[str] = None,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
) -> str:
    """Paginated user list with activity metrics. Each user includes username, role, trust score (0-100), total violations, last activity, and session count. Users with accounts on multiple servers appear once per server."""
    return await _get("/users", {"serverId": server_id, "page": page, "pageSize": page_size})


@mcp.tool()
async def get_violations(
    server_id: Optional[str] = None,
    severity: Optional[str] = None,
    acknowledged: Optional[bool] = None,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
) -> str:
    """Rule violations (sharing detection, concurrent streams, impossible travel, etc). Filter by severity ('low', 'warning', 'high') and acknowledged status. Each violation includes rule type, user info, and rule-specific data."""
    return await _get(
        "/violations",
        {
            "serverId": server_id,
            "severity": severity,
            "acknowledged": acknowledged,
            "page": page,
            "pageSize": page_size,
        },
    )


@mcp.tool()
async def get_history(
    server_id: Optional[str] = None,
    state: Optional[str] = None,
    media_type: Optional[str] = None,
    media_title: Optional[str] = None,
    username: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    timezone: Optional[str] = None,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
) -> str:
    """Session history with full codec and quality details. Filter by state ('playing','paused','stopped'), media_type ('movie','episode','track','live'), date range (YYYY-MM-DD), media_title, or username. media_title and username use case-insensitive substring matching; media_title also searches an episode's show title."""
    params = {
        "serverId": server_id,
        "state": state,
        "mediaType": media_type,
        "startDate": start_date,
        "endDate": end_date,
        "timezone": timezone,
    }

    if media_title is None and username is None:
        return await _get(
            "/history",
            {**params, "page": page, "pageSize": page_size},
        )

    result_page = page or 1
    result_page_size = page_size or 25
    if result_page < 1:
        raise ValueError("page must be greater than 0")
    if not 1 <= result_page_size <= 100:
        raise ValueError("page_size must be between 1 and 100")

    history: list[dict[str, Any]] = []
    source_page = 1
    total = 0
    while source_page == 1 or len(history) < total:
        response = await _get_json(
            "/history",
            {**params, "page": source_page, "pageSize": 100},
        )
        history.extend(response["data"])
        total = response["meta"]["total"]
        source_page += 1

    title_filter = media_title.casefold() if media_title is not None else None
    username_filter = username.casefold() if username is not None else None
    filtered_history = [
        item
        for item in history
        if (
            title_filter is None
            or any(
                title_filter in (item.get(field) or "").casefold()
                for field in ("mediaTitle", "showTitle")
            )
        )
        and (
            username_filter is None
            or username_filter
            in ((item.get("user") or {}).get("username") or "").casefold()
        )
    ]

    start = (result_page - 1) * result_page_size
    return json.dumps(
        {
            "data": filtered_history[start : start + result_page_size],
            "meta": {
                "total": len(filtered_history),
                "page": result_page,
                "pageSize": result_page_size,
            },
        },
        indent=2,
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
