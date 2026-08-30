"""Bounded resource probes for the authenticated readiness surface."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


_FD_DEGRADED_PERCENT = 70.0
_FD_CRITICAL_PERCENT = 85.0
_MAX_FD_SCAN_ENTRIES = 4096
_SQLITE_SUFFIXES = (".db", ".db-wal", ".db-shm", "-wal", "-shm")


@dataclass(frozen=True)
class _FdSnapshot:
    used: int
    limit: int
    sqlite_handles: int
    truncated: bool


def _proc_fd_root(pid: int | None) -> Path | None:
    if os.name != "posix":
        return None
    root = Path("/proc") / str(pid if pid is not None else os.getpid()) / "fd"
    return root if root.is_dir() else None


def _soft_fd_limit() -> int | None:
    try:
        import resource

        soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft_limit <= 0 or soft_limit == resource.RLIM_INFINITY:
            return None
        return int(soft_limit)
    except (ImportError, OSError, ValueError):
        return None


def _sqlite_target_name(handle: Path) -> str | None:
    try:
        target_name = handle.readlink().name.lower()
    except (OSError, ValueError):
        return None
    return target_name.removesuffix(" (deleted)")


def _scan_fd_root(root: Path) -> tuple[int, int, bool]:
    used = 0
    sqlite_handles = 0
    for handle in root.iterdir():
        used += 1
        if used > _MAX_FD_SCAN_ENTRIES:
            return used, sqlite_handles, True
        target_name = _sqlite_target_name(handle)
        if target_name is not None and target_name.endswith(_SQLITE_SUFFIXES):
            sqlite_handles += 1
    return used, sqlite_handles, False


def _collect_fd_snapshot(pid: int | None = None) -> _FdSnapshot | None:
    root = _proc_fd_root(pid)
    limit = _soft_fd_limit()
    if root is None or limit is None:
        return None
    try:
        used, sqlite_handles, truncated = _scan_fd_root(root)
    except (OSError, ValueError):
        return None
    return _FdSnapshot(
        used=used,
        limit=limit,
        sqlite_handles=sqlite_handles,
        truncated=truncated,
    )


def fd_usage(pid: int | None = None) -> tuple[int, int] | None:
    """Return exact current/soft FD usage when a bounded scan can complete."""
    snapshot = _collect_fd_snapshot(pid)
    if snapshot is None or snapshot.truncated:
        return None
    return snapshot.used, snapshot.limit


def sqlite_handle_count(pid: int | None = None) -> int | None:
    """Count SQLite handles when a bounded scan can complete."""
    snapshot = _collect_fd_snapshot(pid)
    if snapshot is None or snapshot.truncated:
        return None
    return snapshot.sqlite_handles


def wal_size_bytes(home: Path) -> int | None:
    """Return the canonical state WAL size without following symlinks."""
    path = home / "state.db-wal"
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return None
    return max(0, int(metadata.st_size))


def _unknown(**extra: object) -> dict[str, object]:
    return {"status": "unknown", **extra}


def collect_resource_budget(home: Path) -> dict[str, dict[str, object]]:
    """Return sanitized counts only; unsupported probes remain explicitly unknown."""
    snapshot = _collect_fd_snapshot()
    if snapshot is None:
        fd_check: dict[str, object] = _unknown()
        sqlite_check: dict[str, object] = _unknown()
    elif snapshot.truncated:
        fd_check = {
            "status": "degraded",
            "used_at_least": snapshot.used,
            "limit": snapshot.limit,
            "scan_truncated": True,
        }
        sqlite_check = _unknown(scan_truncated=True)
    elif snapshot.used < 0 or snapshot.limit <= 0 or snapshot.sqlite_handles < 0:
        fd_check = _unknown()
        sqlite_check = _unknown()
    else:
        raw_percent = (snapshot.used / snapshot.limit) * 100
        if raw_percent >= _FD_CRITICAL_PERCENT:
            status_value = "critical"
        elif raw_percent >= _FD_DEGRADED_PERCENT:
            status_value = "degraded"
        else:
            status_value = "ok"
        fd_check = {
            "status": status_value,
            "used": snapshot.used,
            "limit": snapshot.limit,
            "used_percent": round(raw_percent, 1),
        }
        sqlite_check = {"status": "ok", "count": snapshot.sqlite_handles}

    wal_bytes = wal_size_bytes(home)
    wal_check = (
        _unknown()
        if wal_bytes is None or wal_bytes < 0
        else {"status": "ok", "bytes": wal_bytes}
    )
    return {
        "file_descriptors": fd_check,
        "sqlite_handles": sqlite_check,
        "wal": wal_check,
    }


__all__ = [
    "collect_resource_budget",
    "fd_usage",
    "sqlite_handle_count",
    "wal_size_bytes",
]
