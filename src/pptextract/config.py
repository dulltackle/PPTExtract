from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pptextract.toolchain import load_toolchain_contract

DEFAULT_RENDER_IMAGE = load_toolchain_contract().rendering_image


@dataclass(frozen=True, slots=True)
class Settings:
    """API 与 worker 共用的版本化运行配置。"""

    config_version: int
    database_path: Path
    object_store_path: Path
    web_dist_path: Path
    default_actor_id: str
    worker_id: str
    render_image: str = DEFAULT_RENDER_IMAGE
    render_generation: int = 1
    max_source_upload_bytes: int = 128 * 1024 * 1024
    sqlite_busy_timeout_ms: int = 5_000
    job_retry_base_seconds: float = 5.0
    public_artifact_retention_days: int = 7
    internal_artifact_retention_days: int = 90
    object_gc_grace_seconds: int = 24 * 60 * 60
    allow_temporary_storage: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        data_root = Path(os.environ.get("PPTEXTRACT_DATA_ROOT", "var/data")).resolve()
        return cls(
            config_version=int(os.environ.get("PPTEXTRACT_CONFIG_VERSION", "1")),
            database_path=Path(
                os.environ.get("PPTEXTRACT_DATABASE_PATH", data_root / "pptextract.sqlite3")
            ).resolve(),
            object_store_path=Path(
                os.environ.get("PPTEXTRACT_OBJECT_STORE_PATH", data_root / "objects")
            ).resolve(),
            web_dist_path=Path(
                os.environ.get("PPTEXTRACT_WEB_DIST", "web/dist")
            ).resolve(),
            default_actor_id=os.environ.get("PPTEXTRACT_ACTOR_ID", "local-operator"),
            worker_id=os.environ.get("PPTEXTRACT_WORKER_ID", "worker-1"),
            render_image=os.environ.get(
                "PPTEXTRACT_RENDER_IMAGE", DEFAULT_RENDER_IMAGE
            ),
            render_generation=int(os.environ.get("PPTEXTRACT_RENDER_GENERATION", "1")),
            max_source_upload_bytes=int(
                os.environ.get("PPTEXTRACT_MAX_SOURCE_UPLOAD_BYTES", 128 * 1024 * 1024)
            ),
            job_retry_base_seconds=float(
                os.environ.get("PPTEXTRACT_JOB_RETRY_BASE_SECONDS", "5")
            ),
            public_artifact_retention_days=int(
                os.environ.get("PPTEXTRACT_PUBLIC_ARTIFACT_RETENTION_DAYS", "7")
            ),
            internal_artifact_retention_days=int(
                os.environ.get("PPTEXTRACT_INTERNAL_ARTIFACT_RETENTION_DAYS", "90")
            ),
            object_gc_grace_seconds=int(
                os.environ.get("PPTEXTRACT_OBJECT_GC_GRACE_SECONDS", str(24 * 60 * 60))
            ),
        )

    @classmethod
    def for_test(cls, root: Path) -> Settings:
        return cls(
            config_version=1,
            database_path=root / "pptextract.sqlite3",
            object_store_path=root / "objects",
            web_dist_path=root / "missing-web-dist",
            default_actor_id="test-operator",
            worker_id="test-worker",
            allow_temporary_storage=True,
        )

    def validate(self) -> None:
        if self.config_version != 1:
            raise ValueError(f"不支持的 PPTEXTRACT_CONFIG_VERSION：{self.config_version}")
        if self.sqlite_busy_timeout_ms < 100 or self.sqlite_busy_timeout_ms > 30_000:
            raise ValueError("SQLite busy timeout 必须在 100–30000ms 之间")
        if self.render_generation <= 0:
            raise ValueError("渲染配置代次必须为正整数")
        if self.max_source_upload_bytes <= 0:
            raise ValueError("PPTX 上传上限必须大于 0")
        if self.public_artifact_retention_days < 7:
            raise ValueError("旧产物 ZIP 对外保留期不得少于 7 天")
        if self.internal_artifact_retention_days < self.public_artifact_retention_days:
            raise ValueError("旧产物 ZIP 内部保留期不得短于对外保留期")
        if self.object_gc_grace_seconds <= 0:
            raise ValueError("对象回收宽限期必须大于 0 秒")
        if (
            not math.isfinite(self.job_retry_base_seconds)
            or self.job_retry_base_seconds < 0
            or self.job_retry_base_seconds > 3600
        ):
            raise ValueError("任务重试基础延迟必须在 0–3600 秒之间")
        if self.allow_temporary_storage:
            return
        temporary_root = Path(tempfile.gettempdir()).resolve()
        for label, path in (
            ("SQLite", self.database_path),
            ("对象目录", self.object_store_path),
        ):
            try:
                path.resolve().relative_to(temporary_root)
            except ValueError:
                continue
            raise ValueError(f"{label}不得位于临时目录：{path}")
