from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import resource_budget
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
    monkeypatch.setattr(
        "gateway.readiness.shutil.disk_usage",
        lambda value: SimpleNamespace(total=100, used=10, free=90),
    )
    monkeypatch.setattr("gateway.readiness.detect_code_skew", lambda: None)
    monkeypatch.setattr(
        "gateway.readiness.collect_resource_budget",
        lambda value: {
            "file_descriptors": {
                "status": "ok",
                "used": 7,
                "limit": 100,
                "used_percent": 7.0,
            },
            "sqlite_handles": {"status": "ok", "count": 1},
            "wal": {"status": "ok", "bytes": 0},
        },
    )

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
    monkeypatch.setattr(
        resource_budget,
        "_collect_fd_snapshot",
        lambda pid=None: SimpleNamespace(
            used=750, limit=1024, sqlite_handles=2, truncated=False
        ),
        raising=False,
    )
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


@pytest.mark.parametrize(
    ("used", "limit", "expected_status"),
    [
        (6996, 10000, "ok"),
        (7000, 10000, "degraded"),
        (8496, 10000, "degraded"),
        (8500, 10000, "critical"),
    ],
)
def test_readiness_uses_unrounded_fd_thresholds(
    tmp_path, monkeypatch, used, limit, expected_status
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        resource_budget,
        "_collect_fd_snapshot",
        lambda pid=None: SimpleNamespace(
            used=used, limit=limit, sqlite_handles=0, truncated=False
        ),
        raising=False,
    )

    result = collect_runtime_readiness(
        configured_model="test/model", runtime_status=_running_runtime()
    )

    assert result["checks"]["file_descriptors"]["status"] == expected_status


def test_readiness_reports_unsupported_resource_probes_as_unknown(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "gateway.readiness.shutil.disk_usage",
        lambda value: SimpleNamespace(total=100, used=10, free=90),
    )
    monkeypatch.setattr("gateway.readiness.detect_code_skew", lambda: None)
    monkeypatch.setattr(
        "gateway.readiness.collect_resource_budget",
        lambda value: {
            "file_descriptors": {"status": "unknown"},
            "sqlite_handles": {"status": "unknown"},
            "wal": {"status": "unknown"},
        },
    )

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
        lambda value: SimpleNamespace(total=10000, used=8496, free=1504),
    )

    below_degraded = collect_runtime_readiness(
        configured_model="test/model", runtime_status=_running_runtime()
    )
    assert below_degraded["checks"]["disk"]["status"] == "ok"

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
    monkeypatch.setattr(
        resource_budget,
        "_collect_fd_snapshot",
        lambda pid=None: SimpleNamespace(
            used=7, limit=100, sqlite_handles=3, truncated=False
        ),
        raising=False,
    )
    monkeypatch.setattr(resource_budget, "wal_size_bytes", lambda home: 512)

    result = resource_budget.collect_resource_budget(tmp_path / "private-state")

    assert result["file_descriptors"]["status"] == "ok"
    assert result["sqlite_handles"] == {"status": "ok", "count": 3}
    assert result["wal"] == {"status": "ok", "bytes": 512}
    assert str(tmp_path) not in json.dumps(result, sort_keys=True)


class _FakeFdEntry:
    def __init__(self, target: str | None = None):
        self._target = target

    def readlink(self) -> Path:
        if self._target is None:
            raise FileNotFoundError
        return Path(self._target)


class _FakeFdRoot:
    def __init__(self, entries: list[_FakeFdEntry]):
        self._entries = entries
        self.iterated = 0

    def iterdir(self):
        for entry in self._entries:
            self.iterated += 1
            yield entry


def test_resource_budget_uses_one_bounded_fd_scan(monkeypatch, tmp_path):
    root = _FakeFdRoot([_FakeFdEntry("ordinary.txt") for _ in range(10)])
    monkeypatch.setattr(resource_budget, "_proc_fd_root", lambda pid=None: root)
    monkeypatch.setattr(resource_budget, "_MAX_FD_SCAN_ENTRIES", 3, raising=False)
    monkeypatch.setitem(
        sys.modules,
        "resource",
        SimpleNamespace(
            RLIMIT_NOFILE=7,
            RLIM_INFINITY=-1,
            getrlimit=lambda value: (100, 100),
        ),
    )

    result = resource_budget.collect_resource_budget(tmp_path)

    assert root.iterated == 4
    assert result["file_descriptors"] == {
        "status": "degraded",
        "used_at_least": 4,
        "limit": 100,
        "scan_truncated": True,
    }
    assert result["sqlite_handles"] == {
        "status": "unknown",
        "scan_truncated": True,
    }


def test_sqlite_handle_count_skips_transient_readlink_errors(monkeypatch):
    root = _FakeFdRoot([
        _FakeFdEntry(),
        _FakeFdEntry("state.db-wal"),
        _FakeFdEntry("notes.txt"),
    ])
    monkeypatch.setattr(resource_budget, "_proc_fd_root", lambda pid=None: root)
    monkeypatch.setitem(
        sys.modules,
        "resource",
        SimpleNamespace(
            RLIMIT_NOFILE=7,
            RLIM_INFINITY=-1,
            getrlimit=lambda value: (100, 100),
        ),
    )

    assert resource_budget.sqlite_handle_count() == 1


def test_sqlite_handle_count_observes_real_open_database(tmp_path):
    if os.name != "posix" or not Path("/proc/self/fd").is_dir():
        pytest.skip("procfs file-descriptor inspection is Linux-only")

    with sqlite3.connect(tmp_path / "probe.db") as conn:
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
        assert resource_budget.sqlite_handle_count() >= 1


def test_resource_budget_reports_procfs_absence_as_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(resource_budget, "_proc_fd_root", lambda pid=None: None)

    result = resource_budget.collect_resource_budget(tmp_path)

    assert result["file_descriptors"] == {"status": "unknown"}
    assert result["sqlite_handles"] == {"status": "unknown"}


def test_wal_size_bytes_covers_absent_regular_and_unsafe_paths(tmp_path):
    assert resource_budget.wal_size_bytes(tmp_path) == 0

    wal = tmp_path / "state.db-wal"
    wal.write_bytes(b"wal-bytes")
    assert resource_budget.wal_size_bytes(tmp_path) == 9

    wal.unlink()
    wal.mkdir()
    assert resource_budget.wal_size_bytes(tmp_path) is None

    wal.rmdir()
    try:
        wal.symlink_to(tmp_path / "elsewhere")
    except OSError:
        pytest.skip("symlink creation is unavailable")
    assert resource_budget.wal_size_bytes(tmp_path) is None
