from dataclasses import replace
from pathlib import Path

import pytest

import pptextract.db as db
from pptextract.config import Settings


def test_production_config_rejects_temporary_persistent_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPTEXTRACT_DATA_ROOT", "/tmp/pptextract-data")

    with pytest.raises(ValueError, match="不得位于临时目录"):
        Settings.from_env().validate()


def test_versioned_config_rejects_unknown_version(tmp_path: Path) -> None:
    settings = Settings.for_test(tmp_path)
    incompatible = Settings(
        config_version=2,
        database_path=settings.database_path,
        object_store_path=settings.object_store_path,
        web_dist_path=settings.web_dist_path,
        default_actor_id=settings.default_actor_id,
        worker_id=settings.worker_id,
        allow_temporary_storage=True,
    )

    with pytest.raises(ValueError, match="不支持"):
        incompatible.validate()


def test_storage_location_check_fails_closed_when_filesystem_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "_filesystem_type", lambda _path: None)

    assert db.database_path_is_local(tmp_path) is False


def test_storage_location_check_rejects_network_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "_filesystem_type", lambda _path: "nfs4")

    assert db.database_path_is_local(tmp_path) is False


def test_retry_delay_must_be_finite(tmp_path: Path) -> None:
    settings = replace(Settings.for_test(tmp_path), job_retry_base_seconds=float("nan"))

    with pytest.raises(ValueError, match="任务重试基础延迟"):
        settings.validate()
