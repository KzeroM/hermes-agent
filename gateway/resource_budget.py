"""Bounded resource probes for the authenticated readiness surface."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


_FD_DEGRADED_PERCENT = 70.0
_FD_CRITICAL_PERCENT = 85.0
_SQLITE_SUFFIXES = (".db", ".db-wal", ".db-shm", "-wal", "-shm")


def _proc_fd_root(pid: int | None) -> Path | None:
    if os.name != "posix":
        return None
    root = Path("/proc") / str(pid if pid is not None else os.getpid()) / "fd"
    return root if root.is_dir() else None


def fd_usage(pid: int | None = None) -> tuple[int, int] | None:
    """Return current/soft file-descriptor usage where ``/proc`` is available."""
    root = _proc_fd_root(pid)
    if root is None:
        return None
    try:
        used = sum(1 for _ in root.iterdir())
        import resource

        soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft_limit <= 0 or soft_limit == resource.RLIM_INFINITY:
            return None
        return used, int(soft_limit)
    except (ImportError, OSError, ValueError):
        return None


def sqlite_handle_count(pid: int | None = None) -> int | None:
    """Count open SQLite-related handles without exposing their target paths."""
    root = _proc_fd_root(pid)
    if root is None:
        return None
    try:
        count = 0
        for handle in root.iterdir():
            target_name = handle.readlink().name.lower()
            if target_name.endswith(_SQLITE_SUFFIXES):
                count += 1
        return count
    except (OSError, ValueError):
        return None


def wal_size_bytes(home: Path) -> int | None:
    """Return the canonical state WAL size without following symlinks."""
    path = home / "state.db-wal"
    try:
        if path.is_symlink():
            return None
        if not path.exists():
            return 0
        if not path.is_file():
            return None
        return max(0, int(path.stat().st_size))
    except OSError:
        return None


def _unknown() -> dict[str, object]:
    return {"status": "unknown"}


def collect_resource_budget(home: Path) -> dict[str, dict[str, object]]:
    """Return sanitized counts only; unsupported probes remain explicitly unknown."""
    fd = fd_usage()
    if fd is None:
        fd_check: dict[str, object] = _unknown()
    else:
        used, limit = fd
        if used < 0 or limit <= 0:
            fd_check = _unknown()
        else:
            used_percent = round((used / limit) * 100, 1)
            if used_percent >= _FD_CRITICAL_PERCENT:
                status = "critical"
            elif used_percent >= _FD_DEGRADED_PERCENT:
                status = "degraded"
            else:
                status = "ok"
            fd_check = {
                "status": status,
                "used": used,
                "limit": limit,
                "used_percent": used_percent,
            }

    sqlite_handles = sqlite_handle_count()
    sqlite_check = (
        _unknown()
        if sqlite_handles is None or sqlite_handles < 0
        else {"status": "ok", "count": sqlite_handles}
    )
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
