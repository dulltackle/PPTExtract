from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, cast

from pptextract.config import Settings
from pptextract.db import connect, transaction
from pptextract.ingest_workflow import IngestionRequestError
from pptextract.jobs import timestamp
from pptextract.object_store import LocalObjectStore
from pptextract.pptx_projection import list_source_pages


def retry_failed_version(
    settings: Settings,
    *,
    actor_id: str,
    document_id: str,
    version_id: str,
    idempotency_key: str,
    reason: str,
) -> dict[str, Any]:
    command_scope = (
        f"POST /api/v1/documents/{document_id}/versions/{version_id}/retry"
    )
    normalized_reason = _validate_command(idempotency_key=idempotency_key, reason=reason)
    fingerprint = _command_fingerprint(
        reason=normalized_reason, source_version_id=version_id
    )

    with connect(settings) as connection:
        replay = _read_replay(
            connection,
            actor_id=actor_id,
            command_scope=command_scope,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            return replay
        source = _read_version(connection, document_id=document_id, version_id=version_id)
    source_path = LocalObjectStore(settings.object_store_path).path_for(
        str(source["source_sha256"])
    )
    try:
        total_pages = len(list_source_pages(source_path.read_bytes()))
    except (OSError, ValueError) as error:
        raise IngestionRequestError(
            503, "source_unavailable", "原始 PPTX 暂不可用，无法创建重试版本。"
        ) from error

    now = timestamp()
    with transaction(settings) as connection:
        replay = _read_replay(
            connection,
            actor_id=actor_id,
            command_scope=command_scope,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            return replay
        source = _read_version(connection, document_id=document_id, version_id=version_id)
        _assert_document_active(source)
        if source["status"] != "failed":
            _raise_state_conflict("只有 failed 版本可以显式重试。")
        _assert_no_active_ingestion(connection, document_id)
        response = _create_derived_ingestion(
            connection,
            source=source,
            document_id=document_id,
            actor_id=actor_id,
            command_scope=command_scope,
            idempotency_key=idempotency_key,
            operation="retry",
            total_pages=total_pages,
            now=now,
        )
        _record_event(
            connection,
            document_id=document_id,
            version_id=str(response["version_id"]),
            event_type="version_retried",
            actor_id=actor_id,
            reason=normalized_reason,
            source_version_id=version_id,
            command_scope=command_scope,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            response=response,
            now=now,
        )
        return response


def void_version(
    settings: Settings,
    *,
    actor_id: str,
    document_id: str,
    version_id: str,
    idempotency_key: str,
    reason: str,
) -> dict[str, Any]:
    command_scope = f"POST /api/v1/documents/{document_id}/versions/{version_id}/void"
    normalized_reason = _validate_command(idempotency_key=idempotency_key, reason=reason)
    fingerprint = _command_fingerprint(
        reason=normalized_reason, source_version_id=version_id
    )
    now = timestamp()
    with transaction(settings) as connection:
        replay = _read_replay(
            connection,
            actor_id=actor_id,
            command_scope=command_scope,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            return replay
        version = _read_version(
            connection, document_id=document_id, version_id=version_id
        )
        _assert_document_active(version)
        if version["status"] not in {"processing", "awaiting_mapping", "ready"}:
            _raise_state_conflict(
                "只有 processing、awaiting_mapping 或 ready 版本可以作废。"
            )
        connection.execute(
            """
            UPDATE document_versions
            SET status = 'voided', voided_at = ?, voided_by = ?, void_reason = ?
            WHERE version_id = ?
            """,
            (now, actor_id, normalized_reason, version_id),
        )
        connection.execute(
            """
            UPDATE jobs
            SET status = 'cancelled', lease_owner = NULL, lease_token = NULL,
                lease_expires_at = NULL, next_attempt_at = NULL, updated_at = ?
            WHERE version_id = ? AND kind = 'document.ingest'
              AND status IN ('queued', 'running', 'requires_action')
            """,
            (now, version_id),
        )
        current_version_id = version["current_version_id"]
        if current_version_id == version_id:
            fallback = connection.execute(
                """
                SELECT version_id
                FROM document_versions
                WHERE document_id = ? AND status = 'ready' AND version_id != ?
                ORDER BY ready_at DESC, created_at DESC, version_id DESC
                LIMIT 1
                """,
                (document_id, version_id),
            ).fetchone()
            current_version_id = None if fallback is None else str(fallback["version_id"])
            connection.execute(
                "UPDATE documents SET current_version_id = ? WHERE document_id = ?",
                (current_version_id, document_id),
            )
        response = {
            "document_id": document_id,
            "version_id": version_id,
            "status": "voided",
            "current_version_id": current_version_id,
        }
        _record_event(
            connection,
            document_id=document_id,
            version_id=version_id,
            event_type="version_voided",
            actor_id=actor_id,
            reason=normalized_reason,
            source_version_id=version_id,
            command_scope=command_scope,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            response=response,
            now=now,
        )
        return response


def rollback_to_version(
    settings: Settings,
    *,
    actor_id: str,
    document_id: str,
    version_id: str,
    idempotency_key: str,
    reason: str,
) -> dict[str, Any]:
    command_scope = (
        f"POST /api/v1/documents/{document_id}/versions/{version_id}/rollback"
    )
    normalized_reason = _validate_command(idempotency_key=idempotency_key, reason=reason)
    fingerprint = _command_fingerprint(
        reason=normalized_reason, source_version_id=version_id
    )

    with connect(settings) as connection:
        replay = _read_replay(
            connection,
            actor_id=actor_id,
            command_scope=command_scope,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            return replay
        source = _read_version(connection, document_id=document_id, version_id=version_id)
    source_path = LocalObjectStore(settings.object_store_path).path_for(
        str(source["source_sha256"])
    )
    try:
        total_pages = len(list_source_pages(source_path.read_bytes()))
    except (OSError, ValueError) as error:
        raise IngestionRequestError(
            503, "source_unavailable", "原始 PPTX 暂不可用，无法创建回滚版本。"
        ) from error

    now = timestamp()
    with transaction(settings) as connection:
        replay = _read_replay(
            connection,
            actor_id=actor_id,
            command_scope=command_scope,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            return replay
        source = _read_version(connection, document_id=document_id, version_id=version_id)
        _assert_document_active(source)
        if source["status"] != "ready" or source["current_version_id"] == version_id:
            _raise_state_conflict("只能从历史 ready 版本创建回滚版本。")
        _assert_no_active_ingestion(connection, document_id)
        response = _create_derived_ingestion(
            connection,
            source=source,
            document_id=document_id,
            actor_id=actor_id,
            command_scope=command_scope,
            idempotency_key=idempotency_key,
            operation="rollback",
            total_pages=total_pages,
            now=now,
        )
        _record_event(
            connection,
            document_id=document_id,
            version_id=str(response["version_id"]),
            event_type="version_rolled_back",
            actor_id=actor_id,
            reason=normalized_reason,
            source_version_id=version_id,
            command_scope=command_scope,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            response=response,
            now=now,
        )
        return response


def soft_delete_document(
    settings: Settings,
    *,
    actor_id: str,
    document_id: str,
    idempotency_key: str,
    reason: str,
) -> dict[str, Any]:
    return _change_document_deletion(
        settings,
        actor_id=actor_id,
        document_id=document_id,
        idempotency_key=idempotency_key,
        reason=reason,
        deleting=True,
    )


def restore_document(
    settings: Settings,
    *,
    actor_id: str,
    document_id: str,
    idempotency_key: str,
    reason: str,
) -> dict[str, Any]:
    return _change_document_deletion(
        settings,
        actor_id=actor_id,
        document_id=document_id,
        idempotency_key=idempotency_key,
        reason=reason,
        deleting=False,
    )


def _change_document_deletion(
    settings: Settings,
    *,
    actor_id: str,
    document_id: str,
    idempotency_key: str,
    reason: str,
    deleting: bool,
) -> dict[str, Any]:
    suffix = "" if deleting else "/restore"
    method = "DELETE" if deleting else "POST"
    command_scope = f"{method} /api/v1/documents/{document_id}{suffix}"
    normalized_reason = _validate_command(idempotency_key=idempotency_key, reason=reason)
    fingerprint = _command_fingerprint(reason=normalized_reason, source_version_id=None)
    now = timestamp()
    with transaction(settings) as connection:
        replay = _read_replay(
            connection,
            actor_id=actor_id,
            command_scope=command_scope,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            return replay
        document = connection.execute(
            """
            SELECT current_version_id, deleted_at
            FROM documents WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()
        if document is None:
            raise IngestionRequestError(404, "not_found", "未找到请求的资源。")
        if deleting and document["deleted_at"] is not None:
            raise IngestionRequestError(409, "document_state_conflict", "文档已经软删。")
        if not deleting and document["deleted_at"] is None:
            raise IngestionRequestError(409, "document_state_conflict", "文档当前未被软删。")
        if deleting:
            active_jobs = connection.execute(
                """
                SELECT jobs.job_id, versions.version_id
                FROM jobs
                JOIN document_versions AS versions
                  ON versions.version_id = jobs.version_id
                WHERE jobs.document_id = ? AND jobs.kind = 'document.ingest'
                  AND jobs.status IN ('queued', 'running', 'requires_action')
                  AND versions.status = 'processing'
                """,
                (document_id,),
            ).fetchall()
            cancellation_error = _json(
                {
                    "code": "document_deleted",
                    "message": "文档软删，未完成的摄取任务已取消。",
                    "phase": "lifecycle",
                    "reason": normalized_reason,
                    "retryable": False,
                }
            )
            for active in active_jobs:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'cancelled', lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, next_attempt_at = NULL,
                        error_json = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (cancellation_error, now, active["job_id"]),
                )
                connection.execute(
                    """
                    UPDATE document_versions SET status = 'failed'
                    WHERE version_id = ? AND status = 'processing'
                    """,
                    (active["version_id"],),
                )
            connection.execute(
                """
                UPDATE documents
                SET deleted_at = ?, deleted_by = ?, deletion_reason = ?
                WHERE document_id = ?
                """,
                (now, actor_id, normalized_reason, document_id),
            )
        else:
            connection.execute(
                """
                UPDATE documents
                SET deleted_at = NULL, deleted_by = NULL, deletion_reason = NULL
                WHERE document_id = ?
                """,
                (document_id,),
            )
        current_version_id = document["current_version_id"]
        response = {
            "document_id": document_id,
            "current_version_id": current_version_id,
            "deleted": deleting,
        }
        _record_event(
            connection,
            document_id=document_id,
            version_id=current_version_id,
            event_type="document_deleted" if deleting else "document_restored",
            actor_id=actor_id,
            reason=normalized_reason,
            source_version_id=current_version_id,
            command_scope=command_scope,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            response=response,
            now=now,
        )
        return response


def read_lifecycle_events(settings: Settings, document_id: str) -> list[dict[str, Any]] | None:
    with connect(settings) as connection:
        document = connection.execute(
            "SELECT 1 FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        if document is None:
            return None
        rows = connection.execute(
            """
            SELECT event_id, event_type, actor_id, reason, version_id,
                   source_version_id, created_at
            FROM lifecycle_events
            WHERE document_id = ?
            ORDER BY created_at, event_id
            """,
            (document_id,),
        ).fetchall()
    return [
        {
            "event_id": str(row["event_id"]),
            "type": str(row["event_type"]),
            "actor_id": str(row["actor_id"]),
            "reason": str(row["reason"]),
            "version_id": row["version_id"],
            "source_version_id": row["source_version_id"],
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def _validate_command(*, idempotency_key: str, reason: str) -> str:
    if not idempotency_key or len(idempotency_key) > 200:
        raise IngestionRequestError(400, "invalid_idempotency_key", "缺少有效的幂等键。")
    normalized = reason.strip()
    if not normalized:
        raise IngestionRequestError(422, "invalid_reason", "必须提供操作原因。")
    return normalized


def _read_version(
    connection: Any, *, document_id: str, version_id: str
) -> Any:
    row = connection.execute(
        """
        SELECT versions.*, documents.current_version_id, documents.deleted_at
        FROM document_versions AS versions
        JOIN documents ON documents.document_id = versions.document_id
        WHERE versions.document_id = ? AND versions.version_id = ?
        """,
        (document_id, version_id),
    ).fetchone()
    if row is None:
        raise IngestionRequestError(404, "not_found", "未找到请求的资源。")
    return row


def _assert_document_active(row: Any) -> None:
    if row["deleted_at"] is not None:
        raise IngestionRequestError(409, "document_deleted", "软删文档不能接受该操作。")


def _assert_no_active_ingestion(connection: Any, document_id: str) -> None:
    active = connection.execute(
        """
        SELECT 1 FROM jobs
        WHERE document_id = ? AND kind = 'document.ingest'
          AND status IN ('queued', 'running', 'requires_action')
        LIMIT 1
        """,
        (document_id,),
    ).fetchone()
    if active is not None:
        raise IngestionRequestError(409, "document_busy", "该文档已有正在处理的摄取任务。")


def _create_derived_ingestion(
    connection: Any,
    *,
    source: Any,
    document_id: str,
    actor_id: str,
    command_scope: str,
    idempotency_key: str,
    operation: str,
    total_pages: int,
    now: str,
) -> dict[str, Any]:
    version_id = uuid.uuid4().hex
    job_id = uuid.uuid4().hex
    payload = {
        "document_id": document_id,
        "job_id": job_id,
        "source_sha256": str(source["source_sha256"]),
        "version_id": version_id,
    }
    connection.execute(
        """
        INSERT INTO document_versions (
            version_id, document_id, source_sha256, source_filename,
            source_size_bytes, status, created_at, source_operation,
            source_version_id
        ) VALUES (?, ?, ?, ?, ?, 'processing', ?, ?, ?)
        """,
        (
            version_id,
            document_id,
            source["source_sha256"],
            source["source_filename"],
            source["source_size_bytes"],
            now,
            operation,
            source["version_id"],
        ),
    )
    connection.execute(
        """
        INSERT INTO jobs (
            job_id, kind, payload_json, status, actor_id, idempotency_key,
            document_id, version_id, checkpoint_json, created_at, updated_at
        ) VALUES (?, 'document.ingest', ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            _json(payload),
            actor_id,
            hashlib.sha256(f"{command_scope}\0{idempotency_key}".encode()).hexdigest(),
            document_id,
            version_id,
            _json(
                {
                    "phase": "source_manifest",
                    "completed_pages": 0,
                    "total_pages": total_pages,
                }
            ),
            now,
            now,
        ),
    )
    return {
        "document_id": document_id,
        "version_id": version_id,
        "job_id": job_id,
        "status": "accepted",
        "source_relation": {
            "operation": operation,
            "source_version_id": str(source["version_id"]),
        },
    }


def _read_replay(
    connection: Any,
    *,
    actor_id: str,
    command_scope: str,
    idempotency_key: str,
    request_fingerprint: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT request_fingerprint, response_json
        FROM lifecycle_events
        WHERE actor_id = ? AND command_scope = ? AND idempotency_key = ?
        """,
        (actor_id, command_scope, idempotency_key),
    ).fetchone()
    if row is None:
        return None
    if row["request_fingerprint"] != request_fingerprint:
        raise IngestionRequestError(409, "idempotency_conflict", "同一幂等键已用于不同请求。")
    response = json.loads(str(row["response_json"]))
    if not isinstance(response, dict):
        raise RuntimeError("生命周期命令的幂等响应不是 JSON object")
    return cast(dict[str, Any], response)


def _record_event(
    connection: Any,
    *,
    document_id: str,
    version_id: str | None,
    event_type: str,
    actor_id: str,
    reason: str,
    source_version_id: str | None,
    command_scope: str,
    idempotency_key: str,
    request_fingerprint: str,
    response: dict[str, Any],
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO lifecycle_events (
            event_id, document_id, version_id, event_type, actor_id, reason,
            source_version_id, command_scope, idempotency_key,
            request_fingerprint, response_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid.uuid4().hex,
            document_id,
            version_id,
            event_type,
            actor_id,
            reason,
            source_version_id,
            command_scope,
            idempotency_key,
            request_fingerprint,
            _json(response),
            now,
        ),
    )


def _raise_state_conflict(message: str) -> None:
    raise IngestionRequestError(409, "version_state_conflict", message)


def _command_fingerprint(*, reason: str, source_version_id: str | None) -> str:
    return hashlib.sha256(
        _json({"reason": reason, "source_version_id": source_version_id}).encode()
    ).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
