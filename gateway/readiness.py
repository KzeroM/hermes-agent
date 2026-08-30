"""Bounded, non-destructive readiness probes for authenticated health surfaces."""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import yaml

from gateway.code_skew import detect_code_skew
from gateway.resource_budget import collect_resource_budget
from hermes_constants import get_hermes_home


_DISK_DEGRADED_PERCENT = 85.0
_DISK_CRITICAL_PERCENT = 92.0


def _check(status: str, detail: str | None = None, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status}
    if detail:
        result["detail"] = detail
    result.update(extra)
    return result


def _probe_state_db(home: Path) -> dict[str, Any]:
    path = home / "state.db"
    if not path.exists():
        return _check("ok", "not initialized")
    try:
        # A readiness probe must never compete with normal state writers. A
        # read-only schema query still catches unreadable/corrupt databases
        # without taking a write reservation on every health poll.
        # ``closing(...)`` is required: sqlite3's connection context manager
        # only commits/rolls back — it never closes, so a bare ``with
        # sqlite3.connect(...)`` leaks one connection (and its fds) per
        # health poll in the long-running gateway (#69678/#69567 bug class).
        uri = f"file:{path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as conn:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        return _check("ok")
    except Exception as exc:
        return _check("degraded", type(exc).__name__)


def _probe_config(home: Path) -> dict[str, Any]:
    path = home / "config.yaml"
    if not path.exists():
        return _check("ok", "using defaults")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is not None and not isinstance(raw, dict):
            return _check("degraded", "top level is not a mapping")
        return _check("ok")
    except Exception as exc:
        return _check("degraded", f"invalid config ({type(exc).__name__})")


def _probe_disk(home: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(home)
        raw_percent = (usage.used / usage.total) * 100 if usage.total else 0.0
        if raw_percent >= _DISK_CRITICAL_PERCENT:
            status_value = "critical"
        elif raw_percent >= _DISK_DEGRADED_PERCENT:
            status_value = "degraded"
        else:
            status_value = "ok"
        return _check(
            status_value,
            used_percent=round(raw_percent, 1),
            free_bytes=usage.free,
        )
    except Exception as exc:
        return _check("degraded", type(exc).__name__)


def _probe_gateway(runtime_status: dict[str, Any]) -> dict[str, Any]:
    state = str(runtime_status.get("gateway_state") or "unknown")
    platforms = runtime_status.get("platforms")
    connected = 0
    configured = 0
    if isinstance(platforms, dict):
        configured = len(platforms)
        connected = sum(
            1
            for value in platforms.values()
            if isinstance(value, dict)
            and str(value.get("state") or value.get("status") or "").lower()
            in {"connected", "running", "ok"}
        )
    status_value = "ok" if state in {"running", "draining"} else "degraded"
    return _check(
        status_value,
        state=state,
        connected_platforms=connected,
        platforms=configured,
    )


def _probe_session_store(
    runtime_status: dict[str, Any], state_db_probe: dict[str, Any]
) -> dict[str, Any]:
    """Report the running gateway cache state, not an independent reopen."""
    runtime_store = runtime_status.get("session_store")
    if isinstance(runtime_store, dict):
        state = str(runtime_store.get("status") or "unknown")
        if state in {"ok", "unavailable", "retrying"}:
            return _check(state)
    # Older gateways do not publish a cache state. Preserve their readiness
    # behavior until their process restarts onto a version that does.
    return _check("ok" if state_db_probe.get("status") == "ok" else "unavailable")


def _probe_code_version() -> dict[str, Any]:
    skew = detect_code_skew()
    if skew is None:
        return _check("ok")
    boot, disk = skew
    return _check("degraded", boot=boot, disk=disk)


def collect_runtime_readiness(
    *,
    configured_model: str,
    runtime_status: dict[str, Any] | None,
    active_api_runs: int = 0,
    process_completion_queue_depth: int = 0,
    active_delegations: int = 0,
) -> dict[str, Any]:
    """Return bounded readiness diagnostics without mutating runtime state.

    The detailed health endpoint is authenticated. Even there, probes expose
    status and counts only: never config values, credentials, paths, commands,
    queue payloads, or exception messages.
    """
    home = get_hermes_home()
    runtime = runtime_status if isinstance(runtime_status, dict) else {}
    state_db_probe = _probe_state_db(home)
    checks = {
        "state_db": state_db_probe,
        "session_store": _probe_session_store(runtime, state_db_probe),
        "config": _probe_config(home),
        "model": _check("ok" if str(configured_model or "").strip() else "degraded"),
        "disk": _probe_disk(home),
        "gateway": _probe_gateway(runtime),
        "code_version": _probe_code_version(),
        "background_queues": _check(
            "ok",
            active_api_runs=max(0, int(active_api_runs)),
            process_completions=max(0, int(process_completion_queue_depth)),
            active_delegations=max(0, int(active_delegations)),
        ),
    }
    checks.update(collect_resource_budget(home))
    overall = (
        "degraded"
        if any(item.get("status") not in {"ok", "unknown"} for item in checks.values())
        else "ok"
    )
    return {"status": overall, "checks": checks}


__all__ = ["collect_runtime_readiness"]
