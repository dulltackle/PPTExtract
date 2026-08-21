from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StoredObject:
    sha256: str
    size_bytes: int
    path: Path


class LocalObjectStore:
    """同一文件系统内原子发布的本地 SHA-256 内容寻址目录。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.staging_root = self.root / ".staging"
        self.staging_root.mkdir(parents=True, exist_ok=True)

    def path_for(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
            raise ValueError("对象键必须是小写 SHA-256")
        return self.root / sha256[:2] / sha256

    def put(self, payload: bytes) -> StoredObject:
        sha256 = hashlib.sha256(payload).hexdigest()
        destination = self.path_for(sha256)
        if destination.exists():
            if not self.verify(sha256):
                raise OSError(f"内容寻址对象校验失败：{sha256}")
            return StoredObject(sha256=sha256, size_bytes=len(payload), path=destination)

        destination.parent.mkdir(parents=True, exist_ok=True)
        staging_path = self.staging_root / f"{uuid.uuid4().hex}.partial"
        try:
            with staging_path.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if hashlib.sha256(staging_path.read_bytes()).hexdigest() != sha256:
                raise OSError("对象写入后的完整性校验失败")
            os.replace(staging_path, destination)
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            staging_path.unlink(missing_ok=True)

        return StoredObject(sha256=sha256, size_bytes=len(payload), path=destination)

    def check_writable(self) -> None:
        """以真实落盘、同步和清理验证 staging 目录仍可写。"""

        probe_path = self.staging_root / f"health-{uuid.uuid4().hex}.partial"
        try:
            with probe_path.open("xb") as handle:
                handle.write(b"pptextract-health")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            probe_path.unlink(missing_ok=True)
        directory_fd = os.open(self.staging_root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def verify(self, sha256: str) -> bool:
        path = self.path_for(sha256)
        if not path.is_file():
            return False
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest() == sha256
