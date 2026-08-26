from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pptextract.config import Settings

SCHEMA_VERSION = 16


def connect(settings: Settings) -> sqlite3.Connection:
    connection = sqlite3.connect(
        settings.database_path, timeout=settings.sqlite_busy_timeout_ms / 1000
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {settings.sqlite_busy_timeout_ms}")
    return connection


def initialize_database(settings: Settings) -> None:
    settings.validate()
    if not database_path_is_local(settings.database_path):
        raise ValueError(f"SQLite 不得位于网络文件系统：{settings.database_path}")
    if not database_path_is_local(settings.object_store_path):
        raise ValueError(f"对象目录不得位于网络文件系统：{settings.object_store_path}")
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(settings) as connection:
        existing_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if existing_version > SCHEMA_VERSION:
            raise RuntimeError(f"数据库版本 {existing_version} 高于应用支持版本 {SCHEMA_VERSION}")
        journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        if journal_mode is None or journal_mode[0].lower() != "wal":
            raise RuntimeError("SQLite 无法启用 WAL 模式")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS worker_heartbeats (
                worker_id TEXT PRIMARY KEY,
                config_version INTEGER NOT NULL,
                heartbeat_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK (status IN (
                        'queued', 'running', 'requires_action',
                        'succeeded', 'failed', 'cancelled'
                    )),
                actor_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                document_id TEXT,
                version_id TEXT,
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
                next_attempt_at TEXT,
                checkpoint_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (actor_id, idempotency_key)
            );

            CREATE TABLE IF NOT EXISTS stored_objects (
                sha256 TEXT PRIMARY KEY,
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                media_type TEXT NOT NULL,
                verified_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                current_version_id TEXT,
                created_at TEXT NOT NULL,
                deleted_at TEXT,
                deleted_by TEXT,
                deletion_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS document_versions (
                version_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(document_id),
                source_sha256 TEXT NOT NULL REFERENCES stored_objects(sha256),
                source_filename TEXT NOT NULL,
                source_size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL
                    CHECK (status IN (
                        'processing', 'awaiting_mapping', 'ready', 'failed', 'voided'
                    )),
                created_at TEXT NOT NULL,
                ready_at TEXT,
                source_operation TEXT NOT NULL DEFAULT 'upload'
                    CHECK (source_operation IN ('upload', 'retry', 'rollback')),
                source_version_id TEXT REFERENCES document_versions(version_id),
                voided_at TEXT,
                voided_by TEXT,
                void_reason TEXT,
                mapping_revision INTEGER NOT NULL DEFAULT 0,
                mapping_confirmed_at TEXT,
                mapping_confirmed_by TEXT,
                render_config_version TEXT,
                render_generation INTEGER,
                UNIQUE (document_id, version_id)
            );

            CREATE TABLE IF NOT EXISTS ingestion_page_results (
                version_id TEXT NOT NULL REFERENCES document_versions(version_id),
                page_number INTEGER NOT NULL CHECK (page_number > 0),
                source_slide_id INTEGER NOT NULL,
                relationship_id TEXT NOT NULL,
                source_part TEXT NOT NULL,
                hidden INTEGER NOT NULL CHECK (hidden IN (0, 1)),
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                manifest_key TEXT,
                source_content_json TEXT,
                conversion_key TEXT,
                fingerprint_version INTEGER,
                fingerprint_sha256 TEXT,
                fingerprint_key TEXT,
                render_sha256 TEXT REFERENCES stored_objects(sha256),
                render_media_type TEXT,
                render_dpi INTEGER,
                render_width_px INTEGER,
                render_height_px INTEGER,
                render_key TEXT,
                render_fonts_json TEXT,
                enable_job_id TEXT REFERENCES jobs(job_id),
                PRIMARY KEY (version_id, page_number)
            );

            CREATE TABLE IF NOT EXISTS pages (
                page_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(document_id),
                chunk_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                deleted_at TEXT,
                deleted_in_version_id TEXT REFERENCES document_versions(version_id),
                UNIQUE (document_id, page_id)
            );

            CREATE TABLE IF NOT EXISTS page_versions (
                page_version_id TEXT PRIMARY KEY,
                page_id TEXT NOT NULL REFERENCES pages(page_id),
                document_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                page_number INTEGER NOT NULL CHECK (page_number > 0),
                fingerprint_version INTEGER NOT NULL,
                fingerprint_sha256 TEXT NOT NULL,
                source_content_json TEXT NOT NULL,
                render_sha256 TEXT NOT NULL REFERENCES stored_objects(sha256),
                render_media_type TEXT NOT NULL,
                render_dpi INTEGER NOT NULL,
                render_width_px INTEGER NOT NULL,
                render_height_px INTEGER NOT NULL,
                review_status TEXT NOT NULL
                    CHECK (review_status IN ('pending', 'approved', 'excluded')),
                current_snapshot_id TEXT REFERENCES curation_snapshots(snapshot_id),
                prefill_snapshot_id TEXT REFERENCES curation_snapshots(snapshot_id),
                inherited_from_page_version_id TEXT REFERENCES page_versions(page_version_id),
                reviewed_by TEXT,
                reviewed_at TEXT,
                review_source_version_id TEXT REFERENCES document_versions(version_id),
                exclusion_reason TEXT CHECK (exclusion_reason IS NULL OR exclusion_reason IN (
                    'no_meaningful_content', 'duplicate', 'irrelevant',
                    'unreadable', 'other'
                )),
                exclusion_note TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (document_id, page_id) REFERENCES pages(document_id, page_id),
                FOREIGN KEY (document_id, version_id)
                    REFERENCES document_versions(document_id, version_id),
                UNIQUE (version_id, page_number),
                UNIQUE (version_id, page_id)
            );

            CREATE INDEX IF NOT EXISTS page_versions_review_queue
                ON page_versions(review_status, document_id, page_number);

            CREATE TABLE IF NOT EXISTS page_version_image_sources (
                page_version_id TEXT NOT NULL REFERENCES page_versions(page_version_id),
                reference_index INTEGER NOT NULL CHECK (reference_index >= 0),
                position INTEGER NOT NULL CHECK (position >= 0),
                object_sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                media_type TEXT NOT NULL,
                origin_part TEXT NOT NULL,
                alt_text TEXT NOT NULL,
                PRIMARY KEY (page_version_id, reference_index),
                UNIQUE (page_version_id, position)
            );

            CREATE INDEX IF NOT EXISTS page_version_image_sources_object
                ON page_version_image_sources(object_sha256);

            CREATE TABLE IF NOT EXISTS curation_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                page_version_id TEXT NOT NULL REFERENCES page_versions(page_version_id),
                snapshot_kind TEXT NOT NULL CHECK (snapshot_kind IN ('formal', 'prefill')),
                source_snapshot_id TEXT REFERENCES curation_snapshots(snapshot_id),
                overview TEXT,
                source_content_json TEXT,
                capture_required INTEGER NOT NULL DEFAULT 0
                    CHECK (capture_required IN (0, 1)),
                created_by TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS curation_snapshots_page_version
                ON curation_snapshots(page_version_id, created_at, snapshot_id);

            CREATE TABLE IF NOT EXISTS curation_source_confirmations (
                confirmation_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL UNIQUE
                    REFERENCES curation_snapshots(snapshot_id),
                actor_id TEXT NOT NULL,
                confirmed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS curation_source_reviews (
                review_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL UNIQUE
                    REFERENCES curation_snapshots(snapshot_id),
                actor_id TEXT NOT NULL,
                completed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS visual_objects (
                visual_ref TEXT PRIMARY KEY,
                page_id TEXT NOT NULL REFERENCES pages(page_id),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS curation_snapshot_visuals (
                snapshot_id TEXT NOT NULL REFERENCES curation_snapshots(snapshot_id),
                visual_ref TEXT NOT NULL REFERENCES visual_objects(visual_ref),
                position INTEGER NOT NULL CHECK (position >= 0),
                source_kind TEXT NOT NULL CHECK (source_kind IN ('source_image', 'capture')),
                disposition TEXT NOT NULL CHECK (disposition IN ('included', 'ignored')),
                summary TEXT,
                visual_type TEXT,
                bounds_json TEXT,
                source_visual_ref TEXT REFERENCES visual_objects(visual_ref),
                confirmed INTEGER NOT NULL CHECK (confirmed IN (0, 1)),
                PRIMARY KEY (snapshot_id, visual_ref),
                UNIQUE (snapshot_id, position)
            );

            CREATE TABLE IF NOT EXISTS curation_snapshot_image_sources (
                snapshot_id TEXT NOT NULL REFERENCES curation_snapshots(snapshot_id),
                source_ref TEXT NOT NULL,
                reference_index INTEGER NOT NULL CHECK (reference_index >= 0),
                position INTEGER NOT NULL CHECK (position >= 0),
                disposition TEXT NOT NULL CHECK (disposition IN ('included', 'ignored')),
                summary TEXT,
                ignore_reason TEXT CHECK (
                    ignore_reason IS NULL OR ignore_reason IN (
                        'decorative', 'duplicate_source', 'expressed_elsewhere',
                        'not_relevant', 'corrupt_or_unverifiable', 'other'
                    )
                ),
                ignore_note TEXT,
                visual_ref TEXT REFERENCES visual_objects(visual_ref),
                object_sha256 TEXT,
                media_type TEXT NOT NULL,
                size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
                origin_part TEXT NOT NULL,
                alt_text TEXT NOT NULL,
                decided_by TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                PRIMARY KEY (snapshot_id, source_ref),
                UNIQUE (snapshot_id, position)
            );

            CREATE TABLE IF NOT EXISTS page_review_events (
                event_id TEXT PRIMARY KEY,
                page_version_id TEXT NOT NULL REFERENCES page_versions(page_version_id),
                event_type TEXT NOT NULL CHECK (
                    event_type IN ('approved', 'excluded', 'inherited', 'prefilled')
                ),
                actor_id TEXT,
                occurred_at TEXT NOT NULL,
                source_version_id TEXT REFERENCES document_versions(version_id),
                source_page_version_id TEXT REFERENCES page_versions(page_version_id),
                snapshot_id TEXT REFERENCES curation_snapshots(snapshot_id),
                reason TEXT,
                note TEXT
            );

            CREATE TABLE IF NOT EXISTS idempotency_records (
                actor_id TEXT NOT NULL,
                command_scope TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                response_status TEXT NOT NULL
                    CHECK (response_status IN ('accepted', 'coalesced', 'no_change')),
                document_id TEXT NOT NULL REFERENCES documents(document_id),
                version_id TEXT NOT NULL REFERENCES document_versions(version_id),
                job_id TEXT REFERENCES jobs(job_id),
                created_at TEXT NOT NULL,
                PRIMARY KEY (actor_id, command_scope, idempotency_key)
            );

            CREATE TABLE IF NOT EXISTS page_mapping_cases (
                case_id TEXT PRIMARY KEY,
                version_id TEXT NOT NULL REFERENCES document_versions(version_id),
                page_number INTEGER NOT NULL CHECK (page_number > 0),
                case_kind TEXT NOT NULL CHECK (case_kind IN (
                    'duplicate_fingerprint', 'slide_id_conflict', 'multiple_candidates'
                )),
                candidate_page_ids_json TEXT NOT NULL,
                decision TEXT CHECK (decision IS NULL OR decision IN ('reuse', 'new')),
                selected_page_id TEXT REFERENCES pages(page_id),
                decided_by TEXT,
                decided_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (version_id, page_number),
                CHECK (
                    (decision IS NULL AND selected_page_id IS NULL)
                    OR (decision = 'new' AND selected_page_id IS NULL)
                    OR (decision = 'reuse' AND selected_page_id IS NOT NULL)
                )
            );

            CREATE INDEX IF NOT EXISTS page_mapping_cases_version
                ON page_mapping_cases(version_id, page_number);

            CREATE TABLE IF NOT EXISTS lifecycle_events (
                event_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(document_id),
                version_id TEXT REFERENCES document_versions(version_id),
                event_type TEXT NOT NULL CHECK (event_type IN (
                    'version_retried', 'version_voided', 'version_rolled_back',
                    'document_deleted', 'document_restored'
                )),
                actor_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                source_version_id TEXT REFERENCES document_versions(version_id),
                command_scope TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (actor_id, command_scope, idempotency_key)
            );

            CREATE TABLE IF NOT EXISTS rendering_warnings (
                warning_id TEXT PRIMARY KEY,
                version_id TEXT NOT NULL REFERENCES document_versions(version_id),
                page_number INTEGER NOT NULL CHECK (page_number > 0),
                code TEXT NOT NULL CHECK (code IN ('missing_font', 'animation_flattened')),
                details_json TEXT NOT NULL,
                render_config_version TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                FOREIGN KEY (version_id, page_number)
                    REFERENCES ingestion_page_results(version_id, page_number)
            );

            CREATE INDEX IF NOT EXISTS rendering_warnings_current_version
                ON rendering_warnings(version_id, active, page_number);

            CREATE TABLE IF NOT EXISTS rendering_warning_confirmations (
                confirmation_id TEXT PRIMARY KEY,
                warning_id TEXT NOT NULL UNIQUE REFERENCES rendering_warnings(warning_id),
                actor_id TEXT NOT NULL,
                confirmed_at TEXT NOT NULL,
                warning_details_json TEXT NOT NULL,
                render_config_version TEXT NOT NULL
            );
            """
        )
        if 0 < existing_version < 3:
            _migrate_document_version_states(connection)
        _migrate_curation_snapshots(connection)
        snapshot_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(curation_snapshots)")
        }
        if "capture_required" not in snapshot_columns:
            connection.execute(
                "ALTER TABLE curation_snapshots ADD COLUMN "
                "capture_required INTEGER NOT NULL DEFAULT 0 "
                "CHECK (capture_required IN (0, 1))"
            )
        job_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(jobs)")}
        if "error_json" not in job_columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN error_json TEXT")
        if "document_id" not in job_columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN document_id TEXT")
        if "version_id" not in job_columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN version_id TEXT")
        if "lease_token" not in job_columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN lease_token TEXT")
        if "max_attempts" not in job_columns:
            connection.execute(
                "ALTER TABLE jobs ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3"
            )
        if "next_attempt_at" not in job_columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN next_attempt_at TEXT")
        _migrate_job_states(connection)
        page_result_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(ingestion_page_results)")
        }
        for column in ("manifest_key", "conversion_key", "fingerprint_key", "render_key"):
            if column not in page_result_columns:
                connection.execute(
                    f"ALTER TABLE ingestion_page_results ADD COLUMN {column} TEXT"
                )
        if "render_fonts_json" not in page_result_columns:
            connection.execute(
                "ALTER TABLE ingestion_page_results ADD COLUMN render_fonts_json TEXT"
            )
        version_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(document_versions)")
        }
        if "render_config_version" not in version_columns:
            connection.execute(
                "ALTER TABLE document_versions ADD COLUMN render_config_version TEXT"
            )
        if "render_generation" not in version_columns:
            connection.execute(
                "ALTER TABLE document_versions ADD COLUMN render_generation INTEGER"
            )
        if "enable_job_id" not in page_result_columns:
            connection.execute(
                "ALTER TABLE ingestion_page_results "
                "ADD COLUMN enable_job_id TEXT REFERENCES jobs(job_id)"
            )
        document_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(documents)")
        }
        for column in ("deleted_at", "deleted_by", "deletion_reason"):
            if column not in document_columns:
                connection.execute(f"ALTER TABLE documents ADD COLUMN {column} TEXT")
        version_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(document_versions)")
        }
        if "source_operation" not in version_columns:
            connection.execute(
                "ALTER TABLE document_versions ADD COLUMN "
                "source_operation TEXT NOT NULL DEFAULT 'upload' "
                "CHECK (source_operation IN ('upload', 'retry', 'rollback'))"
            )
        if "source_version_id" not in version_columns:
            connection.execute(
                "ALTER TABLE document_versions ADD COLUMN "
                "source_version_id TEXT REFERENCES document_versions(version_id)"
            )
        for column in ("voided_at", "voided_by", "void_reason"):
            if column not in version_columns:
                connection.execute(
                    f"ALTER TABLE document_versions ADD COLUMN {column} TEXT"
                )
        version_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(document_versions)")
        }
        if "mapping_revision" not in version_columns:
            connection.execute(
                "ALTER TABLE document_versions ADD COLUMN "
                "mapping_revision INTEGER NOT NULL DEFAULT 0"
            )
        for column in ("mapping_confirmed_at", "mapping_confirmed_by"):
            if column not in version_columns:
                connection.execute(
                    f"ALTER TABLE document_versions ADD COLUMN {column} TEXT"
                )
        page_columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(pages)")
        }
        if "deleted_at" not in page_columns:
            connection.execute("ALTER TABLE pages ADD COLUMN deleted_at TEXT")
        if "deleted_in_version_id" not in page_columns:
            connection.execute(
                "ALTER TABLE pages ADD COLUMN deleted_in_version_id TEXT "
                "REFERENCES document_versions(version_id)"
            )
        page_version_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(page_versions)")
        }
        page_version_additions = {
            "current_snapshot_id": "TEXT REFERENCES curation_snapshots(snapshot_id)",
            "prefill_snapshot_id": "TEXT REFERENCES curation_snapshots(snapshot_id)",
            "inherited_from_page_version_id": (
                "TEXT REFERENCES page_versions(page_version_id)"
            ),
            "reviewed_by": "TEXT",
            "reviewed_at": "TEXT",
            "review_source_version_id": "TEXT REFERENCES document_versions(version_id)",
            "exclusion_reason": (
                "TEXT CHECK (exclusion_reason IS NULL OR exclusion_reason IN ("
                "'no_meaningful_content', 'duplicate', 'irrelevant', "
                "'unreadable', 'other'))"
            ),
            "exclusion_note": "TEXT",
        }
        for column, declaration in page_version_additions.items():
            if column not in page_version_columns:
                connection.execute(
                    f"ALTER TABLE page_versions ADD COLUMN {column} {declaration}"
                )
        visual_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(curation_snapshot_visuals)")
        }
        visual_additions = {
            "source_image_ref": "TEXT",
            "asset_sha256": "TEXT REFERENCES stored_objects(sha256)",
            "asset_media_type": "TEXT",
            "asset_size_bytes": "INTEGER",
            "asset_width_px": "INTEGER",
            "asset_height_px": "INTEGER",
        }
        for column, declaration in visual_additions.items():
            if column not in visual_columns:
                connection.execute(
                    f"ALTER TABLE curation_snapshot_visuals ADD COLUMN {column} {declaration}"
                )
        if existing_version < 14:
            _backfill_page_version_image_sources(connection)
        for row in connection.execute(
            "SELECT job_id, payload_json FROM jobs WHERE kind = 'document.ingest'"
        ):
            payload = json.loads(row["payload_json"])
            connection.execute(
                """
                UPDATE jobs SET document_id = ?, version_id = ?
                WHERE job_id = ? AND document_id IS NULL AND version_id IS NULL
                """,
                (payload.get("document_id"), payload.get("version_id"), row["job_id"]),
            )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_ingestion_job_per_document
            ON jobs(document_id)
            WHERE kind = 'document.ingest'
              AND status IN ('queued', 'running', 'requires_action')
            """
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _backfill_page_version_image_sources(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT page_version_id, source_content_json FROM page_versions"
    ).fetchall()
    for row in rows:
        content = json.loads(str(row["source_content_json"]))
        for position, image in enumerate(content.get("images", [])):
            if not isinstance(image, dict):
                continue
            encoded = image.get("data_base64")
            if not isinstance(encoded, str):
                continue
            try:
                payload = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                continue
            connection.execute(
                """
                INSERT OR IGNORE INTO page_version_image_sources (
                    page_version_id, reference_index, position, object_sha256,
                    size_bytes, media_type, origin_part, alt_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["page_version_id"],
                    int(image.get("reference_index", position)),
                    position,
                    hashlib.sha256(payload).hexdigest(),
                    len(payload),
                    str(image.get("media_type", "application/octet-stream")),
                    str(image.get("origin_part", "")),
                    str(image.get("alt_text", "")),
                ),
            )


def _migrate_curation_snapshots(connection: sqlite3.Connection) -> None:
    table = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'curation_snapshots'"
    ).fetchone()
    if table is None:
        return
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(curation_snapshots)")
    }
    table_sql = str(table["sql"])
    if {"source_content_json", "created_by"} <= columns and (
        "UNIQUE (page_version_id, snapshot_kind)" not in table_sql
    ):
        return

    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA legacy_alter_table = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "ALTER TABLE curation_snapshots RENAME TO curation_snapshots_v11"
        )
        connection.execute(
            """
            CREATE TABLE curation_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                page_version_id TEXT NOT NULL REFERENCES page_versions(page_version_id),
                snapshot_kind TEXT NOT NULL CHECK (snapshot_kind IN ('formal', 'prefill')),
                source_snapshot_id TEXT REFERENCES curation_snapshots(snapshot_id),
                overview TEXT,
                source_content_json TEXT,
                created_by TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        source_content = (
            "source_content_json" if "source_content_json" in columns else "NULL"
        )
        created_by = "created_by" if "created_by" in columns else "NULL"
        connection.execute(
            f"""
            INSERT INTO curation_snapshots (
                snapshot_id, page_version_id, snapshot_kind, source_snapshot_id,
                overview, source_content_json, created_by, created_at
            )
            SELECT snapshot_id, page_version_id, snapshot_kind, source_snapshot_id,
                   overview, {source_content}, {created_by}, created_at
            FROM curation_snapshots_v11
            """
        )
        connection.execute("DROP TABLE curation_snapshots_v11")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS curation_snapshots_page_version
            ON curation_snapshots(page_version_id, created_at, snapshot_id)
            """
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA legacy_alter_table = OFF")
        connection.execute("PRAGMA foreign_keys = ON")


def _migrate_job_states(connection: sqlite3.Connection) -> None:
    table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
    ).fetchone()
    if table_sql is None or "'pending'" not in str(table_sql["sql"]):
        return

    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP INDEX IF EXISTS one_active_ingestion_job_per_document")
        connection.execute(
            """
            CREATE TABLE jobs_v4 (
                job_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN (
                    'queued', 'running', 'requires_action',
                    'succeeded', 'failed', 'cancelled'
                )),
                actor_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                document_id TEXT,
                version_id TEXT,
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
                next_attempt_at TEXT,
                checkpoint_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error_json TEXT,
                UNIQUE (actor_id, idempotency_key)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO jobs_v4 (
                job_id, kind, payload_json, status, actor_id, idempotency_key,
                document_id, version_id, lease_owner, lease_token, lease_expires_at,
                attempts, max_attempts, next_attempt_at, checkpoint_json,
                created_at, updated_at, error_json
            )
            SELECT job_id, kind, payload_json,
                   CASE status
                       WHEN 'pending' THEN 'queued'
                       WHEN 'completed' THEN 'succeeded'
                       ELSE status
                   END,
                   actor_id, idempotency_key, document_id, version_id,
                   lease_owner, lease_token, lease_expires_at, attempts,
                   max_attempts, next_attempt_at, checkpoint_json,
                   created_at, updated_at, error_json
            FROM jobs
            """
        )
        connection.execute("DROP TABLE jobs")
        connection.execute("ALTER TABLE jobs_v4 RENAME TO jobs")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")

    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError("数据库 v4 迁移后外键校验失败")


def _migrate_document_version_states(connection: sqlite3.Connection) -> None:
    table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'document_versions'"
    ).fetchone()
    if table_sql is None or "awaiting_mapping" in str(table_sql["sql"]):
        return

    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE document_versions_v3 (
                version_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(document_id),
                source_sha256 TEXT NOT NULL REFERENCES stored_objects(sha256),
                source_filename TEXT NOT NULL,
                source_size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN (
                    'processing', 'awaiting_mapping', 'ready', 'failed', 'voided'
                )),
                created_at TEXT NOT NULL,
                ready_at TEXT,
                UNIQUE (document_id, version_id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO document_versions_v3 (
                version_id, document_id, source_sha256, source_filename,
                source_size_bytes, status, created_at, ready_at
            )
            SELECT version_id, document_id, source_sha256, source_filename,
                   source_size_bytes, status, created_at, ready_at
            FROM document_versions
            """
        )
        connection.execute("DROP TABLE document_versions")
        connection.execute("ALTER TABLE document_versions_v3 RENAME TO document_versions")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")

    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError("数据库 v3 迁移后外键校验失败")


@contextmanager
def transaction(settings: Settings) -> Iterator[sqlite3.Connection]:
    connection = connect(settings)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


NETWORK_FILESYSTEM_TYPES = {
    "ceph",
    "cifs",
    "fuse.smb",
    "fuse.sshfs",
    "glusterfs",
    "lustre",
    "nfs",
    "nfs4",
    "smb3",
    "smbfs",
    "sshfs",
}


def _mount_type_from_proc(candidate: Path) -> str | None:
    try:
        mounts = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    resolved = candidate.resolve()
    best_match: tuple[int, str] | None = None
    for line in mounts:
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        after_fields = after.split()
        if len(fields) < 5 or not after_fields:
            continue
        mount_point = Path(
            fields[4]
            .replace("\\040", " ")
            .replace("\\011", "\t")
            .replace("\\012", "\n")
            .replace("\\134", "\\")
        )
        try:
            resolved.relative_to(mount_point)
        except ValueError:
            continue
        match = (len(mount_point.parts), after_fields[0])
        if best_match is None or match[0] > best_match[0]:
            best_match = match
    return None if best_match is None else best_match[1].lower()


def _mount_type_from_stat(candidate: Path) -> str | None:
    command = (
        ["stat", "-f", "%T", str(candidate)]
        if sys.platform == "darwin"
        else ["stat", "-f", "-c", "%T", str(candidate)]
    )
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    filesystem_type = result.stdout.strip().lower()
    return filesystem_type or None


def _filesystem_type(path: Path) -> str | None:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return _mount_type_from_proc(candidate) or _mount_type_from_stat(candidate)


def database_path_is_local(path: Path) -> bool:
    """只接受能够确认不是已知网络类型的文件系统，无法检测时拒绝启动。"""

    filesystem_type = _filesystem_type(path)
    return filesystem_type is not None and filesystem_type not in NETWORK_FILESYSTEM_TYPES
