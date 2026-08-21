from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException

from pptextract.auth import ActorProvider, HeaderActorProvider
from pptextract.config import Settings
from pptextract.db import connect, database_path_is_local, initialize_database
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
