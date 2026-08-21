from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pptextract.config import Settings
from pptextract.db import SCHEMA_VERSION, connect, initialize_database


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
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
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
                "SELECT document_id, version_id FROM jobs WHERE job_id = 'job'"
            ).fetchone()
        ) == {"document_id": "document", "version_id": "version"}
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
                """
                INSERT INTO jobs (
                    job_id, kind, payload_json, status, actor_id, idempotency_key,
                    document_id, version_id, created_at, updated_at
                ) VALUES (
                    'duplicate', 'document.ingest', '{}', 'pending', 'actor', 'other-key',
                    'document', 'other-version', 'now', 'now'
                )
                """
            )
