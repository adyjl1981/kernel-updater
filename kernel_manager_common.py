"""Common helpers for tool-agnostic logging and build metadata."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CACHE_DIR = Path.home() / ".cache" / "kernel-manager"
_LOG_PATH = _CACHE_DIR / "operations.jsonl"
_METADATA_PATH = _CACHE_DIR / "build-metadata.json"


def _ensure_cache_dir() -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR


def log_event(event: str, **details: Any) -> dict[str, Any]:
    """Write a structured JSON event to the local cache log."""
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **details,
    }
    _ensure_cache_dir()
    with _LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    return payload


def write_build_metadata(**details: Any) -> dict[str, Any]:
    """Record the latest build metadata in a single JSON object."""
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **details,
    }
    _ensure_cache_dir()
    with _METADATA_PATH.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return payload


def read_build_metadata() -> dict[str, Any]:
    if not _METADATA_PATH.exists():
        return {}
    try:
        with _METADATA_PATH.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return {}
