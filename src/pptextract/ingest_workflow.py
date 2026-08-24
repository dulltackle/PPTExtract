from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from pptextract.config import Settings
from pptextract.conversion import (
    NormalizedImage,
    NormalizedPageContent,
    NormalizedTable,
    NormalizedTableCell,
    NormalizedTableSlot,
    convert_page,
)
from pptextract.db import connect, transaction
from pptextract.fingerprint import FINGERPRINT_VERSION, fingerprint_page
from pptextract.jobs import ClaimedJob, lease_expiration, timestamp
from pptextract.object_store import (
    LocalObjectStore,
    ObjectTooLargeError,
    StoredObject,
)
from pptextract.pptx_projection import PackageLimitError, SourcePage, list_source_pages
from pptextract.rendering import (
    DockerRenderingToolchain,
    StandardPageRender,
    audit_rendering_warnings,
    render_configuration_version,
    render_standard_pages,
)
from pptextract.rendering_warnings import replace_active_warnings

PPTX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
CONVERSION_TOOL_VERSION = "firecrawl-anydoc:0.1.9|pptextract-adapter:1"
MANIFEST_TOOL_VERSION = "pptextract-source-manifest:1"


class IngestionRequestError(ValueError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class IngestionStageError(RuntimeError):
    def __init__(
        self,
        *,
        phase: str,
        page_number: int | None,
        retryable: bool,
        code: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.page_number = page_number
        self.retryable = retryable
        self.code = code
        self.message = message


class MappingPreconditionError(IngestionRequestError):
    def __init__(self, current_etag: str) -> None:
        super().__init__(
            412,
            "mapping_precondition_failed",
            "页对应决定已被其他会话更新，请比较后重新确认。",
        )
        self.current_etag = current_etag


@dataclass(frozen=True, slots=True)
class AcceptedIngestion:
    document_id: str
    version_id: str
    job_id: str | None
    status: str = "accepted"


@dataclass(frozen=True, slots=True)
class AcceptedPageEnablement:
    document_id: str
    version_id: str
    page_number: int
    job_id: str | None
    status: str
    page_id: str | None = None


@dataclass(frozen=True, slots=True)
class ValidatedSource:
    stored: StoredObject
    filename: str
    request_fingerprint: str
    total_pages: int


@dataclass(frozen=True, slots=True)
class PageMappingCasePlan:
    page_number: int
    kind: str
    candidate_page_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PageMappingPlan:
    automatic_matches: dict[int, tuple[str, str]]
    cases: tuple[PageMappingCasePlan, ...]


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
            "SELECT current_version_id, deleted_at FROM documents WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        if document is None:
            raise IngestionRequestError(404, "not_found", "未找到请求的资源。")
        if document["deleted_at"] is not None:
            raise IngestionRequestError(409, "document_deleted", "软删文档不能接受新上传。")

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
        ) VALUES (?, 'document.ingest', ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
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
    store = LocalObjectStore(settings.object_store_path)
    try:
        source = store.path_for(source_sha256).read_bytes()
        pages = list_source_pages(source)
    except Exception as error:
        raise _stage_error("source_manifest", None, error) from error
    total_pages = len(pages)

    _checkpoint(settings, job, "source_manifest", 0, total_pages)
    with transaction(settings) as connection:
        _assert_job_lease(connection, settings, job)
        for page in pages:
            manifest_key = _stage_key(
                phase="source_manifest",
                source_sha256=source_sha256,
                version_id=version_id,
                page=page,
                tool_version=MANIFEST_TOOL_VERSION,
            )
            connection.execute(
                """
                INSERT INTO ingestion_page_results (
                    version_id, page_number, source_slide_id, relationship_id,
                    source_part, hidden, enabled, manifest_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(version_id, page_number) DO UPDATE SET
                    source_slide_id = excluded.source_slide_id,
                    relationship_id = excluded.relationship_id,
                    source_part = excluded.source_part,
                    hidden = excluded.hidden,
                    enabled = excluded.enabled,
                    manifest_key = excluded.manifest_key
                """,
                (
                    version_id,
                    page.page_number,
                    page.source_slide_id,
                    page.relationship_id,
                    page.source_part,
                    int(page.hidden),
                    int(not page.hidden),
                    manifest_key,
                ),
            )
    _checkpoint(settings, job, "source_manifest", total_pages, total_pages)

    enabled_pages = tuple(page for page in pages if not page.hidden)
    converted: dict[int, NormalizedPageContent] = {}
    _checkpoint(settings, job, "conversion", 0, total_pages)
    for completed, page in enumerate(enabled_pages, start=1):
        conversion_key = _stage_key(
            phase="conversion",
            source_sha256=source_sha256,
            version_id=version_id,
            page=page,
            tool_version=CONVERSION_TOOL_VERSION,
        )
        with connect(settings) as connection:
            checkpoint = connection.execute(
                """
                SELECT conversion_key, source_content_json
                FROM ingestion_page_results
                WHERE version_id = ? AND page_number = ?
                """,
                (version_id, page.page_number),
            ).fetchone()
        if (
            checkpoint is not None
            and checkpoint["conversion_key"] == conversion_key
            and checkpoint["source_content_json"] is not None
        ):
            content = _content_from_json(str(checkpoint["source_content_json"]))
        else:
            try:
                content = convert_page(source, page)
            except Exception as error:
                raise _stage_error("conversion", page.page_number, error) from error
        converted[page.page_number] = content
        with transaction(settings) as connection:
            _assert_job_lease(connection, settings, job)
            connection.execute(
                """
                UPDATE ingestion_page_results
                SET source_content_json = ?, conversion_key = ?
                WHERE version_id = ? AND page_number = ?
                """,
                (_content_json(content), conversion_key, version_id, page.page_number),
            )
        _checkpoint(settings, job, "conversion", completed, total_pages)

    toolchain = DockerRenderingToolchain(settings.render_image)
    render_config_version = render_configuration_version(settings.render_image)
    _checkpoint(settings, job, "rendering_audit", 0, total_pages)
    try:
        audited_warnings = tuple(
            warning
            for warning in audit_rendering_warnings(source, toolchain)
            if warning.page_number in {page.page_number for page in enabled_pages}
        )
    except Exception as error:
        raise _stage_error("rendering", None, error) from error
    with transaction(settings) as connection:
        _assert_job_lease(connection, settings, job)
        replace_active_warnings(
            connection,
            version_id=version_id,
            render_config_version=render_config_version,
            warnings=audited_warnings,
            page_numbers=tuple(page.page_number for page in enabled_pages),
            observed_at=timestamp(),
        )
    _checkpoint(settings, job, "rendering_audit", total_pages, total_pages)

    _checkpoint(settings, job, "rendering", 0, total_pages)
    rendered_fonts: dict[int, tuple[str, ...]] = {}
    for completed, page in enumerate(enabled_pages, start=1):
        render_key = _stage_key(
            phase="rendering",
            source_sha256=source_sha256,
            version_id=version_id,
            page=page,
            tool_version=render_config_version,
        )
        with connect(settings) as connection:
            checkpoint = connection.execute(
                """
                SELECT render_key, render_sha256, render_media_type, render_dpi,
                       render_width_px, render_height_px, render_fonts_json
                FROM ingestion_page_results
                WHERE version_id = ? AND page_number = ?
                """,
                (version_id, page.page_number),
            ).fetchone()
        try:
            render_is_reusable = (
                checkpoint is not None
                and checkpoint["render_key"] == render_key
                and checkpoint["render_sha256"] is not None
                and checkpoint["render_media_type"] is not None
                and checkpoint["render_dpi"] is not None
                and checkpoint["render_width_px"] is not None
                and checkpoint["render_height_px"] is not None
                and checkpoint["render_fonts_json"] is not None
                and store.verify(str(checkpoint["render_sha256"]))
            )
        except OSError as error:
            raise _stage_error("storage", page.page_number, error) from error
        if render_is_reusable:
            rendered_fonts[page.page_number] = tuple(
                json.loads(str(checkpoint["render_fonts_json"]))
            )
            _checkpoint(settings, job, "rendering", completed, total_pages)
            continue
        try:
            (render,) = render_standard_pages(source, toolchain=toolchain, pages=(page,))
        except Exception as error:
            raise _stage_error("rendering", page.page_number, error) from error
        try:
            stored_render = store.put(render.data)
        except Exception as error:
            raise _stage_error("storage", page.page_number, error) from error
        now = timestamp()
        with transaction(settings) as connection:
            _assert_job_lease(connection, settings, job)
            _record_object(connection, stored_render, render.media_type, now)
            connection.execute(
                """
                UPDATE ingestion_page_results
                SET render_sha256 = ?, render_media_type = ?, render_dpi = ?,
                    render_width_px = ?, render_height_px = ?, render_key = ?,
                    render_fonts_json = ?
                WHERE version_id = ? AND page_number = ?
                """,
                (
                    stored_render.sha256,
                    render.media_type,
                    render.dpi,
                    render.width_px,
                    render.height_px,
                    render_key,
                    _json(list(render.font_families)),
                    version_id,
                    render.page_number,
                ),
            )
        rendered_fonts[page.page_number] = render.font_families
        _checkpoint(settings, job, "rendering", completed, total_pages)

    try:
        final_warnings = tuple(
            warning
            for warning in audit_rendering_warnings(
                source, toolchain, rendered_fonts=rendered_fonts
            )
            if warning.page_number in {page.page_number for page in enabled_pages}
        )
    except Exception as error:
        raise _stage_error("rendering", None, error) from error
    with transaction(settings) as connection:
        _assert_job_lease(connection, settings, job)
        replace_active_warnings(
            connection,
            version_id=version_id,
            render_config_version=render_config_version,
            warnings=final_warnings,
            page_numbers=tuple(page.page_number for page in enabled_pages),
            observed_at=timestamp(),
        )
        connection.execute(
            """
            UPDATE document_versions
            SET render_config_version = ?, render_generation = ?
            WHERE version_id = ?
            """,
            (render_config_version, settings.render_generation, version_id),
        )

    _checkpoint(settings, job, "page_fingerprint", 0, total_pages)
    for completed, page in enumerate(enabled_pages, start=1):
        fingerprint_key = _stage_key(
            phase="page_fingerprint",
            source_sha256=source_sha256,
            version_id=version_id,
            page=page,
            tool_version=f"fingerprint:{FINGERPRINT_VERSION}",
            dependency_key=_stage_key(
                phase="conversion",
                source_sha256=source_sha256,
                version_id=version_id,
                page=page,
                tool_version=CONVERSION_TOOL_VERSION,
            ),
        )
        with connect(settings) as connection:
            checkpoint = connection.execute(
                """
                SELECT fingerprint_key, fingerprint_version, fingerprint_sha256
                FROM ingestion_page_results
                WHERE version_id = ? AND page_number = ?
                """,
                (version_id, page.page_number),
            ).fetchone()
        if not (
            checkpoint is not None
            and checkpoint["fingerprint_key"] == fingerprint_key
            and checkpoint["fingerprint_version"] is not None
            and checkpoint["fingerprint_sha256"] is not None
        ):
            try:
                fingerprint = fingerprint_page(converted[page.page_number])
            except Exception as error:
                raise _stage_error("page_fingerprint", page.page_number, error) from error
            with transaction(settings) as connection:
                _assert_job_lease(connection, settings, job)
                connection.execute(
                    """
                    UPDATE ingestion_page_results
                    SET fingerprint_version = ?, fingerprint_sha256 = ?, fingerprint_key = ?
                    WHERE version_id = ? AND page_number = ?
                    """,
                    (
                        fingerprint.version,
                        fingerprint.sha256,
                        fingerprint_key,
                        version_id,
                        page.page_number,
                    ),
                )
        _checkpoint(settings, job, "page_fingerprint", completed, total_pages)

    _checkpoint(settings, job, "page_mapping", total_pages, total_pages)
    _activate_first_version(
        settings,
        job,
        document_id,
        version_id,
        total_pages,
        enabled_pages=len(enabled_pages),
    )


def enqueue_stale_render_jobs(settings: Settings) -> int:
    """为当前 ready 版本持久化渲染配置迁移任务，并立即废止旧警告。"""
    current_config = render_configuration_version(settings.render_image)
    now = timestamp()
    enqueued = 0
    with transaction(settings) as connection:
        versions = connection.execute(
            """
            SELECT versions.document_id, versions.version_id, versions.source_sha256,
                   versions.render_generation
            FROM documents
            JOIN document_versions AS versions
              ON versions.version_id = documents.current_version_id
            WHERE documents.deleted_at IS NULL AND versions.status = 'ready'
              AND COALESCE(versions.render_config_version, '') <> ?
              AND COALESCE(versions.render_generation, 0) < ?
            ORDER BY versions.ready_at, versions.version_id
            """,
            (current_config, settings.render_generation),
        ).fetchall()
        for version in versions:
            idempotency_key = f"render-config:{version['version_id']}:{current_config}"
            existing = connection.execute(
                "SELECT 1 FROM jobs WHERE actor_id = 'system' AND idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                continue
            job_id = uuid.uuid4().hex
            payload = {
                "document_id": str(version["document_id"]),
                "version_id": str(version["version_id"]),
                "source_sha256": str(version["source_sha256"]),
                "render_config_version": current_config,
                "render_generation": settings.render_generation,
            }
            newer_generation = connection.execute(
                """
                SELECT 1 FROM jobs
                WHERE kind = 'version.rerender' AND version_id = ?
                  AND CAST(json_extract(payload_json, '$.render_generation') AS INTEGER)
                      > ?
                LIMIT 1
                """,
                (version["version_id"], settings.render_generation),
            ).fetchone()
            if newer_generation is not None:
                continue
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, kind, payload_json, status, actor_id, idempotency_key,
                    document_id, version_id, checkpoint_json, created_at, updated_at
                ) VALUES (?, 'version.rerender', ?, 'queued', 'system', ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    _json(payload),
                    idempotency_key,
                    version["document_id"],
                    version["version_id"],
                    _json({"phase": "rendering_audit", "completed_pages": 0, "total_pages": 0}),
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE rendering_warnings SET active = 0 WHERE version_id = ? AND active = 1",
                (version["version_id"],),
            )
            enqueued += 1
    return enqueued


def process_rerender_job(settings: Settings, job: ClaimedJob) -> None:
    """仅重建 ready 版本的渲染/字体证据，不触碰转换、指纹或审核继承。"""
    version_id = str(job.payload["version_id"])
    document_id = str(job.payload["document_id"])
    source_sha256 = str(job.payload["source_sha256"])
    expected_config = str(job.payload["render_config_version"])
    render_generation = int(job.payload["render_generation"])
    current_config = render_configuration_version(settings.render_image)
    if (
        expected_config != current_config
        or render_generation != settings.render_generation
    ):
        raise RuntimeError("渲染配置已再次变化，旧的重建任务不能继续执行")

    store = LocalObjectStore(settings.object_store_path)
    try:
        source = store.path_for(source_sha256).read_bytes()
        manifest = list_source_pages(source)
    except Exception as error:
        raise _stage_error("source_manifest", None, error) from error
    with connect(settings) as connection:
        version = connection.execute(
            """
            SELECT 1
            FROM documents
            JOIN document_versions AS versions
              ON versions.version_id = documents.current_version_id
            WHERE documents.document_id = ? AND versions.version_id = ?
              AND documents.deleted_at IS NULL AND versions.status = 'ready'
            """,
            (document_id, version_id),
        ).fetchone()
        enabled_numbers = {
            int(row["page_number"])
            for row in connection.execute(
                "SELECT page_number FROM ingestion_page_results "
                "WHERE version_id = ? AND enabled = 1",
                (version_id,),
            )
        }
    if version is None:
        return
    pages = tuple(page for page in manifest if page.page_number in enabled_numbers)
    total_pages = len(pages)
    toolchain = DockerRenderingToolchain(settings.render_image)

    _checkpoint(settings, job, "rendering_audit", 0, total_pages)
    try:
        preliminary = tuple(
            warning
            for warning in audit_rendering_warnings(source, toolchain)
            if warning.page_number in enabled_numbers
        )
    except Exception as error:
        raise _stage_error("rendering", None, error) from error
    with transaction(settings) as connection:
        _assert_job_lease(connection, settings, job)
        _assert_latest_render_generation(
            connection,
            job=job,
            render_generation=render_generation,
        )
        replace_active_warnings(
            connection,
            version_id=version_id,
            render_config_version=current_config,
            warnings=preliminary,
            page_numbers=tuple(sorted(enabled_numbers)),
            observed_at=timestamp(),
        )
    _checkpoint(settings, job, "rendering_audit", total_pages, total_pages)

    rendered_fonts: dict[int, tuple[str, ...]] = {}
    staged_renders: list[tuple[SourcePage, StandardPageRender, StoredObject, str]] = []
    _checkpoint(settings, job, "rendering", 0, total_pages)
    for completed, page in enumerate(pages, start=1):
        try:
            (render,) = render_standard_pages(source, toolchain=toolchain, pages=(page,))
            stored_render = store.put(render.data)
        except Exception as error:
            raise _stage_error("rendering", page.page_number, error) from error
        render_key = _stage_key(
            phase="rendering",
            source_sha256=source_sha256,
            version_id=version_id,
            page=page,
            tool_version=current_config,
        )
        staged_renders.append((page, render, stored_render, render_key))
        rendered_fonts[page.page_number] = render.font_families
        _checkpoint(settings, job, "rendering", completed, total_pages)

    try:
        final_warnings = tuple(
            warning
            for warning in audit_rendering_warnings(
                source, toolchain, rendered_fonts=rendered_fonts
            )
            if warning.page_number in enabled_numbers
        )
    except Exception as error:
        raise _stage_error("rendering", None, error) from error
    with transaction(settings) as connection:
        _assert_job_lease(connection, settings, job)
        _assert_latest_render_generation(
            connection,
            job=job,
            render_generation=render_generation,
        )
        now = timestamp()
        for page, render, stored_render, render_key in staged_renders:
            _record_object(connection, stored_render, render.media_type, now)
            connection.execute(
                """
                UPDATE ingestion_page_results
                SET render_sha256 = ?, render_media_type = ?, render_dpi = ?,
                    render_width_px = ?, render_height_px = ?, render_key = ?,
                    render_fonts_json = ?
                WHERE version_id = ? AND page_number = ? AND enabled = 1
                """,
                (
                    stored_render.sha256,
                    render.media_type,
                    render.dpi,
                    render.width_px,
                    render.height_px,
                    render_key,
                    _json(list(render.font_families)),
                    version_id,
                    page.page_number,
                ),
            )
            connection.execute(
                """
                UPDATE page_versions
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
                    page.page_number,
                ),
            )
        replace_active_warnings(
            connection,
            version_id=version_id,
            render_config_version=current_config,
            warnings=final_warnings,
            page_numbers=tuple(sorted(enabled_numbers)),
            observed_at=timestamp(),
        )
        updated_version = connection.execute(
            """
            UPDATE document_versions
            SET render_config_version = ?, render_generation = ?
            WHERE version_id = ?
              AND COALESCE(render_generation, 0) <= ?
            """,
            (current_config, render_generation, version_id, render_generation),
        )
        if updated_version.rowcount != 1:
            raise RuntimeError("渲染配置迁移已被更新代次取代")


def _assert_latest_render_generation(
    connection: Any,
    *,
    job: ClaimedJob,
    render_generation: int,
) -> None:
    version = connection.execute(
        "SELECT render_generation FROM document_versions WHERE version_id = ?",
        (str(job.payload["version_id"]),),
    ).fetchone()
    if version is None or int(version["render_generation"] or 0) > render_generation:
        raise RuntimeError("渲染配置迁移已被更新代次取代")
    superseding = connection.execute(
        """
        SELECT 1
        FROM jobs AS candidate
        JOIN jobs AS current ON current.job_id = ?
        WHERE candidate.kind = 'version.rerender'
          AND candidate.version_id = current.version_id
          AND candidate.job_id <> current.job_id
          AND (
            CAST(json_extract(candidate.payload_json, '$.render_generation') AS INTEGER)
                > ?
            OR (
              CAST(json_extract(candidate.payload_json, '$.render_generation') AS INTEGER)
                  = ?
              AND candidate.created_at > current.created_at
            )
          )
        LIMIT 1
        """,
        (job.job_id, render_generation, render_generation),
    ).fetchone()
    if superseding is not None:
        raise RuntimeError("渲染配置迁移已被更新代次取代")


def accept_hidden_page_enablement(
    settings: Settings,
    *,
    actor_id: str,
    document_id: str,
    version_id: str,
    page_number: int,
    idempotency_key: str,
) -> AcceptedPageEnablement:
    """接受一个隐藏页启用命令，并在并发会话间合并同一源页任务。"""
    if not idempotency_key or len(idempotency_key) > 200:
        raise IngestionRequestError(400, "invalid_idempotency_key", "缺少有效的幂等键。")
    command_scope = (
        f"POST /api/v1/documents/{document_id}/versions/{version_id}"
        f"/source-pages/{page_number}/enable"
    )
    request_fingerprint = hashlib.sha256(command_scope.encode("utf-8")).hexdigest()
    now = timestamp()
    with transaction(settings) as connection:
        target = connection.execute(
            """
            SELECT results.hidden, results.enabled, results.enable_job_id,
                   versions.source_sha256, versions.status AS version_status,
                   versions.render_config_version, versions.render_generation,
                   documents.current_version_id, documents.deleted_at
            FROM ingestion_page_results AS results
            JOIN document_versions AS versions
              ON versions.version_id = results.version_id
             AND versions.document_id = ?
            JOIN documents ON documents.document_id = versions.document_id
            WHERE results.version_id = ? AND results.page_number = ?
            """,
            (document_id, version_id, page_number),
        ).fetchone()
        if target is None:
            raise IngestionRequestError(404, "not_found", "未找到请求的源页登记。")
        if target["deleted_at"] is not None:
            raise IngestionRequestError(409, "document_deleted", "软删文档不能启用隐藏页。")
        if target["version_status"] != "ready" or target["current_version_id"] != version_id:
            raise IngestionRequestError(
                409, "version_not_current", "只能启用当前 ready 版本的隐藏页。"
            )
        current_render_config = render_configuration_version(settings.render_image)
        if (
            target["render_config_version"] != current_render_config
            or int(target["render_generation"] or 0) != settings.render_generation
        ):
            raise IngestionRequestError(
                409,
                "render_configuration_stale",
                "当前版本正在等待匹配本服务代次的渲染重建。",
            )
        if not bool(target["hidden"]):
            raise IngestionRequestError(409, "page_not_hidden", "该源页不是隐藏页。")

        replay = connection.execute(
            """
            SELECT request_fingerprint, response_status, job_id
            FROM idempotency_records
            WHERE actor_id = ? AND command_scope = ? AND idempotency_key = ?
            """,
            (actor_id, command_scope, idempotency_key),
        ).fetchone()
        page_version = connection.execute(
            """
            SELECT page_id FROM page_versions
            WHERE version_id = ? AND page_number = ?
            """,
            (version_id, page_number),
        ).fetchone()
        if replay is not None:
            if replay["request_fingerprint"] != request_fingerprint:
                _raise_idempotency_conflict()
            return AcceptedPageEnablement(
                document_id=document_id,
                version_id=version_id,
                page_number=page_number,
                job_id=replay["job_id"],
                status=str(replay["response_status"]),
                page_id=None if page_version is None else str(page_version["page_id"]),
            )

        if bool(target["enabled"]) or page_version is not None:
            accepted = AcceptedPageEnablement(
                document_id=document_id,
                version_id=version_id,
                page_number=page_number,
                job_id=target["enable_job_id"],
                status="no_change",
                page_id=None if page_version is None else str(page_version["page_id"]),
            )
            _record_page_enablement_idempotency(
                connection,
                accepted=accepted,
                actor_id=actor_id,
                command_scope=command_scope,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                now=now,
            )
            return accepted

        active_job = None
        if target["enable_job_id"] is not None:
            active_job = connection.execute(
                "SELECT job_id, status, payload_json FROM jobs WHERE job_id = ?",
                (target["enable_job_id"],),
            ).fetchone()
        if active_job is not None and active_job["status"] in {"queued", "running"}:
            active_payload = json.loads(str(active_job["payload_json"]))
            active_matches = (
                active_payload.get("render_config_version") == current_render_config
                and int(active_payload.get("render_generation", 0))
                == settings.render_generation
            )
            if active_matches:
                accepted = AcceptedPageEnablement(
                    document_id=document_id,
                    version_id=version_id,
                    page_number=page_number,
                    job_id=str(active_job["job_id"]),
                    status="coalesced",
                )
                _record_page_enablement_idempotency(
                    connection,
                    accepted=accepted,
                    actor_id=actor_id,
                    command_scope=command_scope,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    now=now,
                )
                return accepted
            connection.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, next_attempt_at = NULL, updated_at = ?
                WHERE job_id = ? AND status IN ('queued', 'running')
                """,
                (now, active_job["job_id"]),
            )

        job_id = uuid.uuid4().hex
        payload = {
            "document_id": document_id,
            "version_id": version_id,
            "page_number": page_number,
            "source_sha256": str(target["source_sha256"]),
            "render_config_version": current_render_config,
            "render_generation": settings.render_generation,
        }
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, kind, payload_json, status, actor_id, idempotency_key,
                document_id, version_id, checkpoint_json, created_at, updated_at
            ) VALUES (?, 'page.enable', ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                _json(payload),
                actor_id,
                _job_idempotency_key(command_scope, idempotency_key),
                document_id,
                version_id,
                _json({"phase": "queued", "completed_pages": 0, "total_pages": 1}),
                now,
                now,
            ),
        )
        connection.execute(
            """
            UPDATE ingestion_page_results SET enable_job_id = ?
            WHERE version_id = ? AND page_number = ? AND enabled = 0
            """,
            (job_id, version_id, page_number),
        )
        accepted = AcceptedPageEnablement(
            document_id=document_id,
            version_id=version_id,
            page_number=page_number,
            job_id=job_id,
            status="accepted",
        )
        _record_page_enablement_idempotency(
            connection,
            accepted=accepted,
            actor_id=actor_id,
            command_scope=command_scope,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            now=now,
        )
        return accepted


def _record_page_enablement_idempotency(
    connection: Any,
    *,
    accepted: AcceptedPageEnablement,
    actor_id: str,
    command_scope: str,
    idempotency_key: str,
    request_fingerprint: str,
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


def process_hidden_page_job(settings: Settings, job: ClaimedJob) -> None:
    """处理一个已登记隐藏页，并在最后一个事务中公开页身份与审核状态。"""
    version_id = str(job.payload["version_id"])
    document_id = str(job.payload["document_id"])
    page_number = int(job.payload["page_number"])
    source_sha256 = str(job.payload["source_sha256"])
    expected_render_config = str(
        job.payload.get(
            "render_config_version",
            render_configuration_version(settings.render_image),
        )
    )
    expected_render_generation = int(
        job.payload.get("render_generation", settings.render_generation)
    )
    if (
        expected_render_config != render_configuration_version(settings.render_image)
        or expected_render_generation != settings.render_generation
    ):
        raise RuntimeError("隐藏页启用任务不属于当前渲染配置代次")
    store = LocalObjectStore(settings.object_store_path)
    try:
        source = store.path_for(source_sha256).read_bytes()
        page = next(
            candidate
            for candidate in list_source_pages(source)
            if candidate.page_number == page_number
        )
    except Exception as error:
        raise _stage_error("source_manifest", page_number, error) from error
    if not page.hidden:
        raise _stage_error("source_manifest", page_number, ValueError("源页不再隐藏"))

    _checkpoint(settings, job, "conversion", 0, 1)
    conversion_key = _stage_key(
        phase="conversion",
        source_sha256=source_sha256,
        version_id=version_id,
        page=page,
        tool_version=CONVERSION_TOOL_VERSION,
    )
    with connect(settings) as connection:
        checkpoint = connection.execute(
            """
            SELECT conversion_key, source_content_json
            FROM ingestion_page_results
            WHERE version_id = ? AND page_number = ? AND enable_job_id = ?
            """,
            (version_id, page_number, job.job_id),
        ).fetchone()
    if checkpoint is None:
        raise RuntimeError("隐藏页启用任务不再是该源页的当前任务")
    if (
        checkpoint["conversion_key"] == conversion_key
        and checkpoint["source_content_json"] is not None
    ):
        content = _content_from_json(str(checkpoint["source_content_json"]))
    else:
        try:
            content = convert_page(source, page)
        except Exception as error:
            raise _stage_error("conversion", page_number, error) from error
        with transaction(settings) as connection:
            _assert_job_lease(connection, settings, job)
            connection.execute(
                """
                UPDATE ingestion_page_results
                SET source_content_json = ?, conversion_key = ?
                WHERE version_id = ? AND page_number = ? AND enable_job_id = ?
                """,
                (_content_json(content), conversion_key, version_id, page_number, job.job_id),
            )
    _checkpoint(settings, job, "conversion", 1, 1)

    toolchain = DockerRenderingToolchain(settings.render_image)
    render_config_version = render_configuration_version(settings.render_image)
    _checkpoint(settings, job, "rendering_audit", 0, 1)
    try:
        audited_warnings = tuple(
            warning
            for warning in audit_rendering_warnings(source, toolchain)
            if warning.page_number == page_number
        )
    except Exception as error:
        raise _stage_error("rendering", page_number, error) from error
    with transaction(settings) as connection:
        _assert_job_lease(connection, settings, job)
        _assert_hidden_page_render_generation(connection, job)
        replace_active_warnings(
            connection,
            version_id=version_id,
            render_config_version=render_config_version,
            warnings=audited_warnings,
            page_numbers=(page_number,),
            observed_at=timestamp(),
        )
    _checkpoint(settings, job, "rendering_audit", 1, 1)

    _checkpoint(settings, job, "rendering", 0, 1)
    render_key = _stage_key(
        phase="rendering",
        source_sha256=source_sha256,
        version_id=version_id,
        page=page,
        tool_version=render_config_version,
    )
    with connect(settings) as connection:
        render_checkpoint = connection.execute(
            """
            SELECT render_key, render_sha256, render_media_type, render_dpi,
                   render_width_px, render_height_px, render_fonts_json
            FROM ingestion_page_results
            WHERE version_id = ? AND page_number = ? AND enable_job_id = ?
            """,
            (version_id, page_number, job.job_id),
        ).fetchone()
    render_is_reusable = False
    if render_checkpoint is not None:
        try:
            render_is_reusable = (
                render_checkpoint["render_key"] == render_key
                and render_checkpoint["render_sha256"] is not None
                and render_checkpoint["render_media_type"] is not None
                and render_checkpoint["render_dpi"] is not None
                and render_checkpoint["render_width_px"] is not None
                and render_checkpoint["render_height_px"] is not None
                and render_checkpoint["render_fonts_json"] is not None
                and store.verify(str(render_checkpoint["render_sha256"]))
            )
        except OSError as error:
            raise _stage_error("storage", page_number, error) from error
    if not render_is_reusable:
        try:
            (render,) = render_standard_pages(
                source,
                toolchain=toolchain,
                pages=(page,),
            )
        except Exception as error:
            raise _stage_error("rendering", page_number, error) from error
        try:
            stored_render = store.put(render.data)
        except Exception as error:
            raise _stage_error("storage", page_number, error) from error
        now = timestamp()
        with transaction(settings) as connection:
            _assert_job_lease(connection, settings, job)
            _assert_hidden_page_render_generation(connection, job)
            _record_object(connection, stored_render, render.media_type, now)
            connection.execute(
                """
                UPDATE ingestion_page_results
                SET render_sha256 = ?, render_media_type = ?, render_dpi = ?,
                    render_width_px = ?, render_height_px = ?, render_key = ?,
                    render_fonts_json = ?
                WHERE version_id = ? AND page_number = ? AND enable_job_id = ?
                """,
                (
                    stored_render.sha256,
                    render.media_type,
                    render.dpi,
                    render.width_px,
                    render.height_px,
                    render_key,
                    _json(list(render.font_families)),
                    version_id,
                    page_number,
                    job.job_id,
                ),
            )
        rendered_page_fonts = render.font_families
    else:
        assert render_checkpoint is not None
        rendered_page_fonts = tuple(
            json.loads(str(render_checkpoint["render_fonts_json"]))
        )
    _checkpoint(settings, job, "rendering", 1, 1)

    try:
        final_warnings = tuple(
            warning
            for warning in audit_rendering_warnings(
                source,
                toolchain,
                rendered_fonts={page_number: rendered_page_fonts},
            )
            if warning.page_number == page_number
        )
    except Exception as error:
        raise _stage_error("rendering", page_number, error) from error
    with transaction(settings) as connection:
        _assert_job_lease(connection, settings, job)
        _assert_hidden_page_render_generation(connection, job)
        replace_active_warnings(
            connection,
            version_id=version_id,
            render_config_version=render_config_version,
            warnings=final_warnings,
            page_numbers=(page_number,),
            observed_at=timestamp(),
        )

    _checkpoint(settings, job, "page_fingerprint", 0, 1)
    fingerprint_key = _stage_key(
        phase="page_fingerprint",
        source_sha256=source_sha256,
        version_id=version_id,
        page=page,
        tool_version=f"fingerprint:{FINGERPRINT_VERSION}",
        dependency_key=conversion_key,
    )
    with connect(settings) as connection:
        fingerprint_checkpoint = connection.execute(
            """
            SELECT fingerprint_key, fingerprint_version, fingerprint_sha256
            FROM ingestion_page_results
            WHERE version_id = ? AND page_number = ? AND enable_job_id = ?
            """,
            (version_id, page_number, job.job_id),
        ).fetchone()
    if not (
        fingerprint_checkpoint is not None
        and fingerprint_checkpoint["fingerprint_key"] == fingerprint_key
        and fingerprint_checkpoint["fingerprint_version"] is not None
        and fingerprint_checkpoint["fingerprint_sha256"] is not None
    ):
        try:
            fingerprint = fingerprint_page(content)
        except Exception as error:
            raise _stage_error("page_fingerprint", page_number, error) from error
        with transaction(settings) as connection:
            _assert_job_lease(connection, settings, job)
            connection.execute(
                """
                UPDATE ingestion_page_results
                SET fingerprint_version = ?, fingerprint_sha256 = ?, fingerprint_key = ?
                WHERE version_id = ? AND page_number = ? AND enable_job_id = ?
                """,
                (
                    fingerprint.version,
                    fingerprint.sha256,
                    fingerprint_key,
                    version_id,
                    page_number,
                    job.job_id,
                ),
            )
    _checkpoint(settings, job, "page_fingerprint", 1, 1)
    _activate_hidden_page(settings, job, document_id, version_id, page_number)


def _activate_hidden_page(
    settings: Settings,
    job: ClaimedJob,
    document_id: str,
    version_id: str,
    page_number: int,
) -> None:
    now = timestamp()
    with transaction(settings) as connection:
        _assert_job_lease(connection, settings, job)
        _assert_hidden_page_render_generation(connection, job)
        row = connection.execute(
            """
            SELECT results.*, versions.status AS version_status,
                   documents.current_version_id, documents.deleted_at
            FROM ingestion_page_results AS results
            JOIN document_versions AS versions ON versions.version_id = results.version_id
            JOIN documents ON documents.document_id = versions.document_id
            WHERE results.version_id = ? AND results.page_number = ?
              AND results.enable_job_id = ?
            """,
            (version_id, page_number, job.job_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("隐藏页启用任务不再对应源页登记")
        if (
            row["version_status"] != "ready"
            or row["current_version_id"] != version_id
            or row["deleted_at"] is not None
        ):
            raise RuntimeError("隐藏页所属版本不再可策展")
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
            raise RuntimeError("隐藏页处理结果不完整")
        existing = connection.execute(
            "SELECT page_id FROM page_versions WHERE version_id = ? AND page_number = ?",
            (version_id, page_number),
        ).fetchone()
        if existing is None:
            identity = _match_page_identities(
                connection,
                document_id=document_id,
                current_version_id=_previous_ready_version_id(
                    connection,
                    document_id=document_id,
                    target_version_id=version_id,
                ),
                target_version_id=version_id,
                incoming_rows=[row],
            ).get(page_number)
            page_id = uuid.uuid4().hex if identity is None else identity[0]
            if identity is None:
                connection.execute(
                    """
                    INSERT INTO pages (page_id, document_id, chunk_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (page_id, document_id, uuid.uuid4().hex, now),
                )
            else:
                connection.execute(
                    """
                    UPDATE pages
                    SET deleted_at = NULL, deleted_in_version_id = NULL
                    WHERE page_id = ? AND document_id = ?
                    """,
                    (page_id, document_id),
                )
            page_version_id = uuid.uuid4().hex
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
                    page_number,
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
            _materialize_review_inheritance(
                connection,
                page_version_id=page_version_id,
                page_id=page_id,
                fingerprint_version=int(row["fingerprint_version"]),
                fingerprint_sha256=str(row["fingerprint_sha256"]),
                now=now,
            )
        connection.execute(
            """
            UPDATE ingestion_page_results SET enabled = 1
            WHERE version_id = ? AND page_number = ? AND enable_job_id = ?
            """,
            (version_id, page_number, job.job_id),
        )
        completed_at = timestamp()
        completed = connection.execute(
            """
            UPDATE jobs
            SET status = 'succeeded', lease_owner = NULL, lease_token = NULL,
                lease_expires_at = NULL, next_attempt_at = NULL,
                checkpoint_json = ?, error_json = NULL, updated_at = ?
            WHERE job_id = ? AND status = 'running'
              AND lease_owner = ? AND lease_token = ? AND lease_expires_at > ?
            """,
            (
                _json({"phase": "activation", "completed_pages": 1, "total_pages": 1}),
                completed_at,
                job.job_id,
                settings.worker_id,
                job.lease_token,
                completed_at,
            ),
        )
        if completed.rowcount != 1:
            raise RuntimeError("worker 在隐藏页生效前失去任务租约")


def _assert_hidden_page_render_generation(connection: Any, job: ClaimedJob) -> None:
    version = connection.execute(
        """
        SELECT versions.render_config_version, versions.render_generation,
               versions.status, documents.current_version_id, documents.deleted_at
        FROM document_versions AS versions
        JOIN documents ON documents.document_id = versions.document_id
        WHERE versions.version_id = ? AND versions.document_id = ?
        """,
        (str(job.payload["version_id"]), str(job.payload["document_id"])),
    ).fetchone()
    if (
        version is None
        or version["status"] != "ready"
        or version["current_version_id"] != job.payload["version_id"]
        or version["deleted_at"] is not None
        or version["render_config_version"] != job.payload.get("render_config_version")
        or int(version["render_generation"] or 0)
        != int(job.payload.get("render_generation", 0))
    ):
        raise RuntimeError("隐藏页启用任务的渲染配置代次已失效")


def fail_hidden_page_job(settings: Settings, job: ClaimedJob, error: Exception) -> None:
    now = timestamp()
    structured = _structured_error(error, attempt=job.attempts)
    retryable = isinstance(error, IngestionStageError) and error.retryable
    will_retry = retryable and job.attempts < job.max_attempts
    with transaction(settings) as connection:
        if will_retry:
            retry_at = (
                datetime.now(UTC)
                + timedelta(
                    seconds=settings.job_retry_base_seconds * (2 ** max(job.attempts - 1, 0))
                )
            ).isoformat()
            connection.execute(
                """
                UPDATE jobs
                SET status = 'queued', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, next_attempt_at = ?, error_json = ?, updated_at = ?
                WHERE job_id = ? AND status = 'running'
                  AND lease_owner = ? AND lease_token = ? AND lease_expires_at > ?
                """,
                (
                    retry_at,
                    _json(structured),
                    now,
                    job.job_id,
                    settings.worker_id,
                    job.lease_token,
                    now,
                ),
            )
            return
        failed = connection.execute(
            """
            UPDATE jobs
            SET status = 'failed', lease_owner = NULL, lease_token = NULL,
                lease_expires_at = NULL, next_attempt_at = NULL,
                error_json = ?, updated_at = ?
            WHERE job_id = ? AND status = 'running'
              AND lease_owner = ? AND lease_token = ? AND lease_expires_at > ?
            """,
            (
                _json(structured),
                now,
                job.job_id,
                settings.worker_id,
                job.lease_token,
                now,
            ),
        )
        if failed.rowcount == 1:
            connection.execute(
                """
                UPDATE ingestion_page_results
                SET enabled = 0, source_content_json = NULL, conversion_key = NULL,
                    fingerprint_version = NULL, fingerprint_sha256 = NULL,
                    fingerprint_key = NULL, render_sha256 = NULL,
                    render_media_type = NULL, render_dpi = NULL,
                    render_width_px = NULL, render_height_px = NULL, render_key = NULL
                WHERE version_id = ? AND page_number = ? AND enable_job_id = ?
                """,
                (str(job.payload["version_id"]), int(job.payload["page_number"]), job.job_id),
            )


def fail_rerender_job(settings: Settings, job: ClaimedJob, error: Exception) -> None:
    """保留旧完整资产并按既有退避策略重试渲染配置迁移。"""
    now = timestamp()
    structured = _structured_error(error, attempt=job.attempts)
    retryable = isinstance(error, IngestionStageError) and error.retryable
    will_retry = retryable and job.attempts < job.max_attempts
    retry_at = (
        datetime.now(UTC)
        + timedelta(seconds=settings.job_retry_base_seconds * (2 ** max(job.attempts - 1, 0)))
    ).isoformat()
    with transaction(settings) as connection:
        connection.execute(
            """
            UPDATE jobs
            SET status = ?, lease_owner = NULL, lease_token = NULL,
                lease_expires_at = NULL, next_attempt_at = ?, error_json = ?, updated_at = ?
            WHERE job_id = ? AND status = 'running'
              AND lease_owner = ? AND lease_token = ? AND lease_expires_at > ?
            """,
            (
                "queued" if will_retry else "failed",
                retry_at if will_retry else None,
                _json(structured),
                now,
                job.job_id,
                settings.worker_id,
                job.lease_token,
                now,
            ),
        )


def fail_ingestion_job(settings: Settings, job: ClaimedJob, error: Exception) -> None:
    now = timestamp()
    structured = _structured_error(error, attempt=job.attempts)
    retryable = isinstance(error, IngestionStageError) and error.retryable
    will_retry = retryable and job.attempts < job.max_attempts
    if will_retry:
        retry_at = (
            datetime.now(UTC)
            + timedelta(
                seconds=settings.job_retry_base_seconds * (2 ** max(job.attempts - 1, 0))
            )
        ).isoformat()
        with transaction(settings) as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'queued', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, next_attempt_at = ?, error_json = ?, updated_at = ?
                WHERE job_id = ? AND status = 'running'
                  AND lease_owner = ? AND lease_token = ? AND lease_expires_at > ?
                """,
                (
                    retry_at,
                    _json(structured),
                    now,
                    job.job_id,
                    settings.worker_id,
                    job.lease_token,
                    now,
                ),
            )
        return
    with transaction(settings) as connection:
        failed_job = connection.execute(
            """
            UPDATE jobs
            SET status = 'failed', lease_owner = NULL, lease_token = NULL,
                lease_expires_at = NULL, next_attempt_at = NULL,
                error_json = ?, updated_at = ?
            WHERE job_id = ? AND status = 'running'
              AND lease_owner = ? AND lease_token = ? AND lease_expires_at > ?
            """,
            (
                _json(structured),
                now,
                job.job_id,
                settings.worker_id,
                job.lease_token,
                now,
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
    *,
    enabled_pages: int,
) -> None:
    now = timestamp()
    with transaction(settings) as connection:
        _assert_job_lease(connection, settings, job)
        rows = connection.execute(
            """
            SELECT * FROM ingestion_page_results
            WHERE version_id = ? AND enabled = 1
            ORDER BY page_number
            """,
            (version_id,),
        ).fetchall()
        if len(rows) != enabled_pages:
            raise RuntimeError("启用页的处理检查点数量不完整")
        plan = _build_page_mapping_plan(
            connection,
            document_id=document_id,
            current_version_id=_current_version_id(connection, document_id),
            target_version_id=version_id,
            incoming_rows=rows,
        )
        if plan.cases:
            for case in plan.cases:
                connection.execute(
                    """
                    INSERT INTO page_mapping_cases (
                        case_id, version_id, page_number, case_kind,
                        candidate_page_ids_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(version_id, page_number) DO NOTHING
                    """,
                    (
                        uuid.uuid4().hex,
                        version_id,
                        case.page_number,
                        case.kind,
                        _json(list(case.candidate_page_ids)),
                        now,
                    ),
                )
            awaiting = connection.execute(
                """
                UPDATE document_versions SET status = 'awaiting_mapping'
                WHERE version_id = ? AND status = 'processing'
                """,
                (version_id,),
            )
            if awaiting.rowcount != 1:
                raise RuntimeError("版本不再处于可暂停的 processing 状态")
            action_required = connection.execute(
                """
                UPDATE jobs
                SET status = 'requires_action', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, next_attempt_at = NULL,
                    checkpoint_json = ?, error_json = NULL, updated_at = ?
                WHERE job_id = ? AND status = 'running'
                  AND lease_owner = ? AND lease_token = ? AND lease_expires_at > ?
                """,
                (
                    _json(
                        {
                            "phase": "page_mapping",
                            "completed_pages": total_pages - len(plan.cases),
                            "total_pages": total_pages,
                        }
                    ),
                    now,
                    job.job_id,
                    settings.worker_id,
                    job.lease_token,
                    now,
                ),
            )
            if action_required.rowcount != 1:
                raise RuntimeError("worker 在暂停页对应前失去任务租约")
            return

        _materialize_page_versions(
            connection,
            document_id=document_id,
            version_id=version_id,
            rows=rows,
            identities=plan.automatic_matches,
            now=now,
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
        completed_at = timestamp()
        updated = connection.execute(
            """
            UPDATE jobs
            SET status = 'succeeded', lease_owner = NULL, lease_token = NULL,
                lease_expires_at = NULL, next_attempt_at = NULL,
                checkpoint_json = ?, error_json = NULL, updated_at = ?
            WHERE job_id = ? AND status = 'running'
              AND lease_owner = ? AND lease_token = ? AND lease_expires_at > ?
            """,
            (
                _json(
                    {
                        "phase": "activation",
                        "completed_pages": total_pages,
                        "total_pages": total_pages,
                    }
                ),
                completed_at,
                job.job_id,
                settings.worker_id,
                job.lease_token,
                completed_at,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("worker 在版本生效前失去任务租约")


def _materialize_page_versions(
    connection: Any,
    *,
    document_id: str,
    version_id: str,
    rows: list[Any],
    identities: dict[int, tuple[str, str]],
    now: str,
) -> dict[str, int]:
    summary = {
        "reused_unchanged": 0,
        "reused_changed": 0,
        "created_new": 0,
        "soft_deleted": 0,
    }
    active_before = {
        str(row["page_id"])
        for row in connection.execute(
            "SELECT page_id FROM pages WHERE document_id = ? AND deleted_at IS NULL",
            (document_id,),
        )
    }
    active_page_ids: set[str] = set()
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
        page_number = int(row["page_number"])
        identity = identities.get(page_number)
        page_id = uuid.uuid4().hex if identity is None else identity[0]
        page_version_id = uuid.uuid4().hex
        if identity is None:
            summary["created_new"] += 1
            connection.execute(
                """
                INSERT INTO pages (page_id, document_id, chunk_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (page_id, document_id, uuid.uuid4().hex, now),
            )
        else:
            previous = connection.execute(
                """
                SELECT fingerprint_version, fingerprint_sha256
                FROM page_versions AS pv
                JOIN document_versions AS versions ON versions.version_id = pv.version_id
                WHERE pv.page_id = ? AND versions.status = 'ready'
                ORDER BY versions.ready_at DESC, pv.created_at DESC
                LIMIT 1
                """,
                (page_id,),
            ).fetchone()
            unchanged = (
                previous is not None
                and int(previous["fingerprint_version"]) == int(row["fingerprint_version"])
                and str(previous["fingerprint_sha256"]) == str(row["fingerprint_sha256"])
            )
            summary["reused_unchanged" if unchanged else "reused_changed"] += 1
            connection.execute(
                """
                UPDATE pages
                SET deleted_at = NULL, deleted_in_version_id = NULL
                WHERE page_id = ? AND document_id = ?
                """,
                (page_id, document_id),
            )
        active_page_ids.add(page_id)
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
        _materialize_review_inheritance(
            connection,
            page_version_id=page_version_id,
            page_id=page_id,
            fingerprint_version=int(row["fingerprint_version"]),
            fingerprint_sha256=str(row["fingerprint_sha256"]),
            now=now,
        )
    summary["soft_deleted"] = len(active_before - active_page_ids)
    if active_page_ids:
        placeholders = ", ".join("?" for _ in active_page_ids)
        connection.execute(
            f"""
            UPDATE pages
            SET deleted_at = ?, deleted_in_version_id = ?
            WHERE document_id = ? AND page_id NOT IN ({placeholders})
              AND deleted_at IS NULL
            """,
            (now, version_id, document_id, *sorted(active_page_ids)),
        )
    else:
        connection.execute(
            """
            UPDATE pages
            SET deleted_at = ?, deleted_in_version_id = ?
            WHERE document_id = ? AND deleted_at IS NULL
            """,
            (now, version_id, document_id),
        )
    return summary


def _materialize_review_inheritance(
    connection: Any,
    *,
    page_version_id: str,
    page_id: str,
    fingerprint_version: int,
    fingerprint_sha256: str,
    now: str,
) -> None:
    same_content = connection.execute(
        """
        SELECT pv.*
        FROM page_versions AS pv
        JOIN document_versions AS versions ON versions.version_id = pv.version_id
        WHERE pv.page_id = ? AND pv.page_version_id <> ?
          AND pv.fingerprint_version = ? AND pv.fingerprint_sha256 = ?
          AND versions.status = 'ready'
        ORDER BY versions.ready_at DESC, pv.created_at DESC, pv.page_version_id DESC
        LIMIT 1
        """,
        (page_id, page_version_id, fingerprint_version, fingerprint_sha256),
    ).fetchone()
    if same_content is not None and same_content["review_status"] in {
        "approved",
        "excluded",
    }:
        snapshot_id = _clone_curation_snapshot(
            connection,
            page_id=page_id,
            page_version_id=page_version_id,
            source_snapshot_id=same_content["current_snapshot_id"],
            snapshot_kind="formal",
            preserve_visual_refs=True,
            now=now,
        )
        review_source_version_id = (
            same_content["review_source_version_id"] or same_content["version_id"]
        )
        connection.execute(
            """
            UPDATE page_versions
            SET review_status = ?, current_snapshot_id = ?,
                inherited_from_page_version_id = ?, reviewed_by = ?, reviewed_at = ?,
                review_source_version_id = ?, exclusion_reason = ?, exclusion_note = ?
            WHERE page_version_id = ?
            """,
            (
                same_content["review_status"],
                snapshot_id,
                same_content["page_version_id"],
                same_content["reviewed_by"],
                same_content["reviewed_at"],
                review_source_version_id,
                same_content["exclusion_reason"],
                same_content["exclusion_note"],
                page_version_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO page_review_events (
                event_id, page_version_id, event_type, actor_id, occurred_at,
                source_version_id, source_page_version_id, snapshot_id, reason, note
            ) VALUES (?, ?, 'inherited', NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                page_version_id,
                now,
                same_content["version_id"],
                same_content["page_version_id"],
                snapshot_id,
                same_content["exclusion_reason"],
                same_content["exclusion_note"],
            ),
        )
        return

    previous = connection.execute(
        """
        SELECT pv.*
        FROM page_versions AS pv
        JOIN document_versions AS versions ON versions.version_id = pv.version_id
        WHERE pv.page_id = ? AND pv.page_version_id <> ?
          AND pv.current_snapshot_id IS NOT NULL AND versions.status = 'ready'
        ORDER BY versions.ready_at DESC, pv.created_at DESC, pv.page_version_id DESC
        LIMIT 1
        """,
        (page_id, page_version_id),
    ).fetchone()
    if previous is None:
        return
    snapshot_id = _clone_curation_snapshot(
        connection,
        page_id=page_id,
        page_version_id=page_version_id,
        source_snapshot_id=previous["current_snapshot_id"],
        snapshot_kind="prefill",
        preserve_visual_refs=False,
        now=now,
    )
    connection.execute(
        "UPDATE page_versions SET prefill_snapshot_id = ? WHERE page_version_id = ?",
        (snapshot_id, page_version_id),
    )
    connection.execute(
        """
        INSERT INTO page_review_events (
            event_id, page_version_id, event_type, actor_id, occurred_at,
            source_version_id, source_page_version_id, snapshot_id, reason, note
        ) VALUES (?, ?, 'prefilled', NULL, ?, ?, ?, ?, NULL, NULL)
        """,
        (
            uuid.uuid4().hex,
            page_version_id,
            now,
            previous["version_id"],
            previous["page_version_id"],
            snapshot_id,
        ),
    )


def _clone_curation_snapshot(
    connection: Any,
    *,
    page_id: str,
    page_version_id: str,
    source_snapshot_id: str | None,
    snapshot_kind: str,
    preserve_visual_refs: bool,
    now: str,
) -> str | None:
    if source_snapshot_id is None:
        return None
    source = connection.execute(
        """
        SELECT overview, source_content_json, created_by
        FROM curation_snapshots WHERE snapshot_id = ?
        """,
        (source_snapshot_id,),
    ).fetchone()
    if source is None:
        raise RuntimeError("审核状态引用的策展快照不存在")
    snapshot_id = uuid.uuid4().hex
    connection.execute(
        """
        INSERT INTO curation_snapshots (
            snapshot_id, page_version_id, snapshot_kind, source_snapshot_id,
            overview, source_content_json, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            page_version_id,
            snapshot_kind,
            source_snapshot_id,
            source["overview"],
            source["source_content_json"],
            source["created_by"],
            now,
        ),
    )
    if preserve_visual_refs:
        confirmation = connection.execute(
            """
            SELECT actor_id, confirmed_at
            FROM curation_source_confirmations WHERE snapshot_id = ?
            """,
            (source_snapshot_id,),
        ).fetchone()
        if confirmation is not None:
            connection.execute(
                """
                INSERT INTO curation_source_confirmations (
                    confirmation_id, snapshot_id, actor_id, confirmed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    snapshot_id,
                    confirmation["actor_id"],
                    confirmation["confirmed_at"],
                ),
            )
        review = connection.execute(
            """
            SELECT actor_id, completed_at
            FROM curation_source_reviews WHERE snapshot_id = ?
            """,
            (source_snapshot_id,),
        ).fetchone()
        if review is not None:
            connection.execute(
                """
                INSERT INTO curation_source_reviews (
                    review_id, snapshot_id, actor_id, completed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    snapshot_id,
                    review["actor_id"],
                    review["completed_at"],
                ),
            )
    visuals = connection.execute(
        """
        SELECT * FROM curation_snapshot_visuals
        WHERE snapshot_id = ? ORDER BY position
        """,
        (source_snapshot_id,),
    ).fetchall()
    for visual in visuals:
        old_visual_ref = str(visual["visual_ref"])
        visual_ref = old_visual_ref if preserve_visual_refs else uuid.uuid4().hex
        if not preserve_visual_refs:
            connection.execute(
                """
                INSERT INTO visual_objects (visual_ref, page_id, created_at)
                VALUES (?, ?, ?)
                """,
                (visual_ref, page_id, now),
            )
        connection.execute(
            """
            INSERT INTO curation_snapshot_visuals (
                snapshot_id, visual_ref, position, source_kind, disposition,
                summary, visual_type, bounds_json, source_visual_ref, confirmed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                visual_ref,
                visual["position"],
                visual["source_kind"],
                visual["disposition"],
                visual["summary"],
                visual["visual_type"],
                visual["bounds_json"],
                None if preserve_visual_refs else old_visual_ref,
                visual["confirmed"] if preserve_visual_refs else 0,
            ),
        )
    return snapshot_id


def _current_version_id(connection: Any, document_id: str) -> str | None:
    row = connection.execute(
        "SELECT current_version_id FROM documents WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    if row is None or row["current_version_id"] is None:
        return None
    return str(row["current_version_id"])


def _previous_ready_version_id(
    connection: Any, *, document_id: str, target_version_id: str
) -> str | None:
    row = connection.execute(
        """
        SELECT version_id
        FROM document_versions
        WHERE document_id = ? AND version_id <> ? AND status = 'ready'
        ORDER BY ready_at DESC, created_at DESC, version_id DESC
        LIMIT 1
        """,
        (document_id, target_version_id),
    ).fetchone()
    if row is None:
        return None
    return str(row["version_id"])


def _match_page_identities(
    connection: Any,
    *,
    document_id: str,
    current_version_id: str | None,
    target_version_id: str,
    incoming_rows: list[Any],
) -> dict[int, tuple[str, str]]:
    """保守匹配可唯一证明的页；人工页对应由版本级工作面处理。"""
    return _build_page_mapping_plan(
        connection,
        document_id=document_id,
        current_version_id=current_version_id,
        target_version_id=target_version_id,
        incoming_rows=incoming_rows,
    ).automatic_matches


def _build_page_mapping_plan(
    connection: Any,
    *,
    document_id: str,
    current_version_id: str | None,
    target_version_id: str,
    incoming_rows: list[Any],
) -> PageMappingPlan:
    """把确定性对应与必须由人决定的歧义 case 分离。"""
    history = connection.execute(
        """
        SELECT pv.page_id, p.chunk_id, pv.fingerprint_version,
               pv.fingerprint_sha256, pv.version_id, pv.page_number,
               results.source_slide_id
        FROM page_versions AS pv
        JOIN pages AS p ON p.page_id = pv.page_id
        JOIN document_versions AS versions ON versions.version_id = pv.version_id
        JOIN ingestion_page_results AS results
          ON results.version_id = pv.version_id
         AND results.page_number = pv.page_number
        WHERE pv.document_id = ? AND versions.status = 'ready'
        ORDER BY versions.ready_at DESC, pv.created_at DESC, pv.page_version_id DESC
        """,
        (document_id,),
    ).fetchall()
    used_page_ids = {
        str(row["page_id"])
        for row in connection.execute(
            "SELECT page_id FROM page_versions WHERE version_id = ?",
            (target_version_id,),
        )
    }
    incoming_by_fingerprint: dict[tuple[int, str], list[Any]] = {}
    historical_by_fingerprint: dict[tuple[int, str], dict[str, tuple[str, str]]] = {}
    for row in incoming_rows:
        key = (int(row["fingerprint_version"]), str(row["fingerprint_sha256"]))
        incoming_by_fingerprint.setdefault(key, []).append(row)
    for row in history:
        if str(row["page_id"]) in used_page_ids:
            continue
        key = (int(row["fingerprint_version"]), str(row["fingerprint_sha256"]))
        historical_by_fingerprint.setdefault(key, {})[str(row["page_id"])] = (
            str(row["page_id"]),
            str(row["chunk_id"]),
        )

    matches: dict[int, tuple[str, str]] = {}
    cases: dict[int, PageMappingCasePlan] = {}
    for fingerprint, incoming in incoming_by_fingerprint.items():
        fingerprint_candidates = historical_by_fingerprint.get(fingerprint, {})
        if current_version_id is not None and len(incoming) > 1:
            candidate_ids = tuple(sorted(fingerprint_candidates))
            for row in incoming:
                cases[int(row["page_number"])] = PageMappingCasePlan(
                    page_number=int(row["page_number"]),
                    kind="duplicate_fingerprint",
                    candidate_page_ids=candidate_ids,
                )
        elif len(incoming) == 1 and len(fingerprint_candidates) == 1:
            identity = next(iter(fingerprint_candidates.values()))
            matches[int(incoming[0]["page_number"])] = identity
        elif fingerprint_candidates and (len(incoming) > 1 or len(fingerprint_candidates) > 1):
            candidate_ids = tuple(sorted(fingerprint_candidates))
            for row in incoming:
                cases[int(row["page_number"])] = PageMappingCasePlan(
                    page_number=int(row["page_number"]),
                    kind=(
                        "duplicate_fingerprint"
                        if len(incoming) > 1
                        else "multiple_candidates"
                    ),
                    candidate_page_ids=candidate_ids,
                )

    # 同一个稳定身份可能在历史上出现过多个内容版本。若这些内容同时回到新版本，
    # 每个指纹各自看似唯一，但仍不能让两个源页自动复用同一个 page_id。
    candidate_usage: dict[str, set[int]] = {}
    for page_number, identity in matches.items():
        candidate_usage.setdefault(identity[0], set()).add(page_number)
    for page_number, case in cases.items():
        for candidate_page_id in case.candidate_page_ids:
            candidate_usage.setdefault(candidate_page_id, set()).add(page_number)
    for candidate_page_id, page_numbers in candidate_usage.items():
        if len(page_numbers) <= 1:
            continue
        for page_number in page_numbers:
            matches.pop(page_number, None)
            existing = cases.get(page_number)
            candidates = set(() if existing is None else existing.candidate_page_ids)
            candidates.add(candidate_page_id)
            cases[page_number] = PageMappingCasePlan(
                page_number=page_number,
                kind="multiple_candidates" if existing is None else existing.kind,
                candidate_page_ids=tuple(sorted(candidates)),
            )

    matched_page_ids = {identity[0] for identity in matches.values()}

    if current_version_id is None:
        return PageMappingPlan(matches, tuple(cases.values()))
    current_by_slide_id: dict[int, list[tuple[str, str]]] = {}
    for row in history:
        if row["version_id"] != current_version_id or row["page_id"] in matched_page_ids:
            continue
        current_by_slide_id.setdefault(int(row["source_slide_id"]), []).append(
            (str(row["page_id"]), str(row["chunk_id"]))
        )
    remaining_by_slide_id: dict[int, list[Any]] = {}
    for row in incoming_rows:
        if int(row["page_number"]) not in matches and int(row["page_number"]) not in cases:
            remaining_by_slide_id.setdefault(int(row["source_slide_id"]), []).append(row)
    for slide_id, incoming in remaining_by_slide_id.items():
        slide_candidates = current_by_slide_id.get(slide_id, [])
        if len(incoming) == 1 and len(slide_candidates) == 1:
            identity = slide_candidates[0]
            matches[int(incoming[0]["page_number"])] = identity
            matched_page_ids.add(identity[0])
        elif slide_candidates and (len(incoming) > 1 or len(slide_candidates) > 1):
            candidate_ids = tuple(sorted(identity[0] for identity in slide_candidates))
            for row in incoming:
                cases[int(row["page_number"])] = PageMappingCasePlan(
                    page_number=int(row["page_number"]),
                    kind="slide_id_conflict",
                    candidate_page_ids=candidate_ids,
                )

    # 重复指纹 case 也保留各自 SlideID 指向的身份，供人比较而不静默采用。
    for page_number, case in tuple(cases.items()):
        row = next(row for row in incoming_rows if int(row["page_number"]) == page_number)
        slide_candidates = current_by_slide_id.get(int(row["source_slide_id"]), [])
        combined = tuple(
            sorted({*case.candidate_page_ids, *(identity[0] for identity in slide_candidates)})
        )
        cases[page_number] = PageMappingCasePlan(
            page_number=case.page_number,
            kind=case.kind if len(combined) <= 1 else case.kind,
            candidate_page_ids=combined,
        )
    return PageMappingPlan(matches, tuple(cases[number] for number in sorted(cases)))


def read_page_mapping(
    settings: Settings, *, document_id: str, version_id: str
) -> tuple[dict[str, Any], str] | None:
    connection = connect(settings)
    try:
        connection.execute("BEGIN")
        version = _mapping_version(connection, document_id, version_id)
        if version is None:
            return None
        workspace = _page_mapping_workspace(connection, settings, version)
        etag = _mapping_etag(
            version_id, int(version["mapping_revision"])
        )
        connection.commit()
        return workspace, etag
    finally:
        connection.close()


def save_page_mapping_decision(
    settings: Settings,
    *,
    actor_id: str,
    document_id: str,
    version_id: str,
    case_id: str,
    if_match: str,
    decision: str,
    page_id: str | None,
) -> tuple[dict[str, Any], str]:
    now = timestamp()
    with transaction(settings) as connection:
        version = _mapping_version(connection, document_id, version_id)
        if version is None:
            raise IngestionRequestError(404, "not_found", "未找到请求的资源。")
        if version["status"] == "ready" and version["mapping_confirmed_at"] is not None:
            raise IngestionRequestError(
                409, "mapping_frozen", "页对应关系已经冻结，不能直接改写。"
            )
        if version["status"] != "awaiting_mapping":
            raise IngestionRequestError(
                409, "mapping_unavailable", "该版本当前不接受页对应决定。"
            )
        current_etag = _mapping_etag(version_id, int(version["mapping_revision"]))
        _assert_mapping_precondition(if_match, current_etag)
        case = connection.execute(
            "SELECT * FROM page_mapping_cases WHERE version_id = ? AND case_id = ?",
            (version_id, case_id),
        ).fetchone()
        if case is None:
            raise IngestionRequestError(404, "not_found", "未找到请求的页对应项。")
        if decision not in {"reuse", "new"}:
            raise IngestionRequestError(422, "invalid_mapping_decision", "页对应决定无效。")
        selected_page_id = page_id if decision == "reuse" else None
        if decision == "reuse":
            candidates = set(json.loads(str(case["candidate_page_ids_json"])))
            if page_id is None or page_id not in candidates:
                raise IngestionRequestError(
                    422, "invalid_mapping_candidate", "所选历史页不属于该项候选。"
                )
            occupied = connection.execute(
                """
                SELECT case_id FROM page_mapping_cases
                WHERE version_id = ? AND selected_page_id = ? AND case_id <> ?
                """,
                (version_id, page_id, case_id),
            ).fetchone()
            if occupied is not None:
                raise IngestionRequestError(
                    409,
                    "mapping_candidate_occupied",
                    "该历史页已被另一页对应项占用，请先修改原决定。",
                )
            rows = connection.execute(
                """
                SELECT * FROM ingestion_page_results
                WHERE version_id = ? AND enabled = 1 ORDER BY page_number
                """,
                (version_id,),
            ).fetchall()
            plan = _build_page_mapping_plan(
                connection,
                document_id=document_id,
                current_version_id=version["current_version_id"],
                target_version_id=version_id,
                incoming_rows=rows,
            )
            if page_id in {identity[0] for identity in plan.automatic_matches.values()}:
                raise IngestionRequestError(
                    409,
                    "mapping_candidate_occupied",
                    "该历史页已被确定性页对应占用。",
                )
        connection.execute(
            """
            UPDATE page_mapping_cases
            SET decision = ?, selected_page_id = ?, decided_by = ?, decided_at = ?
            WHERE case_id = ? AND version_id = ?
            """,
            (decision, selected_page_id, actor_id, now, case_id, version_id),
        )
        connection.execute(
            "UPDATE document_versions SET mapping_revision = mapping_revision + 1 "
            "WHERE version_id = ?",
            (version_id,),
        )
        refreshed = _mapping_version(connection, document_id, version_id)
        assert refreshed is not None
        workspace = _page_mapping_workspace(connection, settings, refreshed)
        etag = _mapping_etag(version_id, int(refreshed["mapping_revision"]))
    return workspace, etag


def confirm_page_mapping(
    settings: Settings,
    *,
    actor_id: str,
    document_id: str,
    version_id: str,
    if_match: str,
) -> tuple[dict[str, Any], str]:
    now = timestamp()
    with transaction(settings) as connection:
        version = _mapping_version(connection, document_id, version_id)
        if version is None:
            raise IngestionRequestError(404, "not_found", "未找到请求的资源。")
        if version["status"] == "ready" and version["mapping_confirmed_at"] is not None:
            raise IngestionRequestError(
                409, "mapping_frozen", "页对应关系已经冻结，不能直接改写。"
            )
        if version["status"] != "awaiting_mapping":
            raise IngestionRequestError(
                409, "mapping_unavailable", "该版本当前不能确认页对应。"
            )
        current_etag = _mapping_etag(version_id, int(version["mapping_revision"]))
        _assert_mapping_precondition(if_match, current_etag)
        cases = connection.execute(
            "SELECT * FROM page_mapping_cases WHERE version_id = ? ORDER BY page_number",
            (version_id,),
        ).fetchall()
        if not cases or any(case["decision"] is None for case in cases):
            raise IngestionRequestError(
                409, "mapping_incomplete", "仍有未决定的页对应项，不能启用版本。"
            )
        if _mapping_evidence_error_count(connection, settings, version_id) > 0:
            raise IngestionRequestError(
                409,
                "mapping_evidence_unavailable",
                "页对应证据暂不可用，恢复后才能启用版本。",
            )
        rows = connection.execute(
            """
            SELECT * FROM ingestion_page_results
            WHERE version_id = ? AND enabled = 1 ORDER BY page_number
            """,
            (version_id,),
        ).fetchall()
        plan = _build_page_mapping_plan(
            connection,
            document_id=document_id,
            current_version_id=version["current_version_id"],
            target_version_id=version_id,
            incoming_rows=rows,
        )
        identities = dict(plan.automatic_matches)
        used_page_ids = {identity[0] for identity in identities.values()}
        for case in cases:
            if case["decision"] != "reuse":
                continue
            selected_page_id = str(case["selected_page_id"])
            if selected_page_id in used_page_ids:
                raise IngestionRequestError(
                    409, "mapping_candidate_occupied", "同一历史页不能对应多个新版本页。"
                )
            page = connection.execute(
                "SELECT page_id, chunk_id FROM pages WHERE document_id = ? AND page_id = ?",
                (document_id, selected_page_id),
            ).fetchone()
            if page is None:
                raise IngestionRequestError(
                    409, "mapping_candidate_unavailable", "历史候选页已不可用。"
                )
            identities[int(case["page_number"])] = (
                str(page["page_id"]),
                str(page["chunk_id"]),
            )
            used_page_ids.add(selected_page_id)
        summary = _materialize_page_versions(
            connection,
            document_id=document_id,
            version_id=version_id,
            rows=rows,
            identities=identities,
            now=now,
        )
        activated = connection.execute(
            """
            UPDATE document_versions
            SET status = 'ready', ready_at = ?, mapping_revision = mapping_revision + 1,
                mapping_confirmed_at = ?, mapping_confirmed_by = ?
            WHERE version_id = ? AND status = 'awaiting_mapping'
            """,
            (now, now, actor_id, version_id),
        )
        if activated.rowcount != 1:
            raise IngestionRequestError(
                409, "mapping_state_changed", "版本状态已经变化，请重新加载。"
            )
        switched = connection.execute(
            "UPDATE documents SET current_version_id = ? WHERE document_id = ?",
            (version_id, document_id),
        )
        if switched.rowcount != 1:
            raise RuntimeError("版本所属文档不存在")
        completed = connection.execute(
            """
            UPDATE jobs
            SET status = 'succeeded', checkpoint_json = ?, updated_at = ?
            WHERE version_id = ? AND kind = 'document.ingest'
              AND status = 'requires_action'
            """,
            (
                _json(
                    {
                        "phase": "activation",
                        "completed_pages": len(rows),
                        "total_pages": len(rows),
                    }
                ),
                now,
                version_id,
            ),
        )
        if completed.rowcount != 1:
            raise RuntimeError("等待页对应的摄取任务不存在")
        refreshed = _mapping_version(connection, document_id, version_id)
        assert refreshed is not None
        etag = _mapping_etag(version_id, int(refreshed["mapping_revision"]))
    return {
        "document_id": document_id,
        "version_id": version_id,
        "status": "ready",
        "summary": summary,
    }, etag


def _mapping_version(connection: Any, document_id: str, version_id: str) -> Any | None:
    return connection.execute(
        """
        SELECT versions.*, documents.current_version_id
        FROM document_versions AS versions
        JOIN documents ON documents.document_id = versions.document_id
        WHERE versions.document_id = ? AND versions.version_id = ?
        """,
        (document_id, version_id),
    ).fetchone()


def _page_mapping_workspace(
    connection: Any, settings: Settings, version: Any
) -> dict[str, Any]:
    version_id = str(version["version_id"])
    document_id = str(version["document_id"])
    rows = connection.execute(
        """
        SELECT * FROM ingestion_page_results
        WHERE version_id = ? AND enabled = 1 ORDER BY page_number
        """,
        (version_id,),
    ).fetchall()
    plan = _build_page_mapping_plan(
        connection,
        document_id=document_id,
        current_version_id=version["current_version_id"],
        target_version_id=version_id,
        incoming_rows=rows,
    )
    automatic = plan.automatic_matches
    cases = connection.execute(
        "SELECT * FROM page_mapping_cases WHERE version_id = ? ORDER BY page_number",
        (version_id,),
    ).fetchall()
    decisions_by_page_id = {
        str(case["selected_page_id"]): str(case["case_id"])
        for case in cases
        if case["selected_page_id"] is not None
    }
    decisions_by_page_id.update(
        {
            identity[0]: f"source-page-{page_number}"
            for page_number, identity in automatic.items()
        }
    )

    def adjacent(page_number: int) -> dict[str, Any]:
        before = max((number for number in automatic if number < page_number), default=None)
        after = min((number for number in automatic if number > page_number), default=None)

        def item(number: int | None) -> dict[str, Any] | None:
            if number is None:
                return None
            return {"source_page_number": number, "page_id": automatic[number][0]}

        return {"before": item(before), "after": item(after)}

    payload_cases = []
    for case in cases:
        page_number = int(case["page_number"])
        source = next(row for row in rows if int(row["page_number"]) == page_number)
        candidates = []
        for candidate_page_id in json.loads(str(case["candidate_page_ids_json"])):
            candidate = connection.execute(
                """
                SELECT pv.page_id, p.chunk_id, pv.page_number, pv.review_status,
                       pv.fingerprint_version, pv.fingerprint_sha256,
                       results.source_slide_id, versions.version_id
                FROM page_versions AS pv
                JOIN pages AS p ON p.page_id = pv.page_id
                JOIN document_versions AS versions ON versions.version_id = pv.version_id
                JOIN ingestion_page_results AS results
                  ON results.version_id = pv.version_id
                 AND results.page_number = pv.page_number
                WHERE pv.page_id = ? AND versions.status = 'ready'
                ORDER BY CASE WHEN versions.version_id = ? THEN 0 ELSE 1 END,
                         versions.ready_at DESC, pv.created_at DESC
                LIMIT 1
                """,
                (candidate_page_id, version["current_version_id"]),
            ).fetchone()
            if candidate is None:
                continue
            candidates.append(
                {
                    "page_id": candidate["page_id"],
                    "chunk_id": candidate["chunk_id"],
                    "version_id": candidate["version_id"],
                    "page_number": candidate["page_number"],
                    "slide_id": candidate["source_slide_id"],
                    "review_status": candidate["review_status"],
                    "fingerprint_relation": (
                        "same"
                        if int(candidate["fingerprint_version"])
                        == int(source["fingerprint_version"])
                        and str(candidate["fingerprint_sha256"])
                        == str(source["fingerprint_sha256"])
                        else "changed"
                    ),
                    "adjacent_confirmed": adjacent(page_number),
                    "relative_order": {
                        "source_page_number": page_number,
                        "candidate_page_number": candidate["page_number"],
                        "delta": page_number - int(candidate["page_number"]),
                    },
                    "occupied_by_case_id": decisions_by_page_id.get(str(candidate["page_id"])),
                    "standard_render": {
                        "url": (
                            f"/api/v1/documents/{document_id}/versions/"
                            f"{candidate['version_id']}/source-pages/"
                            f"{candidate['page_number']}/render"
                        )
                    },
                }
            )
        payload_cases.append(
            {
                "case_id": case["case_id"],
                "kind": case["case_kind"],
                "status": "saved" if case["decision"] is not None else "unresolved",
                "source_page": {
                    "page_number": page_number,
                    "slide_id": source["source_slide_id"],
                    "fingerprint": {
                        "version": source["fingerprint_version"],
                        "sha256": source["fingerprint_sha256"],
                    },
                    "standard_render": {
                        "url": (
                            f"/api/v1/documents/{document_id}/versions/{version_id}"
                            f"/source-pages/{page_number}/render"
                        )
                    },
                },
                "candidates": candidates,
                "decision": (
                    None
                    if case["decision"] is None
                    else {"kind": case["decision"], "page_id": case["selected_page_id"]}
                ),
                "decided_by": case["decided_by"],
                "decided_at": case["decided_at"],
            }
        )
    remaining = sum(case["decision"] is None for case in cases)
    resolved_identities = dict(automatic)
    case_by_page = {int(case["page_number"]): case for case in cases}
    for case in cases:
        if case["decision"] != "reuse":
            continue
        page = connection.execute(
            "SELECT page_id, chunk_id FROM pages WHERE page_id = ?",
            (case["selected_page_id"],),
        ).fetchone()
        if page is not None:
            resolved_identities[int(case["page_number"])] = (
                str(page["page_id"]),
                str(page["chunk_id"]),
            )
    impact = {
        "reused_unchanged": 0,
        "reused_changed": 0,
        "created_new": 0,
        "soft_deleted": 0,
        "unresolved": remaining,
        "save_conflicts": 0,
        "evidence_errors": _mapping_evidence_error_count(
            connection, settings, version_id
        ),
    }
    for row in rows:
        page_number = int(row["page_number"])
        case = case_by_page.get(page_number)
        if case is not None and case["decision"] is None:
            continue
        identity = resolved_identities.get(page_number)
        if identity is None:
            impact["created_new"] += 1
            continue
        historical = connection.execute(
            """
            SELECT pv.fingerprint_version, pv.fingerprint_sha256
            FROM page_versions AS pv
            JOIN document_versions AS versions ON versions.version_id = pv.version_id
            WHERE pv.page_id = ? AND versions.status = 'ready'
            ORDER BY versions.ready_at DESC, pv.created_at DESC LIMIT 1
            """,
            (identity[0],),
        ).fetchone()
        unchanged = (
            historical is not None
            and int(historical["fingerprint_version"]) == int(row["fingerprint_version"])
            and str(historical["fingerprint_sha256"]) == str(row["fingerprint_sha256"])
        )
        impact["reused_unchanged" if unchanged else "reused_changed"] += 1
    if remaining == 0:
        current_page_ids = {
            str(row["page_id"])
            for row in connection.execute(
                "SELECT page_id FROM page_versions WHERE version_id = ?",
                (version["current_version_id"],),
            )
        }
        impact["soft_deleted"] = len(
            current_page_ids - {identity[0] for identity in resolved_identities.values()}
        )
    return {
        "document_id": document_id,
        "version_id": version_id,
        "source_filename": version["source_filename"],
        "status": version["status"],
        "revision": version["mapping_revision"],
        "remaining_cases": remaining,
        "current_version": {
            "version_id": version["current_version_id"],
            "still_serving": (
                version["status"] == "awaiting_mapping"
                and version["current_version_id"] is not None
            ),
        },
        "cases": payload_cases,
        "can_confirm": (
            bool(cases)
            and remaining == 0
            and version["status"] == "awaiting_mapping"
            and impact["evidence_errors"] == 0
        ),
        "impact_summary": impact,
        "confirmed_at": version["mapping_confirmed_at"],
        "confirmed_by": version["mapping_confirmed_by"],
    }


def _mapping_evidence_error_count(
    connection: Any, settings: Settings, version_id: str
) -> int:
    """统计工作面必须展示但对象存储中不可用的标准页证据。"""
    store = LocalObjectStore(settings.object_store_path)
    evidence: set[tuple[str, str]] = set()
    incoming = connection.execute(
        """
        SELECT results.page_number, results.render_sha256
        FROM ingestion_page_results AS results
        JOIN page_mapping_cases AS cases
          ON cases.version_id = results.version_id
         AND cases.page_number = results.page_number
        WHERE results.version_id = ?
        """,
        (version_id,),
    ).fetchall()
    for row in incoming:
        evidence.add((f"source:{row['page_number']}", str(row["render_sha256"] or "")))

    cases = connection.execute(
        "SELECT candidate_page_ids_json FROM page_mapping_cases WHERE version_id = ?",
        (version_id,),
    ).fetchall()
    candidate_page_ids = {
        str(page_id)
        for case in cases
        for page_id in json.loads(str(case["candidate_page_ids_json"]))
    }
    for page_id in candidate_page_ids:
        row = connection.execute(
            """
            SELECT pv.render_sha256
            FROM page_versions AS pv
            JOIN document_versions AS versions ON versions.version_id = pv.version_id
            WHERE pv.page_id = ? AND versions.status = 'ready'
            ORDER BY versions.ready_at DESC, pv.created_at DESC
            LIMIT 1
            """,
            (page_id,),
        ).fetchone()
        evidence.add(
            (
                f"candidate:{page_id}",
                "" if row is None else str(row["render_sha256"] or ""),
            )
        )
    return sum(
        not sha256 or not store.path_for(sha256).is_file()
        for _, sha256 in evidence
    )


def _mapping_etag(version_id: str, revision: int) -> str:
    return f'"mapping-{version_id}-{revision}"'


def _assert_mapping_precondition(if_match: str, current_etag: str) -> None:
    if not if_match:
        raise IngestionRequestError(
            428, "mapping_precondition_required", "保存页对应决定必须携带 If-Match。"
        )
    if if_match != current_etag:
        raise MappingPreconditionError(current_etag)


def _checkpoint(
    settings: Settings, job: ClaimedJob, phase: str, completed_pages: int, total_pages: int
) -> None:
    checkpoint_at = datetime.now(UTC)
    checkpoint_at_iso = checkpoint_at.isoformat()
    with transaction(settings) as connection:
        updated = connection.execute(
            """
            UPDATE jobs
            SET checkpoint_json = ?, lease_expires_at = ?, updated_at = ?
            WHERE job_id = ? AND status = 'running'
              AND lease_owner = ? AND lease_token = ? AND lease_expires_at > ?
            """,
            (
                _json(
                    {
                        "phase": phase,
                        "completed_pages": completed_pages,
                        "total_pages": total_pages,
                    }
                ),
                lease_expiration(at=checkpoint_at),
                checkpoint_at_iso,
                job.job_id,
                settings.worker_id,
                job.lease_token,
                checkpoint_at_iso,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("worker 无法保存未持有任务的进度")


def _assert_job_lease(connection: Any, settings: Settings, job: ClaimedJob) -> None:
    now = timestamp()
    row = connection.execute(
        """
        SELECT 1 FROM jobs
        WHERE job_id = ? AND status = 'running'
          AND lease_owner = ? AND lease_token = ? AND lease_expires_at > ?
        """,
        (job.job_id, settings.worker_id, job.lease_token, now),
    ).fetchone()
    if row is None:
        raise RuntimeError("worker 无法写入未持有任务的处理结果")


def _stage_error(
    phase: str, page_number: int | None, error: Exception
) -> IngestionStageError:
    retryable = isinstance(error, (OSError, TimeoutError, subprocess.SubprocessError)) or (
        phase in {"conversion", "rendering"} and isinstance(error, RuntimeError)
    )
    label = {
        "conversion": "转换",
        "rendering": "渲染",
        "storage": "存储",
        "page_fingerprint": "页指纹计算",
        "source_manifest": "源页清单",
    }.get(phase, "处理")
    suffix = "temporarily_unavailable" if retryable else "failed"
    prefix = f"第 {page_number} 页" if page_number is not None else ""
    message = f"{prefix}{label}{'暂时失败' if retryable else '失败'}。"
    return IngestionStageError(
        phase=phase,
        page_number=page_number,
        retryable=retryable,
        code=f"{phase}_{suffix}",
        message=message,
    )


def _structured_error(error: Exception, *, attempt: int) -> dict[str, Any]:
    if isinstance(error, IngestionStageError):
        payload: dict[str, Any] = {
            "attempt": attempt,
            "code": error.code,
            "message": error.message,
            "phase": error.phase,
            "retryable": error.retryable,
        }
        if error.page_number is not None:
            payload["page_number"] = error.page_number
        return payload
    return {
        "attempt": attempt,
        "code": "ingestion_failed",
        "message": "文档摄取失败。",
        "phase": "unknown",
        "retryable": False,
    }


def _stage_key(
    *,
    phase: str,
    source_sha256: str,
    version_id: str,
    page: SourcePage,
    tool_version: str,
    dependency_key: str | None = None,
) -> str:
    canonical = _json(
        {
            "dependency_key": dependency_key,
            "input_sha256": source_sha256,
            "phase": phase,
            "target": {
                "page_number": page.page_number,
                "relationship_id": page.relationship_id,
                "source_part": page.source_part,
                "source_slide_id": page.source_slide_id,
                "version_id": version_id,
            },
            "tool_version": tool_version,
        }
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


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


def _content_from_json(serialized: str) -> NormalizedPageContent:
    payload = json.loads(serialized)
    tables: list[NormalizedTable] = []
    for table in payload["tables"]:
        grid: list[tuple[NormalizedTableSlot, ...]] = []
        for row in table["grid"]:
            slots: list[NormalizedTableSlot] = []
            for slot in row:
                cell_payload = slot["cell"]
                cell = (
                    None
                    if cell_payload is None
                    else NormalizedTableCell(
                        text=str(cell_payload["text"]),
                        col_span=int(cell_payload["col_span"]),
                        row_span=int(cell_payload["row_span"]),
                    )
                )
                slots.append(
                    NormalizedTableSlot(
                        kind=slot["kind"],
                        cell=cell,
                        origin_row=slot["origin_row"],
                        origin_col=slot["origin_col"],
                    )
                )
            grid.append(tuple(slots))
        tables.append(
            NormalizedTable(
                kind=table["kind"],
                header_rows=int(table["header_rows"]),
                grid=tuple(grid),
            )
        )
    return NormalizedPageContent(
        titles=tuple(str(value) for value in payload["titles"]),
        body=tuple(str(value) for value in payload["body"]),
        tables=tuple(tables),
        images=tuple(
            NormalizedImage(
                reference_index=int(image["reference_index"]),
                alt_text=str(image["alt_text"]),
                media_type=str(image["media_type"]),
                origin_part=str(image["origin_part"]),
                data=base64.b64decode(image["data_base64"], validate=True),
            )
            for image in payload["images"]
        ),
        speaker_notes=tuple(str(value) for value in payload["speaker_notes"]),
    )


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
            SELECT job_id, kind, payload_json, status, attempts, checkpoint_json,
                   error_json, next_attempt_at, version_id
            FROM jobs WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        page_rows = []
        if (
            row is not None
            and row["kind"] == "document.ingest"
            and row["version_id"] is not None
        ):
            page_rows = connection.execute(
                """
                SELECT page_number, enabled, manifest_key, conversion_key,
                       render_key, fingerprint_key
                FROM ingestion_page_results
                WHERE version_id = ?
                ORDER BY page_number
                """,
                (row["version_id"],),
            ).fetchall()
    if row is None:
        return None
    progress = json.loads(row["checkpoint_json"]) if row["checkpoint_json"] else None
    if progress is not None:
        if row["kind"] == "page.enable":
            payload = json.loads(row["payload_json"])
            status = "in_progress"
            if row["status"] == "queued":
                status = "waiting_retry" if row["next_attempt_at"] is not None else "pending"
            elif row["status"] == "succeeded":
                status = "completed"
            elif row["status"] in {"failed", "cancelled"}:
                status = "failed"
            progress["pages"] = [
                {
                    "page_number": int(payload["page_number"]),
                    "phase": str(progress["phase"]),
                    "status": status,
                }
            ]
        else:
            progress["pages"] = [_page_progress(page) for page in page_rows]
    return {
        "job_id": str(row["job_id"]),
        "kind": str(row["kind"]),
        "status": str(row["status"]),
        "attempts": int(row["attempts"]),
        "next_retry_at": row["next_attempt_at"],
        "progress": progress,
        "error": json.loads(row["error_json"]) if row["error_json"] else None,
    }


def _page_progress(row: Any) -> dict[str, Any]:
    if not bool(row["enabled"]):
        return {
            "page_number": int(row["page_number"]),
            "phase": "source_manifest",
            "status": "skipped",
        }
    phase = "source_manifest"
    for key, candidate in (
        ("conversion_key", "conversion"),
        ("render_key", "rendering"),
        ("fingerprint_key", "page_fingerprint"),
    ):
        if row[key] is not None:
            phase = candidate
    return {
        "page_number": int(row["page_number"]),
        "phase": phase,
        "status": "completed",
    }
