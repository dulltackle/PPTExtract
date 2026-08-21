from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO

from pptextract.config import Settings
from pptextract.conversion import NormalizedPageContent, convert_page
from pptextract.db import connect, transaction
from pptextract.fingerprint import fingerprint_page
from pptextract.jobs import ClaimedJob, lease_expiration, timestamp
from pptextract.object_store import (
    LocalObjectStore,
    ObjectTooLargeError,
    StoredObject,
)
from pptextract.pptx_projection import PackageLimitError, list_source_pages
from pptextract.rendering import DockerRenderingToolchain, render_standard_pages

PPTX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class IngestionRequestError(ValueError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class AcceptedIngestion:
    document_id: str
    version_id: str
    job_id: str | None
    status: str = "accepted"


@dataclass(frozen=True, slots=True)
class ValidatedSource:
    stored: StoredObject
    filename: str
    request_fingerprint: str
    total_pages: int


def accept_first_upload(
    settings: Settings,
    *,
    actor_id: str,
    idempotency_key: str,
    filename: str,
    media_type: str,
    stream: BinaryIO,
) -> AcceptedIngestion:
    """可靠持久化首个源文件，并以一个事务接受对应领域身份与任务。"""
    source = _validate_and_store_source(
        settings,
        idempotency_key=idempotency_key,
        filename=filename,
        media_type=media_type,
        stream=stream,
    )
    command_scope = "POST /api/v1/documents"
    now = timestamp()
    with transaction(settings) as connection:
        replay = _read_idempotency_record(
            connection,
            actor_id=actor_id,
            command_scope=command_scope,
            idempotency_key=idempotency_key,
            request_fingerprint=source.request_fingerprint,
        )
        if replay is not None:
            return replay

        legacy = connection.execute(
            """
            SELECT kind, payload_json
            FROM jobs
            WHERE actor_id = ? AND idempotency_key = ?
            """,
            (actor_id, idempotency_key),
        ).fetchone()
        if legacy is not None and legacy["kind"] == "document.ingest":
            payload = json.loads(legacy["payload_json"])
            existing_fingerprint = payload.get("request_fingerprint")
            if existing_fingerprint is None:
                version = connection.execute(
                    "SELECT source_filename FROM document_versions WHERE version_id = ?",
                    (payload.get("version_id"),),
                ).fetchone()
                if version is not None:
                    existing_fingerprint = _request_fingerprint(
                        source_sha256=str(payload["source_sha256"]),
                        filename=str(version["source_filename"]),
                        media_type=PPTX_MEDIA_TYPE,
                    )
            if existing_fingerprint != source.request_fingerprint:
                _raise_idempotency_conflict()
            accepted = AcceptedIngestion(
                document_id=str(payload["document_id"]),
                version_id=str(payload["version_id"]),
                job_id=str(payload["job_id"]),
            )
            _record_idempotency(
                connection,
                actor_id=actor_id,
                command_scope=command_scope,
                idempotency_key=idempotency_key,
                request_fingerprint=source.request_fingerprint,
                accepted=accepted,
                now=now,
            )
            return accepted

        document_id = uuid.uuid4().hex
        accepted = _create_ingestion(
            connection,
            source=source,
            actor_id=actor_id,
            internal_idempotency_key=_job_idempotency_key(command_scope, idempotency_key),
            document_id=document_id,
            create_document=True,
            now=now,
        )
        _record_idempotency(
            connection,
            actor_id=actor_id,
            command_scope=command_scope,
            idempotency_key=idempotency_key,
            request_fingerprint=source.request_fingerprint,
            accepted=accepted,
            now=now,
        )
        return accepted


def accept_document_version(
    settings: Settings,
    *,
    actor_id: str,
    document_id: str,
    idempotency_key: str,
    filename: str,
    media_type: str,
    stream: BinaryIO,
) -> AcceptedIngestion:
    """在同一事务内处理命令重放、内容合并与逐文档串行。"""
    source = _validate_and_store_source(
        settings,
        idempotency_key=idempotency_key,
        filename=filename,
        media_type=media_type,
        stream=stream,
    )
    command_scope = f"POST /api/v1/documents/{document_id}/versions"
    now = timestamp()
    with transaction(settings) as connection:
        replay = _read_idempotency_record(
            connection,
            actor_id=actor_id,
            command_scope=command_scope,
            idempotency_key=idempotency_key,
            request_fingerprint=source.request_fingerprint,
        )
        if replay is not None:
            return replay

        document = connection.execute(
            "SELECT current_version_id FROM documents WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        if document is None:
            raise IngestionRequestError(404, "not_found", "未找到请求的资源。")

        active = connection.execute(
            """
            SELECT versions.version_id, versions.source_sha256, jobs.job_id
            FROM document_versions AS versions
            JOIN jobs ON jobs.version_id = versions.version_id
            WHERE versions.document_id = ?
              AND versions.status IN ('processing', 'awaiting_mapping')
              AND jobs.kind = 'document.ingest'
            ORDER BY versions.created_at, versions.version_id
            LIMIT 1
            """,
            (document_id,),
        ).fetchone()
        if active is not None:
            if active["source_sha256"] != source.stored.sha256:
                raise IngestionRequestError(409, "document_busy", "该文档已有正在处理的摄取任务。")
            accepted = AcceptedIngestion(
                document_id=document_id,
                version_id=str(active["version_id"]),
                job_id=str(active["job_id"]),
                status="coalesced",
            )
            _record_idempotency(
                connection,
                actor_id=actor_id,
                command_scope=command_scope,
                idempotency_key=idempotency_key,
                request_fingerprint=source.request_fingerprint,
                accepted=accepted,
                now=now,
            )
            return accepted

        current_version_id = document["current_version_id"]
        if current_version_id is not None:
            current = connection.execute(
                """
                SELECT version_id, source_sha256
                FROM document_versions
                WHERE document_id = ? AND version_id = ? AND status = 'ready'
                """,
                (document_id, current_version_id),
            ).fetchone()
            if current is not None and current["source_sha256"] == source.stored.sha256:
                accepted = AcceptedIngestion(
                    document_id=document_id,
                    version_id=str(current["version_id"]),
                    job_id=None,
                    status="no_change",
                )
                _record_idempotency(
                    connection,
                    actor_id=actor_id,
                    command_scope=command_scope,
                    idempotency_key=idempotency_key,
                    request_fingerprint=source.request_fingerprint,
                    accepted=accepted,
                    now=now,
                )
                return accepted

        accepted = _create_ingestion(
            connection,
            source=source,
            actor_id=actor_id,
            internal_idempotency_key=_job_idempotency_key(command_scope, idempotency_key),
            document_id=document_id,
            create_document=False,
            now=now,
        )
        _record_idempotency(
            connection,
            actor_id=actor_id,
            command_scope=command_scope,
            idempotency_key=idempotency_key,
            request_fingerprint=source.request_fingerprint,
            accepted=accepted,
            now=now,
        )
        return accepted


def _validate_and_store_source(
    settings: Settings,
    *,
    idempotency_key: str,
    filename: str,
    media_type: str,
    stream: BinaryIO,
) -> ValidatedSource:
    if not idempotency_key or len(idempotency_key) > 200:
        raise IngestionRequestError(400, "invalid_idempotency_key", "缺少有效的幂等键。")
    safe_filename = Path(filename).name
    if not safe_filename.lower().endswith(".pptx") or media_type != PPTX_MEDIA_TYPE:
        raise IngestionRequestError(415, "unsupported_presentation", "仅接受 PPTX 文件。")

    store = LocalObjectStore(settings.object_store_path)
    try:
        stored = store.put_stream(stream, max_bytes=settings.max_source_upload_bytes)
    except ObjectTooLargeError as error:
        raise IngestionRequestError(
            413, "source_too_large", "PPTX 超过部署允许的上传上限。"
        ) from error
    try:
        source_pages = list_source_pages(stored.path.read_bytes())
    except PackageLimitError as error:
        raise IngestionRequestError(
            413, "source_too_large", "PPTX 超过部署允许的资源上限。"
        ) from error
    except (OSError, ValueError) as error:
        raise IngestionRequestError(422, "invalid_pptx", "上传内容不是有效的 PPTX。") from error
    if not source_pages:
        raise IngestionRequestError(422, "empty_presentation", "PPTX 至少需要一页。")

    return ValidatedSource(
        stored=stored,
        filename=safe_filename,
        request_fingerprint=_request_fingerprint(
            source_sha256=stored.sha256,
            filename=safe_filename,
            media_type=media_type,
        ),
        total_pages=len(source_pages),
    )


def _create_ingestion(
    connection: Any,
    *,
    source: ValidatedSource,
    actor_id: str,
    internal_idempotency_key: str,
    document_id: str,
    create_document: bool,
    now: str,
) -> AcceptedIngestion:
    version_id = uuid.uuid4().hex
    job_id = uuid.uuid4().hex
    payload = {
        "document_id": document_id,
        "job_id": job_id,
        "request_fingerprint": source.request_fingerprint,
        "source_sha256": source.stored.sha256,
        "version_id": version_id,
    }
    _record_object(connection, source.stored, PPTX_MEDIA_TYPE, now)
    if create_document:
        connection.execute(
            "INSERT INTO documents (document_id, created_at) VALUES (?, ?)",
            (document_id, now),
        )
    connection.execute(
        """
        INSERT INTO document_versions (
            version_id, document_id, source_sha256, source_filename,
            source_size_bytes, status, created_at
        ) VALUES (?, ?, ?, ?, ?, 'processing', ?)
        """,
        (
            version_id,
            document_id,
            source.stored.sha256,
            source.filename,
            source.stored.size_bytes,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO jobs (
            job_id, kind, payload_json, status, actor_id, idempotency_key,
            document_id, version_id, checkpoint_json, created_at, updated_at
        ) VALUES (?, 'document.ingest', ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            _json(payload),
            actor_id,
            internal_idempotency_key,
            document_id,
            version_id,
            _json(
                {
                    "phase": "source_manifest",
                    "completed_pages": 0,
                    "total_pages": source.total_pages,
                }
            ),
            now,
            now,
        ),
    )
    return AcceptedIngestion(document_id=document_id, version_id=version_id, job_id=job_id)


def _read_idempotency_record(
    connection: Any,
    *,
    actor_id: str,
    command_scope: str,
    idempotency_key: str,
    request_fingerprint: str,
) -> AcceptedIngestion | None:
    row = connection.execute(
        """
        SELECT request_fingerprint, response_status, document_id, version_id, job_id
        FROM idempotency_records
        WHERE actor_id = ? AND command_scope = ? AND idempotency_key = ?
        """,
        (actor_id, command_scope, idempotency_key),
    ).fetchone()
    if row is None:
        return None
    if row["request_fingerprint"] != request_fingerprint:
        _raise_idempotency_conflict()
    return AcceptedIngestion(
        document_id=str(row["document_id"]),
        version_id=str(row["version_id"]),
        job_id=None if row["job_id"] is None else str(row["job_id"]),
        status=str(row["response_status"]),
    )


def _record_idempotency(
    connection: Any,
    *,
    actor_id: str,
    command_scope: str,
    idempotency_key: str,
    request_fingerprint: str,
    accepted: AcceptedIngestion,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO idempotency_records (
            actor_id, command_scope, idempotency_key, request_fingerprint,
            response_status, document_id, version_id, job_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            actor_id,
            command_scope,
            idempotency_key,
            request_fingerprint,
            accepted.status,
            accepted.document_id,
            accepted.version_id,
            accepted.job_id,
            now,
        ),
    )


def _raise_idempotency_conflict() -> None:
    raise IngestionRequestError(
        409,
        "idempotency_conflict",
        "同一幂等键已用于不同请求。",
    )


def _job_idempotency_key(command_scope: str, idempotency_key: str) -> str:
    return hashlib.sha256(f"{command_scope}\0{idempotency_key}".encode()).hexdigest()


def process_ingestion_job(settings: Settings, job: ClaimedJob) -> None:
    source_sha256 = str(job.payload["source_sha256"])
    version_id = str(job.payload["version_id"])
    document_id = str(job.payload["document_id"])
    source = LocalObjectStore(settings.object_store_path).path_for(source_sha256).read_bytes()
    pages = list_source_pages(source)
    total_pages = len(pages)

    _checkpoint(settings, job.job_id, "source_manifest", 0, total_pages)
    with transaction(settings) as connection:
        for page in pages:
            connection.execute(
                """
                INSERT INTO ingestion_page_results (
                    version_id, page_number, source_slide_id, relationship_id,
                    source_part, hidden, enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(version_id, page_number) DO UPDATE SET
                    source_slide_id = excluded.source_slide_id,
                    relationship_id = excluded.relationship_id,
                    source_part = excluded.source_part,
                    hidden = excluded.hidden,
                    enabled = excluded.enabled
                """,
                (
                    version_id,
                    page.page_number,
                    page.source_slide_id,
                    page.relationship_id,
                    page.source_part,
                    int(page.hidden),
                    int(not page.hidden),
                ),
            )
    _checkpoint(settings, job.job_id, "source_manifest", total_pages, total_pages)

    enabled_pages = tuple(page for page in pages if not page.hidden)
    converted: dict[int, NormalizedPageContent] = {}
    _checkpoint(settings, job.job_id, "conversion", 0, total_pages)
    for completed, page in enumerate(enabled_pages, start=1):
        content = convert_page(source, page)
        converted[page.page_number] = content
        with transaction(settings) as connection:
            connection.execute(
                """
                UPDATE ingestion_page_results
                SET source_content_json = ?
                WHERE version_id = ? AND page_number = ?
                """,
                (_content_json(content), version_id, page.page_number),
            )
        _checkpoint(settings, job.job_id, "conversion", completed, total_pages)

    _checkpoint(settings, job.job_id, "rendering", 0, total_pages)
    store = LocalObjectStore(settings.object_store_path)
    toolchain = DockerRenderingToolchain(settings.render_image)
    for completed, page in enumerate(enabled_pages, start=1):
        (render,) = render_standard_pages(source, toolchain=toolchain, pages=(page,))
        stored_render = store.put(render.data)
        now = timestamp()
        with transaction(settings) as connection:
            _record_object(connection, stored_render, render.media_type, now)
            connection.execute(
                """
                UPDATE ingestion_page_results
                SET render_sha256 = ?, render_media_type = ?, render_dpi = ?,
                    render_width_px = ?, render_height_px = ?
                WHERE version_id = ? AND page_number = ?
                """,
                (
                    stored_render.sha256,
                    render.media_type,
                    render.dpi,
                    render.width_px,
                    render.height_px,
                    version_id,
                    render.page_number,
                ),
            )
        _checkpoint(settings, job.job_id, "rendering", completed, total_pages)

    _checkpoint(settings, job.job_id, "page_fingerprint", 0, total_pages)
    for completed, page in enumerate(enabled_pages, start=1):
        fingerprint = fingerprint_page(converted[page.page_number])
        with transaction(settings) as connection:
            connection.execute(
                """
                UPDATE ingestion_page_results
                SET fingerprint_version = ?, fingerprint_sha256 = ?
                WHERE version_id = ? AND page_number = ?
                """,
                (fingerprint.version, fingerprint.sha256, version_id, page.page_number),
            )
        _checkpoint(settings, job.job_id, "page_fingerprint", completed, total_pages)

    _checkpoint(settings, job.job_id, "page_mapping", total_pages, total_pages)
    _activate_first_version(settings, job, document_id, version_id, total_pages)


def fail_ingestion_job(settings: Settings, job: ClaimedJob, error: Exception) -> None:
    now = timestamp()
    with transaction(settings) as connection:
        failed_job = connection.execute(
            """
            UPDATE jobs
            SET status = 'failed', lease_owner = NULL, lease_expires_at = NULL,
                error_json = ?, updated_at = ?
            WHERE job_id = ? AND status = 'running' AND lease_owner = ?
            """,
            (
                _json({"code": "ingestion_failed", "message": "文档摄取失败。"}),
                now,
                job.job_id,
                settings.worker_id,
            ),
        )
        if failed_job.rowcount != 1:
            return
        connection.execute(
            """
            UPDATE document_versions SET status = 'failed'
            WHERE version_id = ? AND status = 'processing'
            """,
            (str(job.payload["version_id"]),),
        )


def _activate_first_version(
    settings: Settings,
    job: ClaimedJob,
    document_id: str,
    version_id: str,
    total_pages: int,
) -> None:
    now = timestamp()
    with transaction(settings) as connection:
        rows = connection.execute(
            """
            SELECT * FROM ingestion_page_results
            WHERE version_id = ? AND enabled = 1
            ORDER BY page_number
            """,
            (version_id,),
        ).fetchall()
        if len(rows) != total_pages:
            raise RuntimeError("首次版本存在未启用页，无法按快乐路径生效")
        for row in rows:
            required = (
                "source_content_json",
                "fingerprint_version",
                "fingerprint_sha256",
                "render_sha256",
                "render_media_type",
                "render_dpi",
                "render_width_px",
                "render_height_px",
            )
            if any(row[field] is None for field in required):
                raise RuntimeError(f"第 {row['page_number']} 页处理结果不完整")
            page_id = uuid.uuid4().hex
            chunk_id = uuid.uuid4().hex
            page_version_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO pages (page_id, document_id, chunk_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (page_id, document_id, chunk_id, now),
            )
            connection.execute(
                """
                INSERT INTO page_versions (
                    page_version_id, page_id, document_id, version_id, page_number,
                    fingerprint_version, fingerprint_sha256, source_content_json,
                    render_sha256, render_media_type, render_dpi, render_width_px,
                    render_height_px, review_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    page_version_id,
                    page_id,
                    document_id,
                    version_id,
                    row["page_number"],
                    row["fingerprint_version"],
                    row["fingerprint_sha256"],
                    row["source_content_json"],
                    row["render_sha256"],
                    row["render_media_type"],
                    row["render_dpi"],
                    row["render_width_px"],
                    row["render_height_px"],
                    now,
                ),
            )
        activated = connection.execute(
            """
            UPDATE document_versions SET status = 'ready', ready_at = ?
            WHERE version_id = ? AND status = 'processing'
            """,
            (now, version_id),
        )
        if activated.rowcount != 1:
            raise RuntimeError("版本不再处于可生效的 processing 状态")
        switched = connection.execute(
            "UPDATE documents SET current_version_id = ? WHERE document_id = ?",
            (version_id, document_id),
        )
        if switched.rowcount != 1:
            raise RuntimeError("版本所属文档不存在")
        updated = connection.execute(
            """
            UPDATE jobs
            SET status = 'completed', lease_owner = NULL, lease_expires_at = NULL,
                checkpoint_json = ?, error_json = NULL, updated_at = ?
            WHERE job_id = ? AND status = 'running' AND lease_owner = ?
            """,
            (
                _json(
                    {
                        "phase": "activation",
                        "completed_pages": total_pages,
                        "total_pages": total_pages,
                    }
                ),
                now,
                job.job_id,
                settings.worker_id,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("worker 在版本生效前失去任务租约")


def _checkpoint(
    settings: Settings, job_id: str, phase: str, completed_pages: int, total_pages: int
) -> None:
    with transaction(settings) as connection:
        updated = connection.execute(
            """
            UPDATE jobs
            SET checkpoint_json = ?, lease_expires_at = ?, updated_at = ?
            WHERE job_id = ? AND status = 'running' AND lease_owner = ?
            """,
            (
                _json(
                    {
                        "phase": phase,
                        "completed_pages": completed_pages,
                        "total_pages": total_pages,
                    }
                ),
                lease_expiration(),
                timestamp(),
                job_id,
                settings.worker_id,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("worker 无法保存未持有任务的进度")


def _record_object(
    connection: Any, stored: StoredObject, media_type: str, verified_at: str
) -> None:
    connection.execute(
        """
        INSERT INTO stored_objects (sha256, size_bytes, media_type, verified_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(sha256) DO UPDATE SET
            size_bytes = excluded.size_bytes,
            verified_at = excluded.verified_at
        """,
        (stored.sha256, stored.size_bytes, media_type, verified_at),
    )


def _content_json(content: NormalizedPageContent) -> str:
    payload = asdict(content)
    for image in payload["images"]:
        image["data_base64"] = base64.b64encode(image.pop("data")).decode("ascii")
    return _json(payload)


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _request_fingerprint(*, source_sha256: str, filename: str, media_type: str) -> str:
    canonical = _json(
        {
            "filename": filename,
            "media_type": media_type,
            "source_sha256": source_sha256,
        }
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def read_job(settings: Settings, job_id: str) -> dict[str, Any] | None:
    with connect(settings) as connection:
        row = connection.execute(
            """
            SELECT job_id, kind, status, attempts, checkpoint_json, error_json
            FROM jobs WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
    if row is None:
        return None
    public_status = {
        "pending": "queued",
        "running": "running",
        "completed": "succeeded",
        "failed": "failed",
    }[str(row["status"])]
    return {
        "job_id": str(row["job_id"]),
        "kind": str(row["kind"]),
        "status": public_status,
        "attempts": int(row["attempts"]),
        "progress": json.loads(row["checkpoint_json"]) if row["checkpoint_json"] else None,
        "error": json.loads(row["error_json"]) if row["error_json"] else None,
    }
