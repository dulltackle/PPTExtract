from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pptextract.config import Settings
from pptextract.db import SCHEMA_VERSION, connect, initialize_database


def test_v16_review_event_rebuild_rolls_back_as_one_unit_on_copy_failure(
    tmp_path: Path,
) -> None:
    settings = Settings.for_test(tmp_path)
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript(
            """
            PRAGMA user_version = 16;
            CREATE TABLE page_review_events (
                event_id TEXT PRIMARY KEY,
                page_version_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor_id TEXT,
                occurred_at TEXT NOT NULL,
                source_version_id TEXT,
                source_page_version_id TEXT,
                snapshot_id TEXT,
                reason TEXT,
                note TEXT
            );
            INSERT INTO page_review_events (
                event_id, page_version_id, event_type, occurred_at
            ) VALUES ('event-invalid', 'page-version', 'invalid', 'now');
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        initialize_database(settings)

    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute(
            "SELECT event_type FROM page_review_events WHERE event_id = 'event-invalid'"
        ).fetchone()[0] == "invalid"
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'page_review_events_v17'"
        ).fetchone() is None


def test_v2_database_migrates_version_states_and_active_job_targets(tmp_path: Path) -> None:
    settings = Settings.for_test(tmp_path)
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "document_id": "document",
            "job_id": "job",
            "source_sha256": "a" * 64,
            "version_id": "version",
        }
    )
    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            PRAGMA user_version = 2;

            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'running', 'completed', 'failed')
                ),
                actor_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                lease_owner TEXT,
                lease_expires_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                checkpoint_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error_json TEXT,
                UNIQUE (actor_id, idempotency_key)
            );
            CREATE TABLE stored_objects (
                sha256 TEXT PRIMARY KEY,
                size_bytes INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                verified_at TEXT NOT NULL
            );
            CREATE TABLE documents (
                document_id TEXT PRIMARY KEY,
                current_version_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE document_versions (
                version_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(document_id),
                source_sha256 TEXT NOT NULL REFERENCES stored_objects(sha256),
                source_filename TEXT NOT NULL,
                source_size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('processing', 'ready', 'failed')),
                created_at TEXT NOT NULL,
                ready_at TEXT,
                UNIQUE (document_id, version_id)
            );
            CREATE TABLE ingestion_page_results (
                version_id TEXT NOT NULL REFERENCES document_versions(version_id),
                page_number INTEGER NOT NULL,
                source_slide_id INTEGER NOT NULL,
                relationship_id TEXT NOT NULL,
                source_part TEXT NOT NULL,
                hidden INTEGER NOT NULL,
                enabled INTEGER NOT NULL,
                source_content_json TEXT,
                fingerprint_version INTEGER,
                fingerprint_sha256 TEXT,
                render_sha256 TEXT REFERENCES stored_objects(sha256),
                render_media_type TEXT,
                render_dpi INTEGER,
                render_width_px INTEGER,
                render_height_px INTEGER,
                PRIMARY KEY (version_id, page_number)
            );

            INSERT INTO stored_objects VALUES (
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                1, 'application/test', 'now'
            );
            INSERT INTO documents VALUES ('document', NULL, 'now');
            INSERT INTO document_versions VALUES (
                'version', 'document',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'source.pptx', 1, 'processing', 'now', NULL
            );
            INSERT INTO ingestion_page_results (
                version_id, page_number, source_slide_id, relationship_id,
                source_part, hidden, enabled
            ) VALUES ('version', 1, 256, 'rId1', 'ppt/slides/slide1.xml', 0, 1);
            """
        )
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, kind, payload_json, status, actor_id, idempotency_key,
                created_at, updated_at
            ) VALUES ('job', 'document.ingest', ?, 'pending', 'actor', 'key', 'now', 'now')
            """,
            (payload,),
        )

    initialize_database(settings)

    with connect(settings) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'page_version_image_sources'
            """
        ).fetchone() is not None
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {"replaced_at", "purged_at"} <= {
            row["name"]
            for row in connection.execute("PRAGMA table_info(publication_artifacts)")
        }
        assert dict(
            connection.execute(
                "SELECT version_id, document_id, status FROM document_versions"
            ).fetchone()
        ) == {
            "version_id": "version",
            "document_id": "document",
            "status": "processing",
        }
        assert dict(
            connection.execute(
                """
                SELECT document_id, version_id, status, max_attempts
                FROM jobs WHERE job_id = 'job'
                """
            ).fetchone()
        ) == {
            "document_id": "document",
            "version_id": "version",
            "status": "queued",
            "max_attempts": 3,
        }
        checkpoint_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(ingestion_page_results)")
        }
        assert {"manifest_key", "conversion_key", "render_key", "fingerprint_key"} <= (
            checkpoint_columns
        )
        assert connection.execute(
            "SELECT source_part FROM ingestion_page_results WHERE version_id = 'version'"
        ).fetchone()[0] == "ppt/slides/slide1.xml"
        connection.execute(
            "UPDATE document_versions SET status = 'awaiting_mapping' WHERE version_id = 'version'"
        )
        connection.execute(
            "UPDATE document_versions SET status = 'voided' WHERE version_id = 'version'"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE document_versions SET source_operation = 'unknown' "
                "WHERE version_id = 'version'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, kind, payload_json, status, actor_id, idempotency_key,
                    document_id, version_id, created_at, updated_at
                ) VALUES (
                    'duplicate', 'document.ingest', '{}', 'queued', 'actor', 'other-key',
                    'document', 'other-version', 'now', 'now'
                )
                """
            )


def test_v3_job_table_migrates_states_index_and_idempotency_foreign_key(
    tmp_path: Path,
) -> None:
    settings = Settings.for_test(tmp_path)
    initialize_database(settings)
    payload = json.dumps(
        {
            "document_id": "document",
            "job_id": "job",
            "source_sha256": "b" * 64,
            "version_id": "version",
        }
    )
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            INSERT INTO stored_objects VALUES (
                'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                1, 'application/test', 'now'
            );
            INSERT INTO documents (document_id, current_version_id, created_at)
            VALUES ('document', NULL, 'now');
            INSERT INTO document_versions (
                version_id, document_id, source_sha256, source_filename,
                source_size_bytes, status, created_at, ready_at
            ) VALUES (
                'version', 'document',
                'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                'source.pptx', 1, 'processing', 'now', NULL
            );
            DROP INDEX one_active_ingestion_job_per_document;
            CREATE TABLE jobs_v3 (
                job_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'running', 'completed', 'failed')
                ),
                actor_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                document_id TEXT,
                version_id TEXT,
                lease_owner TEXT,
                lease_expires_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                checkpoint_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error_json TEXT,
                UNIQUE (actor_id, idempotency_key)
            );
            DROP TABLE jobs;
            ALTER TABLE jobs_v3 RENAME TO jobs;
            CREATE UNIQUE INDEX one_active_ingestion_job_per_document
            ON jobs(document_id)
            WHERE kind = 'document.ingest' AND status IN ('pending', 'running');
            PRAGMA user_version = 3;
            """
        )
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, kind, payload_json, status, actor_id, idempotency_key,
                document_id, version_id, lease_owner, lease_expires_at,
                attempts, checkpoint_json, created_at, updated_at
            ) VALUES (
                'job', 'document.ingest', ?, 'running', 'actor', 'key',
                'document', 'version', 'worker', '2099-01-01T00:00:00+00:00',
                1, '{"phase":"conversion"}', 'now', 'now'
            )
            """,
            (payload,),
        )
        connection.execute(
            """
            INSERT INTO idempotency_records (
                actor_id, command_scope, idempotency_key, request_fingerprint,
                response_status, document_id, version_id, job_id, created_at
            ) VALUES (
                'actor', 'POST /api/v1/documents', 'request', 'fingerprint',
                'accepted', 'document', 'version', 'job', 'now'
            )
            """
        )

    initialize_database(settings)

    with connect(settings) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert dict(
            connection.execute(
                """
                SELECT status, attempts, max_attempts, lease_owner, lease_token
                FROM jobs WHERE job_id = 'job'
                """
            ).fetchone()
        ) == {
            "status": "running",
            "attempts": 1,
            "max_attempts": 3,
            "lease_owner": "worker",
            "lease_token": None,
        }
        assert connection.execute(
            "SELECT job_id FROM idempotency_records WHERE job_id = 'job'"
        ).fetchone()[0] == "job"
        index_sql = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'index' AND name = 'one_active_ingestion_job_per_document'
            """
        ).fetchone()[0]
        assert "requires_action" in index_sql
        assert {"deleted_at", "deleted_by", "deletion_reason"} <= {
            row["name"] for row in connection.execute("PRAGMA table_info(documents)")
        }
        assert {
            "source_operation",
            "source_version_id",
            "voided_at",
            "voided_by",
            "void_reason",
        } <= {
            row["name"]
            for row in connection.execute("PRAGMA table_info(document_versions)")
        }
        source_version_foreign_keys = {
            (row["from"], row["table"], row["to"])
            for row in connection.execute("PRAGMA foreign_key_list(document_versions)")
        }
        assert (
            "source_version_id",
            "document_versions",
            "version_id",
        ) in source_version_foreign_keys
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lifecycle_events'"
        ).fetchone() is not None
        assert {"deleted_at", "deleted_in_version_id"} <= {
            row["name"] for row in connection.execute("PRAGMA table_info(pages)")
        }
        assert {
            "current_snapshot_id",
            "prefill_snapshot_id",
            "inherited_from_page_version_id",
            "reviewed_by",
            "reviewed_at",
            "review_source_version_id",
            "exclusion_reason",
            "exclusion_note",
        } <= {
            row["name"] for row in connection.execute("PRAGMA table_info(page_versions)")
        }
        for table in (
            "curation_snapshots",
            "visual_objects",
            "curation_snapshot_visuals",
            "page_review_events",
        ):
            assert connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone() is not None


def test_v20_retention_backfill_recovers_after_columns_were_already_added(
    tmp_path: Path,
) -> None:
    settings = Settings.for_test(tmp_path)
    initialize_database(settings)
    sha256 = "a" * 64
    now = "2026-08-29T00:00:00+00:00"
    with connect(settings) as connection:
        connection.execute(
            "INSERT INTO stored_objects VALUES (?, 1, 'application/zip', ?)",
            (sha256, now),
        )
        for publication_seq in (1, 2):
            candidate_id = f"candidate-{publication_seq}"
            connection.execute(
                """
                INSERT INTO publication_candidates (
                    candidate_id, business_state_token, content_set_hash, scope_json,
                    status, created_by, created_at, publication_seq
                ) VALUES (?, ?, ?, '{}', 'succeeded', 'publisher', ?, ?)
                """,
                (candidate_id, f"token-{publication_seq}", sha256, now, publication_seq),
            )
            connection.execute(
                """
                INSERT INTO publication_artifacts (
                    publication_seq, candidate_id, snapshot_id, content_set_hash,
                    artifact_sha256, media_type, size_bytes, chunk_count,
                    asset_count, published_at
                ) VALUES (?, ?, ?, ?, ?, 'application/zip', 1, 0, 0, ?)
                """,
                (
                    publication_seq,
                    candidate_id,
                    f"snapshot-{publication_seq}",
                    sha256,
                    sha256,
                    now,
                ),
            )
        connection.execute("INSERT INTO current_publication VALUES (1, 2)")
        connection.execute("PRAGMA user_version = 20")
        connection.commit()

    initialize_database(settings)

    with connect(settings) as connection:
        rows = connection.execute(
            "SELECT publication_seq, replaced_at FROM publication_artifacts "
            "ORDER BY publication_seq"
        ).fetchall()
    assert rows[0]["replaced_at"] is not None
    assert rows[1]["replaced_at"] is None
