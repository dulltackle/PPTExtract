from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException

from pptextract.auth import ActorProvider, HeaderActorProvider
from pptextract.config import Settings
from pptextract.db import connect, database_path_is_local, initialize_database
from pptextract.ingest_workflow import (
    IngestionRequestError,
    accept_first_upload,
    read_job,
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
        return {
            "actor": {"actor_id": actor.actor_id, "display_name": actor.display_name},
            "runways": [
                {"id": "pending", "label": "待处理", "documents": []},
                {"id": "processing", "label": "处理中", "documents": []},
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
                "status": "accepted",
            },
        )

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: str) -> JSONResponse:
        job = read_job(resolved, job_id)
        if job is None:
            return error_response(404, "not_found", "未找到请求的资源。")
        return JSONResponse(content=job)

    @app.get("/api/v1/documents/{document_id}")
    async def get_document(document_id: str) -> JSONResponse:
        with connect(resolved) as connection:
            row = connection.execute(
                """
                SELECT document_id, current_version_id, created_at
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
                       source_filename, source_size_bytes, created_at, ready_at
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
            }
        )

    @app.get("/api/v1/curation/pages")
    async def list_curation_pages(review_status: str = "pending") -> JSONResponse:
        if review_status not in {"pending", "approved", "excluded"}:
            return error_response(422, "invalid_request", "审核状态无效。")
        with connect(resolved) as connection:
            rows = connection.execute(
                """
                SELECT pv.page_id, p.chunk_id, pv.document_id, pv.version_id,
                       pv.page_number, pv.review_status
                FROM page_versions AS pv
                JOIN pages AS p ON p.page_id = pv.page_id
                JOIN documents AS d
                  ON d.document_id = pv.document_id
                 AND d.current_version_id = pv.version_id
                WHERE pv.review_status = ?
                ORDER BY pv.document_id, pv.page_number, pv.page_id
                """,
                (review_status,),
            ).fetchall()
        return JSONResponse(content={"pages": [dict(row) for row in rows]})

    @app.get("/api/v1/pages/{page_id}")
    async def get_page(page_id: str) -> JSONResponse:
        with connect(resolved) as connection:
            row = connection.execute(
                """
                SELECT pv.page_id, p.chunk_id, pv.document_id, pv.version_id,
                       pv.page_number, pv.review_status, pv.fingerprint_version,
                       pv.fingerprint_sha256, pv.source_content_json,
                       pv.render_sha256, pv.render_media_type, pv.render_dpi,
                       pv.render_width_px, pv.render_height_px
                FROM page_versions AS pv
                JOIN pages AS p ON p.page_id = pv.page_id
                JOIN documents AS d
                  ON d.document_id = pv.document_id
                 AND d.current_version_id = pv.version_id
                WHERE pv.page_id = ?
                """,
                (page_id,),
            ).fetchone()
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
                WHERE pv.page_id = ?
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
