from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pptextract.api import create_app
from pptextract.config import Settings
from pptextract.db import connect
from pptextract.downstream import (
    DownstreamGeneration,
    DownstreamSimulator,
    DownstreamSyncError,
    SourceResponse,
)
from pptextract.worker import run_once
from tests.test_publication_api import _create_candidate, _seed_publication_scope


@dataclass
class _Source:
    client: TestClient
    transform: Callable[[str, bytes], bytes] = lambda _uri, payload: payload

    def __call__(self, uri: str, headers: Mapping[str, str]) -> SourceResponse:
        response = self.client.get(uri, headers=dict(headers))
        payload = self.transform(uri, response.content)
        blocks: Iterable[bytes] = (
            payload[index : index + 17] for index in range(0, len(payload), 17)
        )
        return SourceResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            body=blocks,
        )


def _publish(client: TestClient, settings: Settings) -> dict[str, object]:
    candidate = _create_candidate(client)
    response = client.post(
        f"/api/v1/publications/candidates/{candidate['candidate_id']}/confirm"
    )
    assert response.status_code == 202
    assert run_once(settings) is True
    return client.get("/api/v1/publications/current").json()


def test_downstream_imports_in_isolation_and_switches_generation_atomically(
    tmp_path: Path,
) -> None:
    settings = Settings.for_test(tmp_path)
    reject_import = False

    def importer(generation: DownstreamGeneration) -> DownstreamGeneration:
        if reject_import:
            raise RuntimeError("注入的 generation 导入失败")
        return generation

    with TestClient(create_app(settings)) as client:
        _seed_publication_scope(settings)
        first_pointer = _publish(client, settings)
        simulator = DownstreamSimulator(import_generation=importer)

        assert simulator.synchronize(_Source(client)) is True
        assert simulator.current_generation is not None
        assert simulator.current_generation.publication_seq == first_pointer["publication_seq"]
        assert set(simulator.current_generation.chunks) == {"chunk-approved"}
        with pytest.raises(TypeError):
            simulator.current_generation.chunks["chunk-approved"]["text"] = "切换后篡改"

        with connect(settings) as connection:
            connection.execute(
                "UPDATE page_versions SET review_status = 'pending', current_snapshot_id = NULL "
                "WHERE page_version_id = 'page-version-approved'"
            )
            connection.commit()
        second_pointer = _publish(client, settings)

        reject_import = True
        with pytest.raises(RuntimeError, match="generation 导入失败"):
            simulator.synchronize(_Source(client))
        assert simulator.current_generation.publication_seq == first_pointer["publication_seq"]
        assert set(simulator.current_generation.chunks) == {"chunk-approved"}

        reject_import = False
        assert simulator.synchronize(_Source(client)) is True
        assert simulator.current_generation.publication_seq == second_pointer["publication_seq"]
        assert simulator.current_generation.chunks == {}
        assert simulator.synchronize(_Source(client)) is False


def test_downstream_rejects_incomplete_import_and_can_retry(
    tmp_path: Path,
) -> None:
    settings = Settings.for_test(tmp_path)
    drop_chunks = True

    def importer(generation: DownstreamGeneration) -> DownstreamGeneration:
        return DownstreamGeneration(
            publication_seq=generation.publication_seq,
            snapshot_id=generation.snapshot_id,
            chunks={} if drop_chunks else generation.chunks,
            assets=generation.assets,
        )

    with TestClient(create_app(settings)) as client:
        _seed_publication_scope(settings)
        pointer = _publish(client, settings)
        simulator = DownstreamSimulator(import_generation=importer)

        with pytest.raises(DownstreamSyncError, match="不完整"):
            simulator.synchronize(_Source(client))
        assert simulator.current_generation is None

        drop_chunks = False
        assert simulator.synchronize(_Source(client)) is True
        assert simulator.current_generation is not None
        assert simulator.current_generation.publication_seq == pointer["publication_seq"]
        assert set(simulator.current_generation.chunks) == {"chunk-approved"}


def test_downstream_rejects_self_consistent_download_with_invalid_archive_and_retries(
    tmp_path: Path,
) -> None:
    settings = Settings.for_test(tmp_path)
    with TestClient(create_app(settings)) as client:
        _seed_publication_scope(settings)
        pointer = _publish(client, settings)
        archive_response = client.get(str(pointer["artifact_uri"]))
        with zipfile.ZipFile(BytesIO(archive_response.content)) as source:
            files = {name: source.read(name) for name in source.namelist()}
        asset_path = next(name for name in files if name.startswith("assets/"))
        files[asset_path] = files[asset_path] + b"tampered"
        damaged_output = BytesIO()
        with zipfile.ZipFile(damaged_output, "w") as damaged:
            for name, payload in files.items():
                damaged.writestr(name, payload)
        damaged_archive = damaged_output.getvalue()
        damaged_sha = hashlib.sha256(damaged_archive).hexdigest()
        damaged_pointer = {
            **pointer,
            "sha256": damaged_sha,
            "size_bytes": len(damaged_archive),
        }
        damaged_etag = f'"{damaged_sha}"'

        def damaged_source(uri: str, _headers: Mapping[str, str]) -> SourceResponse:
            if uri == "/api/v1/publications/current":
                body = json.dumps(damaged_pointer).encode()
                return SourceResponse(200, {"ETag": damaged_etag}, (body,))
            return SourceResponse(
                200,
                {
                    "Content-Type": "application/zip",
                    "Content-Length": str(len(damaged_archive)),
                    "ETag": damaged_etag,
                },
                (damaged_archive,),
            )

        simulator = DownstreamSimulator()
        with pytest.raises(DownstreamSyncError, match="内部契约"):
            simulator.synchronize(damaged_source)
        assert simulator.current_generation is None

        assert simulator.synchronize(_Source(client)) is True
        assert simulator.current_generation is not None
        assert simulator.current_generation.publication_seq == pointer["publication_seq"]


def test_downstream_rejects_importer_that_mutates_verified_generation(
    tmp_path: Path,
) -> None:
    settings = Settings.for_test(tmp_path)
    mutate_import = True

    def importer(generation: DownstreamGeneration) -> DownstreamGeneration:
        if mutate_import:
            generation.chunks["chunk-approved"]["text"] = "导入时被篡改"
        return generation

    with TestClient(create_app(settings)) as client:
        _seed_publication_scope(settings)
        _publish(client, settings)
        simulator = DownstreamSimulator(import_generation=importer)

        with pytest.raises(DownstreamSyncError, match="不完整"):
            simulator.synchronize(_Source(client))
        assert simulator.current_generation is None

        mutate_import = False
        assert simulator.synchronize(_Source(client)) is True
        assert simulator.current_generation is not None
        assert simulator.current_generation.chunks["chunk-approved"]["text"] != "导入时被篡改"
