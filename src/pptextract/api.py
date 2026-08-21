from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException

from pptextract.auth import ActorProvider, HeaderActorProvider
from pptextract.config import Settings
from pptextract.db import connect, database_path_is_local, initialize_database
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
from pptextract.lifecycle import (
    read_lifecycle_events,
    restore_document,
    retry_failed_version,
    rollback_to_version,
    soft_delete_document,
    void_version,
)
from pptextract.object_store import LocalObjectStore
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
               visual_type, bounds_json, source_visual_ref, confirmed
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
                    None
                    if visual["bounds_json"] is None
                    else json.loads(visual["bounds_json"])
                ),
                "source_visual_ref": visual["source_visual_ref"],
                "confirmed": bool(visual["confirmed"]),
            }
            for visual in visuals
        ],
    }


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
        processing_documents = []
        for version in active_versions:
            requires_mapping = version["job_status"] == "requires_action"
            processing_documents.append(
                {
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
                {"id": "curatable", "label": "可策展", "documents": []},
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

    @app.get(
        "/api/v1/documents/{document_id}/versions/{version_id}/page-mapping"
    )
    async def get_page_mapping(document_id: str, version_id: str) -> JSONResponse:
        result = read_page_mapping(
            resolved, document_id=document_id, version_id=version_id
        )
        if result is None:
            return error_response(404, "not_found", "未找到请求的资源。")
        payload, etag = result
        return JSONResponse(content=payload, headers={"ETag": etag})

    @app.put(
        "/api/v1/documents/{document_id}/versions/{version_id}"
        "/page-mapping/cases/{case_id}"
    )
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

    @app.post(
        "/api/v1/documents/{document_id}/versions/{version_id}/page-mapping/confirm"
    )
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
        "/api/v1/documents/{document_id}/versions/{version_id}"
        "/source-pages/{page_number}/render",
        response_model=None,
    )
    async def get_version_source_page_render(
        document_id: str, version_id: str, page_number: int
    ) -> FileResponse | JSONResponse:
        with connect(resolved) as connection:
            row = connection.execute(
                """
                SELECT results.render_sha256, results.render_media_type
                FROM ingestion_page_results AS results
                JOIN document_versions AS versions ON versions.version_id = results.version_id
                WHERE versions.document_id = ? AND results.version_id = ?
                  AND results.page_number = ? AND results.render_sha256 IS NOT NULL
                """,
                (document_id, version_id, page_number),
            ).fetchone()
        if row is None:
            return error_response(404, "not_found", "未找到标准页渲染结果。")
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

    @app.get("/api/v1/curation/pages")
    async def list_curation_pages(review_status: str = "pending") -> JSONResponse:
        if review_status not in {"pending", "approved", "excluded", "all"}:
            return error_response(422, "invalid_request", "审核状态无效。")
        with connect(resolved) as connection:
            rows = connection.execute(
                """
                SELECT pv.page_id, p.chunk_id, results.version_id,
                       versions.document_id, results.page_number, pv.review_status,
                       pv.source_content_json, results.source_slide_id,
                       results.relationship_id, results.source_part,
                       results.hidden, results.enabled, jobs.job_id AS enable_job_id,
                       jobs.status AS enable_status, jobs.error_json AS enable_error
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
                  AND (? = 'all' OR pv.review_status = ?)
                ORDER BY versions.document_id, results.page_number, pv.page_id
                """,
                (review_status, review_status),
            ).fetchall()
        pages = []
        for row in rows:
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
                        None
                        if row["enable_error"] is None
                        else json.loads(row["enable_error"])
                    ),
                }
            pages.append(
                {
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
            )
        return JSONResponse(content={"pages": pages})

    @app.post(
        "/api/v1/documents/{document_id}/versions/{version_id}"
        "/source-pages/{page_number}/enable"
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
                    "inherited_from_page_version_id": row[
                        "inherited_from_page_version_id"
                    ],
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
                "standard_render": {
                    "sha256": row["render_sha256"],
                    "media_type": row["render_media_type"],
                    "dpi": row["render_dpi"],
                    "width_px": row["render_width_px"],
                    "height_px": row["render_height_px"],
                    "url": f"/api/v1/pages/{page_id}/render",
                },
            }
        )

    @app.get("/api/v1/pages/{page_id}/render", response_model=None)
    async def get_page_render(page_id: str) -> FileResponse | JSONResponse:
        with connect(resolved) as connection:
            row = connection.execute(
                """
                SELECT pv.render_sha256, pv.render_media_type
                FROM page_versions AS pv
                JOIN documents AS d
                  ON d.document_id = pv.document_id
                 AND d.current_version_id = pv.version_id
                WHERE d.deleted_at IS NULL AND pv.page_id = ?
                """,
                (page_id,),
            ).fetchone()
        if row is None:
            return error_response(404, "not_found", "未找到请求的资源。")
        path = LocalObjectStore(resolved.object_store_path).path_for(row["render_sha256"])
        if not path.is_file():
            return error_response(503, "render_unavailable", "标准页渲染结果暂不可用。")
        return FileResponse(path, media_type=row["render_media_type"])

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
