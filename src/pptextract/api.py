from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException

from pptextract.auth import ActorProvider, HeaderActorProvider
from pptextract.config import Settings
from pptextract.curation import (
    CurationRequestError,
    approve_page,
    batch_exclude_pages,
    complete_source_review,
    confirm_source_snapshot,
    delete_capture_visual,
    exclude_page,
    mark_capture_source_complete,
    move_capture_visual,
    read_page_curation,
    read_source_image,
    reopen_page,
    save_capture_visual,
    save_image_source_disposition,
    save_source_snapshot,
    update_capture_visual,
)
from pptextract.db import connect, database_path_is_local, initialize_database, transaction
from pptextract.ingest_workflow import (
    IngestionRequestError,
    MappingPreconditionError,
    accept_document_version,
    accept_first_upload,
    accept_hidden_page_enablement,
    confirm_page_mapping,
    read_job,
    read_page_mapping,
    save_page_mapping_decision,
)
from pptextract.jobs import timestamp
from pptextract.lifecycle import (
    read_lifecycle_events,
    restore_document,
    retry_failed_version,
    rollback_to_version,
    soft_delete_document,
    void_version,
)
from pptextract.object_store import LocalObjectStore
from pptextract.publication import (
    PublicationRequestError,
    artifact_content,
    create_candidate,
    iter_file,
    read_artifact,
    read_candidate,
    read_current_artifact,
    read_publication_workspace,
    retry_publication_job,
)
from pptextract.publication import (
    confirm_candidate as confirm_publication_candidate_command,
)
from pptextract.rendering import render_configuration_version
from pptextract.rendering_warnings import (
    confirm_warning,
    read_warning_rows,
    serialize_warning,
    summarize_rows,
)
from pptextract.repeated_footer_noise import (
    RepeatedFooterNoiseError,
    confirm_candidate,
    preview_candidate,
    revoke_confirmation,
)
from pptextract.runtime_facts import (
    RuntimeFactError,
    read_runtime_facts,
    record_timing_sample,
    runtime_facts_csv,
)
from pptextract.worker import worker_is_fresh


def error_response(
    status_code: int, code: str, message: str, details: Any | None = None
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status_code, content={"error": error})


class LifecycleCommand(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class PageMappingDecision(BaseModel):
    decision: str
    page_id: str | None = None


class ConfirmAllRenderingWarnings(BaseModel):
    render_config_version: str = Field(min_length=1, max_length=200)
    warning_ids: list[str] = Field(min_length=1, max_length=10_000)


class SaveSourceSnapshot(BaseModel):
    base_snapshot_id: str | None = None
    titles: list[str] = Field(max_length=10_000)
    body: list[str] = Field(max_length=100_000)


class SnapshotCommand(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=128)


class ExcludePageCommand(BaseModel):
    reason: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=100_000)


class BatchExcludePagesCommand(ExcludePageCommand):
    page_ids: list[str] = Field(min_length=1, max_length=10_000)


class SaveImageSourceDisposition(BaseModel):
    base_snapshot_id: str = Field(min_length=1, max_length=128)
    disposition: str
    summary: str | None = Field(default=None, max_length=100_000)
    ignore_reason: str | None = Field(default=None, max_length=128)
    ignore_note: str | None = Field(default=None, max_length=100_000)


class NormalizedBounds(BaseModel):
    left: float
    top: float
    width: float
    height: float


class SaveCaptureVisual(BaseModel):
    base_snapshot_id: str = Field(min_length=1, max_length=128)
    summary: str = Field(max_length=100_000)
    visual_type: str | None = Field(default=None, max_length=128)
    bounds: NormalizedBounds


class DeleteCaptureVisual(BaseModel):
    base_snapshot_id: str = Field(min_length=1, max_length=128)


class MoveCaptureVisual(BaseModel):
    base_snapshot_id: str = Field(min_length=1, max_length=128)
    direction: str


class ConfirmRepeatedFooterNoise(BaseModel):
    candidate_id: str = Field(min_length=64, max_length=64)
    source_ref: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=100_000)


class RevokeRepeatedFooterNoise(BaseModel):
    note: str | None = Field(default=None, max_length=100_000)


class CurationTimingSample(BaseModel):
    sample_id: str = Field(min_length=1, max_length=128)
    page_id: str = Field(min_length=1, max_length=128)
    version_id: str = Field(min_length=1, max_length=128)
    stage: str = Field(min_length=1, max_length=64)
    duration_ms: int = Field(ge=0)


def _read_curation_snapshot(
    connection: sqlite3.Connection, snapshot_id: str | None
) -> dict[str, Any] | None:
    if snapshot_id is None:
        return None
    snapshot = connection.execute(
        """
        SELECT snapshot_id, snapshot_kind, source_snapshot_id, overview
        FROM curation_snapshots WHERE snapshot_id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    if snapshot is None:
        return None
    visuals = connection.execute(
        """
        SELECT visual_ref, position, source_kind, disposition, summary,
               visual_type, bounds_json, source_visual_ref, confirmed,
               source_image_ref, asset_sha256, asset_media_type, asset_size_bytes,
               asset_width_px, asset_height_px
        FROM curation_snapshot_visuals
        WHERE snapshot_id = ? ORDER BY position
        """,
        (snapshot_id,),
    ).fetchall()
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "kind": snapshot["snapshot_kind"],
        "source_snapshot_id": snapshot["source_snapshot_id"],
        "overview": snapshot["overview"],
        "visuals": [
            {
                "visual_ref": visual["visual_ref"],
                "position": visual["position"],
                "source_kind": visual["source_kind"],
                "disposition": visual["disposition"],
                "summary": visual["summary"],
                "visual_type": visual["visual_type"],
                "bounds": (
                    None if visual["bounds_json"] is None else json.loads(visual["bounds_json"])
                ),
                "source_visual_ref": visual["source_visual_ref"],
                "confirmed": bool(visual["confirmed"]),
                **(
                    {
                        "source_image_ref": visual["source_image_ref"],
                        "asset": {
                            "sha256": visual["asset_sha256"],
                            "media_type": visual["asset_media_type"],
                            "size_bytes": visual["asset_size_bytes"],
                            "byte_contract": "anydoc_original",
                        },
                    }
                    if visual["source_kind"] == "source_image"
                    else {}
                ),
                **(
                    {
                        "asset": {
                            "sha256": visual["asset_sha256"],
                            "media_type": visual["asset_media_type"],
                            "size_bytes": visual["asset_size_bytes"],
                            "width_px": visual["asset_width_px"],
                            "height_px": visual["asset_height_px"],
                            "byte_contract": "standard_render_crop",
                        }
                    }
                    if (visual["source_kind"] == "capture" and visual["asset_sha256"] is not None)
                    else {}
                ),
            }
            for visual in visuals
        ],
    }


def _visual_mutation_content(settings: Settings, curation: dict[str, Any]) -> dict[str, Any]:
    snapshot = curation.get("current_snapshot")
    snapshot_id = None if snapshot is None else snapshot.get("snapshot_id")
    with connect(settings) as connection:
        annotation = _read_curation_snapshot(connection, snapshot_id)
    return {"curation": curation, "annotation": annotation}


def create_app(
    settings: Settings | None = None, actor_provider: ActorProvider | None = None
) -> FastAPI:
    resolved = settings or Settings.from_env()
    actors = actor_provider or HeaderActorProvider(resolved)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        initialize_database(resolved)
        resolved.object_store_path.mkdir(parents=True, exist_ok=True)
        yield

    app = FastAPI(title="PPTExtract API", version="0.1.0", lifespan=lifespan)
    app.state.settings = resolved

    @app.exception_handler(HTTPException)
    async def handle_http_exception(_request: Request, exception: HTTPException) -> JSONResponse:
        if exception.status_code == 404:
            return error_response(404, "not_found", "未找到请求的资源。")
        message = exception.detail if isinstance(exception.detail, str) else "请求无法完成。"
        return error_response(exception.status_code, "request_error", message)

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(_request: Request, _exception: Exception) -> JSONResponse:
        return error_response(500, "internal_error", "系统暂时无法完成请求，请稍后重试。")

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exception: RequestValidationError
    ) -> JSONResponse:
        details = [
            {
                "field": ".".join(
                    str(part) for part in error["loc"] if part not in {"query", "body"}
                ),
                "message": error["msg"],
            }
            for error in exception.errors()
        ]
        return error_response(422, "invalid_request", "请求参数无效。", details)

    @app.get("/api/v1/app/bootstrap")
    async def bootstrap(request: Request) -> dict[str, Any]:
        actor = actors.resolve(request)
        with connect(resolved) as connection:
            active_versions = connection.execute(
                """
                SELECT versions.document_id, versions.version_id,
                       versions.source_filename, versions.status AS version_status,
                       jobs.status AS job_status
                FROM document_versions AS versions
                JOIN documents ON documents.document_id = versions.document_id
                JOIN jobs ON jobs.version_id = versions.version_id
                         AND jobs.kind = 'document.ingest'
                WHERE documents.deleted_at IS NULL
                  AND versions.status IN ('processing', 'awaiting_mapping')
                  AND jobs.status IN ('queued', 'running', 'requires_action')
                ORDER BY versions.created_at, versions.version_id
                """
            ).fetchall()
            active_warning_summaries = {
                str(version["version_id"]): summarize_rows(
                    read_warning_rows(connection, version_id=str(version["version_id"]))
                )
                for version in active_versions
            }
            curatable_versions = connection.execute(
                """
                SELECT versions.document_id, versions.version_id, versions.source_filename
                FROM documents
                JOIN document_versions AS versions
                  ON versions.version_id = documents.current_version_id
                WHERE documents.deleted_at IS NULL AND versions.status = 'ready'
                ORDER BY versions.ready_at, versions.version_id
                """
            ).fetchall()
        processing_documents = []
        for version in active_versions:
            requires_mapping = version["job_status"] == "requires_action"
            processing_payload = {
                "document_id": version["document_id"],
                "version_id": version["version_id"],
                "title": version["source_filename"],
                "status": version["job_status"],
                "status_label": "需要页对应" if requires_mapping else "正在处理",
                "action": (
                    {
                        "label": "处理页对应",
                        "href": (
                            f"/documents/{version['document_id']}/versions/"
                            f"{version['version_id']}/page-mapping"
                        ),
                    }
                    if requires_mapping
                    else None
                ),
            }
            warning_summary = active_warning_summaries[str(version["version_id"])]
            if warning_summary["total"]:
                processing_payload["rendering_warnings"] = warning_summary
            processing_documents.append(processing_payload)
        curatable_documents = []
        with connect(resolved) as connection:
            for version in curatable_versions:
                warning_summary = summarize_rows(
                    read_warning_rows(connection, version_id=str(version["version_id"]))
                )
                curatable_documents.append(
                    {
                        "document_id": version["document_id"],
                        "version_id": version["version_id"],
                        "title": version["source_filename"],
                        "status": "ready",
                        "status_label": "可策展",
                        "rendering_warnings": warning_summary,
                        "action": {
                            "label": "进入策展",
                            "href": (
                                "/curation?filter=rendering-warnings"
                                f"&document={version['document_id']}"
                                f"&version={version['version_id']}"
                                if warning_summary["total"]
                                else "/curation"
                            ),
                        },
                    }
                )
        return {
            "actor": {"actor_id": actor.actor_id, "display_name": actor.display_name},
            "runways": [
                {"id": "pending", "label": "待处理", "documents": []},
                {
                    "id": "processing",
                    "label": "处理中",
                    "documents": processing_documents,
                },
                {
                    "id": "curatable",
                    "label": "可策展",
                    "documents": curatable_documents,
                },
            ],
        }

    @app.post("/api/v1/documents", status_code=202)
    async def create_document(
        request: Request, file: Annotated[UploadFile, File()]
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            accepted = accept_first_upload(
                resolved,
                actor_id=actor.actor_id,
                idempotency_key=request.headers.get("Idempotency-Key", ""),
                filename=file.filename or "",
                media_type=file.content_type or "",
                stream=file.file,
            )
        except IngestionRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        finally:
            await file.close()
        return JSONResponse(
            status_code=202,
            content={
                "document_id": accepted.document_id,
                "version_id": accepted.version_id,
                "job_id": accepted.job_id,
                "status": accepted.status,
            },
        )

    @app.post("/api/v1/documents/{document_id}/versions")
    async def create_document_version(
        document_id: str,
        request: Request,
        file: Annotated[UploadFile, File()],
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            accepted = accept_document_version(
                resolved,
                actor_id=actor.actor_id,
                document_id=document_id,
                idempotency_key=request.headers.get("Idempotency-Key", ""),
                filename=file.filename or "",
                media_type=file.content_type or "",
                stream=file.file,
            )
        except IngestionRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        finally:
            await file.close()
        return JSONResponse(
            status_code=200 if accepted.status == "no_change" else 202,
            content={
                "document_id": accepted.document_id,
                "version_id": accepted.version_id,
                "job_id": accepted.job_id,
                "status": accepted.status,
            },
        )

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: str) -> JSONResponse:
        job = read_job(resolved, job_id)
        if job is None:
            return error_response(404, "not_found", "未找到请求的资源。")
        return JSONResponse(content=job)

    @app.get("/api/v1/documents/{document_id}/versions/{version_id}/page-mapping")
    async def get_page_mapping(document_id: str, version_id: str) -> JSONResponse:
        result = read_page_mapping(resolved, document_id=document_id, version_id=version_id)
        if result is None:
            return error_response(404, "not_found", "未找到请求的资源。")
        payload, etag = result
        return JSONResponse(content=payload, headers={"ETag": etag})

    @app.put("/api/v1/documents/{document_id}/versions/{version_id}/page-mapping/cases/{case_id}")
    async def put_page_mapping_decision(
        document_id: str,
        version_id: str,
        case_id: str,
        command: PageMappingDecision,
        request: Request,
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            payload, etag = save_page_mapping_decision(
                resolved,
                actor_id=actor.actor_id,
                document_id=document_id,
                version_id=version_id,
                case_id=case_id,
                if_match=request.headers.get("If-Match", ""),
                decision=command.decision,
                page_id=command.page_id,
            )
        except MappingPreconditionError as error:
            response = error_response(error.status_code, error.code, error.message)
            response.headers["ETag"] = error.current_etag
            return response
        except IngestionRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(content=payload, headers={"ETag": etag})

    @app.post("/api/v1/documents/{document_id}/versions/{version_id}/page-mapping/confirm")
    async def post_page_mapping_confirmation(
        document_id: str, version_id: str, request: Request
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            payload, etag = confirm_page_mapping(
                resolved,
                actor_id=actor.actor_id,
                document_id=document_id,
                version_id=version_id,
                if_match=request.headers.get("If-Match", ""),
            )
        except MappingPreconditionError as error:
            response = error_response(error.status_code, error.code, error.message)
            response.headers["ETag"] = error.current_etag
            return response
        except IngestionRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(content=payload, headers={"ETag": etag})

    @app.get(
        "/api/v1/documents/{document_id}/versions/{version_id}/source-pages/{page_number}/render",
        response_model=None,
    )
    async def get_version_source_page_render(
        document_id: str, version_id: str, page_number: int
    ) -> FileResponse | JSONResponse:
        with connect(resolved) as connection:
            row = connection.execute(
                """
                SELECT results.render_sha256, results.render_media_type,
                       versions.render_config_version
                FROM ingestion_page_results AS results
                JOIN document_versions AS versions ON versions.version_id = results.version_id
                WHERE versions.document_id = ? AND results.version_id = ?
                  AND results.page_number = ? AND results.render_sha256 IS NOT NULL
                """,
                (document_id, version_id, page_number),
            ).fetchone()
        if row is None:
            return error_response(404, "not_found", "未找到标准页渲染结果。")
        if row["render_config_version"] != render_configuration_version(resolved.render_image):
            return error_response(409, "render_configuration_stale", "标准页正在按新渲染配置重建。")
        path = LocalObjectStore(resolved.object_store_path).path_for(row["render_sha256"])
        if not path.is_file():
            return error_response(503, "render_unavailable", "标准页渲染结果暂不可用。")
        return FileResponse(path, media_type=row["render_media_type"])

    @app.post("/api/v1/documents/{document_id}/versions/{version_id}/retry")
    async def retry_version(
        document_id: str,
        version_id: str,
        command: LifecycleCommand,
        request: Request,
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            result = retry_failed_version(
                resolved,
                actor_id=actor.actor_id,
                document_id=document_id,
                version_id=version_id,
                idempotency_key=request.headers.get("Idempotency-Key", ""),
                reason=command.reason,
            )
        except IngestionRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(status_code=202, content=result)

    @app.get("/api/v1/documents/{document_id}/events")
    async def get_document_events(document_id: str) -> JSONResponse:
        events = read_lifecycle_events(resolved, document_id)
        if events is None:
            return error_response(404, "not_found", "未找到请求的资源。")
        return JSONResponse(content={"events": events})

    @app.post("/api/v1/documents/{document_id}/versions/{version_id}/void")
    async def void_document_version(
        document_id: str,
        version_id: str,
        command: LifecycleCommand,
        request: Request,
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            result = void_version(
                resolved,
                actor_id=actor.actor_id,
                document_id=document_id,
                version_id=version_id,
                idempotency_key=request.headers.get("Idempotency-Key", ""),
                reason=command.reason,
            )
        except IngestionRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(content=result)

    @app.post("/api/v1/documents/{document_id}/versions/{version_id}/rollback")
    async def rollback_document_version(
        document_id: str,
        version_id: str,
        command: LifecycleCommand,
        request: Request,
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            result = rollback_to_version(
                resolved,
                actor_id=actor.actor_id,
                document_id=document_id,
                version_id=version_id,
                idempotency_key=request.headers.get("Idempotency-Key", ""),
                reason=command.reason,
            )
        except IngestionRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(status_code=202, content=result)

    @app.delete("/api/v1/documents/{document_id}")
    async def delete_document(
        document_id: str, command: LifecycleCommand, request: Request
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            result = soft_delete_document(
                resolved,
                actor_id=actor.actor_id,
                document_id=document_id,
                idempotency_key=request.headers.get("Idempotency-Key", ""),
                reason=command.reason,
            )
        except IngestionRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(content=result)

    @app.post("/api/v1/documents/{document_id}/restore")
    async def restore_deleted_document(
        document_id: str, command: LifecycleCommand, request: Request
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            result = restore_document(
                resolved,
                actor_id=actor.actor_id,
                document_id=document_id,
                idempotency_key=request.headers.get("Idempotency-Key", ""),
                reason=command.reason,
            )
        except IngestionRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(content=result)

    @app.get("/api/v1/documents/{document_id}")
    async def get_document(document_id: str) -> JSONResponse:
        with connect(resolved) as connection:
            row = connection.execute(
                """
                SELECT document_id, current_version_id, created_at,
                       deleted_at, deleted_by, deletion_reason
                FROM documents WHERE document_id = ?
                """,
                (document_id,),
            ).fetchone()
        if row is None:
            return error_response(404, "not_found", "未找到请求的资源。")
        return JSONResponse(content=dict(row))

    @app.get("/api/v1/documents/{document_id}/versions/{version_id}")
    async def get_document_version(document_id: str, version_id: str) -> JSONResponse:
        with connect(resolved) as connection:
            row = connection.execute(
                """
                SELECT version_id, document_id, status, source_sha256,
                       source_filename, source_size_bytes, created_at, ready_at,
                       source_operation, source_version_id,
                       voided_at, voided_by, void_reason
                FROM document_versions
                WHERE document_id = ? AND version_id = ?
                """,
                (document_id, version_id),
            ).fetchone()
        if row is None:
            return error_response(404, "not_found", "未找到请求的资源。")
        return JSONResponse(
            content={
                "version_id": row["version_id"],
                "document_id": row["document_id"],
                "status": row["status"],
                "source": {
                    "sha256": row["source_sha256"],
                    "filename": row["source_filename"],
                    "size_bytes": row["source_size_bytes"],
                },
                "created_at": row["created_at"],
                "ready_at": row["ready_at"],
                "source_relation": (
                    None
                    if row["source_version_id"] is None
                    else {
                        "operation": row["source_operation"],
                        "source_version_id": row["source_version_id"],
                    }
                ),
                "voided_at": row["voided_at"],
                "voided_by": row["voided_by"],
                "void_reason": row["void_reason"],
            }
        )

    def rendering_warning_payload(
        connection: sqlite3.Connection, *, document_id: str, version_id: str
    ) -> dict[str, Any] | None:
        version = connection.execute(
            """
            SELECT version_id, render_config_version FROM document_versions
            WHERE document_id = ? AND version_id = ?
            """,
            (document_id, version_id),
        ).fetchone()
        if version is None:
            return None
        rows = read_warning_rows(connection, version_id=version_id)
        config_version = str(
            version["render_config_version"] or render_configuration_version(resolved.render_image)
        )
        return {
            "document_id": document_id,
            "version_id": version_id,
            "render_config_version": config_version,
            "summary": summarize_rows(rows),
            "warnings": [serialize_warning(row) for row in rows],
        }

    def warning_version_is_curatable(
        connection: sqlite3.Connection, *, document_id: str, version_id: str
    ) -> bool:
        return (
            connection.execute(
                """
                SELECT 1
                FROM documents
                JOIN document_versions AS versions
                  ON versions.version_id = documents.current_version_id
                WHERE documents.document_id = ? AND versions.version_id = ?
                  AND documents.deleted_at IS NULL AND versions.status = 'ready'
                  AND versions.render_config_version = ?
                """,
                (
                    document_id,
                    version_id,
                    render_configuration_version(resolved.render_image),
                ),
            ).fetchone()
            is not None
        )

    @app.get("/api/v1/documents/{document_id}/versions/{version_id}/rendering-warnings")
    async def get_rendering_warnings(document_id: str, version_id: str) -> JSONResponse:
        with connect(resolved) as connection:
            payload = rendering_warning_payload(
                connection, document_id=document_id, version_id=version_id
            )
        if payload is None:
            return error_response(404, "not_found", "未找到请求的资源。")
        return JSONResponse(content=payload)

    @app.post(
        "/api/v1/documents/{document_id}/versions/{version_id}/rendering-warnings/confirm-all"
    )
    async def confirm_all_rendering_warnings(
        document_id: str,
        version_id: str,
        command: ConfirmAllRenderingWarnings,
        request: Request,
    ) -> JSONResponse:
        actor = actors.resolve(request)
        confirmed_count = 0
        with transaction(resolved) as connection:
            payload = rendering_warning_payload(
                connection, document_id=document_id, version_id=version_id
            )
            if payload is None:
                return error_response(404, "not_found", "未找到请求的资源。")
            if not warning_version_is_curatable(
                connection, document_id=document_id, version_id=version_id
            ):
                return error_response(
                    409,
                    "version_not_current",
                    "只能确认当前 ready 版本的渲染警告。",
                )
            current_versions = {warning["render_config_version"] for warning in payload["warnings"]}
            if current_versions and current_versions != {command.render_config_version}:
                return error_response(
                    409,
                    "rendering_warnings_stale",
                    "渲染配置或警告集合已变化，请重新检查后确认。",
                )
            current_unconfirmed_ids = {
                str(warning["warning_id"])
                for warning in payload["warnings"]
                if warning["status"] == "unconfirmed"
            }
            supplied_ids = set(command.warning_ids)
            if (
                len(supplied_ids) != len(command.warning_ids)
                or supplied_ids != current_unconfirmed_ids
            ):
                return error_response(
                    409,
                    "rendering_warnings_stale",
                    "渲染配置或警告集合已变化，请重新检查后确认。",
                )
            confirmed_at = timestamp()
            for warning in payload["warnings"]:
                if warning["status"] == "confirmed":
                    continue
                if confirm_warning(
                    connection,
                    warning_id=str(warning["warning_id"]),
                    version_id=version_id,
                    actor_id=actor.actor_id,
                    confirmed_at=confirmed_at,
                ):
                    confirmed_count += 1
            refreshed = rendering_warning_payload(
                connection, document_id=document_id, version_id=version_id
            )
        assert refreshed is not None
        return JSONResponse(
            content={
                "confirmed_count": confirmed_count,
                "summary": refreshed["summary"],
                "render_config_version": refreshed["render_config_version"],
                "warnings": refreshed["warnings"],
            }
        )

    @app.post(
        "/api/v1/documents/{document_id}/versions/{version_id}"
        "/rendering-warnings/{warning_id}/confirm"
    )
    async def confirm_one_rendering_warning(
        document_id: str, version_id: str, warning_id: str, request: Request
    ) -> JSONResponse:
        actor = actors.resolve(request)
        with transaction(resolved) as connection:
            version = connection.execute(
                "SELECT 1 FROM document_versions WHERE document_id = ? AND version_id = ?",
                (document_id, version_id),
            ).fetchone()
            if version is None:
                return error_response(404, "not_found", "未找到请求的资源。")
            if not warning_version_is_curatable(
                connection, document_id=document_id, version_id=version_id
            ):
                return error_response(
                    409,
                    "version_not_current",
                    "只能确认当前 ready 版本的渲染警告。",
                )
            if not confirm_warning(
                connection,
                warning_id=warning_id,
                version_id=version_id,
                actor_id=actor.actor_id,
                confirmed_at=timestamp(),
            ):
                return error_response(404, "not_found", "未找到请求的资源。")
            row = next(
                row
                for row in read_warning_rows(connection, version_id=version_id)
                if row["warning_id"] == warning_id
            )
        return JSONResponse(content=serialize_warning(row))

    def publication_warning_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                """
                SELECT warnings.warning_id, warnings.version_id, warnings.page_number,
                       documents.document_id,
                       warnings.code, warnings.details_json,
                       warnings.render_config_version, warnings.observed_at,
                       confirmations.actor_id AS confirmed_by,
                       confirmations.confirmed_at
                FROM documents
                JOIN document_versions AS versions
                  ON versions.version_id = documents.current_version_id
                JOIN rendering_warnings AS warnings
                  ON warnings.version_id = versions.version_id AND warnings.active = 1
                LEFT JOIN rendering_warning_confirmations AS confirmations
                  ON confirmations.warning_id = warnings.warning_id
                WHERE documents.deleted_at IS NULL AND versions.status = 'ready'
                ORDER BY warnings.version_id, warnings.page_number,
                         warnings.code, warnings.warning_id
                """
            ).fetchall()
        )

    def publication_preflight_content(connection: sqlite3.Connection) -> dict[str, Any]:
        rows = publication_warning_rows(connection)
        summary = summarize_rows(rows)
        current_config = render_configuration_version(resolved.render_image)
        stale = connection.execute(
            """
            SELECT versions.document_id, versions.version_id
            FROM documents
            JOIN document_versions AS versions
              ON versions.version_id = documents.current_version_id
            WHERE documents.deleted_at IS NULL AND versions.status = 'ready'
              AND COALESCE(versions.render_config_version, '') <> ?
            ORDER BY versions.ready_at, versions.version_id
            """,
            (current_config,),
        ).fetchall()
        first_unconfirmed = next(
            (row for row in rows if row["confirmed_at"] is None),
            None,
        )
        href = None
        if first_unconfirmed is not None:
            href = (
                "/curation?filter=rendering-warnings"
                f"&document={first_unconfirmed['document_id']}"
                f"&version={first_unconfirmed['version_id']}"
                f"&page={first_unconfirmed['page_number']}"
                f"&warning={first_unconfirmed['warning_id']}"
            )
        return {
            "can_publish": summary["unconfirmed"] == 0 and not stale,
            "summary": summary,
            "stale_render_versions": len(stale),
            "href": href,
        }

    @app.get("/api/v1/publications/preflight")
    async def get_publication_preflight() -> JSONResponse:
        with connect(resolved) as connection:
            content = publication_preflight_content(connection)
        return JSONResponse(content=content)

    @app.post("/api/v1/publications/preflight")
    async def validate_publication_preflight() -> JSONResponse:
        with connect(resolved) as connection:
            content = publication_preflight_content(connection)
        if content["can_publish"]:
            return JSONResponse(content=content)
        if content["stale_render_versions"]:
            return error_response(
                409,
                "render_configuration_stale",
                "渲染配置已变化，当前版本的标准页正在等待确定性重建。",
                {"stale_versions": content["stale_render_versions"]},
            )
        summary = content["summary"]
        return error_response(
            409,
            "rendering_warnings_unconfirmed",
            (
                f"仍有 {summary['unconfirmed_pages']} 页 / "
                f"{summary['unconfirmed']} 条渲染警告未确认，发布被阻止。"
            ),
            {
                "unconfirmed": summary["unconfirmed"],
                "unconfirmed_pages": summary["unconfirmed_pages"],
                "href": content["href"],
            },
        )

    def publication_error(error: PublicationRequestError) -> JSONResponse:
        return error_response(error.status_code, error.code, error.message, error.details)

    @app.get("/api/v1/publications")
    async def publication_workspace() -> JSONResponse:
        return JSONResponse(content=read_publication_workspace(resolved))

    @app.post("/api/v1/publications/candidates", status_code=201)
    async def create_publication_candidate(request: Request) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            content = create_candidate(resolved, actor_id=actor.actor_id)
        except PublicationRequestError as error:
            return publication_error(error)
        return JSONResponse(status_code=201, content=content)

    @app.get("/api/v1/publications/candidates/{candidate_id}")
    async def get_publication_candidate(candidate_id: str) -> JSONResponse:
        content = read_candidate(resolved, candidate_id)
        if content is None:
            return error_response(404, "candidate_not_found", "未找到发布候选。")
        return JSONResponse(content=content)

    @app.post("/api/v1/publications/candidates/{candidate_id}/confirm")
    async def confirm_publication_candidate(request: Request, candidate_id: str) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            status_code, content = confirm_publication_candidate_command(
                resolved, candidate_id=candidate_id, actor_id=actor.actor_id
            )
        except PublicationRequestError as error:
            return publication_error(error)
        return JSONResponse(status_code=status_code, content=content)

    @app.post("/api/v1/publications/tasks/{job_id}/retry", status_code=202)
    async def retry_publication_task(request: Request, job_id: str) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            content = retry_publication_job(resolved, failed_job_id=job_id, actor_id=actor.actor_id)
        except PublicationRequestError as error:
            return publication_error(error)
        return JSONResponse(status_code=202, content=content)

    @app.get("/api/v1/publications/current")
    async def get_current_publication(request: Request) -> Response:
        artifact = read_current_artifact(resolved)
        if artifact is None:
            return error_response(404, "current_publication_not_found", "尚无当前产物。")
        etag = f'"{artifact.sha256}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(content=artifact_content(artifact), headers={"ETag": etag})

    @app.get("/api/v1/publications/{publication_seq}/artifact")
    async def download_publication_artifact(request: Request, publication_seq: int) -> Response:
        artifact = read_artifact(resolved, publication_seq)
        if artifact is None or not artifact.path.is_file():
            return error_response(404, "publication_artifact_not_found", "未找到发布产物。")
        etag = f'"{artifact.sha256}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        common_headers = {
            "Accept-Ranges": "bytes",
            "ETag": etag,
            "Content-Disposition": (
                f'attachment; filename="pptextract-publication-{publication_seq}.zip"'
            ),
        }
        range_header = request.headers.get("range")
        if range_header:
            if not range_header.startswith("bytes=") or "," in range_header:
                return Response(
                    status_code=416,
                    headers={
                        **common_headers,
                        "Content-Range": f"bytes */{artifact.size_bytes}",
                    },
                )
            start_text, separator, end_text = range_header[6:].partition("-")
            try:
                if not separator:
                    raise ValueError
                if start_text:
                    start = int(start_text)
                    end = int(end_text) if end_text else artifact.size_bytes - 1
                else:
                    suffix = int(end_text)
                    start = max(0, artifact.size_bytes - suffix)
                    end = artifact.size_bytes - 1
                if start < 0 or end < start or start >= artifact.size_bytes:
                    raise ValueError
                end = min(end, artifact.size_bytes - 1)
            except ValueError:
                return Response(
                    status_code=416,
                    headers={
                        **common_headers,
                        "Content-Range": f"bytes */{artifact.size_bytes}",
                    },
                )
            return StreamingResponse(
                iter_file(artifact.path, start=start, end=end),
                status_code=206,
                media_type=artifact.media_type,
                headers={
                    **common_headers,
                    "Content-Range": f"bytes {start}-{end}/{artifact.size_bytes}",
                    "Content-Length": str(end - start + 1),
                },
            )
        return StreamingResponse(
            iter_file(artifact.path),
            media_type=artifact.media_type,
            headers={**common_headers, "Content-Length": str(artifact.size_bytes)},
        )

    @app.get("/api/v1/curation/pages")
    async def list_curation_pages(review_status: str = "pending") -> JSONResponse:
        if review_status not in {"pending", "approved", "excluded", "inherited", "all"}:
            return error_response(422, "invalid_request", "审核状态无效。")
        with connect(resolved) as connection:
            rows = connection.execute(
                """
                SELECT pv.page_id, p.chunk_id, results.version_id,
                       versions.document_id, results.page_number, pv.review_status,
                       pv.source_content_json, results.source_slide_id,
                       results.relationship_id, results.source_part,
                       results.hidden, results.enabled, jobs.job_id AS enable_job_id,
                       jobs.status AS enable_status, jobs.error_json AS enable_error,
                       pv.inherited_from_page_version_id, pv.reviewed_by,
                       pv.reviewed_at, pv.review_source_version_id,
                       pv.exclusion_reason, pv.exclusion_note
                FROM ingestion_page_results AS results
                JOIN document_versions AS versions
                  ON versions.version_id = results.version_id
                JOIN documents AS d
                  ON d.document_id = versions.document_id
                 AND d.current_version_id = versions.version_id
                LEFT JOIN page_versions AS pv
                  ON pv.version_id = results.version_id
                 AND pv.page_number = results.page_number
                LEFT JOIN pages AS p ON p.page_id = pv.page_id
                LEFT JOIN jobs ON jobs.job_id = results.enable_job_id
                WHERE d.deleted_at IS NULL AND versions.status = 'ready'
                  AND (
                    ? = 'all'
                    OR (? = 'inherited' AND pv.inherited_from_page_version_id IS NOT NULL)
                    OR pv.review_status = ?
                  )
                ORDER BY versions.document_id, results.page_number, pv.page_id
                """,
                (review_status, review_status, review_status),
            ).fetchall()
            warning_rows_by_version = {
                version_id: read_warning_rows(connection, version_id=version_id)
                for version_id in {str(row["version_id"]) for row in rows}
            }
        pages = []
        for row in rows:
            version_warning_rows = warning_rows_by_version[str(row["version_id"])]
            page_warning_rows = [
                warning
                for warning in version_warning_rows
                if int(warning["page_number"]) == int(row["page_number"])
            ]
            source_content = (
                None
                if row["source_content_json"] is None
                else json.loads(row["source_content_json"])
            )
            titles = None if source_content is None else source_content.get("titles")
            enablement = None
            if bool(row["hidden"]):
                enablement = {
                    "status": row["enable_status"] or "not_started",
                    "job_id": row["enable_job_id"],
                    "error": (
                        None if row["enable_error"] is None else json.loads(row["enable_error"])
                    ),
                }
            page_payload = {
                "page_id": row["page_id"],
                "chunk_id": row["chunk_id"],
                "document_id": row["document_id"],
                "version_id": row["version_id"],
                "page_number": row["page_number"],
                "review_status": row["review_status"],
                "title": titles[0] if titles else None,
                "hidden": bool(row["hidden"]),
                "enabled": bool(row["enabled"]),
                "source_reference": {
                    "slide_id": row["source_slide_id"],
                    "relationship_id": row["relationship_id"],
                    "part": row["source_part"],
                },
                "enablement": enablement,
            }
            if row["review_status"] is not None:
                page_payload["review"] = {
                    "status": row["review_status"],
                    "reviewed_by": row["reviewed_by"],
                    "reviewed_at": row["reviewed_at"],
                    "source_version_id": row["review_source_version_id"],
                    "inherited_from_page_version_id": row["inherited_from_page_version_id"],
                    "exclusion_reason": row["exclusion_reason"],
                    "exclusion_note": row["exclusion_note"],
                }
            if version_warning_rows:
                page_payload["rendering_warnings"] = summarize_rows(page_warning_rows)
                page_payload["version_rendering_warnings"] = summarize_rows(version_warning_rows)
            pages.append(page_payload)
        return JSONResponse(content={"pages": pages})

    @app.post("/api/v1/curation/runtime-facts/samples")
    async def create_curation_timing_sample(
        command: CurationTimingSample, request: Request
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            result = record_timing_sample(
                resolved,
                sample_id=command.sample_id,
                page_id=command.page_id,
                version_id=command.version_id,
                actor_id=actor.actor_id,
                stage=command.stage,
                duration_ms=command.duration_ms,
            )
        except RuntimeFactError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(
            status_code=201 if result["status"] == "recorded" else 200,
            content=result,
        )

    @app.get("/api/v1/curation/runtime-facts")
    async def get_curation_runtime_facts(format: str = "json") -> Response:
        if format not in {"json", "csv"}:
            return error_response(422, "invalid_format", "运行事实仅支持 JSON 或 CSV。")
        facts = read_runtime_facts(resolved)
        if format == "csv":
            return Response(
                content=runtime_facts_csv(facts),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=curation-runtime-facts.csv"},
            )
        return JSONResponse(content=facts)

    @app.post(
        "/api/v1/documents/{document_id}/versions/{version_id}/source-pages/{page_number}/enable"
    )
    async def enable_hidden_page(
        document_id: str, version_id: str, page_number: int, request: Request
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            accepted = accept_hidden_page_enablement(
                resolved,
                actor_id=actor.actor_id,
                document_id=document_id,
                version_id=version_id,
                page_number=page_number,
                idempotency_key=request.headers.get("Idempotency-Key", ""),
            )
        except IngestionRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(
            status_code=200 if accepted.status == "no_change" else 202,
            content={
                "document_id": accepted.document_id,
                "version_id": accepted.version_id,
                "page_number": accepted.page_number,
                "job_id": accepted.job_id,
                "status": accepted.status,
                "page_id": accepted.page_id,
            },
        )

    @app.get("/api/v1/pages/{page_id}")
    async def get_page(page_id: str) -> JSONResponse:
        with connect(resolved) as connection:
            row = connection.execute(
                """
                SELECT pv.page_id, p.chunk_id, pv.document_id, pv.version_id,
                       pv.page_number, pv.review_status, pv.fingerprint_version,
                       pv.fingerprint_sha256, pv.source_content_json,
                       pv.render_sha256, pv.render_media_type, pv.render_dpi,
                       pv.render_width_px, pv.render_height_px,
                       pv.current_snapshot_id, pv.prefill_snapshot_id,
                       pv.inherited_from_page_version_id, pv.reviewed_by,
                       pv.reviewed_at, pv.review_source_version_id,
                       pv.exclusion_reason, pv.exclusion_note
                FROM page_versions AS pv
                JOIN pages AS p ON p.page_id = pv.page_id
                JOIN documents AS d
                  ON d.document_id = pv.document_id
                 AND d.current_version_id = pv.version_id
                WHERE d.deleted_at IS NULL AND pv.page_id = ?
                """,
                (page_id,),
            ).fetchone()
            annotation = (
                None
                if row is None
                else _read_curation_snapshot(connection, row["current_snapshot_id"])
            )
            prefill = (
                None
                if row is None
                else _read_curation_snapshot(connection, row["prefill_snapshot_id"])
            )
            curation = None if row is None else read_page_curation(connection, page_id)
            page_warning_rows = (
                []
                if row is None
                else [
                    warning
                    for warning in read_warning_rows(connection, version_id=str(row["version_id"]))
                    if int(warning["page_number"]) == int(row["page_number"])
                ]
            )
        if row is None:
            return error_response(404, "not_found", "未找到请求的资源。")
        return JSONResponse(
            content={
                "page_id": row["page_id"],
                "chunk_id": row["chunk_id"],
                "document_id": row["document_id"],
                "version_id": row["version_id"],
                "page_number": row["page_number"],
                "review_status": row["review_status"],
                "review": {
                    "status": row["review_status"],
                    "reviewed_by": row["reviewed_by"],
                    "reviewed_at": row["reviewed_at"],
                    "source_version_id": row["review_source_version_id"],
                    "inherited_from_page_version_id": row["inherited_from_page_version_id"],
                    "exclusion_reason": row["exclusion_reason"],
                    "exclusion_note": row["exclusion_note"],
                },
                "annotation": annotation,
                "prefill": prefill,
                "fingerprint": {
                    "version": row["fingerprint_version"],
                    "sha256": row["fingerprint_sha256"],
                },
                "source_content": json.loads(row["source_content_json"]),
                "curation": curation,
                "standard_render": {
                    "sha256": row["render_sha256"],
                    "media_type": row["render_media_type"],
                    "dpi": row["render_dpi"],
                    "width_px": row["render_width_px"],
                    "height_px": row["render_height_px"],
                    "url": f"/api/v1/pages/{page_id}/render",
                },
                "rendering_warnings": {
                    "summary": summarize_rows(page_warning_rows),
                    "warnings": [serialize_warning(warning) for warning in page_warning_rows],
                },
            }
        )

    @app.post("/api/v1/pages/{page_id}/curation/snapshots")
    async def create_source_snapshot(
        page_id: str, command: SaveSourceSnapshot, request: Request
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            curation = save_source_snapshot(
                resolved,
                page_id=page_id,
                actor_id=actor.actor_id,
                base_snapshot_id=command.base_snapshot_id,
                titles=command.titles,
                body=command.body,
            )
        except CurationRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(status_code=201, content={"curation": curation})

    @app.get("/api/v1/pages/{page_id}/repeated-footer-noise/candidates/{source_ref}")
    async def get_repeated_footer_noise_candidate(page_id: str, source_ref: str) -> JSONResponse:
        try:
            with connect(resolved) as connection:
                candidate = preview_candidate(connection, page_id=page_id, source_ref=source_ref)
        except RepeatedFooterNoiseError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(content={"candidate": candidate})

    @app.post(
        "/api/v1/pages/{page_id}/repeated-footer-noise/confirmations",
        status_code=201,
    )
    async def create_repeated_footer_noise_confirmation(
        page_id: str, command: ConfirmRepeatedFooterNoise, request: Request
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            confirmation = confirm_candidate(
                resolved,
                page_id=page_id,
                actor_id=actor.actor_id,
                candidate_id=command.candidate_id,
                source_ref=command.source_ref,
                note=command.note,
            )
        except RepeatedFooterNoiseError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(status_code=201, content={"confirmation": confirmation})

    @app.post("/api/v1/repeated-footer-noise/confirmations/{confirmation_id}/revoke")
    async def revoke_repeated_footer_noise_confirmation(
        confirmation_id: str, command: RevokeRepeatedFooterNoise, request: Request
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            confirmation = revoke_confirmation(
                resolved,
                confirmation_id=confirmation_id,
                actor_id=actor.actor_id,
                note=command.note,
            )
        except RepeatedFooterNoiseError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(content={"confirmation": confirmation})

    @app.post("/api/v1/pages/{page_id}/curation/source-confirmation")
    async def confirm_page_source(
        page_id: str, command: SnapshotCommand, request: Request
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            curation = confirm_source_snapshot(
                resolved,
                page_id=page_id,
                actor_id=actor.actor_id,
                snapshot_id=command.snapshot_id,
            )
        except CurationRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(content={"curation": curation})

    @app.post(
        "/api/v1/pages/{page_id}/curation/image-sources/{source_ref}",
        status_code=201,
    )
    async def save_page_image_source(
        page_id: str,
        source_ref: str,
        command: SaveImageSourceDisposition,
        request: Request,
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            curation = save_image_source_disposition(
                resolved,
                page_id=page_id,
                actor_id=actor.actor_id,
                base_snapshot_id=command.base_snapshot_id,
                source_ref=source_ref,
                disposition=command.disposition,
                summary=command.summary,
                ignore_reason=command.ignore_reason,
                ignore_note=command.ignore_note,
            )
        except CurationRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(status_code=201, content={"curation": curation})

    @app.post("/api/v1/pages/{page_id}/curation/source-review")
    async def review_page_source(
        page_id: str, command: SnapshotCommand, request: Request
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            curation = complete_source_review(
                resolved,
                page_id=page_id,
                actor_id=actor.actor_id,
                snapshot_id=command.snapshot_id,
            )
        except CurationRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(content={"curation": curation})

    @app.post("/api/v1/pages/{page_id}/curation/visuals", status_code=201)
    async def create_capture_visual(
        page_id: str, command: SaveCaptureVisual, request: Request
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            curation = save_capture_visual(
                resolved,
                page_id=page_id,
                actor_id=actor.actor_id,
                base_snapshot_id=command.base_snapshot_id,
                summary=command.summary,
                visual_type=command.visual_type,
                bounds=command.bounds.model_dump(),
            )
        except CurationRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(status_code=201, content=_visual_mutation_content(resolved, curation))

    @app.patch("/api/v1/pages/{page_id}/curation/visuals/{visual_ref}", status_code=201)
    async def edit_capture_visual(
        page_id: str,
        visual_ref: str,
        command: SaveCaptureVisual,
        request: Request,
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            curation = update_capture_visual(
                resolved,
                page_id=page_id,
                visual_ref=visual_ref,
                actor_id=actor.actor_id,
                base_snapshot_id=command.base_snapshot_id,
                summary=command.summary,
                visual_type=command.visual_type,
                bounds=command.bounds.model_dump(),
            )
        except CurationRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(status_code=201, content=_visual_mutation_content(resolved, curation))

    @app.delete("/api/v1/pages/{page_id}/curation/visuals/{visual_ref}", status_code=201)
    async def remove_capture_visual(
        page_id: str,
        visual_ref: str,
        command: DeleteCaptureVisual,
        request: Request,
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            curation = delete_capture_visual(
                resolved,
                page_id=page_id,
                visual_ref=visual_ref,
                actor_id=actor.actor_id,
                base_snapshot_id=command.base_snapshot_id,
            )
        except CurationRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(status_code=201, content=_visual_mutation_content(resolved, curation))

    @app.post(
        "/api/v1/pages/{page_id}/curation/visuals/{visual_ref}/move",
        status_code=201,
    )
    async def reorder_capture_visual(
        page_id: str,
        visual_ref: str,
        command: MoveCaptureVisual,
        request: Request,
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            curation = move_capture_visual(
                resolved,
                page_id=page_id,
                visual_ref=visual_ref,
                actor_id=actor.actor_id,
                base_snapshot_id=command.base_snapshot_id,
                direction=command.direction,
            )
        except CurationRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(status_code=201, content=_visual_mutation_content(resolved, curation))

    @app.post("/api/v1/pages/{page_id}/curation/source-completeness", status_code=201)
    async def mark_page_source_complete(
        page_id: str, command: SnapshotCommand, request: Request
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            curation = mark_capture_source_complete(
                resolved,
                page_id=page_id,
                actor_id=actor.actor_id,
                snapshot_id=command.snapshot_id,
            )
        except CurationRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(status_code=201, content=_visual_mutation_content(resolved, curation))

    @app.post("/api/v1/pages/{page_id}/approve")
    async def approve_curation_page(
        page_id: str, command: SnapshotCommand, request: Request
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            result = approve_page(
                resolved,
                page_id=page_id,
                actor_id=actor.actor_id,
                snapshot_id=command.snapshot_id,
            )
        except CurationRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(content=result)

    @app.post("/api/v1/pages/{page_id}/exclude")
    async def exclude_curation_page(
        page_id: str, command: ExcludePageCommand, request: Request
    ) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            result = exclude_page(
                resolved,
                page_id=page_id,
                actor_id=actor.actor_id,
                reason=command.reason,
                note=command.note,
            )
        except CurationRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(content=result)

    @app.post("/api/v1/pages/{page_id}/reopen")
    async def reopen_curation_page(page_id: str, request: Request) -> JSONResponse:
        actor = actors.resolve(request)
        try:
            result = reopen_page(resolved, page_id=page_id, actor_id=actor.actor_id)
        except CurationRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(content=result)

    @app.post("/api/v1/pages/batch-exclude")
    async def batch_exclude_curation_pages(
        command: BatchExcludePagesCommand, request: Request
    ) -> JSONResponse:
        if len(set(command.page_ids)) != len(command.page_ids):
            return error_response(422, "invalid_request", "批量排除不能包含重复页面。")
        actor = actors.resolve(request)
        try:
            result = batch_exclude_pages(
                resolved,
                page_ids=command.page_ids,
                actor_id=actor.actor_id,
                reason=command.reason,
                note=command.note,
            )
        except CurationRequestError as error:
            return error_response(error.status_code, error.code, error.message)
        return JSONResponse(status_code=200 if result["complete"] else 207, content=result)

    @app.get("/api/v1/pages/{page_id}/render", response_model=None)
    async def get_page_render(page_id: str) -> FileResponse | JSONResponse:
        with connect(resolved) as connection:
            row = connection.execute(
                """
                SELECT pv.render_sha256, pv.render_media_type,
                       versions.render_config_version
                FROM page_versions AS pv
                JOIN documents AS d
                  ON d.document_id = pv.document_id
                 AND d.current_version_id = pv.version_id
                JOIN document_versions AS versions ON versions.version_id = pv.version_id
                WHERE d.deleted_at IS NULL AND pv.page_id = ?
                """,
                (page_id,),
            ).fetchone()
        if row is None:
            return error_response(404, "not_found", "未找到请求的资源。")
        if row["render_config_version"] != render_configuration_version(resolved.render_image):
            return error_response(409, "render_configuration_stale", "标准页正在按新渲染配置重建。")
        path = LocalObjectStore(resolved.object_store_path).path_for(row["render_sha256"])
        if not path.is_file():
            return error_response(503, "render_unavailable", "标准页渲染结果暂不可用。")
        return FileResponse(path, media_type=row["render_media_type"])

    @app.get(
        "/api/v1/pages/{page_id}/source-images/{source_ref}",
        response_model=None,
    )
    async def get_source_image(page_id: str, source_ref: str) -> Response | JSONResponse:
        with connect(resolved) as connection:
            try:
                source_image = read_source_image(connection, page_id=page_id, source_ref=source_ref)
            except CurationRequestError as error:
                return error_response(error.status_code, error.code, error.message)
        if source_image is None:
            return error_response(404, "source_image_not_found", "未找到图片来源原始字节。")
        payload, media_type = source_image
        return Response(
            content=payload,
            media_type=media_type,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": (
                    "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"
                ),
            },
        )

    @app.get("/api/v1/health")
    async def health() -> JSONResponse:
        components: dict[str, dict[str, Any]] = {
            "api": {"status": "ready"},
            "database": {"status": "unavailable"},
            "object_store": {"status": "unavailable"},
            "worker": {"status": "unavailable", "worker_id": resolved.worker_id},
        }

        try:
            with connect(resolved) as connection:
                connection.execute("SELECT 1").fetchone()
            if database_path_is_local(resolved.database_path):
                components["database"] = {"status": "ready"}
        except (OSError, RuntimeError, sqlite3.Error):
            pass

        try:
            object_store = LocalObjectStore(resolved.object_store_path)
            object_store.check_writable()
            if database_path_is_local(resolved.object_store_path):
                components["object_store"] = {"status": "ready"}
        except OSError:
            pass

        try:
            if worker_is_fresh(resolved):
                components["worker"] = {"status": "ready", "worker_id": resolved.worker_id}
        except sqlite3.Error:
            pass

        ready = all(component["status"] == "ready" for component in components.values())
        content = {
            "status": "ready" if ready else "degraded",
            "config_version": resolved.config_version,
            "components": components,
        }
        return JSONResponse(status_code=200 if ready else 503, content=content)

    if (resolved.web_dist_path / "index.html").is_file():

        @app.get("/{requested_path:path}", include_in_schema=False, response_model=None)
        async def product_shell(requested_path: str) -> FileResponse | JSONResponse:
            if requested_path == "api" or requested_path.startswith("api/"):
                return error_response(404, "not_found", "未找到请求的资源。")
            requested_file = (resolved.web_dist_path / requested_path).resolve()
            try:
                requested_file.relative_to(resolved.web_dist_path)
            except ValueError:
                return error_response(404, "not_found", "未找到请求的资源。")
            if requested_file.is_file():
                return FileResponse(requested_file)
            return FileResponse(
                resolved.web_dist_path / "index.html",
                headers={"Cache-Control": "no-cache"},
            )

    return app


app = create_app()
