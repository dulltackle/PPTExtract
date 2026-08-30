from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Callable
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from pptextract.config import Settings
from pptextract.db import connect, initialize_database
from pptextract.jobs import claim_next_job
from pptextract.object_store import LocalObjectStore
from pptextract.publication import (
    PublicationRequestError,
    confirm_candidate,
    create_candidate,
    process_publication_job,
    read_artifact,
    validate_publication_archive,
)
from tests.test_publication_api import _seed_publication_scope


def _build_archive(settings: Settings) -> bytes:
    candidate = create_candidate(settings, actor_id="publisher-contract")
    status_code, confirmation = confirm_candidate(
        settings,
        candidate_id=str(candidate["candidate_id"]),
        actor_id="publisher-contract",
    )
    assert status_code == 202
    job = claim_next_job(settings)
    assert job is not None
    process_publication_job(settings, job)
    artifact = read_artifact(settings, int(confirmation["publication_seq"]))
    assert artifact is not None
    return LocalObjectStore(settings.object_store_path).path_for(artifact.sha256).read_bytes()


def _contract_archive(
    chunk: dict[str, object] | list[dict[str, object]],
    *,
    assets: list[dict[str, object]] | None = None,
    asset_entries: list[tuple[str | zipfile.ZipInfo, bytes]] | None = None,
) -> bytes:
    chunks = chunk if isinstance(chunk, list) else [chunk]
    chunks_bytes = (
        "\n".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for item in chunks
        )
        + "\n"
    ).encode()
    declared_assets = assets or []
    content_set = {
        "chunks": sorted(chunks, key=lambda item: str(item["chunk_id"])),
        "assets": sorted(declared_assets, key=lambda item: str(item["path"])),
    }
    content_set_hash = hashlib.sha256(
        json.dumps(
            content_set,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    manifest = {
        "schema_version": 1,
        "snapshot_id": "snapshot-contract",
        "publication_seq": 1,
        "captured_at": "2026-08-29T00:00:00+00:00",
        "content_set_hash": content_set_hash,
        "chunk_count": len(chunks),
        "asset_count": len(declared_assets),
        "chunks": {
            "path": "chunks.jsonl",
            "sha256": hashlib.sha256(chunks_bytes).hexdigest(),
            "size_bytes": len(chunks_bytes),
        },
        "assets": declared_assets,
    }
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr(
            "manifest.json",
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
        )
        bundle.writestr("chunks.jsonl", chunks_bytes)
        for entry, payload in asset_entries or []:
            bundle.writestr(entry, payload)
    return output.getvalue()


def _replace_chunks_bytes(archive_payload: bytes, chunks_bytes: bytes) -> bytes:
    with zipfile.ZipFile(BytesIO(archive_payload)) as source:
        files = {name: source.read(name) for name in source.namelist()}
    manifest = json.loads(files["manifest.json"])
    manifest["chunks"]["sha256"] = hashlib.sha256(chunks_bytes).hexdigest()
    manifest["chunks"]["size_bytes"] = len(chunks_bytes)
    files["manifest.json"] = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    files["chunks.jsonl"] = chunks_bytes
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for name, payload in files.items():
            bundle.writestr(name, payload)
    return output.getvalue()


def _replace_manifest(archive_payload: bytes, mutate: Callable[[dict[str, Any]], None]) -> bytes:
    with zipfile.ZipFile(BytesIO(archive_payload)) as source:
        files = {name: source.read(name) for name in source.namelist()}
    manifest = json.loads(files["manifest.json"])
    mutate(manifest)
    files["manifest.json"] = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for name, payload in files.items():
            bundle.writestr(name, payload)
    return output.getvalue()


def _ordinary_content_hash(text: str, parts: list[dict[str, object]]) -> str:
    canonical = json.dumps(
        {"text": text, "parts": parts},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _valid_chunk() -> dict[str, object]:
    text = "契约文档\n\n正文"
    parts: list[dict[str, object]] = [
        {"kind": "document_title", "text": "契约文档"},
        {"kind": "body", "text": "正文"},
    ]
    return {
        "schema_version": 1,
        "chunk_id": "chunk-contract",
        "content_hash": _ordinary_content_hash(text, parts),
        "text": text,
        "parts": parts,
        "metadata": {
            "document_id": "doc-contract",
            "document_version_id": "version-contract",
            "page_id": "page-contract",
            "page_version_id": "page-version-contract",
            "page_number": 1,
            "page_fingerprint": "f" * 64,
            "fingerprint_version": 1,
            "document_title": "契约文档",
            "source_filename": "契约文档.pptx",
            "approved_by": "curator-contract",
            "approved_at": "2026-08-29T00:00:00+00:00",
            "approval_source_version_id": "version-contract",
            "snapshot_id": "snapshot-contract",
            "text_characters": len(text),
        },
    }


def _chunk_with_asset(
    asset: dict[str, object], *, media_type: str | None = None
) -> dict[str, object]:
    chunk = _valid_chunk()
    summary = "图表显示稳定趋势。"
    reference = dict(asset)
    if media_type is not None:
        reference["media_type"] = media_type
    parts: list[dict[str, object]] = [
        {"kind": "document_title", "text": "契约文档"},
        {
            "kind": "annotation",
            "text": summary,
            "data": {
                "visuals": [
                    {
                        "visual_ref": "visual-contract",
                        "summary": summary,
                        "asset": reference,
                    }
                ]
            },
        }
    ]
    text = f"契约文档\n\n{summary}"
    chunk["text"] = text
    chunk["parts"] = parts
    chunk["content_hash"] = _ordinary_content_hash(text, parts)
    metadata = deepcopy(chunk["metadata"])
    assert isinstance(metadata, dict)
    metadata["text_characters"] = len(text)
    chunk["metadata"] = metadata
    return chunk


def test_publication_builds_complete_chunk_in_deterministic_source_order(tmp_path: Path) -> None:
    settings = Settings.for_test(tmp_path)
    initialize_database(settings)
    seeded = _seed_publication_scope(settings)
    content = {
        "titles": ["公开页标题"],
        "body": ["先读正文。"],
        "tables": [
            {
                "kind": "data",
                "header_rows": 1,
                "grid": [
                    [
                        {
                            "kind": "origin",
                            "cell": {"text": "指标", "row_span": 2, "col_span": 1},
                            "origin_row": None,
                            "origin_col": None,
                        },
                        {
                            "kind": "origin",
                            "cell": {"text": "值", "row_span": 1, "col_span": 1},
                            "origin_row": None,
                            "origin_col": None,
                        },
                    ],
                    [
                        {
                            "kind": "covered",
                            "cell": None,
                            "origin_row": 0,
                            "origin_col": 0,
                        },
                        {
                            "kind": "origin",
                            "cell": {"text": "42", "row_span": 1, "col_span": 1},
                            "origin_row": None,
                            "origin_col": None,
                        },
                    ],
                ],
            }
        ],
        "images": [
            {"alt_text": ""},
            {"alt_text": "随后读取图片说明。"},
        ],
        "speaker_notes": ["最后读取演讲者备注。"],
        "source_order": [
            {"kind": "image_alt", "index": 0},
            {"kind": "body", "index": 0},
            {"kind": "image_alt", "index": 1},
            {"kind": "table", "index": 0},
        ],
    }
    serialized = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    with connect(settings) as connection:
        connection.execute(
            "UPDATE page_versions SET source_content_json = ? "
            "WHERE page_version_id = 'page-version-approved'",
            (serialized,),
        )
        connection.execute(
            "UPDATE curation_snapshots SET source_content_json = ? "
            "WHERE snapshot_id = 'snapshot-approved'",
            (serialized,),
        )
        connection.commit()

    with zipfile.ZipFile(BytesIO(_build_archive(settings))) as bundle:
        chunk = json.loads(bundle.read("chunks.jsonl"))

    assert set(chunk) == {
        "schema_version",
        "chunk_id",
        "content_hash",
        "text",
        "parts",
        "metadata",
    }
    assert [part["kind"] for part in chunk["parts"]] == [
        "document_title",
        "page_title",
        "annotation",
        "body",
        "image_alt",
        "table",
        "speaker_notes",
    ]
    assert chunk["parts"][0] == {"kind": "document_title", "text": "公开知识源"}
    assert chunk["parts"][2]["data"] == {
        "overview": "公开页总述。",
        "visuals": [
            {
                "visual_ref": "visual-approved",
                "visual_type": "chart",
                "summary": "公开图表显示稳定趋势。",
                "asset": {
                    "path": f"assets/{seeded['asset_sha256']}.png",
                    "sha256": seeded["asset_sha256"],
                    "media_type": "image/png",
                    "size_bytes": 27,
                    "byte_contract": "standard_render_crop",
                    "width_px": 640,
                    "height_px": 360,
                },
            }
        ],
    }
    assert chunk["parts"][5] == {
        "kind": "table",
        "text": "| 指标 | 值 |\n| --- | --- |\n|  | 42 |",
        "data": {
            "header_rows": 1,
            "grid": {
                "rows": 2,
                "columns": 2,
                "cells": [
                    {"row": 0, "column": 0, "text": "指标", "row_span": 2, "col_span": 1},
                    {"row": 0, "column": 1, "text": "值", "row_span": 1, "col_span": 1},
                    {"row": 1, "column": 1, "text": "42", "row_span": 1, "col_span": 1},
                ],
            }
        },
    }
    assert chunk["text"] == "\n\n".join(part["text"] for part in chunk["parts"])
    assert chunk["metadata"] == {
        "document_id": "doc-approved",
        "document_version_id": "version-approved",
        "page_id": "page-approved",
        "page_version_id": "page-version-approved",
        "page_number": 1,
        "page_fingerprint": "fingerprint-approved",
        "fingerprint_version": 1,
        "document_title": "公开知识源",
        "source_filename": "公开知识源.pptx",
        "page_title": "公开页标题",
        "approved_by": "curator-1",
        "approved_at": "2026-08-29T00:00:00+00:00",
        "approval_source_version_id": "version-approved",
        "snapshot_id": "snapshot-approved",
        "text_characters": len(chunk["text"]),
    }


def test_archive_validator_uses_rfc8785_content_hash() -> None:
    chunk: dict[str, object] = {
        "schema_version": 1,
        "chunk_id": "chunk-rfc8785",
        "content_hash": "0e741d4d42d69a5e0487f13752aa6101a0c613ec9559f3bf10b2630ff85c89ec",
        "text": "RFC 8785\n\n正文",
        "parts": [
            {"kind": "document_title", "text": "RFC 8785"},
            {
                "kind": "body",
                "text": "正文",
                "data": {"\ue000": 1, "\U00010000": 2},
            }
        ],
        "metadata": {
            "document_id": "doc-rfc8785",
            "document_version_id": "version-rfc8785",
            "page_id": "page-rfc8785",
            "page_version_id": "page-version-rfc8785",
            "page_number": 1,
            "page_fingerprint": "f" * 64,
            "fingerprint_version": 1,
            "document_title": "RFC 8785",
            "source_filename": "rfc8785.pptx",
            "approved_by": "curator-rfc8785",
            "approved_at": "2026-08-29T00:00:00+00:00",
            "approval_source_version_id": "version-rfc8785",
            "snapshot_id": "snapshot-rfc8785",
            "text_characters": len("RFC 8785\n\n正文"),
        },
    }

    validate_publication_archive(_contract_archive(chunk))


def test_inherited_review_metadata_keeps_original_approval_and_current_version(
    tmp_path: Path,
) -> None:
    settings = Settings.for_test(tmp_path)
    initialize_database(settings)
    _seed_publication_scope(settings)
    now = "2026-08-29T01:00:00+00:00"
    with connect(settings) as connection:
        old_version = connection.execute(
            "SELECT * FROM document_versions WHERE version_id = 'version-approved'"
        ).fetchone()
        old_page = connection.execute(
            "SELECT * FROM page_versions WHERE page_version_id = 'page-version-approved'"
        ).fetchone()
        assert old_version is not None and old_page is not None
        connection.execute(
            """
            INSERT INTO document_versions (
                version_id, document_id, source_sha256, source_filename,
                source_size_bytes, status, created_at, ready_at,
                render_config_version, render_generation
            ) VALUES ('version-current', 'doc-approved', ?, '公开知识源-更新版.pptx', ?,
                      'ready', ?, ?, ?, ?)
            """,
            (
                old_version["source_sha256"],
                old_version["source_size_bytes"],
                now,
                now,
                old_version["render_config_version"],
                old_version["render_generation"],
            ),
        )
        connection.execute(
            """
            INSERT INTO ingestion_page_results (
                version_id, page_number, source_slide_id, relationship_id,
                source_part, hidden, enabled, source_content_json,
                fingerprint_version, fingerprint_sha256, render_sha256,
                render_media_type, render_dpi, render_width_px, render_height_px
            ) VALUES ('version-current', 5, 300, 'rId5', 'ppt/slides/slide5.xml',
                      0, 1, ?, ?, ?, ?, 'image/png', 144, 1280, 720)
            """,
            (
                old_page["source_content_json"],
                old_page["fingerprint_version"],
                old_page["fingerprint_sha256"],
                old_page["render_sha256"],
            ),
        )
        connection.execute(
            """
            INSERT INTO page_versions (
                page_version_id, page_id, document_id, version_id, page_number,
                fingerprint_version, fingerprint_sha256, source_content_json,
                render_sha256, render_media_type, render_dpi, render_width_px,
                render_height_px, review_status, inherited_from_page_version_id,
                reviewed_by, reviewed_at, review_source_version_id, created_at
            ) VALUES ('page-version-inherited', 'page-approved', 'doc-approved',
                      'version-current', 5, ?, ?, ?, ?, 'image/png', 144, 1280, 720,
                      'approved', 'page-version-approved', 'curator-1',
                      '2026-08-29T00:00:00+00:00', 'version-approved', ?)
            """,
            (
                old_page["fingerprint_version"],
                old_page["fingerprint_sha256"],
                old_page["source_content_json"],
                old_page["render_sha256"],
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO curation_snapshots (
                snapshot_id, page_version_id, snapshot_kind, source_snapshot_id,
                overview, source_content_json, capture_required, created_by, created_at
            ) VALUES ('snapshot-inherited', 'page-version-inherited', 'formal',
                      'snapshot-approved', '继承后的正式标注。', ?, 0, 'curator-1', ?)
            """,
            (old_page["source_content_json"], now),
        )
        connection.execute(
            "UPDATE page_versions SET current_snapshot_id = 'snapshot-inherited' "
            "WHERE page_version_id = 'page-version-inherited'"
        )
        connection.execute(
            "UPDATE documents SET current_version_id = 'version-current' "
            "WHERE document_id = 'doc-approved'"
        )
        connection.commit()

    with zipfile.ZipFile(BytesIO(_build_archive(settings))) as bundle:
        chunk = json.loads(bundle.read("chunks.jsonl"))

    assert chunk["chunk_id"] == "chunk-approved"
    assert chunk["metadata"]["document_version_id"] == "version-current"
    assert chunk["metadata"]["page_number"] == 5
    assert chunk["metadata"]["approved_by"] == "curator-1"
    assert chunk["metadata"]["approved_at"] == "2026-08-29T00:00:00+00:00"
    assert chunk["metadata"]["approval_source_version_id"] == "version-approved"


def test_candidate_rejects_approved_page_without_actual_review_metadata(tmp_path: Path) -> None:
    settings = Settings.for_test(tmp_path)
    initialize_database(settings)
    _seed_publication_scope(settings)
    with connect(settings) as connection:
        connection.execute(
            "UPDATE page_versions SET reviewed_by = NULL "
            "WHERE page_version_id = 'page-version-approved'"
        )
        connection.commit()

    with pytest.raises(PublicationRequestError, match="审核来源"):
        create_candidate(settings, actor_id="publisher-contract")


def test_candidate_supports_approved_page_without_page_title(tmp_path: Path) -> None:
    settings = Settings.for_test(tmp_path)
    initialize_database(settings)
    _seed_publication_scope(settings)
    content = {
        "titles": [],
        "body": ["无标题页正文。"],
        "tables": [],
        "images": [],
        "speaker_notes": [],
        "source_order": [{"kind": "body", "index": 0}],
    }
    serialized = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    with connect(settings) as connection:
        connection.execute(
            "UPDATE page_versions SET source_content_json = ? "
            "WHERE page_version_id = 'page-version-approved'",
            (serialized,),
        )
        connection.execute(
            "UPDATE curation_snapshots SET source_content_json = ? "
            "WHERE snapshot_id = 'snapshot-approved'",
            (serialized,),
        )
        connection.commit()

    candidate = create_candidate(settings, actor_id="publisher-contract")

    assert candidate["documents"][0]["pages"][0]["title"] is None


def test_metadata_only_page_move_requires_new_publication(tmp_path: Path) -> None:
    settings = Settings.for_test(tmp_path)
    initialize_database(settings)
    _seed_publication_scope(settings)
    _build_archive(settings)
    with connect(settings) as connection:
        connection.execute(
            "UPDATE ingestion_page_results SET page_number = 5 "
            "WHERE version_id = 'version-approved' AND page_number = 1"
        )
        connection.execute(
            "UPDATE page_versions SET page_number = 5 "
            "WHERE page_version_id = 'page-version-approved'"
        )
        connection.commit()

    candidate = create_candidate(settings, actor_id="publisher-contract-2")
    status_code, confirmation = confirm_candidate(
        settings,
        candidate_id=str(candidate["candidate_id"]),
        actor_id="publisher-contract-2",
    )

    assert status_code == 202
    assert confirmation["status"] == "queued"


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("empty_text", "正文为空"),
        ("unknown_kind", "未知 kind"),
        ("invalid_order", "阅读顺序"),
        ("annotation_mismatch", "annotation"),
        ("table_gfm_mismatch", "GFM"),
        ("table_overlap", "重叠"),
        ("hash_mismatch", "内容哈希不一致"),
        ("null_optional", "null"),
        ("incomplete_metadata", "metadata"),
    ],
)
def test_archive_validator_rejects_invalid_chunk_contract(
    case: str, expected_error: str
) -> None:
    chunk = _valid_chunk()
    if case == "empty_text":
        chunk["text"] = ""
        chunk["parts"] = []
        chunk["content_hash"] = _ordinary_content_hash("", [])
    elif case == "unknown_kind":
        parts: list[dict[str, object]] = [{"kind": "future_kind", "text": "正文"}]
        chunk["parts"] = parts
        chunk["content_hash"] = _ordinary_content_hash("正文", parts)
    elif case == "invalid_order":
        parts = [
            {"kind": "body", "text": "正文"},
            {"kind": "document_title", "text": "契约文档"},
        ]
        text = "正文\n\n契约文档"
        chunk["text"] = text
        chunk["parts"] = parts
        chunk["content_hash"] = _ordinary_content_hash(text, parts)
        metadata = deepcopy(chunk["metadata"])
        assert isinstance(metadata, dict)
        metadata["text_characters"] = len(text)
        chunk["metadata"] = metadata
    elif case == "annotation_mismatch":
        parts = [
            {"kind": "document_title", "text": "契约文档"},
            {
                "kind": "annotation",
                "text": "展示文本。",
                "data": {"overview": "另一段文本。", "visuals": []},
            }
        ]
        text = "契约文档\n\n展示文本。"
        chunk["text"] = text
        chunk["parts"] = parts
        chunk["content_hash"] = _ordinary_content_hash(text, parts)
        metadata = deepcopy(chunk["metadata"])
        assert isinstance(metadata, dict)
        metadata["text_characters"] = len(text)
        chunk["metadata"] = metadata
    elif case in {"table_gfm_mismatch", "table_overlap"}:
        cells: list[dict[str, object]] = [
            {"row": 0, "column": 0, "text": "甲", "row_span": 1, "col_span": 1}
        ]
        if case == "table_overlap":
            cells = [
                {"row": 0, "column": 0, "text": "甲", "row_span": 1, "col_span": 2},
                {"row": 0, "column": 1, "text": "乙", "row_span": 1, "col_span": 1},
            ]
        table_text = (
            "| 错误 |\n| --- |"
            if case == "table_gfm_mismatch"
            else "| 甲 |  |\n| --- | --- |"
        )
        parts = [
            {"kind": "document_title", "text": "契约文档"},
            {
                "kind": "table",
                "text": table_text,
                "data": {
                    "header_rows": 1,
                    "grid": {
                        "rows": 1,
                        "columns": 2 if case == "table_overlap" else 1,
                        "cells": cells,
                    },
                },
            },
        ]
        text = f"契约文档\n\n{table_text}"
        chunk["text"] = text
        chunk["parts"] = parts
        chunk["content_hash"] = _ordinary_content_hash(text, parts)
        metadata = deepcopy(chunk["metadata"])
        assert isinstance(metadata, dict)
        metadata["text_characters"] = len(text)
        chunk["metadata"] = metadata
    elif case == "hash_mismatch":
        chunk["content_hash"] = "0" * 64
    elif case == "null_optional":
        metadata = deepcopy(chunk["metadata"])
        assert isinstance(metadata, dict)
        metadata["page_title"] = None
        chunk["metadata"] = metadata
    elif case == "incomplete_metadata":
        metadata = deepcopy(chunk["metadata"])
        assert isinstance(metadata, dict)
        del metadata["source_filename"]
        chunk["metadata"] = metadata

    with pytest.raises(ValueError, match=expected_error):
        validate_publication_archive(_contract_archive(chunk))


def test_archive_validator_rejects_duplicate_chunk_id() -> None:
    first = _valid_chunk()
    second = deepcopy(first)
    second["metadata"] = {**second["metadata"], "page_id": "page-contract-2"}  # type: ignore[misc]

    with pytest.raises(ValueError, match="Chunk ID 重复"):
        validate_publication_archive(_contract_archive([first, second]))


@pytest.mark.parametrize("encoding", ["bom", "crlf", "pretty", "missing_final_lf"])
def test_archive_validator_rejects_non_deterministic_jsonl_encoding(encoding: str) -> None:
    chunk = _valid_chunk()
    compact = json.dumps(
        chunk, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    if encoding == "bom":
        chunks_bytes = b"\xef\xbb\xbf" + compact + b"\n"
    elif encoding == "crlf":
        chunks_bytes = compact + b"\r\n"
    elif encoding == "pretty":
        chunks_bytes = json.dumps(chunk, ensure_ascii=False, sort_keys=True).encode() + b"\n"
    else:
        chunks_bytes = compact
    archive = _replace_chunks_bytes(_contract_archive(chunk), chunks_bytes)

    with pytest.raises(ValueError, match="JSONL 编码"):
        validate_publication_archive(archive)


def test_archive_validator_rejects_manifest_content_set_hash_mismatch() -> None:
    archive = _contract_archive(_valid_chunk())
    with zipfile.ZipFile(BytesIO(archive)) as source:
        files = {name: source.read(name) for name in source.namelist()}
    manifest = json.loads(files["manifest.json"])
    manifest["content_set_hash"] = "0" * 64
    files["manifest.json"] = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for name, payload in files.items():
            bundle.writestr(name, payload)

    with pytest.raises(ValueError, match="内容集合哈希"):
        validate_publication_archive(output.getvalue())


@pytest.mark.parametrize(
    "case",
    ["captured_at", "chunks_path", "chunk_count", "asset_count", "chunks_size"],
)
def test_archive_validator_rejects_manifest_field_type_and_value_mismatches(
    case: str,
) -> None:
    def mutate(manifest: dict[str, Any]) -> None:
        if case == "captured_at":
            manifest["captured_at"] = None
        elif case == "chunks_path":
            manifest["chunks"]["path"] = "other.jsonl"
        elif case == "chunk_count":
            manifest["chunk_count"] = "1"
        elif case == "asset_count":
            manifest["asset_count"] = "0"
        else:
            manifest["chunks"]["size_bytes"] = str(manifest["chunks"]["size_bytes"])

    archive = _replace_manifest(_contract_archive(_valid_chunk()), mutate)

    with pytest.raises(ValueError, match="manifest"):
        validate_publication_archive(archive)


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("missing_file", "缺失"),
        ("unreferenced", "未引用"),
        ("duplicate_manifest_path", "重复路径"),
        ("duplicate_zip_path", "重复路径"),
        ("traversal", "不安全路径"),
        ("symlink", "符号链接"),
        ("media_type", "媒体类型"),
        ("size", "字节数"),
        ("size_type", "字节数"),
    ],
)
def test_archive_validator_rejects_asset_and_path_contract_mismatches(
    case: str, expected_error: str
) -> None:
    payload = b"\x89PNG\r\n\x1a\ncontract-asset"
    sha256 = hashlib.sha256(payload).hexdigest()
    asset: dict[str, object] = {
        "path": f"assets/{sha256}.png",
        "sha256": sha256,
        "size_bytes": len(payload),
        "media_type": "image/png",
        "byte_contract": "standard_render_crop",
    }
    chunk = _chunk_with_asset(asset)
    assets = [dict(asset)]
    entries: list[tuple[str | zipfile.ZipInfo, bytes]] = [(str(asset["path"]), payload)]

    if case == "missing_file":
        entries = []
    elif case == "unreferenced":
        chunk = _valid_chunk()
    elif case == "duplicate_manifest_path":
        assets.append({**asset, "sha256": "b" * 64})
    elif case == "duplicate_zip_path":
        entries.append((str(asset["path"]), payload))
    elif case == "traversal":
        asset["path"] = "../contract-asset.png"
        assets = [dict(asset)]
        chunk = _chunk_with_asset(asset)
        entries = [(str(asset["path"]), payload)]
    elif case == "symlink":
        info = zipfile.ZipInfo(str(asset["path"]))
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        entries = [(info, payload)]
    elif case == "media_type":
        asset["media_type"] = "image/jpeg"
        assets = [dict(asset)]
        chunk = _chunk_with_asset(asset)
    elif case == "size":
        asset["size_bytes"] = len(payload) + 1
        assets = [dict(asset)]
        chunk = _chunk_with_asset(asset)
    elif case == "size_type":
        asset["size_bytes"] = str(len(payload))
        assets = [dict(asset)]
        chunk = _chunk_with_asset(asset)

    with pytest.raises((ValueError, KeyError), match=expected_error):
        validate_publication_archive(
            _contract_archive(chunk, assets=assets, asset_entries=entries)
        )


def test_archive_validator_accepts_matching_original_jpeg_asset() -> None:
    payload = b"\xff\xd8\xff\xe0synthetic-jpeg"
    sha256 = hashlib.sha256(payload).hexdigest()
    asset: dict[str, object] = {
        "path": f"assets/{sha256}.jpg",
        "sha256": sha256,
        "size_bytes": len(payload),
        "media_type": "image/jpeg",
        "byte_contract": "anydoc_original",
    }
    chunk = _chunk_with_asset(asset)

    validate_publication_archive(
        _contract_archive(
            chunk,
            assets=[asset],
            asset_entries=[(str(asset["path"]), payload)],
        )
    )
