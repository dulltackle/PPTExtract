from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pptextract.config import Settings
from pptextract.db import SCHEMA_VERSION, connect, initialize_database, transaction
from pptextract.jobs import JOB_LEASE_DURATION
from pptextract.object_store import LocalObjectStore

MIN_BACKUP_SCHEMA_VERSION = 23


@dataclass(frozen=True, slots=True)
class GarbageCollectionResult:
    marked: tuple[str, ...]
    unmarked: tuple[str, ...]
    deleted: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReferenceFailure:
    category: str
    owner_id: str
    sha256: str
    reason: str
    expected_size: int
    actual_size: int | None


@dataclass(frozen=True, slots=True)
class AuditReport:
    ok: bool
    checked_references: int
    checked_objects: int
    failures: tuple[ReferenceFailure, ...]


@dataclass(frozen=True, slots=True)
class BackupResult:
    path: Path
    database_sha256: str
    object_count: int
    audit: AuditReport


@dataclass(frozen=True, slots=True)
class RecoveryDrillResult:
    drill_id: str
    status: str
    workspace: Path
    steps: dict[str, str]
    quantitative_objectives_verified: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _ReferenceExpectation:
    category: str
    owner_id: str
    sha256: str
    size_bytes: int


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _reference_expectations(
    connection: sqlite3.Connection,
) -> tuple[_ReferenceExpectation, ...]:
    queries = (
        (
            "original_source",
            "SELECT version_id, source_sha256, source_size_bytes FROM document_versions",
        ),
        (
            "ingestion_render",
            """
            SELECT results.version_id || ':' || results.page_number,
                   results.render_sha256, stored.size_bytes
            FROM ingestion_page_results AS results
            JOIN stored_objects AS stored ON stored.sha256 = results.render_sha256
            WHERE results.render_sha256 IS NOT NULL
            """,
        ),
        (
            "version_render",
            """
            SELECT page_version_id, render_sha256, stored.size_bytes
            FROM page_versions
            JOIN stored_objects AS stored ON stored.sha256 = page_versions.render_sha256
            """,
        ),
        (
            "version_source_image",
            """
            SELECT page_version_id || ':' || reference_index, object_sha256, size_bytes
            FROM page_version_image_sources
            """,
        ),
        (
            "formal_visual_asset",
            """
            SELECT visuals.snapshot_id || ':' || visuals.visual_ref,
                   visuals.asset_sha256, visuals.asset_size_bytes
            FROM curation_snapshot_visuals AS visuals
            JOIN curation_snapshots AS snapshots
              ON snapshots.snapshot_id = visuals.snapshot_id
            WHERE snapshots.snapshot_kind = 'formal'
              AND visuals.asset_sha256 IS NOT NULL
              AND visuals.asset_size_bytes IS NOT NULL
            """,
        ),
        (
            "formal_source_asset",
            """
            SELECT sources.snapshot_id || ':' || sources.source_ref,
                   sources.object_sha256, sources.size_bytes
            FROM curation_snapshot_image_sources AS sources
            JOIN curation_snapshots AS snapshots
              ON snapshots.snapshot_id = sources.snapshot_id
            WHERE snapshots.snapshot_kind = 'formal'
              AND sources.object_sha256 IS NOT NULL
              AND sources.size_bytes IS NOT NULL
            """,
        ),
        (
            "frozen_asset",
            "SELECT candidate_id || ':' || path, asset_sha256, size_bytes "
            "FROM publication_frozen_assets",
        ),
        (
            "current_artifact",
            """
            SELECT CAST(artifacts.publication_seq AS TEXT), artifacts.artifact_sha256,
                   artifacts.size_bytes
            FROM current_publication AS current
            JOIN publication_artifacts AS artifacts
              ON artifacts.publication_seq = current.publication_seq
            """,
        ),
        (
            "retained_artifact",
            """
            SELECT CAST(artifacts.publication_seq AS TEXT), artifacts.artifact_sha256,
                   artifacts.size_bytes
            FROM publication_artifacts AS artifacts
            LEFT JOIN current_publication AS current
              ON current.publication_seq = artifacts.publication_seq
            WHERE current.publication_seq IS NULL AND artifacts.purged_at IS NULL
            """,
        ),
    )
    expectations: list[_ReferenceExpectation] = []
    for category, query in queries:
        for row in connection.execute(query):
            expectations.append(
                _ReferenceExpectation(
                    category=category,
                    owner_id=str(row[0]),
                    sha256=str(row[1]),
                    size_bytes=int(row[2]),
                )
            )
    return tuple(expectations)


def _perform_reference_audit(settings: Settings) -> AuditReport:
    with connect(settings) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            return AuditReport(
                ok=False,
                checked_references=0,
                checked_objects=0,
                failures=(
                    ReferenceFailure(
                        category="database",
                        owner_id="sqlite",
                        sha256="",
                        reason="integrity_check_failed",
                        expected_size=0,
                        actual_size=None,
                    ),
                ),
            )
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            return AuditReport(
                ok=False,
                checked_references=0,
                checked_objects=0,
                failures=(
                    ReferenceFailure(
                        category="database",
                        owner_id="sqlite",
                        sha256="",
                        reason="foreign_key_check_failed",
                        expected_size=0,
                        actual_size=None,
                    ),
                ),
            )
        expectations = _reference_expectations(connection)
    store = LocalObjectStore(settings.object_store_path)
    observed: dict[str, tuple[int, str] | None] = {}
    failures: list[ReferenceFailure] = []
    for expectation in expectations:
        if expectation.sha256 not in observed:
            path = store.path_for(expectation.sha256)
            observed[expectation.sha256] = (
                None if not path.is_file() else (path.stat().st_size, _hash_file(path))
            )
        actual = observed[expectation.sha256]
        reason: str | None = None
        actual_size: int | None = None
        if actual is None:
            reason = "missing"
        else:
            actual_size, actual_sha256 = actual
            if actual_size != expectation.size_bytes:
                reason = "size_mismatch"
            elif actual_sha256 != expectation.sha256:
                reason = "sha256_mismatch"
        if reason is not None:
            failures.append(
                ReferenceFailure(
                    category=expectation.category,
                    owner_id=expectation.owner_id,
                    sha256=expectation.sha256,
                    reason=reason,
                    expected_size=expectation.size_bytes,
                    actual_size=actual_size,
                )
            )
    return AuditReport(
        ok=not failures,
        checked_references=len(expectations),
        checked_objects=len(observed),
        failures=tuple(failures),
    )


def _audit_json(report: AuditReport) -> str:
    return json.dumps(
        {
            "checked_objects": report.checked_objects,
            "checked_references": report.checked_references,
            "failures": [
                {
                    "actual_size": failure.actual_size,
                    "category": failure.category,
                    "expected_size": failure.expected_size,
                    "owner_id": failure.owner_id,
                    "reason": failure.reason,
                    "sha256": failure.sha256,
                }
                for failure in report.failures
            ],
            "ok": report.ok,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _set_recovery_state(
    settings: Settings,
    *,
    status: str,
    reason: str | None,
    report: AuditReport | None = None,
) -> None:
    with transaction(settings, require_recovery_ready=False) as connection:
        connection.execute(
            """
            UPDATE storage_recovery_state
            SET status = ?, reason = ?, updated_at = ?, last_audit_json = ?
            WHERE singleton_id = 1
            """,
            (
                status,
                reason,
                datetime.now(UTC).isoformat(),
                None if report is None else _audit_json(report),
            ),
        )


def read_recovery_state(settings: Settings) -> tuple[str, str | None]:
    with connect(settings) as connection:
        row = connection.execute(
            "SELECT status, reason FROM storage_recovery_state WHERE singleton_id = 1"
        ).fetchone()
    if row is None:
        return "blocked", "recovery_state_missing"
    return str(row["status"]), None if row["reason"] is None else str(row["reason"])


def audit_references(settings: Settings) -> AuditReport:
    """全量校验恢复关键引用，并以结果原子开关写入与发布门禁。"""

    _set_recovery_state(settings, status="audit_required", reason="reference_audit_running")
    report = _perform_reference_audit(settings)
    _set_recovery_state(
        settings,
        status="ready" if report.ok else "blocked",
        reason=None if report.ok else "reference_audit_failed",
        report=report,
    )
    return report


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_coordinated_backup(settings: Settings, destination: Path) -> BackupResult:
    """创建 SQLite 一致快照及其同期全部可达本地对象。"""

    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"备份目标已存在：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.partial"
    staging.mkdir()
    try:
        database_backup = staging / "pptextract.sqlite3"
        with connect(settings) as source, sqlite3.connect(database_backup) as target:
            source.backup(target)
        _fsync_file(database_backup)
        object_backup = staging / "objects"
        object_backup.mkdir()
        snapshot_settings = replace(
            settings,
            database_path=database_backup,
            object_store_path=object_backup,
            allow_temporary_storage=True,
        )
        with connect(snapshot_settings) as connection:
            reachable = sorted(_reachable_object_hashes(connection))
        source_store = LocalObjectStore(settings.object_store_path)
        backup_store = LocalObjectStore(object_backup)
        for sha256 in reachable:
            source_path = source_store.path_for(sha256)
            if not source_path.is_file():
                raise RuntimeError(f"备份缺失可达对象：{sha256}")
            with source_path.open("rb") as source:
                copied = backup_store.put_stream(source)
            if copied.sha256 != sha256:
                raise RuntimeError(f"备份对象哈希不一致：{sha256}")
        report = _perform_reference_audit(snapshot_settings)
        if not report.ok:
            raise RuntimeError("协调备份引用审计失败")
        database_sha256 = _hash_file(database_backup)
        manifest_path = staging / "backup.json"
        with manifest_path.open("x", encoding="utf-8") as handle:
            json.dump(
                {
                    "created_at": datetime.now(UTC).isoformat(),
                    "database_sha256": database_sha256,
                    "object_sha256s": reachable,
                    "schema_version": SCHEMA_VERSION,
                },
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(staging)
        os.replace(staging, destination)
        _fsync_directory(destination.parent)
        return BackupResult(
            path=destination,
            database_sha256=database_sha256,
            object_count=len(reachable),
            audit=report,
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def restore_backup(backup_path: Path, target: Settings) -> AuditReport:
    """把协调备份恢复到空目标；审计失败的恢复仍以 blocked 门禁落地。"""

    backup_path = backup_path.resolve()
    manifest = json.loads((backup_path / "backup.json").read_text(encoding="utf-8"))
    schema_version = manifest.get("schema_version") if isinstance(manifest, dict) else None
    if (
        not isinstance(schema_version, int)
        or schema_version < MIN_BACKUP_SCHEMA_VERSION
        or schema_version > SCHEMA_VERSION
    ):
        raise ValueError("备份 schema 版本与当前程序不兼容")
    object_sha256s = manifest.get("object_sha256s")
    if not isinstance(object_sha256s, list) or any(
        not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
        for sha256 in object_sha256s
    ):
        raise ValueError("备份对象清单无效")
    backup_database = backup_path / "pptextract.sqlite3"
    if _hash_file(backup_database) != str(manifest["database_sha256"]):
        raise ValueError("备份 SQLite 哈希不一致")
    target_database = target.database_path.resolve()
    target_objects = target.object_store_path.resolve()
    data_root = target_database.parent
    if target_objects.parent != data_root:
        raise ValueError("协调恢复要求 SQLite 与对象目录位于同一数据根目录")
    if target_database.name == target_objects.name:
        raise ValueError("SQLite 文件与对象目录名称不能相同")
    if data_root.exists():
        raise FileExistsError("恢复数据根目录必须尚不存在")
    data_root.parent.mkdir(parents=True, exist_ok=True)
    staged_root = data_root.parent / f".{data_root.name}.{uuid.uuid4().hex}.restore"
    staged_root.mkdir()
    staged_database = staged_root / target_database.name
    staged_objects = staged_root / target_objects.name
    staged_objects.mkdir()
    try:
        shutil.copy2(backup_database, staged_database)
        _fsync_file(staged_database)
        for sha256 in object_sha256s:
            source = backup_path / "objects" / str(sha256)[:2] / str(sha256)
            destination = staged_objects / str(sha256)[:2] / str(sha256)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            _fsync_file(destination)
            _fsync_directory(destination.parent)
        staged_settings = replace(
            target,
            database_path=staged_database,
            object_store_path=staged_objects,
            allow_temporary_storage=True,
        )
        initialize_database(staged_settings)
        report = audit_references(staged_settings)
        with connect(staged_settings) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        Path(f"{staged_database}-wal").unlink(missing_ok=True)
        Path(f"{staged_database}-shm").unlink(missing_ok=True)
        _fsync_file(staged_database)
        _fsync_directory(staged_objects)
        _fsync_directory(staged_root)
        os.replace(staged_root, data_root)
        _fsync_directory(data_root.parent)
        return report
    except BaseException:
        shutil.rmtree(staged_root, ignore_errors=True)
        raise


def _probe_ingestion_resume(settings: Settings) -> None:
    with transaction(settings) as connection:
        row = connection.execute(
            """
            SELECT job_id, status, payload_json, checkpoint_json
            FROM jobs
            WHERE kind = 'document.ingest'
              AND status IN ('queued', 'running')
              AND checkpoint_json IS NOT NULL
            ORDER BY created_at DESC, job_id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            raise RuntimeError("恢复演练缺少非终态摄取任务")
        job_id = str(row["job_id"])
        original_checkpoint = (
            None if row["checkpoint_json"] is None else str(row["checkpoint_json"])
        )
        payload = json.loads(str(row["payload_json"]))
        source_sha256 = str(payload.get("source_sha256", ""))
        if not LocalObjectStore(settings.object_store_path).verify(source_sha256):
            raise RuntimeError("恢复后的摄取任务来源对象不可用")
        connection.execute(
            """
            UPDATE jobs
            SET status = 'cancelled', lease_owner = NULL, lease_token = NULL,
                lease_expires_at = NULL, next_attempt_at = NULL
            WHERE job_id <> ? AND status IN ('queued', 'running', 'requires_action')
            """,
            (job_id,),
        )
        if row["status"] == "running":
            connection.execute(
                """
                UPDATE jobs
                SET lease_expires_at = '2000-01-01T00:00:00+00:00'
                WHERE job_id = ?
                """,
                (job_id,),
            )
    from pptextract import worker as worker_module

    if not worker_module.run_once(settings):
        raise RuntimeError("恢复后的摄取任务未被 worker 续跑")
    with connect(settings) as connection:
        resumed = connection.execute(
            "SELECT status, checkpoint_json FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    if resumed is None or resumed["status"] not in {"succeeded", "requires_action"}:
        raise RuntimeError("恢复后的摄取任务未从检查点继续到稳定状态")
    if original_checkpoint is not None and resumed["checkpoint_json"] is None:
        raise RuntimeError("摄取任务续跑时丢失原检查点")


def _probe_current_artifact_download(settings: Settings) -> None:
    with connect(settings) as connection:
        row = connection.execute(
            """
            SELECT artifacts.artifact_sha256, artifacts.size_bytes
            FROM current_publication AS current
            JOIN publication_artifacts AS artifacts
              ON artifacts.publication_seq = current.publication_seq
            WHERE current.singleton_id = 1
            """
        ).fetchone()
    if row is None:
        raise RuntimeError("恢复演练缺少当前产物")
    sha256 = str(row["artifact_sha256"])
    path = LocalObjectStore(settings.object_store_path).path_for(sha256)
    if not path.is_file() or path.stat().st_size != int(row["size_bytes"]):
        raise RuntimeError("恢复后的当前产物无法完整下载")
    if _hash_file(path) != sha256:
        raise RuntimeError("恢复后的当前产物下载哈希不一致")


def run_recovery_drill(
    settings: Settings,
    backup_path: Path,
    workspace: Path,
) -> RecoveryDrillResult:
    """在隔离目录执行协调恢复、审计、任务续跑与当前产物下载演练。"""

    drill_id = uuid.uuid4().hex
    started_at = datetime.now(UTC).isoformat()
    workspace = workspace.resolve()
    steps = {
        "sqlite_and_objects_restore": "not_run",
        "reference_audit": "not_run",
        "ingestion_resume": "not_run",
        "current_artifact_download": "not_run",
    }
    error_message: str | None = None
    try:
        drill_settings = replace(
            settings,
            database_path=workspace / "pptextract.sqlite3",
            object_store_path=workspace / "objects",
            worker_id=f"{settings.worker_id}-recovery-drill",
            allow_temporary_storage=True,
        )
        audit = restore_backup(backup_path, drill_settings)
        steps["sqlite_and_objects_restore"] = "passed"
        steps["reference_audit"] = "passed" if audit.ok else "failed"
        if not audit.ok:
            raise RuntimeError("恢复后的全量引用审计失败")
        _probe_ingestion_resume(drill_settings)
        steps["ingestion_resume"] = "passed"
        _probe_current_artifact_download(drill_settings)
        steps["current_artifact_download"] = "passed"
    except Exception as error:
        error_message = str(error)
        for step, status in steps.items():
            if status == "not_run":
                steps[step] = "failed"
                break
    status = "passed" if all(value == "passed" for value in steps.values()) else "failed"
    result = RecoveryDrillResult(
        drill_id=drill_id,
        status=status,
        workspace=workspace,
        steps=steps,
        quantitative_objectives_verified=False,
        error=error_message,
    )
    completed_at = datetime.now(UTC).isoformat()
    result_json = json.dumps(
        {
            "drill_id": drill_id,
            "error": error_message,
            "quantitative_objectives_verified": False,
            "status": status,
            "steps": steps,
            "workspace": str(workspace),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with transaction(settings) as connection:
        connection.execute(
            """
            INSERT INTO recovery_drills (
                drill_id, backup_path, status, result_json, started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                drill_id,
                str(backup_path.resolve()),
                status,
                result_json,
                started_at,
                completed_at,
            ),
        )
    return result


def _reachable_object_hashes(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        """
        SELECT source_sha256 AS sha256 FROM document_versions
        UNION
        SELECT render_sha256 FROM ingestion_page_results WHERE render_sha256 IS NOT NULL
        UNION
        SELECT render_sha256 FROM page_versions
        UNION
        SELECT object_sha256 FROM page_version_image_sources
        UNION
        SELECT visuals.asset_sha256
        FROM curation_snapshot_visuals AS visuals
        JOIN curation_snapshots AS snapshots
          ON snapshots.snapshot_id = visuals.snapshot_id
        WHERE snapshots.snapshot_kind = 'formal' AND visuals.asset_sha256 IS NOT NULL
        UNION
        SELECT sources.object_sha256
        FROM curation_snapshot_image_sources AS sources
        JOIN curation_snapshots AS snapshots
          ON snapshots.snapshot_id = sources.snapshot_id
        WHERE snapshots.snapshot_kind = 'formal' AND sources.object_sha256 IS NOT NULL
        UNION
        SELECT asset_sha256 FROM publication_frozen_assets
        UNION
        SELECT artifact_sha256 FROM publication_artifacts WHERE purged_at IS NULL
        """
    )
    return {str(row[0]) for row in rows}


def collect_unreachable_objects(
    settings: Settings,
    *,
    at: datetime | None = None,
) -> GarbageCollectionResult:
    """以“标记—宽限—复扫”回收当前数据库不可达的本地对象。"""

    checked_at = at or datetime.now(UTC)
    if checked_at.tzinfo is None:
        raise ValueError("对象回收时间必须包含时区")
    store = LocalObjectStore(settings.object_store_path)
    with store.maintenance_lock(exclusive=True):
        return _collect_unreachable_objects_locked(settings, store, checked_at)


def _collect_unreachable_objects_locked(
    settings: Settings,
    store: LocalObjectStore,
    checked_at: datetime,
) -> GarbageCollectionResult:
    physical_hashes = {
        path.name
        for path in store.root.glob("[0-9a-f][0-9a-f]/*")
        if path.is_file()
        and len(path.name) == 64
        and all(character in "0123456789abcdef" for character in path.name)
    }
    marked: list[str] = []
    unmarked: list[str] = []
    deleted: list[str] = []
    with transaction(settings) as connection:
        max_attempts = int(
            connection.execute(
                "SELECT COALESCE(MAX(max_attempts), 3) FROM jobs"
            ).fetchone()[0]
        )
        retry_window = settings.job_retry_base_seconds * max(0, (2 ** (max_attempts - 1)) - 1)
        minimum_grace = JOB_LEASE_DURATION.total_seconds() + retry_window
        if settings.object_gc_grace_seconds <= minimum_grace:
            raise ValueError(
                "对象回收宽限期必须长于最长任务租约和正常重试窗口"
            )

        reachable = _reachable_object_hashes(connection)
        candidate_rows = {
            str(row["sha256"]): row
            for row in connection.execute(
                """
                SELECT sha256, first_unreachable_at, activity_token, deleted_at
                FROM object_gc_candidates
                """
            )
        }
        for sha256 in sorted(reachable & candidate_rows.keys()):
            connection.execute(
                "DELETE FROM object_gc_candidates WHERE sha256 = ?", (sha256,)
            )
            store.forget_activity(sha256)
            unmarked.append(sha256)

        recorded_hashes = {
            str(row[0]) for row in connection.execute("SELECT sha256 FROM stored_objects")
        }
        already_deleted = {
            sha256
            for sha256, row in candidate_rows.items()
            if row["deleted_at"] is not None and sha256 not in physical_hashes
        }
        unreachable = (recorded_hashes | physical_hashes) - reachable - already_deleted
        for sha256 in sorted(unreachable):
            candidate = candidate_rows.get(sha256)
            activity_token = store.activity_token(sha256)
            if candidate is None:
                connection.execute(
                    """
                    INSERT INTO object_gc_candidates (
                        sha256, first_unreachable_at, last_unreachable_at, activity_token
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        sha256,
                        checked_at.isoformat(),
                        checked_at.isoformat(),
                        activity_token,
                    ),
                )
                marked.append(sha256)
                continue
            if activity_token != candidate["activity_token"]:
                connection.execute(
                    """
                    UPDATE object_gc_candidates
                    SET first_unreachable_at = ?, last_unreachable_at = ?,
                        activity_token = ?, deleted_at = NULL
                    WHERE sha256 = ?
                    """,
                    (
                        checked_at.isoformat(),
                        checked_at.isoformat(),
                        activity_token,
                        sha256,
                    ),
                )
                continue
            first_unreachable_at = datetime.fromisoformat(
                str(candidate["first_unreachable_at"])
            )
            connection.execute(
                "UPDATE object_gc_candidates SET last_unreachable_at = ? WHERE sha256 = ?",
                (checked_at.isoformat(), sha256),
            )
            if checked_at - first_unreachable_at <= timedelta(
                seconds=settings.object_gc_grace_seconds
            ):
                continue
            store.path_for(sha256).unlink(missing_ok=True)
            store.forget_activity(sha256)
            connection.execute(
                "UPDATE object_gc_candidates SET deleted_at = ? WHERE sha256 = ?",
                (checked_at.isoformat(), sha256),
            )
            deleted.append(sha256)

    return GarbageCollectionResult(
        marked=tuple(marked), unmarked=tuple(unmarked), deleted=tuple(deleted)
    )


def _print_result(value: Any) -> None:
    print(
        json.dumps(
            asdict(value),
            default=str,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="PPTExtract 存储维护命令")
    commands = parser.add_subparsers(dest="command", required=True)
    gc_parser = commands.add_parser("gc", help="执行两阶段可达性扫描")
    gc_parser.add_argument("--at", help="用于演练的 ISO-8601 扫描时间")
    backup_parser = commands.add_parser("backup", help="创建协调备份")
    backup_parser.add_argument("destination", type=Path)
    commands.add_parser("audit", help="全量审计恢复关键引用")
    restore_parser = commands.add_parser("restore", help="恢复到新的空数据根目录")
    restore_parser.add_argument("backup", type=Path)
    restore_parser.add_argument("target_root", type=Path)
    drill_parser = commands.add_parser("drill", help="在隔离目录执行恢复演练")
    drill_parser.add_argument("backup", type=Path)
    drill_parser.add_argument("workspace", type=Path)
    arguments = parser.parse_args()
    settings = Settings.from_env()

    if arguments.command == "restore":
        target = replace(
            settings,
            database_path=arguments.target_root / "pptextract.sqlite3",
            object_store_path=arguments.target_root / "objects",
        )
        report = restore_backup(arguments.backup, target)
        _print_result(report)
        if not report.ok:
            raise SystemExit(1)
        return

    initialize_database(settings)
    if arguments.command == "gc":
        checked_at = None if arguments.at is None else datetime.fromisoformat(arguments.at)
        _print_result(collect_unreachable_objects(settings, at=checked_at))
    elif arguments.command == "backup":
        _print_result(create_coordinated_backup(settings, arguments.destination))
    elif arguments.command == "audit":
        report = audit_references(settings)
        _print_result(report)
        if not report.ok:
            raise SystemExit(1)
    elif arguments.command == "drill":
        result = run_recovery_drill(settings, arguments.backup, arguments.workspace)
        _print_result(result)
        if result.status != "passed":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
