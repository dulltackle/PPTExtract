from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from collections.abc import Iterator
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pptextract.api import create_app
from pptextract.config import Settings
from pptextract.db import connect
from pptextract.jobs import claim_next_job
from pptextract.object_store import LocalObjectStore
from pptextract.publication import (
    _checkpoint,
    confirm_candidate,
    fail_publication_job,
    validate_publication_archive,
)
from pptextract.rendering import render_configuration_version
from pptextract.worker import run_once


@pytest.fixture
def system(tmp_path: Path) -> Iterator[tuple[TestClient, Settings]]:
    settings = replace(Settings.for_test(tmp_path), job_retry_base_seconds=0)
    with TestClient(create_app(settings)) as client:
        yield client, settings


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _seed_publication_scope(settings: Settings) -> dict[str, str]:
    now = "2026-08-29T00:00:00+00:00"
    source = b"synthetic-pptx"
    render = b"synthetic-render"
    asset = b"\x89PNG\r\n\x1a\npublic-visual-asset"
    store = LocalObjectStore(settings.object_store_path)
    for payload in (source, render, asset):
        store.put(payload)
    render_config = render_configuration_version(settings.render_image)
    content = {
        "titles": ["公开页标题"],
        "body": ["公开页正文。"],
        "tables": [],
        "images": [],
        "speaker_notes": ["公开演讲者备注。"],
    }
    with connect(settings) as connection:
        for payload, media_type in (
            (source, "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            (render, "image/png"),
            (asset, "image/png"),
        ):
            connection.execute(
                """
                INSERT INTO stored_objects (sha256, size_bytes, media_type, verified_at)
                VALUES (?, ?, ?, ?)
                """,
                (_sha(payload), len(payload), media_type, now),
            )
        connection.execute(
            "INSERT INTO documents (document_id, current_version_id, created_at) "
            "VALUES ('doc-approved', 'version-approved', ?)",
            (now,),
        )
        connection.execute(
            """
            INSERT INTO document_versions (
                version_id, document_id, source_sha256, source_filename,
                source_size_bytes, status, created_at, ready_at,
                render_config_version, render_generation
            ) VALUES ('version-approved', 'doc-approved', ?, '公开知识源.pptx', ?,
                      'ready', ?, ?, ?, ?)
            """,
            (
                _sha(source),
                len(source),
                now,
                now,
                render_config,
                settings.render_generation,
            ),
        )
        for number, suffix, status in (
            (1, "approved", "approved"),
            (2, "pending", "pending"),
            (3, "excluded", "excluded"),
        ):
            page_id = f"page-{suffix}"
            page_version_id = f"page-version-{suffix}"
            connection.execute(
                """
                INSERT INTO ingestion_page_results (
                    version_id, page_number, source_slide_id, relationship_id,
                    source_part, hidden, enabled, source_content_json,
                    fingerprint_version, fingerprint_sha256, render_sha256,
                    render_media_type, render_dpi, render_width_px, render_height_px
                ) VALUES ('version-approved', ?, ?, ?, ?, 0, 1, ?, 1, ?, ?,
                          'image/png', 144, 1280, 720)
                """,
                (
                    number,
                    255 + number,
                    f"rId{number}",
                    f"ppt/slides/slide{number}.xml",
                    json.dumps(content, ensure_ascii=False),
                    f"fingerprint-{suffix}",
                    _sha(render),
                ),
            )
            connection.execute(
                "INSERT INTO pages (page_id, document_id, chunk_id, created_at) "
                "VALUES (?, 'doc-approved', ?, ?)",
                (page_id, f"chunk-{suffix}", now),
            )
            connection.execute(
                """
                INSERT INTO page_versions (
                    page_version_id, page_id, document_id, version_id, page_number,
                    fingerprint_version, fingerprint_sha256, source_content_json,
                    render_sha256, render_media_type, render_dpi, render_width_px,
                    render_height_px, review_status, current_snapshot_id,
                    reviewed_by, reviewed_at, review_source_version_id, created_at
                ) VALUES (?, ?, 'doc-approved', 'version-approved', ?, 1, ?, ?, ?,
                          'image/png', 144, 1280, 720, ?, ?, ?, ?, ?, ?)
                """,
                (
                    page_version_id,
                    page_id,
                    number,
                    f"fingerprint-{suffix}",
                    json.dumps(content, ensure_ascii=False),
                    _sha(render),
                    status,
                    None,
                    "curator-1" if status != "pending" else None,
                    now if status != "pending" else None,
                    "version-approved" if status != "pending" else None,
                    now,
                ),
            )
        connection.execute(
            """
            INSERT INTO ingestion_page_results (
                version_id, page_number, source_slide_id, relationship_id,
                source_part, hidden, enabled
            ) VALUES ('version-approved', 4, 259, 'rId4',
                      'ppt/slides/slide4.xml', 1, 0)
            """
        )
        connection.execute(
            """
            INSERT INTO curation_snapshots (
                snapshot_id, page_version_id, snapshot_kind, source_snapshot_id,
                overview, source_content_json, capture_required, created_by, created_at
            ) VALUES ('snapshot-approved', 'page-version-approved', 'formal', NULL,
                      '公开页总述。', ?, 0, 'curator-1', ?)
            """,
            (json.dumps(content, ensure_ascii=False), now),
        )
        connection.execute(
            "UPDATE page_versions SET current_snapshot_id = 'snapshot-approved' "
            "WHERE page_version_id = 'page-version-approved'"
        )
        connection.execute(
            "INSERT INTO visual_objects (visual_ref, page_id, created_at) "
            "VALUES ('visual-approved', 'page-approved', ?)",
            (now,),
        )
        connection.execute(
            """
            INSERT INTO curation_snapshot_visuals (
                snapshot_id, visual_ref, position, source_kind, disposition,
                summary, visual_type, bounds_json, confirmed, asset_sha256,
                asset_media_type, asset_size_bytes, asset_width_px, asset_height_px
            ) VALUES ('snapshot-approved', 'visual-approved', 0, 'capture', 'included',
                      '公开图表显示稳定趋势。', 'chart',
                      '{"left":0.1,"top":0.1,"width":0.5,"height":0.5}', 1,
                      ?, 'image/png', ?, 640, 360)
            """,
            (_sha(asset), len(asset)),
        )
        connection.commit()
    return {"asset_sha256": _sha(asset), "asset_bytes": asset.hex()}


def _create_candidate(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/v1/publications/candidates",
        headers={"X-Actor-ID": "publisher-1"},
    )
    assert response.status_code == 201
    return response.json()


def test_human_can_create_candidate_and_stale_business_state_cannot_be_confirmed(
    system: tuple[TestClient, Settings],
) -> None:
    client, settings = system
    _seed_publication_scope(settings)

    assert client.get("/api/v1/publications/current").status_code == 404
    candidate = _create_candidate(client)

    assert candidate["status"] == "ready"
    assert candidate["diff"] == {"added": 1, "updated": 0, "removed": 0, "unchanged": 0}
    assert candidate["excluded"] == {
        "pending_pages": 1,
        "excluded_pages": 1,
        "disabled_hidden_pages": 1,
        "soft_deleted_documents": 0,
    }
    assert candidate["documents"][0]["pages"][0]["chunk_id"] == "chunk-approved"

    with connect(settings) as connection:
        connection.execute(
            "UPDATE page_versions SET review_status = 'pending' "
            "WHERE page_version_id = 'page-version-approved'"
        )
        connection.commit()

    stale = client.post(
        f"/api/v1/publications/candidates/{candidate['candidate_id']}/confirm",
        headers={"X-Actor-ID": "publisher-1"},
    )

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "publication_candidate_stale"
    refreshed = client.get(f"/api/v1/publications/candidates/{candidate['candidate_id']}").json()
    assert refreshed["status"] == "stale"

    with connect(settings) as connection:
        connection.execute(
            "UPDATE page_versions SET review_status = 'approved' "
            "WHERE page_version_id = 'page-version-approved'"
        )
        connection.execute(
            "UPDATE ingestion_page_results SET hidden = 1, enabled = 0 "
            "WHERE version_id = 'version-approved' AND page_number = 1"
        )
        connection.commit()

    hidden = _create_candidate(client)
    assert hidden["chunk_count"] == 0
    assert hidden["documents"] == []
    assert hidden["excluded"]["disabled_hidden_pages"] == 2


def test_confirmed_candidate_builds_verified_immutable_zip_and_switches_current_atomically(
    system: tuple[TestClient, Settings],
) -> None:
    client, settings = system
    seeded = _seed_publication_scope(settings)
    candidate = _create_candidate(client)

    confirmed = client.post(
        f"/api/v1/publications/candidates/{candidate['candidate_id']}/confirm",
        headers={"X-Actor-ID": "publisher-1"},
    )
    assert confirmed.status_code == 202
    assert confirmed.json()["publication_seq"] == 1
    assert confirmed.json()["status"] == "queued"

    with connect(settings) as connection:
        connection.execute(
            "UPDATE page_versions SET review_status = 'pending', current_snapshot_id = NULL "
            "WHERE page_version_id = 'page-version-approved'"
        )
        connection.commit()

    assert run_once(settings) is True
    current = client.get("/api/v1/publications/current")
    assert current.status_code == 200
    assert current.json()["publication_seq"] == 1
    assert current.json()["chunk_count"] == 1
    assert current.json()["asset_count"] == 1
    assert current.headers["etag"]

    archive = client.get(current.json()["download_url"])
    assert archive.status_code == 200
    assert archive.headers["etag"] == current.headers["etag"]
    assert _sha(archive.content) == current.json()["sha256"]
    with zipfile.ZipFile(BytesIO(archive.content)) as bundle:
        assert set(bundle.namelist()) == {
            "manifest.json",
            "chunks.jsonl",
            f"assets/{seeded['asset_sha256']}.png",
        }
        manifest = json.loads(bundle.read("manifest.json"))
        chunk = json.loads(bundle.read("chunks.jsonl"))
        assert manifest["publication_seq"] == 1
        assert manifest["chunk_count"] == 1
        assert manifest["asset_count"] == 1
        assert chunk["chunk_id"] == "chunk-approved"
        assert chunk["text"].startswith("公开知识源\n\n公开页标题")
        annotation = next(
            part["data"] for part in chunk["parts"] if part["kind"] == "annotation"
        )
        assert annotation["visuals"][0]["visual_ref"] == "visual-approved"
        assert annotation["visuals"][0]["asset"]["byte_contract"] == (
            "standard_render_crop"
        )
        assert bundle.read(f"assets/{seeded['asset_sha256']}.png").hex() == seeded["asset_bytes"]

        files = {name: bundle.read(name) for name in bundle.namelist()}

    tampered_chunk = json.loads(files["chunks.jsonl"])
    tampered_annotation = next(
        part["data"] for part in tampered_chunk["parts"] if part["kind"] == "annotation"
    )
    tampered_annotation["visuals"][0]["asset"]["sha256"] = "0" * 64
    tampered_chunk["content_hash"] = _sha(
        json.dumps(
            {
                "text": tampered_chunk["text"],
                "parts": tampered_chunk["parts"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    files["chunks.jsonl"] = (
        json.dumps(tampered_chunk, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    tampered_manifest = json.loads(files["manifest.json"])
    tampered_manifest["chunks"]["sha256"] = _sha(files["chunks.jsonl"])
    tampered_manifest["chunks"]["size_bytes"] = len(files["chunks.jsonl"])
    files["manifest.json"] = json.dumps(
        tampered_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    tampered = BytesIO()
    with zipfile.ZipFile(tampered, "w") as bundle:
        for name, payload in files.items():
            bundle.writestr(name, payload)
    with pytest.raises(ValueError, match="视觉资产引用描述"):
        validate_publication_archive(tampered.getvalue())

    partial = client.get(current.json()["download_url"], headers={"Range": "bytes=0-31"})
    assert partial.status_code == 206
    assert partial.headers["content-range"].startswith("bytes 0-31/")
    assert partial.content == archive.content[:32]
    assert (
        client.get(
            "/api/v1/publications/current",
            headers={"If-None-Match": current.headers["etag"]},
        ).status_code
        == 304
    )


def test_no_change_does_not_create_zip_or_increment_sequence_and_active_build_is_singleton(
    system: tuple[TestClient, Settings],
) -> None:
    client, settings = system
    _seed_publication_scope(settings)
    first = _create_candidate(client)
    first_confirmation = client.post(
        f"/api/v1/publications/candidates/{first['candidate_id']}/confirm"
    )
    assert first_confirmation.status_code == 202

    concurrent = _create_candidate(client)
    busy = client.post(f"/api/v1/publications/candidates/{concurrent['candidate_id']}/confirm")
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "publication_busy"

    assert run_once(settings) is True
    unchanged = _create_candidate(client)
    no_change = client.post(f"/api/v1/publications/candidates/{unchanged['candidate_id']}/confirm")
    assert no_change.status_code == 200
    assert no_change.json() == {
        "candidate_id": unchanged["candidate_id"],
        "status": "no_change",
        "publication_seq": None,
        "job_id": None,
    }
    assert client.get("/api/v1/publications/current").json()["publication_seq"] == 1


def test_busy_conflict_exposes_active_task_and_workspace_keeps_frozen_candidate(
    system: tuple[TestClient, Settings],
) -> None:
    client, settings = system
    _seed_publication_scope(settings)
    frozen_candidate = _create_candidate(client)
    confirmation = client.post(
        f"/api/v1/publications/candidates/{frozen_candidate['candidate_id']}/confirm"
    ).json()

    newer_candidate = _create_candidate(client)
    busy = client.post(
        f"/api/v1/publications/candidates/{newer_candidate['candidate_id']}/confirm"
    )

    assert busy.status_code == 409
    error = busy.json()["error"]
    assert error["code"] == "publication_busy"
    assert error["details"] == {
        "job_id": confirmation["job_id"],
        "candidate_id": frozen_candidate["candidate_id"],
        "publication_seq": confirmation["publication_seq"],
        "status": "queued",
        "phase": "frozen_input",
        "updated_at": error["details"]["updated_at"],
    }

    workspace = client.get("/api/v1/publications").json()
    assert workspace["candidate"]["candidate_id"] == frozen_candidate["candidate_id"]
    assert workspace["task"] == {
        "job_id": confirmation["job_id"],
        "candidate_id": frozen_candidate["candidate_id"],
        "publication_seq": confirmation["publication_seq"],
        "status": "queued",
        "phase": "frozen_input",
        "progress": {
            "phase": "frozen_input",
            "completed_pages": 0,
            "total_pages": frozen_candidate["chunk_count"],
        },
        "error": None,
        "attempts": 0,
        "updated_at": error["details"]["updated_at"],
    }

    with (
        connect(settings) as connection,
        pytest.raises(sqlite3.IntegrityError, match="jobs.kind"),
    ):
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, kind, payload_json, status, actor_id, idempotency_key,
                checkpoint_json, created_at, updated_at
            ) VALUES ('job-forbidden', 'publication.build', ?, 'running',
                      'publisher-2', 'publication:forbidden', ?, ?, ?)
            """,
            (
                json.dumps({"candidate_id": "candidate-forbidden", "publication_seq": 99}),
                json.dumps({"phase": "build", "completed_pages": 0, "total_pages": 0}),
                "2026-08-29T00:10:00+00:00",
                "2026-08-29T00:10:00+00:00",
            ),
        )


def test_failed_build_keeps_current_and_retry_reuses_original_frozen_input(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    _seed_publication_scope(settings)
    first = _create_candidate(client)
    assert (
        client.post(f"/api/v1/publications/candidates/{first['candidate_id']}/confirm").status_code
        == 202
    )
    assert run_once(settings) is True
    original = client.get("/api/v1/publications/current").json()

    with connect(settings) as connection:
        connection.execute(
            "UPDATE page_versions SET review_status = 'approved', "
            "current_snapshot_id = 'snapshot-approved', "
            "reviewed_by = 'curator-1', "
            "reviewed_at = '2026-08-29T00:00:00+00:00', "
            "review_source_version_id = 'version-approved' "
            "WHERE page_version_id = 'page-version-pending'"
        )
        connection.commit()
    candidate = _create_candidate(client)
    confirmation = client.post(
        f"/api/v1/publications/candidates/{candidate['candidate_id']}/confirm"
    )
    assert confirmation.status_code == 202

    def reject_archive(_payload: bytes) -> None:
        raise ValueError("注入的完整性校验失败")

    monkeypatch.setattr("pptextract.publication.validate_publication_archive", reject_archive)
    assert run_once(settings) is True
    failed_job = client.get(f"/api/v1/jobs/{confirmation.json()['job_id']}").json()
    assert failed_job["status"] == "failed"
    assert failed_job["error"]["phase"] == "validate"
    assert client.get("/api/v1/publications/current").json() == original

    monkeypatch.undo()
    retried = client.post(
        f"/api/v1/publications/tasks/{confirmation.json()['job_id']}/retry",
        headers={"X-Actor-ID": "publisher-2"},
    )
    assert retried.status_code == 202
    assert retried.json()["publication_seq"] == confirmation.json()["publication_seq"]
    assert retried.json()["frozen_input_hash"] == confirmation.json()["frozen_input_hash"]
    assert run_once(settings) is True
    assert client.get("/api/v1/publications/current").json()["publication_seq"] == 2

    duplicate_retry = client.post(
        f"/api/v1/publications/tasks/{confirmation.json()['job_id']}/retry",
        headers={"X-Actor-ID": "publisher-3"},
    )
    assert duplicate_retry.status_code == 409
    assert duplicate_retry.json()["error"]["code"] == "publication_already_succeeded"
    assert client.get(f"/api/v1/publications/candidates/{candidate['candidate_id']}").json()[
        "status"
    ] == "succeeded"


def test_freeze_failure_rolls_back_reserved_sequence_and_frozen_input(
    system: tuple[TestClient, Settings],
) -> None:
    client, settings = system
    _seed_publication_scope(settings)
    candidate = _create_candidate(client)
    with connect(settings) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_publication_freeze
            BEFORE INSERT ON jobs
            WHEN NEW.kind = 'publication.build'
            BEGIN
                SELECT RAISE(ABORT, '注入的冻结失败');
            END
            """
        )
        connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="注入的冻结失败"):
        confirm_candidate(
            settings,
            candidate_id=str(candidate["candidate_id"]),
            actor_id="publisher-freeze-fault",
        )

    with connect(settings) as connection:
        assert connection.execute(
            "SELECT next_value FROM publication_sequences WHERE singleton_id = 1"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM publication_frozen_chunks WHERE candidate_id = ?",
            (candidate["candidate_id"],),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM publication_candidates WHERE candidate_id = ?",
            (candidate["candidate_id"],),
        ).fetchone()[0] == "ready"
    assert client.get("/api/v1/publications/current").status_code == 404


@pytest.mark.parametrize(
    ("fault_phase", "expected_phase"),
    (("build", "build"), ("store", "store"), ("switch_pointer", "switch_pointer")),
)
def test_publication_fault_before_pointer_commit_keeps_current_artifact(
    system: tuple[TestClient, Settings],
    monkeypatch: pytest.MonkeyPatch,
    fault_phase: str,
    expected_phase: str,
) -> None:
    client, settings = system
    _seed_publication_scope(settings)
    first = _create_candidate(client)
    client.post(f"/api/v1/publications/candidates/{first['candidate_id']}/confirm")
    assert run_once(settings) is True
    original = client.get("/api/v1/publications/current").json()

    with connect(settings) as connection:
        connection.execute(
            "UPDATE page_versions SET review_status = 'approved', "
            "current_snapshot_id = 'snapshot-approved', "
            "reviewed_by = 'curator-1', "
            "reviewed_at = '2026-08-29T00:00:00+00:00', "
            "review_source_version_id = 'version-approved' "
            "WHERE page_version_id = 'page-version-pending'"
        )
        connection.commit()
    candidate = _create_candidate(client)
    confirmation = client.post(
        f"/api/v1/publications/candidates/{candidate['candidate_id']}/confirm"
    ).json()

    def reject_phase(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"注入的 {fault_phase} 失败")

    if fault_phase == "build":
        monkeypatch.setattr("pptextract.publication._archive_bytes", reject_phase)
    elif fault_phase == "store":
        monkeypatch.setattr("pptextract.publication.LocalObjectStore.put", reject_phase)
    else:
        with connect(settings) as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_publication_pointer
                BEFORE UPDATE ON current_publication
                BEGIN
                    SELECT RAISE(ABORT, '注入的指针事务失败');
                END
                """
            )
            connection.commit()

    assert run_once(settings) is True

    failed = client.get(f"/api/v1/jobs/{confirmation['job_id']}").json()
    assert failed["status"] == "failed"
    assert failed["error"]["phase"] == expected_phase
    assert client.get("/api/v1/publications/current").json() == original
    artifact = client.get(
        f"/api/v1/publications/{confirmation['publication_seq']}/artifact"
    )
    assert artifact.status_code == 404


def test_failed_sequence_cannot_be_retried_after_a_newer_publication_succeeds(
    system: tuple[TestClient, Settings],
) -> None:
    client, settings = system
    _seed_publication_scope(settings)
    first = _create_candidate(client)
    assert (
        client.post(f"/api/v1/publications/candidates/{first['candidate_id']}/confirm").status_code
        == 202
    )
    assert run_once(settings) is True

    with connect(settings) as connection:
        connection.execute(
            "UPDATE page_versions SET review_status = 'approved', "
            "current_snapshot_id = 'snapshot-approved', "
            "reviewed_by = 'curator-1', "
            "reviewed_at = '2026-08-29T00:00:00+00:00', "
            "review_source_version_id = 'version-approved' "
            "WHERE page_version_id = 'page-version-pending'"
        )
        connection.commit()

    failed_candidate = _create_candidate(client)
    failed_confirmation = client.post(
        f"/api/v1/publications/candidates/{failed_candidate['candidate_id']}/confirm"
    ).json()
    failed_claim = claim_next_job(settings)
    assert failed_claim is not None
    fail_publication_job(settings, failed_claim, RuntimeError("保留序号空洞"))

    newer_candidate = _create_candidate(client)
    newer_confirmation = client.post(
        f"/api/v1/publications/candidates/{newer_candidate['candidate_id']}/confirm"
    ).json()
    assert newer_confirmation["publication_seq"] == 3
    assert run_once(settings) is True
    assert client.get("/api/v1/publications/current").json()["publication_seq"] == 3

    superseded = client.post(
        f"/api/v1/publications/tasks/{failed_confirmation['job_id']}/retry"
    )

    assert superseded.status_code == 409
    assert superseded.json()["error"] == {
        "code": "publication_sequence_superseded",
        "message": "该失败序号已被更高的当前产物越过，请按最新业务状态创建新候选。",
        "details": {"failed_publication_seq": 2, "current_publication_seq": 3},
    }
    assert client.get("/api/v1/publications/current").json()["publication_seq"] == 3
    assert client.get(
        f"/api/v1/publications/candidates/{failed_candidate['candidate_id']}"
    ).json()["status"] == "failed"


def test_footer_noise_change_invalidates_candidate(
    system: tuple[TestClient, Settings],
) -> None:
    client, settings = system
    _seed_publication_scope(settings)
    candidate = _create_candidate(client)

    with connect(settings) as connection:
        connection.execute(
            """
            INSERT INTO repeated_footer_noise_confirmations (
                confirmation_id, document_id, version_id, source_text,
                normalized_text, rule_version, actor_id, confirmed_at
            ) VALUES ('noise-1', 'doc-approved', 'version-approved', '公开页正文。',
                      '公开页正文。', 'rule-v1', 'curator-2', '2026-08-29T00:01:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO repeated_footer_noise_sources (
                confirmation_id, page_id, page_version_id, page_number,
                source_ref, source_kind, source_index, source_text
            ) VALUES ('noise-1', 'page-approved', 'page-version-approved', 1,
                      'body:0', 'body', 0, '公开页正文。')
            """
        )
        connection.execute(
            """
            INSERT INTO repeated_footer_noise_events (
                event_id, confirmation_id, event_type, actor_id, occurred_at
            ) VALUES ('noise-event-1', 'noise-1', 'confirmed', 'curator-2',
                      '2026-08-29T00:01:00+00:00')
            """
        )
        connection.commit()

    response = client.post(
        f"/api/v1/publications/candidates/{candidate['candidate_id']}/confirm"
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "publication_candidate_stale"


def test_soft_delete_restore_and_content_update_are_reflected_in_candidate_diff(
    system: tuple[TestClient, Settings],
) -> None:
    client, settings = system
    _seed_publication_scope(settings)
    first = _create_candidate(client)
    client.post(f"/api/v1/publications/candidates/{first['candidate_id']}/confirm")
    assert run_once(settings) is True

    with connect(settings) as connection:
        connection.execute(
            "UPDATE documents SET deleted_at = '2026-08-29T01:00:00+00:00' "
            "WHERE document_id = 'doc-approved'"
        )
        connection.commit()
    removed = _create_candidate(client)
    assert removed["diff"] == {"added": 0, "updated": 0, "removed": 1, "unchanged": 0}
    assert removed["excluded"]["soft_deleted_documents"] == 1
    assert removed["chunk_count"] == 0
    client.post(f"/api/v1/publications/candidates/{removed['candidate_id']}/confirm")
    assert run_once(settings) is True

    with connect(settings) as connection:
        connection.execute(
            "UPDATE documents SET deleted_at = NULL WHERE document_id = 'doc-approved'"
        )
        connection.commit()
    restored = _create_candidate(client)
    assert restored["diff"] == {"added": 1, "updated": 0, "removed": 0, "unchanged": 0}
    client.post(f"/api/v1/publications/candidates/{restored['candidate_id']}/confirm")
    assert run_once(settings) is True

    with connect(settings) as connection:
        connection.execute(
            "UPDATE curation_snapshots SET overview = '公开页总述已更新。' "
            "WHERE snapshot_id = 'snapshot-approved'"
        )
        connection.commit()
    updated = _create_candidate(client)
    assert updated["diff"] == {"added": 0, "updated": 1, "removed": 0, "unchanged": 0}


def test_publication_checkpoint_renews_lease_and_stale_failure_cannot_revert_candidate(
    system: tuple[TestClient, Settings],
) -> None:
    client, settings = system
    _seed_publication_scope(settings)
    candidate = _create_candidate(client)
    confirmation = client.post(
        f"/api/v1/publications/candidates/{candidate['candidate_id']}/confirm"
    ).json()
    claim = claim_next_job(settings)
    assert claim is not None

    with connect(settings) as connection:
        before = str(
            connection.execute(
                "SELECT lease_expires_at FROM jobs WHERE job_id = ?", (claim.job_id,)
            ).fetchone()[0]
        )
    _checkpoint(settings, claim, "build", 1)
    with connect(settings) as connection:
        after = str(
            connection.execute(
                "SELECT lease_expires_at FROM jobs WHERE job_id = ?", (claim.job_id,)
            ).fetchone()[0]
        )
    assert after > before

    with connect(settings) as connection:
        connection.execute(
            "UPDATE jobs SET status = 'succeeded', lease_owner = NULL, lease_token = NULL, "
            "lease_expires_at = NULL WHERE job_id = ?",
            (claim.job_id,),
        )
        connection.execute(
            "UPDATE publication_candidates SET status = 'succeeded' WHERE candidate_id = ?",
            (candidate["candidate_id"],),
        )
        connection.commit()

    fail_publication_job(settings, claim, RuntimeError("过期 worker 的迟到错误"))
    with connect(settings) as connection:
        status = connection.execute(
            "SELECT status FROM publication_candidates WHERE candidate_id = ?",
            (candidate["candidate_id"],),
        ).fetchone()[0]
        task_status = connection.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (confirmation["job_id"],)
        ).fetchone()[0]
    assert status == "succeeded"
    assert task_status == "succeeded"
