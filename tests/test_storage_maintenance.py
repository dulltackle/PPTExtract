from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pptextract.api import create_app
from pptextract.config import Settings
from pptextract.db import (
    RecoveryGateClosedError,
    connect,
    initialize_database,
    transaction,
)
from pptextract.jobs import ClaimedJob, finish_job
from pptextract.object_store import LocalObjectStore
from pptextract.publication import purge_expired_publication_artifacts
from pptextract.storage_maintenance import (
    audit_references,
    collect_unreachable_objects,
    create_coordinated_backup,
    restore_backup,
    run_recovery_drill,
)
from pptextract.worker import run_once


def _record_object(settings: Settings, payload: bytes, media_type: str = "application/test") -> str:
    stored = LocalObjectStore(settings.object_store_path).put(payload)
    with transaction(settings) as connection:
        connection.execute(
            """
            INSERT INTO stored_objects (sha256, size_bytes, media_type, verified_at)
            VALUES (?, ?, ?, ?)
            """,
            (stored.sha256, stored.size_bytes, media_type, "2026-01-01T00:00:00+00:00"),
        )
    return stored.sha256


def test_gc_requires_two_unreachable_scans_separated_by_the_safe_grace_period(
    tmp_path: Path,
) -> None:
    settings = Settings.for_test(tmp_path)
    initialize_database(settings)
    source_sha256 = _record_object(settings, b"permanent source", "application/pptx")
    unused_sha256 = _record_object(settings, b"rebuildable orphan")
    physical_orphan = LocalObjectStore(settings.object_store_path).put(b"transaction orphan")
    with transaction(settings) as connection:
        connection.execute(
            "INSERT INTO documents (document_id, created_at) VALUES ('document', ?)",
            ("2026-01-01T00:00:00+00:00",),
        )
        connection.execute(
            """
            INSERT INTO document_versions (
                version_id, document_id, source_sha256, source_filename,
                source_size_bytes, status, created_at
            ) VALUES ('version', 'document', ?, 'source.pptx', 16, 'ready', ?)
            """,
            (source_sha256, "2026-01-01T00:00:00+00:00"),
        )

    first_scan_at = datetime(2026, 1, 2, tzinfo=UTC)
    first = collect_unreachable_objects(settings, at=first_scan_at)

    assert first.marked == tuple(sorted((physical_orphan.sha256, unused_sha256)))
    assert first.deleted == ()
    assert LocalObjectStore(settings.object_store_path).path_for(unused_sha256).is_file()

    too_early = collect_unreachable_objects(
        settings,
        at=first_scan_at + timedelta(seconds=settings.object_gc_grace_seconds),
    )
    assert too_early.deleted == ()

    second = collect_unreachable_objects(
        settings,
        at=first_scan_at + timedelta(seconds=settings.object_gc_grace_seconds + 1),
    )

    assert second.deleted == tuple(sorted((physical_orphan.sha256, unused_sha256)))
    store = LocalObjectStore(settings.object_store_path)
    assert store.path_for(source_sha256).is_file()
    assert not store.path_for(unused_sha256).exists()
    assert not store.path_for(physical_orphan.sha256).exists()
    with connect(settings) as connection:
        assert connection.execute(
            "SELECT 1 FROM stored_objects WHERE sha256 = ?", (unused_sha256,)
        ).fetchone() is not None
        assert connection.execute(
            "SELECT COUNT(*) FROM object_gc_candidates WHERE deleted_at IS NOT NULL"
        ).fetchone()[0] == 2


def test_gc_cancels_a_candidate_that_becomes_reachable_again(tmp_path: Path) -> None:
    settings = Settings.for_test(tmp_path)
    initialize_database(settings)
    payload = b"late business reference"
    sha256 = _record_object(settings, payload)
    first_scan_at = datetime(2026, 1, 2, tzinfo=UTC)
    assert collect_unreachable_objects(settings, at=first_scan_at).marked == (sha256,)

    with transaction(settings) as connection:
        connection.execute(
            "INSERT INTO documents (document_id, created_at) VALUES ('document', ?)",
            ("2026-01-01T00:00:00+00:00",),
        )
        connection.execute(
            """
            INSERT INTO document_versions (
                version_id, document_id, source_sha256, source_filename,
                source_size_bytes, status, created_at
            ) VALUES ('version', 'document', ?, 'source.pptx', ?, 'ready', ?)
            """,
            (sha256, len(payload), "2026-01-01T00:00:00+00:00"),
        )

    result = collect_unreachable_objects(
        settings,
        at=first_scan_at + timedelta(seconds=settings.object_gc_grace_seconds + 1),
    )

    assert result.deleted == ()
    assert result.unmarked == (sha256,)
    assert LocalObjectStore(settings.object_store_path).verify(sha256)
    with connect(settings) as connection:
        assert connection.execute("SELECT COUNT(*) FROM object_gc_candidates").fetchone()[0] == 0


def test_gc_resets_an_old_candidate_when_the_same_object_is_published_again(
    tmp_path: Path,
) -> None:
    settings = Settings.for_test(tmp_path)
    initialize_database(settings)
    payload = b"republished before reference commit"
    sha256 = _record_object(settings, payload)
    first_scan_at = datetime(2026, 1, 2, tzinfo=UTC)
    assert collect_unreachable_objects(settings, at=first_scan_at).marked == (sha256,)

    LocalObjectStore(settings.object_store_path).put(payload)
    after_grace = first_scan_at + timedelta(seconds=settings.object_gc_grace_seconds + 1)
    result = collect_unreachable_objects(settings, at=after_grace)

    assert result.deleted == ()
    assert LocalObjectStore(settings.object_store_path).verify(sha256)
    with connect(settings) as connection:
        candidate = connection.execute(
            "SELECT first_unreachable_at FROM object_gc_candidates WHERE sha256 = ?",
            (sha256,),
        ).fetchone()
    assert candidate["first_unreachable_at"] == after_grace.isoformat()


def test_gc_reconsiders_a_deleted_tombstone_when_the_object_is_republished(
    tmp_path: Path,
) -> None:
    settings = Settings.for_test(tmp_path)
    initialize_database(settings)
    payload = b"republished after prior collection"
    sha256 = _record_object(settings, payload)
    first_scan_at = datetime(2026, 1, 2, tzinfo=UTC)
    collect_unreachable_objects(settings, at=first_scan_at)
    collect_unreachable_objects(
        settings,
        at=first_scan_at + timedelta(seconds=settings.object_gc_grace_seconds + 1),
    )
    assert not LocalObjectStore(settings.object_store_path).path_for(sha256).exists()

    LocalObjectStore(settings.object_store_path).put(payload)
    result = collect_unreachable_objects(
        settings,
        at=first_scan_at + timedelta(seconds=settings.object_gc_grace_seconds * 2 + 2),
    )

    assert result.deleted == ()
    assert LocalObjectStore(settings.object_store_path).verify(sha256)
    with connect(settings) as connection:
        candidate = connection.execute(
            "SELECT deleted_at FROM object_gc_candidates WHERE sha256 = ?", (sha256,)
        ).fetchone()
    assert candidate["deleted_at"] is None


def test_gc_preserves_all_durable_reference_categories(tmp_path: Path) -> None:
    settings = Settings.for_test(tmp_path)
    initialize_database(settings)
    source = _record_object(settings, b"source", "application/pptx")
    render = _record_object(settings, b"render", "image/png")
    source_image = _record_object(settings, b"source-image", "image/png")
    formal_asset = _record_object(settings, b"formal-asset", "image/png")
    frozen_asset = _record_object(settings, b"frozen-asset", "image/png")
    current_artifact = _record_object(settings, b"current-artifact", "application/zip")
    retained_artifact = _record_object(settings, b"retained-artifact", "application/zip")
    now = "2026-01-01T00:00:00+00:00"
    with transaction(settings) as connection:
        connection.execute(
            "INSERT INTO documents (document_id, current_version_id, created_at) "
            "VALUES ('document', 'version', ?)",
            (now,),
        )
        connection.execute(
            """
            INSERT INTO document_versions (
                version_id, document_id, source_sha256, source_filename,
                source_size_bytes, status, created_at, ready_at
            ) VALUES ('version', 'document', ?, 'source.pptx', 6, 'ready', ?, ?)
            """,
            (source, now, now),
        )
        connection.execute(
            """
            INSERT INTO ingestion_page_results (
                version_id, page_number, source_slide_id, relationship_id,
                source_part, hidden, enabled, render_sha256
            ) VALUES ('version', 1, 256, 'rId1', 'ppt/slides/slide1.xml', 0, 1, ?)
            """,
            (render,),
        )
        connection.execute(
            "INSERT INTO pages (page_id, document_id, chunk_id, created_at) "
            "VALUES ('page', 'document', 'chunk', ?)",
            (now,),
        )
        connection.execute(
            """
            INSERT INTO page_versions (
                page_version_id, page_id, document_id, version_id, page_number,
                fingerprint_version, fingerprint_sha256, source_content_json,
                render_sha256, render_media_type, render_dpi, render_width_px,
                render_height_px, review_status, created_at
            ) VALUES (
                'page-version', 'page', 'document', 'version', 1,
                1, ?, '{}', ?, 'image/png', 144, 10, 10, 'approved', ?
            )
            """,
            ("f" * 64, render, now),
        )
        connection.execute(
            """
            INSERT INTO page_version_image_sources (
                page_version_id, reference_index, position, object_sha256,
                size_bytes, media_type, origin_part, alt_text
            ) VALUES ('page-version', 0, 0, ?, 12, 'image/png', 'ppt/media/a.png', '')
            """,
            (source_image,),
        )
        connection.execute(
            """
            INSERT INTO curation_snapshots (
                snapshot_id, page_version_id, snapshot_kind, created_at
            ) VALUES ('snapshot', 'page-version', 'formal', ?)
            """,
            (now,),
        )
        connection.execute(
            "INSERT INTO visual_objects (visual_ref, page_id, created_at) "
            "VALUES ('visual', 'page', ?)",
            (now,),
        )
        connection.execute(
            """
            INSERT INTO curation_snapshot_visuals (
                snapshot_id, visual_ref, position, source_kind, disposition,
                confirmed, asset_sha256, asset_media_type, asset_size_bytes
            ) VALUES ('snapshot', 'visual', 0, 'capture', 'included', 1, ?, 'image/png', 12)
            """,
            (formal_asset,),
        )
        connection.execute(
            """
            INSERT INTO publication_candidates (
                candidate_id, business_state_token, content_set_hash, scope_json,
                status, created_by, created_at, publication_seq
            ) VALUES ('frozen', 'token', 'hash', '{}', 'confirmed', 'operator', ?, 1)
            """,
            (now,),
        )
        connection.execute(
            """
            INSERT INTO publication_frozen_assets (
                candidate_id, asset_sha256, path, media_type, size_bytes, byte_contract
            ) VALUES ('frozen', ?, 'assets/frozen.png', 'image/png', 12, 'anydoc_original')
            """,
            (frozen_asset,),
        )
        for candidate_id, sequence, sha256, replaced_at in (
            ("current", 2, current_artifact, None),
            ("retained", 3, retained_artifact, "2026-01-01T00:00:00+00:00"),
        ):
            connection.execute(
                """
                INSERT INTO publication_candidates (
                    candidate_id, business_state_token, content_set_hash, scope_json,
                    status, created_by, created_at, publication_seq
                ) VALUES (?, ?, ?, '{}', 'succeeded', 'operator', ?, ?)
                """,
                (candidate_id, candidate_id, candidate_id, now, sequence),
            )
            connection.execute(
                """
                INSERT INTO publication_artifacts (
                    publication_seq, candidate_id, snapshot_id, content_set_hash,
                    artifact_sha256, media_type, size_bytes, chunk_count,
                    asset_count, published_at, replaced_at
                ) VALUES (?, ?, ?, ?, ?, 'application/zip', ?, 1, 0, ?, ?)
                """,
                (
                    sequence,
                    candidate_id,
                    candidate_id,
                    candidate_id,
                    sha256,
                    LocalObjectStore(settings.object_store_path).path_for(sha256).stat().st_size,
                    now,
                    replaced_at,
                ),
            )
        connection.execute(
            "INSERT INTO current_publication (singleton_id, publication_seq) VALUES (1, 2)"
        )

    result = collect_unreachable_objects(
        settings, at=datetime(2026, 1, 2, tzinfo=UTC)
    )

    assert result.marked == ()
    assert result.deleted == ()


def test_expired_artifact_is_only_deleted_after_gc_grace_and_rescan(tmp_path: Path) -> None:
    settings = Settings.for_test(tmp_path)
    initialize_database(settings)
    artifact = _record_object(settings, b"expired-artifact", "application/zip")
    published_at = datetime(2025, 1, 1, tzinfo=UTC)
    with transaction(settings) as connection:
        connection.execute(
            """
            INSERT INTO publication_candidates (
                candidate_id, business_state_token, content_set_hash, scope_json,
                status, created_by, created_at, publication_seq
            ) VALUES ('candidate', 'token', 'hash', '{}', 'succeeded', 'operator', ?, 1)
            """,
            (published_at.isoformat(),),
        )
        connection.execute(
            """
            INSERT INTO publication_artifacts (
                publication_seq, candidate_id, snapshot_id, content_set_hash,
                artifact_sha256, media_type, size_bytes, chunk_count, asset_count,
                published_at, replaced_at
            ) VALUES (1, 'candidate', 'snapshot', 'hash', ?, 'application/zip',
                      16, 1, 0, ?, ?)
            """,
            (artifact, published_at.isoformat(), published_at.isoformat()),
        )

    purge_at = published_at + timedelta(days=settings.internal_artifact_retention_days + 1)
    assert purge_expired_publication_artifacts(settings, at=purge_at) == [1]
    assert LocalObjectStore(settings.object_store_path).path_for(artifact).is_file()
    assert collect_unreachable_objects(settings, at=purge_at).marked == (artifact,)

    deleted = collect_unreachable_objects(
        settings,
        at=purge_at + timedelta(seconds=settings.object_gc_grace_seconds + 1),
    )
    assert deleted.deleted == (artifact,)


def test_coordinated_backup_restores_a_consistent_database_and_object_set(
    tmp_path: Path,
) -> None:
    source_settings = Settings.for_test(tmp_path / "source")
    initialize_database(source_settings)
    source_payload = b"coordinated source"
    source_sha256 = _record_object(source_settings, source_payload, "application/pptx")
    with transaction(source_settings) as connection:
        connection.execute(
            "INSERT INTO documents (document_id, current_version_id, created_at) "
            "VALUES ('document', 'version', ?)",
            ("2026-01-01T00:00:00+00:00",),
        )
        connection.execute(
            """
            INSERT INTO document_versions (
                version_id, document_id, source_sha256, source_filename,
                source_size_bytes, status, created_at, ready_at
            ) VALUES ('version', 'document', ?, 'source.pptx', ?, 'ready', ?, ?)
            """,
            (
                source_sha256,
                len(source_payload),
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    backup_path = tmp_path / "backups" / "backup-20260102"
    backup = create_coordinated_backup(source_settings, backup_path)

    assert backup.path == backup_path
    assert backup.object_count == 1
    assert backup.audit.ok is True
    assert (backup_path / "pptextract.sqlite3").is_file()
    assert (backup_path / "backup.json").is_file()
    assert (
        backup_path / "objects" / source_sha256[:2] / source_sha256
    ).read_bytes() == source_payload

    target_settings = Settings.for_test(tmp_path / "restored")
    restored = restore_backup(backup_path, target_settings)

    assert restored.ok is True
    assert LocalObjectStore(target_settings.object_store_path).verify(source_sha256)
    with connect(target_settings) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute(
            "SELECT status FROM storage_recovery_state WHERE singleton_id = 1"
        ).fetchone()[0] == "ready"
        assert connection.execute(
            "SELECT source_filename FROM document_versions WHERE version_id = 'version'"
        ).fetchone()[0] == "source.pptx"


def test_failed_reference_audit_blocks_writes_and_worker_reopening(tmp_path: Path) -> None:
    settings = Settings.for_test(tmp_path)
    initialize_database(settings)
    payload = b"missing source"
    sha256 = _record_object(settings, payload, "application/pptx")
    with transaction(settings) as connection:
        connection.execute(
            "INSERT INTO documents (document_id, created_at) VALUES ('document', ?)",
            ("2026-01-01T00:00:00+00:00",),
        )
        connection.execute(
            """
            INSERT INTO document_versions (
                version_id, document_id, source_sha256, source_filename,
                source_size_bytes, status, created_at
            ) VALUES ('version', 'document', ?, 'source.pptx', ?, 'ready', ?)
            """,
            (sha256, len(payload), "2026-01-01T00:00:00+00:00"),
        )
    LocalObjectStore(settings.object_store_path).path_for(sha256).unlink()

    audit = audit_references(settings)

    assert audit.ok is False
    assert [(failure.category, failure.reason) for failure in audit.failures] == [
        ("original_source", "missing")
    ]
    with pytest.raises(RecoveryGateClosedError), transaction(settings):
        pass
    assert run_once(settings) is False
    with connect(settings) as connection:
        assert connection.execute("SELECT COUNT(*) FROM worker_heartbeats").fetchone()[0] == 0
    with TestClient(create_app(settings)) as client:
        health = client.get("/api/v1/health")
        blocked = client.post("/api/v1/publications/preflight")
    assert health.status_code == 503
    assert health.json()["components"]["recovery"] == {
        "status": "blocked",
        "reason": "reference_audit_failed",
    }
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "recovery_audit_failed"


def test_reference_audit_rejects_foreign_key_corruption(tmp_path: Path) -> None:
    settings = Settings.for_test(tmp_path)
    initialize_database(settings)
    payload = b"source with deleted metadata"
    sha256 = _record_object(settings, payload, "application/pptx")
    with transaction(settings) as connection:
        connection.execute(
            "INSERT INTO documents (document_id, created_at) VALUES ('document', ?)",
            ("2026-01-01T00:00:00+00:00",),
        )
        connection.execute(
            """
            INSERT INTO document_versions (
                version_id, document_id, source_sha256, source_filename,
                source_size_bytes, status, created_at
            ) VALUES ('version', 'document', ?, 'source.pptx', ?, 'ready', ?)
            """,
            (sha256, len(payload), "2026-01-01T00:00:00+00:00"),
        )
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM stored_objects WHERE sha256 = ?", (sha256,))

    report = audit_references(settings)

    assert report.ok is False
    assert report.failures[0].reason == "foreign_key_check_failed"


def test_restore_keeps_gate_blocked_when_a_backup_object_fails_hash_audit(
    tmp_path: Path,
) -> None:
    source_settings = Settings.for_test(tmp_path / "source")
    initialize_database(source_settings)
    payload = b"audit me after restore"
    sha256 = _record_object(source_settings, payload, "application/pptx")
    with transaction(source_settings) as connection:
        connection.execute(
            "INSERT INTO documents (document_id, created_at) VALUES ('document', ?)",
            ("2026-01-01T00:00:00+00:00",),
        )
        connection.execute(
            """
            INSERT INTO document_versions (
                version_id, document_id, source_sha256, source_filename,
                source_size_bytes, status, created_at
            ) VALUES ('version', 'document', ?, 'source.pptx', ?, 'ready', ?)
            """,
            (sha256, len(payload), "2026-01-01T00:00:00+00:00"),
        )
    backup_path = tmp_path / "backup"
    create_coordinated_backup(source_settings, backup_path)
    (backup_path / "objects" / sha256[:2] / sha256).write_bytes(b"x" * len(payload))

    target = Settings.for_test(tmp_path / "restored")
    report = restore_backup(backup_path, target)

    assert report.ok is False
    assert report.failures[0].reason == "sha256_mismatch"
    with connect(target) as connection:
        assert dict(
            connection.execute(
                "SELECT status, reason FROM storage_recovery_state WHERE singleton_id = 1"
            ).fetchone()
        ) == {"status": "blocked", "reason": "reference_audit_failed"}


def test_restore_publishes_the_data_root_atomically_and_can_retry_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Settings.for_test(tmp_path / "source")
    initialize_database(source)
    backup_path = tmp_path / "backup"
    create_coordinated_backup(source, backup_path)
    target = Settings.for_test(tmp_path / "restored")
    import pptextract.storage_maintenance as maintenance

    original_replace = maintenance.os.replace

    def fail_root_publish(source_path: Path, destination_path: Path) -> None:
        if Path(destination_path) == target.database_path.parent:
            raise OSError("simulated root rename failure")
        original_replace(source_path, destination_path)

    monkeypatch.setattr(maintenance.os, "replace", fail_root_publish)
    with pytest.raises(OSError, match="simulated root rename failure"):
        restore_backup(backup_path, target)
    assert not target.database_path.parent.exists()

    monkeypatch.setattr(maintenance.os, "replace", original_replace)
    assert restore_backup(backup_path, target).ok is True
    assert target.database_path.is_file()
    assert target.object_store_path.is_dir()


def test_recovery_drill_records_restore_audit_ingestion_resume_and_artifact_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings.for_test(tmp_path / "source")
    initialize_database(settings)
    source_payload = b"drill source"
    source_sha256 = _record_object(settings, source_payload, "application/pptx")
    artifact_payload = b"drill current artifact"
    artifact_sha256 = _record_object(settings, artifact_payload, "application/zip")
    now = "2026-01-01T00:00:00+00:00"
    with transaction(settings) as connection:
        connection.execute(
            "INSERT INTO documents (document_id, created_at) VALUES ('document', ?)",
            (now,),
        )
        connection.execute(
            """
            INSERT INTO document_versions (
                version_id, document_id, source_sha256, source_filename,
                source_size_bytes, status, created_at, ready_at
            ) VALUES ('version', 'document', ?, 'source.pptx', ?, 'processing', ?, NULL)
            """,
            (source_sha256, len(source_payload), now),
        )
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, kind, payload_json, status, actor_id, idempotency_key,
                document_id, version_id, attempts, max_attempts, checkpoint_json,
                created_at, updated_at
            ) VALUES (
                'ingest-job', 'document.ingest', ?, 'queued', 'operator', 'ingest',
                'document', 'version', 1, 3, ?, ?, ?
            )
            """,
            (
                json.dumps(
                    {
                        "document_id": "document",
                        "job_id": "ingest-job",
                        "source_sha256": source_sha256,
                        "version_id": "version",
                    }
                ),
                json.dumps({"phase": "render", "completed_pages": 1, "total_pages": 2}),
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO publication_candidates (
                candidate_id, business_state_token, content_set_hash, scope_json,
                status, created_by, created_at, publication_seq
            ) VALUES ('candidate', 'token', 'hash', '{}', 'succeeded', 'operator', ?, 1)
            """,
            (now,),
        )
        connection.execute(
            """
            INSERT INTO publication_artifacts (
                publication_seq, candidate_id, snapshot_id, content_set_hash,
                artifact_sha256, media_type, size_bytes, chunk_count, asset_count,
                published_at
            ) VALUES (
                1, 'candidate', 'snapshot', 'hash', ?, 'application/zip', ?, 1, 0, ?
            )
            """,
            (artifact_sha256, len(artifact_payload), now),
        )
        connection.execute(
            "INSERT INTO current_publication (singleton_id, publication_seq) VALUES (1, 1)"
        )
    backup_path = tmp_path / "backup"
    create_coordinated_backup(settings, backup_path)

    def resume_from_checkpoint(restored: Settings, job: ClaimedJob) -> None:
        with connect(restored) as connection:
            checkpoint = json.loads(
                str(
                    connection.execute(
                        "SELECT checkpoint_json FROM jobs WHERE job_id = ?",
                        (job.job_id,),
                    ).fetchone()[0]
                )
            )
        assert checkpoint == {"phase": "render", "completed_pages": 1, "total_pages": 2}
        finish_job(restored, job, succeeded=True)

    monkeypatch.setattr("pptextract.worker.process_ingestion_job", resume_from_checkpoint)

    result = run_recovery_drill(settings, backup_path, tmp_path / "drill-workspace")

    assert result.status == "passed"
    assert result.steps == {
        "sqlite_and_objects_restore": "passed",
        "reference_audit": "passed",
        "ingestion_resume": "passed",
        "current_artifact_download": "passed",
    }
    assert result.quantitative_objectives_verified is False
    with connect(settings) as connection:
        recorded = connection.execute(
            "SELECT status, result_json FROM recovery_drills WHERE drill_id = ?",
            (result.drill_id,),
        ).fetchone()
    assert recorded["status"] == "passed"
    assert json.loads(recorded["result_json"])["quantitative_objectives_verified"] is False
