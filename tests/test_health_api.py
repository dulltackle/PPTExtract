from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pptextract.api import create_app
from pptextract.config import Settings
from pptextract.db import connect
from pptextract.object_store import LocalObjectStore
from pptextract.worker import record_heartbeat


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.for_test(tmp_path)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_sqlite_runtime_pragmas_are_enabled(client: TestClient, settings: Settings) -> None:
    with connect(settings) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_health_reports_api_database_object_store_and_fresh_worker(
    client: TestClient, settings: Settings
) -> None:
    record_heartbeat(settings)

    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "config_version": 1,
        "components": {
            "api": {"status": "ready"},
            "database": {"status": "ready"},
            "object_store": {"status": "ready"},
            "worker": {"status": "ready", "worker_id": "test-worker"},
        },
    }


def test_health_is_degraded_until_worker_has_heartbeated(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["components"]["worker"] == {
        "status": "unavailable",
        "worker_id": "test-worker",
    }


def test_health_is_degraded_when_object_store_is_not_writable(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_heartbeat(settings)

    def fail_probe(_store: LocalObjectStore) -> None:
        raise OSError("read-only object store")

    monkeypatch.setattr(LocalObjectStore, "check_writable", fail_probe)

    response = client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json()["components"]["object_store"] == {"status": "unavailable"}
