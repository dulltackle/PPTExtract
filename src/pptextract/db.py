from __future__ import annotations

import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pptextract.config import Settings

SCHEMA_VERSION = 2


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
            raise RuntimeError(
                f"数据库版本 {existing_version} 高于应用支持版本 {SCHEMA_VERSION}"
            )
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
                    CHECK (status IN ('pending', 'running', 'completed', 'failed')),
                actor_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                lease_owner TEXT,
                lease_expires_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
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
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS document_versions (
                version_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(document_id),
                source_sha256 TEXT NOT NULL REFERENCES stored_objects(sha256),
                source_filename TEXT NOT NULL,
                source_size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL
                    CHECK (status IN ('processing', 'ready', 'failed')),
                created_at TEXT NOT NULL,
                ready_at TEXT,
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

            CREATE TABLE IF NOT EXISTS pages (
                page_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(document_id),
                chunk_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
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
                created_at TEXT NOT NULL,
                FOREIGN KEY (document_id, page_id) REFERENCES pages(document_id, page_id),
                FOREIGN KEY (document_id, version_id)
                    REFERENCES document_versions(document_id, version_id),
                UNIQUE (version_id, page_number),
                UNIQUE (version_id, page_id)
            );

            CREATE INDEX IF NOT EXISTS page_versions_review_queue
                ON page_versions(review_status, document_id, page_number);
            """
        )
        job_columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(jobs)")
        }
        if "error_json" not in job_columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN error_json TEXT")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


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
