from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from gateway.readiness import collect_runtime_readiness


def test_collect_runtime_readiness_reports_healthy_local_runtime(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  model: test/model\n",
        encoding="utf-8",
    )
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "platforms": {"telegram": {"state": "connected"}},
            "updated_at": "2026-07-09T00:00:00Z",
        },
        active_api_runs=2,
    )

    assert result["status"] == "ok"
    assert result["checks"]["state_db"]["status"] == "ok"
    assert result["checks"]["session_store"]["status"] == "ok"
    assert result["checks"]["config"]["status"] == "ok"
    assert result["checks"]["model"]["status"] == "ok"
    assert result["checks"]["gateway"]["status"] == "ok"
    assert result["checks"]["background_queues"]["active_api_runs"] == 2
    assert result["checks"]["disk"]["status"] in {"ok", "degraded"}


def test_collect_runtime_readiness_degrades_on_invalid_config_and_stopped_gateway(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: [unterminated", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="",
        runtime_status={"gateway_state": "stopped", "platforms": {}},
    )

    assert result["status"] == "degraded"
    assert result["checks"]["config"]["status"] == "degraded"
    assert result["checks"]["model"]["status"] == "degraded"
    assert result["checks"]["gateway"]["status"] == "degraded"
    # Readiness is diagnostic data, not an exception or a destructive repair.
    assert (home / "config.yaml").read_text(encoding="utf-8") == "model: [unterminated"


def test_readiness_uses_running_session_store_state_over_independent_probe(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    monkeypatch.setenv("HERMES_HOME", str(home))

    unavailable = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "platforms": {},
            "session_store": {"status": "unavailable"},
        },
    )

    assert unavailable["checks"]["state_db"]["status"] == "ok"
    assert unavailable["checks"]["session_store"] == {"status": "unavailable"}
    assert unavailable["status"] == "degraded"

    recovered = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "platforms": {},
            "session_store": {"status": "ok"},
        },
    )
    assert recovered["checks"]["session_store"] == {"status": "ok"}


def _running_runtime():
    return {
        "gateway_state": "running",
        "platforms": {},
        "session_store": {"status": "ok"},
    }


def test_readiness_degrades_before_fd_limit(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("gateway.resource_budget.fd_usage", lambda pid=None: (750, 1024))
    monkeypatch.setattr("gateway.resource_budget.sqlite_handle_count", lambda pid=None: 2)
    monkeypatch.setattr("gateway.resource_budget.wal_size_bytes", lambda value: 64)

    result = collect_runtime_readiness(
        configured_model="test/model", runtime_status=_running_runtime()
    )

    assert result["status"] == "degraded"
    assert result["checks"]["file_descriptors"] == {
        "status": "degraded",
        "used": 750,
        "limit": 1024,
        "used_percent": 73.2,
    }
    assert result["checks"]["sqlite_handles"] == {"status": "ok", "count": 2}
    assert result["checks"]["wal"] == {"status": "ok", "bytes": 64}


def test_readiness_marks_fd_usage_critical_at_fixed_threshold(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("gateway.resource_budget.fd_usage", lambda pid=None: (870, 1024))

    result = collect_runtime_readiness(
        configured_model="test/model", runtime_status=_running_runtime()
    )

    assert result["checks"]["file_descriptors"]["status"] == "critical"


def test_readiness_reports_unsupported_resource_probes_as_unknown(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("gateway.resource_budget.fd_usage", lambda pid=None: None)
    monkeypatch.setattr("gateway.resource_budget.sqlite_handle_count", lambda pid=None: None)
    monkeypatch.setattr("gateway.resource_budget.wal_size_bytes", lambda value: None)

    result = collect_runtime_readiness(
        configured_model="test/model", runtime_status=_running_runtime()
    )

    assert result["checks"]["file_descriptors"] == {"status": "unknown"}
    assert result["checks"]["sqlite_handles"] == {"status": "unknown"}
    assert result["checks"]["wal"] == {"status": "unknown"}
    assert result["status"] == "ok"


def test_readiness_reports_code_skew_without_mutation(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "gateway.readiness.detect_code_skew", lambda: ("bootsha", "disksha")
    )

    result = collect_runtime_readiness(
        configured_model="test/model", runtime_status=_running_runtime()
    )

    assert result["checks"]["code_version"] == {
        "status": "degraded",
        "boot": "bootsha",
        "disk": "disksha",
    }


def test_readiness_uses_fixed_disk_degraded_and_critical_thresholds(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "gateway.readiness.shutil.disk_usage",
        lambda value: SimpleNamespace(total=100, used=85, free=15),
    )

    degraded = collect_runtime_readiness(
        configured_model="test/model", runtime_status=_running_runtime()
    )
    assert degraded["checks"]["disk"] == {
        "status": "degraded",
        "used_percent": 85.0,
        "free_bytes": 15,
    }

    monkeypatch.setattr(
        "gateway.readiness.shutil.disk_usage",
        lambda value: SimpleNamespace(total=100, used=92, free=8),
    )
    critical = collect_runtime_readiness(
        configured_model="test/model", runtime_status=_running_runtime()
    )
    assert critical["checks"]["disk"]["status"] == "critical"


def test_resource_budget_payload_contains_counts_but_no_paths(tmp_path, monkeypatch):
    from gateway import resource_budget

    monkeypatch.setattr(resource_budget, "fd_usage", lambda pid=None: (7, 100))
    monkeypatch.setattr(resource_budget, "sqlite_handle_count", lambda pid=None: 3)
    monkeypatch.setattr(resource_budget, "wal_size_bytes", lambda home: 512)

    result = resource_budget.collect_resource_budget(tmp_path / "private-state")

    assert result["file_descriptors"]["status"] == "ok"
    assert result["sqlite_handles"] == {"status": "ok", "count": 3}
    assert result["wal"] == {"status": "ok", "bytes": 512}
    assert str(tmp_path) not in json.dumps(result, sort_keys=True)


