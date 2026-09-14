#!/usr/bin/env python3
"""Profile-local stdio MCP tools for managed Hermes profiles."""
from __future__ import annotations

import argparse
import json
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP

DOCUMENT_MAX_BYTES = 1024 * 1024
DOCUMENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
CREDENTIAL_KEY_RE = re.compile(
    r"(?:token|password|secret|auth|privatekey|apikey|sshkey|credential)",
    re.IGNORECASE,
)
FORBIDDEN_PATH_RE = re.compile(
    r"(?:^|[._-])(?:env|auth|token|secret|privatekey|apikey|ssh|key|keys|"
    r"database|db|sqlite|session|cookie)(?:[._-]|$)",
    re.IGNORECASE,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("virtuajimmy", "copilot"), required=True)
    parser.add_argument("--profile-root", type=Path, required=True)
    parser.add_argument("--runtime-host", required=True)
    parser.add_argument("--terminal-host", required=True)
    parser.add_argument("--terminal-cwd", required=True)
    parser.add_argument("--documents-json", required=True)
    return parser.parse_args()


def _load_documents(raw: str, profile_root: Path) -> dict[str, Path]:
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("documents-json must be a JSON object") from exc
    if not isinstance(configured, dict) or not configured:
        raise ValueError("documents-json must be a non-empty JSON object")

    root = profile_root.expanduser().resolve()
    documents: dict[str, Path] = {}
    for name, raw_path in configured.items():
        if not isinstance(name, str) or not DOCUMENT_NAME_RE.fullmatch(name):
            raise ValueError("document names must be lower-case symbolic identifiers")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError(f"document {name!r} must have a configured path")
        candidate = Path(raw_path).expanduser().resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"document {name!r} is outside the active profile")
        relative_parts = candidate.relative_to(root).parts
        if any(part == ".env" or FORBIDDEN_PATH_RE.search(part) for part in relative_parts):
            raise ValueError(f"document {name!r} names a forbidden file type")
        documents[name] = candidate
    return documents


ARGS = _parse_args()
DOCUMENTS = _load_documents(ARGS.documents_json, ARGS.profile_root)
mcp = FastMCP(f"Hermes local tools ({ARGS.profile})")


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _document_metadata(name: str, path: Path) -> dict[str, Any]:
    try:
        file_stat = path.stat()
    except FileNotFoundError:
        return {"name": name, "exists": False}
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError(f"managed document {name!r} is not a regular file")
    return {
        "name": name,
        "exists": True,
        "size_bytes": file_stat.st_size,
        "modified_utc": datetime.fromtimestamp(
            file_stat.st_mtime, tz=timezone.utc
        ).isoformat(),
        "mode": f"{stat.S_IMODE(file_stat.st_mode):04o}",
    }


def _configured_file_marker(value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith("/") or "\n" in value:
        return None
    return f"<redacted credential; configured file={Path(value).name}>"


def _redact_config(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            normalized_key = re.sub(r"[^a-z0-9]", "", key_text.lower())
            if CREDENTIAL_KEY_RE.search(normalized_key):
                redacted[key_text] = _configured_file_marker(child) or "<redacted>"
            else:
                redacted[key_text] = _redact_config(child)
        return redacted
    if isinstance(value, list):
        return [_redact_config(item) for item in value]
    return value


def _read_managed_document(name: str) -> tuple[Path, str]:
    path = DOCUMENTS.get(name)
    if path is None:
        raise ValueError(
            f"unknown document {name!r}; use profile_manifest for allowed names"
        )
    metadata = _document_metadata(name, path)
    if not metadata["exists"]:
        raise ValueError(f"managed document {name!r} is not deployed")
    if metadata["size_bytes"] > DOCUMENT_MAX_BYTES:
        raise ValueError(f"managed document {name!r} exceeds the read limit")
    return path, path.read_text(encoding="utf-8")


@mcp.tool()
def profile_manifest() -> str:
    """Describe this fixed profile, terminal topology, and readable documents."""
    return _json(
        {
            "profile": ARGS.profile,
            "runtime_host": ARGS.runtime_host,
            "terminal_host": ARGS.terminal_host,
            "terminal_cwd": ARGS.terminal_cwd,
            "documents": [
                _document_metadata(name, path)
                for name, path in sorted(DOCUMENTS.items())
            ],
        }
    )


@mcp.tool()
def profile_read(document: str) -> str:
    """Read one document named by profile_manifest; filesystem paths are rejected."""
    if not DOCUMENT_NAME_RE.fullmatch(document):
        raise ValueError("document must be a symbolic name, not a path")
    path, content = _read_managed_document(document)
    if document == "config":
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise ValueError("managed config is not valid YAML") from exc
        content_value: Any = _redact_config(parsed)
        content_format = "yaml-parsed-redacted"
    else:
        content_value = content
        content_format = "text"
    return _json(
        {
            "document": document,
            "format": content_format,
            "metadata": _document_metadata(document, path),
            "content": content_value,
        }
    )


if __name__ == "__main__":
    mcp.run()