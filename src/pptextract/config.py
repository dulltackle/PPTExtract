from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    """API 与 worker 共用的版本化运行配置。"""

    config_version: int
    database_path: Path
    object_store_path: Path
    web_dist_path: Path
    default_actor_id: str
    worker_id: str
    sqlite_busy_timeout_ms: int = 5_000
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
