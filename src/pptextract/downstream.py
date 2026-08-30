from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from types import MappingProxyType
from typing import Any, cast

from pptextract.publication import ARCHIVE_MEDIA_TYPE, validate_publication_archive


class DownstreamSyncError(ValueError):
    """当前产物无法安全形成一个完整 generation。"""


@dataclass(frozen=True, slots=True)
class SourceResponse:
    status_code: int
    headers: Mapping[str, str]
    body: Iterable[bytes]


@dataclass(frozen=True, slots=True)
class DownstreamGeneration:
    publication_seq: int
    snapshot_id: str
    chunks: Mapping[str, Mapping[str, Any]]
    assets: Mapping[str, bytes]


SourceRequest = Callable[[str, Mapping[str, str]], SourceResponse]
GenerationImporter = Callable[[DownstreamGeneration], DownstreamGeneration]


def _identity_importer(generation: DownstreamGeneration) -> DownstreamGeneration:
    return generation


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _freeze_generation(generation: DownstreamGeneration) -> DownstreamGeneration:
    chunks = {
        chunk_id: cast(Mapping[str, Any], _freeze_json(dict(chunk)))
        for chunk_id, chunk in generation.chunks.items()
    }
    return DownstreamGeneration(
        publication_seq=generation.publication_seq,
        snapshot_id=generation.snapshot_id,
        chunks=MappingProxyType(chunks),
        assets=MappingProxyType(dict(generation.assets)),
    )


def _body_bytes(response: SourceResponse) -> bytes:
    blocks: list[bytes] = []
    for block in response.body:
        if not isinstance(block, bytes):
            raise DownstreamSyncError("产物下载流包含非字节数据")
        blocks.append(block)
    return b"".join(blocks)


def _headers(response: SourceResponse) -> dict[str, str]:
    return {name.lower(): value for name, value in response.headers.items()}


def _pointer(payload: bytes) -> dict[str, Any]:
    try:
        pointer = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DownstreamSyncError("当前产物指针不是有效 UTF-8 JSON") from error
    if not isinstance(pointer, dict):
        raise DownstreamSyncError("当前产物指针必须是 JSON 对象")
    publication_seq = pointer.get("publication_seq")
    size_bytes = pointer.get("size_bytes")
    if (
        not isinstance(publication_seq, int)
        or isinstance(publication_seq, bool)
        or publication_seq <= 0
        or not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes < 0
        or not isinstance(pointer.get("snapshot_id"), str)
        or not pointer["snapshot_id"]
        or not isinstance(pointer.get("published_at"), str)
        or not pointer["published_at"]
        or pointer.get("media_type") != ARCHIVE_MEDIA_TYPE
    ):
        raise DownstreamSyncError("当前产物指针身份或包元数据不完整")
    artifact_uri = pointer.get("artifact_uri")
    if artifact_uri != f"/api/v1/publications/{publication_seq}/artifact":
        raise DownstreamSyncError("当前产物指针的包 URI 与发布序号不一致")
    sha256 = pointer.get("sha256")
    if (
        not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        raise DownstreamSyncError("当前产物指针的 SHA-256 无效")
    return pointer


def _generation(pointer: Mapping[str, Any], archive_payload: bytes) -> DownstreamGeneration:
    validate_publication_archive(archive_payload)
    with zipfile.ZipFile(BytesIO(archive_payload)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if (
            manifest["publication_seq"] != pointer["publication_seq"]
            or manifest["snapshot_id"] != pointer["snapshot_id"]
        ):
            raise DownstreamSyncError("manifest 与当前产物指针身份不一致")
        chunks = {
            str(chunk["chunk_id"]): chunk
            for line in archive.read("chunks.jsonl").splitlines()
            for chunk in [json.loads(line)]
        }
        assets = {
            str(asset["path"]): archive.read(str(asset["path"]))
            for asset in manifest["assets"]
        }
    return DownstreamGeneration(
        publication_seq=int(pointer["publication_seq"]),
        snapshot_id=str(pointer["snapshot_id"]),
        chunks=MappingProxyType(chunks),
        assets=MappingProxyType(assets),
    )


class DownstreamSimulator:
    """通过公开下载契约模拟下游的隔离导入与 generation 原子切换。"""

    def __init__(self, *, import_generation: GenerationImporter | None = None) -> None:
        self._import_generation = import_generation or _identity_importer
        self._current_generation: DownstreamGeneration | None = None
        self._pointer_etag: str | None = None

    @property
    def current_generation(self) -> DownstreamGeneration | None:
        return self._current_generation

    def synchronize(self, request: SourceRequest) -> bool:
        pointer_headers = (
            {} if self._pointer_etag is None else {"If-None-Match": self._pointer_etag}
        )
        pointer_response = request("/api/v1/publications/current", pointer_headers)
        if pointer_response.status_code == 304:
            return False
        if pointer_response.status_code != 200:
            raise DownstreamSyncError(
                f"读取当前产物指针失败：HTTP {pointer_response.status_code}"
            )
        pointer_headers_received = _headers(pointer_response)
        pointer_etag = pointer_headers_received.get("etag")
        if not pointer_etag:
            raise DownstreamSyncError("当前产物指针缺少 ETag")
        pointer = _pointer(_body_bytes(pointer_response))
        publication_seq = int(pointer["publication_seq"])
        if (
            self._current_generation is not None
            and publication_seq <= self._current_generation.publication_seq
        ):
            return False

        artifact_response = request(str(pointer["artifact_uri"]), {})
        if artifact_response.status_code != 200:
            raise DownstreamSyncError(
                f"下载不可变 ZIP 失败：HTTP {artifact_response.status_code}"
            )
        artifact_headers = _headers(artifact_response)
        if artifact_headers.get("content-type", "").split(";", 1)[0] != ARCHIVE_MEDIA_TYPE:
            raise DownstreamSyncError("不可变 ZIP 的媒体类型无效")
        if artifact_headers.get("etag") != pointer_etag:
            raise DownstreamSyncError("不可变 ZIP 的 ETag 与当前产物指针不一致")
        payload = _body_bytes(artifact_response)
        expected_size = int(pointer["size_bytes"])
        if len(payload) != expected_size:
            raise DownstreamSyncError("不可变 ZIP 的实际长度与当前产物指针不一致")
        content_length = artifact_headers.get("content-length")
        if content_length is None or not content_length.isdigit() or int(content_length) != len(
            payload
        ):
            raise DownstreamSyncError("不可变 ZIP 的 Content-Length 无效")
        if hashlib.sha256(payload).hexdigest() != pointer["sha256"]:
            raise DownstreamSyncError("不可变 ZIP 的 SHA-256 与当前产物指针不一致")

        try:
            isolated = _generation(pointer, payload)
        except (KeyError, TypeError, ValueError, zipfile.BadZipFile) as error:
            raise DownstreamSyncError("不可变 ZIP 内部契约验证失败") from error
        expected_chunks = deepcopy(dict(isolated.chunks))
        expected_assets = dict(isolated.assets)
        imported = self._import_generation(isolated)
        if (
            imported.publication_seq != isolated.publication_seq
            or imported.snapshot_id != isolated.snapshot_id
        ):
            raise DownstreamSyncError("导入结果改变了 generation 身份")
        if dict(imported.chunks) != expected_chunks or dict(imported.assets) != expected_assets:
            raise DownstreamSyncError("导入后的 generation 不完整")
        self._current_generation = _freeze_generation(imported)
        self._pointer_etag = pointer_etag
        return True
