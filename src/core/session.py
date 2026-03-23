"""Scan session persistence — save and load scan results to avoid re-scanning."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    DiskInfo,
    FileStatus,
    FileSystemType,
    PartitionInfo,
    PartitionScheme,
    RecoveredEntry,
    ScanResult,
)

logger = logging.getLogger(__name__)

# Default directory for session files
SESSIONS_DIR = Path(os.environ.get("APPDATA", "")) / "DataForge" / "sessions"


def _ensure_sessions_dir() -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR


def _session_id(disk: DiskInfo, partition: PartitionInfo | None) -> str:
    """Generate a unique session ID from disk serial + partition offset."""
    key = f"{disk.serial}_{disk.index}_{disk.size_bytes}"
    if partition:
        key += f"_{partition.index}_{partition.offset_bytes}_{partition.size_bytes}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _session_path(session_id: str) -> Path:
    return _ensure_sessions_dir() / f"{session_id}.json"


# --- Serialization helpers ---

def _entry_to_dict(entry: RecoveredEntry) -> dict[str, Any]:
    return {
        "name": entry.name,
        "path": entry.path,
        "is_directory": entry.is_directory,
        "size_bytes": entry.size_bytes,
        "status": entry.status.value,
        "confidence": entry.confidence,
        "created": entry.created.isoformat() if entry.created else None,
        "modified": entry.modified.isoformat() if entry.modified else None,
        "accessed": entry.accessed.isoformat() if entry.accessed else None,
        "inode": entry.inode,
        "data_runs": entry.data_runs,
        "children": [_entry_to_dict(c) for c in entry.children],
    }


def _entry_from_dict(d: dict[str, Any]) -> RecoveredEntry:
    return RecoveredEntry(
        name=d["name"],
        path=d["path"],
        is_directory=d["is_directory"],
        size_bytes=d.get("size_bytes", 0),
        status=FileStatus(d.get("status", "intact")),
        confidence=d.get("confidence", 1.0),
        created=_parse_iso(d.get("created")),
        modified=_parse_iso(d.get("modified")),
        accessed=_parse_iso(d.get("accessed")),
        inode=d.get("inode", 0),
        data_runs=[tuple(r) for r in d.get("data_runs", [])],
        children=[_entry_from_dict(c) for c in d.get("children", [])],
    )


def _parse_iso(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except (ValueError, TypeError):
        return None


def _disk_to_dict(disk: DiskInfo) -> dict[str, Any]:
    return {
        "index": disk.index,
        "model": disk.model,
        "serial": disk.serial,
        "size_bytes": disk.size_bytes,
        "sector_size": disk.sector_size,
        "partition_scheme": disk.partition_scheme.value,
        "partitions": [_partition_to_dict(p) for p in disk.partitions],
    }


def _disk_from_dict(d: dict[str, Any]) -> DiskInfo:
    disk = DiskInfo(
        index=d["index"],
        model=d.get("model", ""),
        serial=d.get("serial", ""),
        size_bytes=d.get("size_bytes", 0),
        sector_size=d.get("sector_size", 512),
        partition_scheme=PartitionScheme(d.get("partition_scheme", "Unknown")),
    )
    disk.partitions = [_partition_from_dict(p) for p in d.get("partitions", [])]
    return disk


def _partition_to_dict(part: PartitionInfo) -> dict[str, Any]:
    return {
        "index": part.index,
        "offset_bytes": part.offset_bytes,
        "size_bytes": part.size_bytes,
        "fs_type": part.fs_type.value,
        "label": part.label,
        "drive_letter": part.drive_letter,
        "is_active": part.is_active,
    }


def _partition_from_dict(d: dict[str, Any]) -> PartitionInfo:
    return PartitionInfo(
        index=d["index"],
        offset_bytes=d["offset_bytes"],
        size_bytes=d["size_bytes"],
        fs_type=FileSystemType(d.get("fs_type", "Unknown")),
        label=d.get("label", ""),
        drive_letter=d.get("drive_letter", ""),
        is_active=d.get("is_active", False),
    )


def _result_to_dict(result: ScanResult) -> dict[str, Any]:
    return {
        "disk": _disk_to_dict(result.disk),
        "partition": _partition_to_dict(result.partition),
        "root_entries": [_entry_to_dict(e) for e in result.root_entries],
        "total_files": result.total_files,
        "total_deleted": result.total_deleted,
        "total_size_bytes": result.total_size_bytes,
        "scan_duration_seconds": result.scan_duration_seconds,
        "target_path": result.target_path,
    }


def _result_from_dict(d: dict[str, Any]) -> ScanResult:
    return ScanResult(
        disk=_disk_from_dict(d["disk"]),
        partition=_partition_from_dict(d["partition"]),
        root_entries=[_entry_from_dict(e) for e in d.get("root_entries", [])],
        total_files=d.get("total_files", 0),
        total_deleted=d.get("total_deleted", 0),
        total_size_bytes=d.get("total_size_bytes", 0),
        scan_duration_seconds=d.get("scan_duration_seconds", 0.0),
        target_path=d.get("target_path", "/"),
    )


# --- Public API ---

def save_session(result: ScanResult, partition: PartitionInfo | None = None) -> Path:
    """Save a scan result to a session file.

    Returns:
        Path to the saved session file.
    """
    sid = _session_id(result.disk, partition or result.partition)
    path = _session_path(sid)

    session_data = {
        "version": 1,
        "session_id": sid,
        "saved_at": datetime.now(tz=timezone.utc).isoformat(),
        "scan_result": _result_to_dict(result),
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(session_data, f, ensure_ascii=False)

    logger.info("Session saved: %s (%d entries)", path.name, result.total_files)
    return path


def load_session(
    disk: DiskInfo, partition: PartitionInfo | None = None
) -> ScanResult | None:
    """Load a previously saved session for the given disk/partition.

    Returns:
        ScanResult if a session exists, or None.
    """
    sid = _session_id(disk, partition)
    path = _session_path(sid)

    if not path.exists():
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if data.get("version") != 1:
            logger.warning("Unknown session version: %s", data.get("version"))
            return None

        result = _result_from_dict(data["scan_result"])
        saved_at = data.get("saved_at", "unknown")
        logger.info(
            "Session loaded: %s (saved %s, %d entries)",
            path.name, saved_at, result.total_files,
        )
        return result

    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("Corrupt session file %s: %s", path.name, exc)
        return None


def list_sessions() -> list[dict[str, Any]]:
    """List all saved sessions with metadata."""
    sessions_dir = _ensure_sessions_dir()
    sessions = []

    for f in sessions_dir.glob("*.json"):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            scan = data.get("scan_result", {})
            disk = scan.get("disk", {})
            partition = scan.get("partition", {})
            sessions.append({
                "session_id": data.get("session_id", f.stem),
                "saved_at": data.get("saved_at", ""),
                "disk_model": disk.get("model", "Unknown"),
                "disk_index": disk.get("index", -1),
                "partition_index": partition.get("index", -1),
                "fs_type": partition.get("fs_type", ""),
                "total_files": scan.get("total_files", 0),
                "total_deleted": scan.get("total_deleted", 0),
                "target_path": scan.get("target_path", "/"),
                "file_path": str(f),
            })
        except Exception:
            continue

    sessions.sort(key=lambda s: s.get("saved_at", ""), reverse=True)
    return sessions


def delete_session(session_id: str) -> bool:
    """Delete a saved session file."""
    path = _session_path(session_id)
    if path.exists():
        path.unlink()
        logger.info("Session deleted: %s", session_id)
        return True
    return False
