from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import sqlite3
import uuid
from io import BytesIO
from typing import Any, cast

from PIL import Image, UnidentifiedImageError

from pptextract.config import Settings
from pptextract.db import transaction
from pptextract.jobs import timestamp
from pptextract.object_store import LocalObjectStore


class CurationRequestError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


VISUAL_TYPES = {
    "chart",
    "diagram",
    "map",
    "table",
    "screenshot",
    "photo",
    "illustration",
    "other",
}
CAPTURE_PADDING_PX = 8


def _read_current_page(
    connection: sqlite3.Connection, page_id: str
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT pv.page_version_id, pv.page_id, pv.document_id, pv.version_id,
               pv.page_number, pv.review_status, pv.source_content_json,
               pv.current_snapshot_id, pv.render_sha256, pv.render_media_type,
               pv.render_width_px, pv.render_height_px
        FROM page_versions AS pv
        JOIN documents AS d
          ON d.document_id = pv.document_id
         AND d.current_version_id = pv.version_id
        WHERE d.deleted_at IS NULL AND pv.page_id = ?
        """,
        (page_id,),
    ).fetchone()
    if row is None:
        raise CurationRequestError(404, "not_found", "未找到请求的资源。")
    return cast(sqlite3.Row, row)


def _read_confirmation(
    connection: sqlite3.Connection, snapshot_id: str
) -> dict[str, str] | None:
    row = connection.execute(
        """
        SELECT actor_id, confirmed_at
        FROM curation_source_confirmations WHERE snapshot_id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    if row is None:
        return None
    return {"actor_id": str(row["actor_id"]), "confirmed_at": str(row["confirmed_at"])}


def _read_review(
    connection: sqlite3.Connection, snapshot_id: str
) -> dict[str, str] | None:
    row = connection.execute(
        """
        SELECT actor_id, completed_at
        FROM curation_source_reviews WHERE snapshot_id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    if row is None:
        return None
    return {"actor_id": str(row["actor_id"]), "completed_at": str(row["completed_at"])}


def _read_image_source_decisions(
    connection: sqlite3.Connection, snapshot_id: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT source_ref, reference_index, position, disposition, summary,
               ignore_reason, ignore_note, visual_ref, object_sha256,
               media_type, size_bytes, origin_part, alt_text,
               decided_by, decided_at
        FROM curation_snapshot_image_sources
        WHERE snapshot_id = ? ORDER BY position
        """,
        (snapshot_id,),
    ).fetchall()
    return [
        {
            "source_ref": str(row["source_ref"]),
            "reference_index": int(row["reference_index"]),
            "position": int(row["position"]),
            "disposition": str(row["disposition"]),
            "summary": row["summary"],
            "ignore_reason": row["ignore_reason"],
            "ignore_note": row["ignore_note"],
            "visual_ref": row["visual_ref"],
            "object_sha256": row["object_sha256"],
            "media_type": str(row["media_type"]),
            "size_bytes": row["size_bytes"],
            "origin_part": str(row["origin_part"]),
            "alt_text": str(row["alt_text"]),
            "decided_by": str(row["decided_by"]),
            "decided_at": str(row["decided_at"]),
        }
        for row in rows
    ]


def _read_snapshot(
    connection: sqlite3.Connection, snapshot_id: str | None
) -> dict[str, Any] | None:
    if snapshot_id is None:
        return None
    row = connection.execute(
        """
        SELECT snapshot_id, source_snapshot_id, source_content_json,
               created_by, created_at
        FROM curation_snapshots WHERE snapshot_id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    if row is None or row["source_content_json"] is None:
        return None
    return {
        "snapshot_id": str(row["snapshot_id"]),
        "source_snapshot_id": row["source_snapshot_id"],
        "source_content": json.loads(str(row["source_content_json"])),
        "created_by": row["created_by"],
        "created_at": str(row["created_at"]),
        "source_confirmation": _read_confirmation(connection, snapshot_id),
        "source_review": _read_review(connection, snapshot_id),
        "image_source_decisions": _read_image_source_decisions(connection, snapshot_id),
    }


def _text_fragments(content: dict[str, Any]) -> list[str]:
    fragments: list[str] = []
    for key in ("titles", "body"):
        fragments.extend(str(value).strip() for value in content.get(key, []) if str(value).strip())
    for table in content.get("tables", []):
        for row in table.get("grid", []):
            cells = []
            for slot in row:
                if slot.get("kind") != "origin":
                    continue
                cell = slot.get("cell") or {}
                value = str(cell.get("text", "")).strip()
                if value:
                    cells.append(value)
            if cells:
                fragments.append("\t".join(cells))
    fragments.extend(
        str(value).strip()
        for value in content.get("speaker_notes", [])
        if str(value).strip()
    )
    return fragments


def build_chunk_body(content: dict[str, Any]) -> str:
    return "\n\n".join(_text_fragments(content))


def _source_image_count(content: dict[str, Any]) -> int:
    images = content.get("images", [])
    return len(images) if isinstance(images, list) else 0


def source_image_ref(page_version_id: str, reference_index: int) -> str:
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"https://pptextract.local/source-images/{page_version_id}/{reference_index}",
    ).hex


def _decode_source_image(image: dict[str, Any]) -> bytes | None:
    encoded = image.get("data_base64")
    if not isinstance(encoded, str):
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None


def _source_object_counts(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute(
        """
        SELECT sources.object_sha256, COUNT(*) AS reference_count
        FROM page_version_image_sources AS sources
        JOIN page_versions AS pv
          ON pv.page_version_id = sources.page_version_id
        JOIN documents AS d
          ON d.document_id = pv.document_id
         AND d.current_version_id = pv.version_id
        WHERE d.deleted_at IS NULL
        GROUP BY sources.object_sha256
        """
    ).fetchall()
    return {
        str(row["object_sha256"]): int(row["reference_count"])
        for row in rows
    }


def _read_source_images(
    connection: sqlite3.Connection,
    page: sqlite3.Row,
    original: dict[str, Any],
) -> list[dict[str, Any]]:
    object_counts = _source_object_counts(connection)
    source_metadata = {
        int(row["reference_index"]): row
        for row in connection.execute(
            """
            SELECT reference_index, position, object_sha256, size_bytes
            FROM page_version_image_sources
            WHERE page_version_id = ? ORDER BY position
            """,
            (page["page_version_id"],),
        ).fetchall()
    }
    items: list[dict[str, Any]] = []
    for position, raw in enumerate(original.get("images", [])):
        if not isinstance(raw, dict):
            continue
        reference_index = int(raw.get("reference_index", position))
        payload = _decode_source_image(raw)
        current_sha256 = None if payload is None else hashlib.sha256(payload).hexdigest()
        metadata = source_metadata.get(reference_index)
        if metadata is None:
            object_sha256 = current_sha256
            size_bytes = None if payload is None else len(payload)
        else:
            object_sha256 = str(metadata["object_sha256"])
            size_bytes = int(metadata["size_bytes"])
        integrity = (
            "missing"
            if payload is None
            else "hash_mismatch"
            if object_sha256 != current_sha256
            else "verified"
        )
        source_ref = source_image_ref(str(page["page_version_id"]), reference_index)
        items.append(
            {
                "source_ref": source_ref,
                "reference_index": reference_index,
                "position": position,
                "alt_text": str(raw.get("alt_text", "")),
                "media_type": str(raw.get("media_type", "application/octet-stream")),
                "origin_part": str(raw.get("origin_part", "")),
                "object_sha256": object_sha256,
                "size_bytes": size_bytes,
                "integrity": integrity,
                "duplicate_object": (
                    object_sha256 is not None and object_counts.get(object_sha256, 0) > 1
                ),
                "preview_url": f"/api/v1/pages/{page['page_id']}/source-images/{source_ref}",
                "disposition": None,
                "summary": None,
                "ignore_reason": None,
                "ignore_note": None,
                "visual_ref": None,
                "decided_by": None,
                "decided_at": None,
            }
        )
    snapshot_id = page["current_snapshot_id"]
    if snapshot_id is not None:
        decisions = {
            decision["source_ref"]: decision
            for decision in _read_image_source_decisions(connection, str(snapshot_id))
        }
        for item in items:
            decision = decisions.get(item["source_ref"])
            if decision is not None:
                item.update(
                    {
                        key: decision[key]
                        for key in (
                            "disposition",
                            "summary",
                            "ignore_reason",
                            "ignore_note",
                            "visual_ref",
                            "decided_by",
                            "decided_at",
                        )
                    }
                )
                if (
                    decision["disposition"] == "included"
                    and decision["object_sha256"] is not None
                    and decision["object_sha256"] != item["object_sha256"]
                ):
                    item["integrity"] = "hash_mismatch"
    return items


def _image_source_blocker(item: dict[str, Any]) -> dict[str, str] | None:
    number = int(item["position"]) + 1
    source_ref = str(item["source_ref"])
    disposition = item["disposition"]
    if disposition is None:
        return {
            "code": "image_disposition_required",
            "message": f"图片来源 {number:02d}：尚未选择保留或忽略。",
            "source_ref": source_ref,
        }
    if disposition == "included" and not str(item["summary"] or "").strip():
        return {
            "code": "image_summary_required",
            "message": f"图片来源 {number:02d}：保留项缺少 summary。",
            "source_ref": source_ref,
        }
    if disposition == "included" and item["integrity"] == "hash_mismatch":
        return {
            "code": "image_hash_mismatch",
            "message": f"图片来源 {number:02d}：原始字节与已记录哈希不一致。",
            "source_ref": source_ref,
        }
    if disposition == "ignored" and not item["ignore_reason"]:
        return {
            "code": "image_reason_required",
            "message": f"图片来源 {number:02d}：忽略项缺少原因。",
            "source_ref": source_ref,
        }
    if (
        disposition == "ignored"
        and item["ignore_reason"] == "other"
        and not str(item["ignore_note"] or "").strip()
    ):
        return {
            "code": "image_other_note_required",
            "message": f"图片来源 {number:02d}：“其他”原因缺少说明。",
            "source_ref": source_ref,
        }
    if disposition == "included" and item["object_sha256"] is None:
        return {
            "code": "image_bytes_unavailable",
            "message": f"图片来源 {number:02d}：原始字节缺失或无法校验。",
            "source_ref": source_ref,
        }
    if disposition == "included" and not str(item["media_type"]).startswith("image/"):
        return {
            "code": "image_media_type_unsupported",
            "message": f"图片来源 {number:02d}：媒体类型不受产物契约支持。",
            "source_ref": source_ref,
        }
    return None


def _capture_visual_blockers(
    connection: sqlite3.Connection, snapshot_id: str | None
) -> list[dict[str, str]]:
    if snapshot_id is None:
        return []
    rows = connection.execute(
        """
        SELECT visual_ref, position, summary
        FROM curation_snapshot_visuals
        WHERE snapshot_id = ? AND source_kind = 'capture' AND disposition = 'included'
        ORDER BY position
        """,
        (snapshot_id,),
    ).fetchall()
    return [
        {
            "code": "visual_summary_required",
            "message": f"视觉对象 {index + 1:02d}：缺少 summary。",
            "visual_ref": str(row["visual_ref"]),
        }
        for index, row in enumerate(rows)
        if not str(row["summary"] or "").strip()
    ]


def read_curation_state(
    connection: sqlite3.Connection, page: sqlite3.Row
) -> dict[str, Any]:
    original = json.loads(str(page["source_content_json"]))
    snapshot = _read_snapshot(connection, page["current_snapshot_id"])
    effective = original if snapshot is None else snapshot["source_content"]
    image_sources = _read_source_images(connection, page, original)
    image_count = len(image_sources)
    image_blockers = [
        blocker
        for item in image_sources
        if (blocker := _image_source_blocker(item)) is not None
    ]
    visual_blockers = _capture_visual_blockers(
        connection, None if snapshot is None else str(snapshot["snapshot_id"])
    )
    capture_required = bool(
        snapshot is not None
        and connection.execute(
            "SELECT capture_required FROM curation_snapshots WHERE snapshot_id = ?",
            (snapshot["snapshot_id"],),
        ).fetchone()[0]
    )
    confirmation = None if snapshot is None else snapshot["source_confirmation"]
    review = None if snapshot is None else snapshot["source_review"]
    chunk_nonempty = bool(build_chunk_body(effective).strip())
    blockers: list[dict[str, str]] = []
    if snapshot is None:
        blockers.append({"code": "source_unsaved", "message": "文字修改尚未保存。"})
    if confirmation is None:
        blockers.append({"code": "source_unconfirmed", "message": "文字来源尚未确认。"})
    if review is None:
        blockers.append(
            {"code": "source_review_incomplete", "message": "来源审核尚未完成。"}
        )
    blockers.extend(image_blockers)
    blockers.extend(visual_blockers)
    if capture_required:
        blockers.append(
            {
                "code": "capture_required",
                "message": "来源仍有缺口：请重新框选视觉对象，或明确改选来源完整。",
            }
        )
    if not chunk_nonempty:
        blockers.append(
            {
                "code": "chunk_body_empty",
                "message": "已确认来源无法生成非空 Chunk 正文。",
            }
        )
    pending = page["review_status"] == "pending"
    return {
        "current_snapshot": snapshot,
        "image_sources": {
            "total": image_count,
            "unresolved": len(image_blockers),
            "items": image_sources,
        },
        "chunk_body": {"nonempty": chunk_nonempty},
        "blockers": blockers,
        "can_confirm_source": pending and snapshot is not None,
        "can_complete_source_review": (
            pending
            and snapshot is not None
            and confirmation is not None
            and not image_blockers
        ),
        "can_approve": pending and snapshot is not None and not blockers,
    }


def read_page_curation(
    connection: sqlite3.Connection, page_id: str
) -> dict[str, Any]:
    return read_curation_state(connection, _read_current_page(connection, page_id))


def read_source_image(
    connection: sqlite3.Connection, *, page_id: str, source_ref: str
) -> tuple[bytes, str] | None:
    page = _read_current_page(connection, page_id)
    original = json.loads(str(page["source_content_json"]))
    for position, raw in enumerate(original.get("images", [])):
        if not isinstance(raw, dict):
            continue
        reference_index = int(raw.get("reference_index", position))
        if source_image_ref(str(page["page_version_id"]), reference_index) != source_ref:
            continue
        payload = _decode_source_image(raw)
        if payload is None:
            return None
        return payload, str(raw.get("media_type", "application/octet-stream"))
    return None


def _validate_editable_source(
    original: dict[str, Any], titles: list[str], body: list[str]
) -> dict[str, Any]:
    if len(titles) != len(original.get("titles", [])) or len(body) != len(
        original.get("body", [])
    ):
        raise CurationRequestError(
            422,
            "source_structure_changed",
            "本工作位只能修订既有标题与正文块，不能新增、删除或重排来源块。",
        )
    if any(len(value) > 100_000 for value in [*titles, *body]) or sum(
        len(value) for value in [*titles, *body]
    ) > 1_000_000:
        raise CurationRequestError(422, "source_too_long", "来源文字超出单页可保存长度。")
    content = cast(dict[str, Any], json.loads(json.dumps(original)))
    content["titles"] = titles
    content["body"] = body
    for image in content.get("images", []):
        image.pop("data_base64", None)
    return content


def save_source_snapshot(
    settings: Settings,
    *,
    page_id: str,
    actor_id: str,
    base_snapshot_id: str | None,
    titles: list[str],
    body: list[str],
) -> dict[str, Any]:
    with transaction(settings) as connection:
        page = _read_current_page(connection, page_id)
        if page["review_status"] != "pending":
            raise CurationRequestError(409, "page_not_pending", "只有待处理页可以修改来源。")
        if page["current_snapshot_id"] != base_snapshot_id:
            raise CurationRequestError(
                409,
                "curation_snapshot_stale",
                "此页已被其他会话更新，请重新加载后比较修改。",
            )
        original = json.loads(str(page["source_content_json"]))
        content = _validate_editable_source(original, titles, body)
        capture_required = 0
        if base_snapshot_id is not None:
            capture_required = int(
                connection.execute(
                    "SELECT capture_required FROM curation_snapshots WHERE snapshot_id = ?",
                    (base_snapshot_id,),
                ).fetchone()[0]
            )
        snapshot_id = uuid.uuid4().hex
        now = timestamp()
        connection.execute(
            """
            INSERT INTO curation_snapshots (
                snapshot_id, page_version_id, snapshot_kind, source_snapshot_id,
                overview, source_content_json, capture_required, created_by, created_at
            ) VALUES (?, ?, 'formal', ?, NULL, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                page["page_version_id"],
                base_snapshot_id,
                json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                capture_required,
                actor_id,
                now,
            ),
        )
        if base_snapshot_id is not None:
            _copy_image_source_records(
                connection,
                source_snapshot_id=base_snapshot_id,
                target_snapshot_id=snapshot_id,
            )
        updated = connection.execute(
            """
            UPDATE page_versions SET current_snapshot_id = ?
            WHERE page_version_id = ? AND current_snapshot_id IS ?
            """,
            (snapshot_id, page["page_version_id"], base_snapshot_id),
        )
        if updated.rowcount != 1:
            raise CurationRequestError(
                409,
                "curation_snapshot_stale",
                "此页已被其他会话更新，请重新加载后比较修改。",
            )
        refreshed = _read_current_page(connection, page_id)
        return read_curation_state(connection, refreshed)


def confirm_source_snapshot(
    settings: Settings, *, page_id: str, actor_id: str, snapshot_id: str
) -> dict[str, Any]:
    with transaction(settings) as connection:
        page = _read_current_page(connection, page_id)
        _assert_current_pending_snapshot(page, snapshot_id)
        existing = _read_confirmation(connection, snapshot_id)
        if existing is None:
            connection.execute(
                """
                INSERT INTO curation_source_confirmations (
                    confirmation_id, snapshot_id, actor_id, confirmed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (uuid.uuid4().hex, snapshot_id, actor_id, timestamp()),
            )
        return read_curation_state(connection, page)


def _copy_image_source_records(
    connection: sqlite3.Connection,
    *,
    source_snapshot_id: str,
    target_snapshot_id: str,
    excluded_source_ref: str | None = None,
    excluded_visual_ref: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO curation_snapshot_image_sources (
            snapshot_id, source_ref, reference_index, position, disposition,
            summary, ignore_reason, ignore_note, visual_ref, object_sha256,
            media_type, size_bytes, origin_part, alt_text, decided_by, decided_at
        )
        SELECT ?, source_ref, reference_index, position, disposition,
               summary, ignore_reason, ignore_note, visual_ref, object_sha256,
               media_type, size_bytes, origin_part, alt_text, decided_by, decided_at
        FROM curation_snapshot_image_sources
        WHERE snapshot_id = ? AND (? IS NULL OR source_ref <> ?)
        """,
        (
            target_snapshot_id,
            source_snapshot_id,
            excluded_source_ref,
            excluded_source_ref,
        ),
    )
    connection.execute(
        """
        INSERT INTO curation_snapshot_visuals (
            snapshot_id, visual_ref, position, source_kind, disposition,
            summary, visual_type, bounds_json, source_visual_ref, confirmed,
            source_image_ref, asset_sha256, asset_media_type, asset_size_bytes,
            asset_width_px, asset_height_px
        )
        SELECT ?, visual_ref, position, source_kind, disposition,
               summary, visual_type, bounds_json, source_visual_ref, confirmed,
               source_image_ref, asset_sha256, asset_media_type, asset_size_bytes,
               asset_width_px, asset_height_px
        FROM curation_snapshot_visuals
        WHERE snapshot_id = ?
          AND (? IS NULL OR source_image_ref IS NULL OR source_image_ref <> ?)
          AND (? IS NULL OR visual_ref <> ?)
        """,
        (
            target_snapshot_id,
            source_snapshot_id,
            excluded_source_ref,
            excluded_source_ref,
            excluded_visual_ref,
            excluded_visual_ref,
        ),
    )


def save_image_source_disposition(
    settings: Settings,
    *,
    page_id: str,
    actor_id: str,
    base_snapshot_id: str,
    source_ref: str,
    disposition: str,
    summary: str | None,
    ignore_reason: str | None,
    ignore_note: str | None,
) -> dict[str, Any]:
    if disposition not in {"included", "ignored"}:
        raise CurationRequestError(
            422, "invalid_image_disposition", "图片来源处置必须选择保留或忽略。"
        )
    valid_reasons = {
        "decorative",
        "duplicate_source",
        "expressed_elsewhere",
        "not_relevant",
        "corrupt_or_unverifiable",
        "other",
    }
    if ignore_reason is not None and ignore_reason not in valid_reasons:
        raise CurationRequestError(
            422, "invalid_image_ignore_reason", "图片来源忽略原因无效。"
        )
    if disposition == "included" and not str(summary or "").strip():
        raise CurationRequestError(
            422, "image_summary_required", "保留的图片来源必须填写自足 summary。"
        )
    if disposition == "ignored" and ignore_reason is None:
        raise CurationRequestError(
            422, "image_reason_required", "忽略的图片来源必须选择原因。"
        )
    if (
        disposition == "ignored"
        and ignore_reason == "other"
        and not str(ignore_note or "").strip()
    ):
        raise CurationRequestError(
            422, "image_other_note_required", "选择“其他”时必须填写原因说明。"
        )
    with transaction(settings) as connection:
        page = _read_current_page(connection, page_id)
        _assert_current_pending_snapshot(page, base_snapshot_id)
        snapshot = _read_snapshot(connection, base_snapshot_id)
        if snapshot is None:
            raise CurationRequestError(409, "source_unsaved", "请先保存文字来源。")
        original = json.loads(str(page["source_content_json"]))
        source_items = _read_source_images(connection, page, original)
        item = next(
            (
                candidate
                for candidate in source_items
                if candidate["source_ref"] == source_ref
            ),
            None,
        )
        if item is None:
            raise CurationRequestError(404, "source_image_not_found", "未找到图片来源引用。")
        raw = next(
            (
                candidate
                for candidate in original.get("images", [])
                if isinstance(candidate, dict)
                and int(candidate.get("reference_index", -1)) == item["reference_index"]
            ),
            None,
        )
        payload = None if raw is None else _decode_source_image(raw)
        object_sha256 = item["object_sha256"]
        size_bytes = item["size_bytes"]
        now = timestamp()
        visual_ref: str | None = None
        stored = None
        if disposition == "included" and item["integrity"] == "hash_mismatch":
            raise CurationRequestError(
                409,
                "source_image_hash_mismatch",
                "图片来源原始字节与已记录哈希不一致，不能覆盖既有处置。",
            )
        if disposition == "included" and payload is None:
            raise CurationRequestError(
                409,
                "source_image_bytes_unavailable",
                "图片来源原始字节缺失或无法校验，不能纳入产物。",
            )
        if disposition == "included" and not str(item["media_type"]).startswith(
            "image/"
        ):
            raise CurationRequestError(
                422,
                "source_image_media_type_unsupported",
                "图片来源媒体类型不受产物契约支持。",
            )
        if disposition == "included" and payload is not None:
            stored = LocalObjectStore(settings.object_store_path).put(payload)
            if stored.sha256 != object_sha256:
                raise CurationRequestError(
                    409,
                    "source_image_hash_mismatch",
                    "图片来源原始字节的内容哈希不一致。",
                )
            connection.execute(
                """
                INSERT INTO stored_objects (sha256, size_bytes, media_type, verified_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(sha256) DO UPDATE SET
                    size_bytes = excluded.size_bytes,
                    verified_at = excluded.verified_at
                """,
                (stored.sha256, stored.size_bytes, item["media_type"], now),
            )
            historical = connection.execute(
                """
                SELECT decisions.visual_ref
                FROM curation_snapshot_image_sources AS decisions
                JOIN curation_snapshots AS snapshots
                  ON snapshots.snapshot_id = decisions.snapshot_id
                WHERE snapshots.page_version_id = ?
                  AND decisions.source_ref = ? AND decisions.visual_ref IS NOT NULL
                ORDER BY snapshots.created_at DESC, snapshots.snapshot_id DESC
                LIMIT 1
                """,
                (page["page_version_id"], source_ref),
            ).fetchone()
            visual_ref = (
                str(historical["visual_ref"])
                if historical is not None
                else uuid.uuid4().hex
            )
            if historical is None:
                connection.execute(
                    """
                    INSERT INTO visual_objects (visual_ref, page_id, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (visual_ref, page["page_id"], now),
                )

        snapshot_id = uuid.uuid4().hex
        capture_required = int(
            connection.execute(
                "SELECT capture_required FROM curation_snapshots WHERE snapshot_id = ?",
                (base_snapshot_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO curation_snapshots (
                snapshot_id, page_version_id, snapshot_kind, source_snapshot_id,
                overview, source_content_json, capture_required, created_by, created_at
            ) VALUES (?, ?, 'formal', ?, NULL, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                page["page_version_id"],
                base_snapshot_id,
                json.dumps(
                    snapshot["source_content"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                capture_required,
                actor_id,
                now,
            ),
        )
        _copy_image_source_records(
            connection,
            source_snapshot_id=base_snapshot_id,
            target_snapshot_id=snapshot_id,
            excluded_source_ref=source_ref,
        )
        confirmation = _read_confirmation(connection, base_snapshot_id)
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
        normalized_summary = str(summary).strip() if disposition == "included" else None
        normalized_reason = ignore_reason if disposition == "ignored" else None
        normalized_note = (
            str(ignore_note).strip() or None
            if disposition == "ignored" and ignore_note is not None
            else None
        )
        connection.execute(
            """
            INSERT INTO curation_snapshot_image_sources (
                snapshot_id, source_ref, reference_index, position, disposition,
                summary, ignore_reason, ignore_note, visual_ref, object_sha256,
                media_type, size_bytes, origin_part, alt_text, decided_by, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                source_ref,
                item["reference_index"],
                item["position"],
                disposition,
                normalized_summary,
                normalized_reason,
                normalized_note,
                visual_ref,
                object_sha256,
                item["media_type"],
                size_bytes,
                item["origin_part"],
                item["alt_text"],
                actor_id,
                now,
            ),
        )
        if disposition == "included" and visual_ref is not None and stored is not None:
            connection.execute(
                """
                INSERT INTO curation_snapshot_visuals (
                    snapshot_id, visual_ref, position, source_kind, disposition,
                    summary, visual_type, bounds_json, source_visual_ref, confirmed,
                    source_image_ref, asset_sha256, asset_media_type, asset_size_bytes
                ) VALUES (?, ?, ?, 'source_image', 'included', ?, NULL, NULL, NULL,
                          ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    visual_ref,
                    item["position"],
                    normalized_summary,
                    int(bool(str(normalized_summary or "").strip())),
                    source_ref,
                    stored.sha256,
                    item["media_type"],
                    stored.size_bytes,
                ),
            )
        updated = connection.execute(
            """
            UPDATE page_versions SET current_snapshot_id = ?
            WHERE page_version_id = ? AND current_snapshot_id = ?
            """,
            (snapshot_id, page["page_version_id"], base_snapshot_id),
        )
        if updated.rowcount != 1:
            raise CurationRequestError(
                409,
                "curation_snapshot_stale",
                "此页已被其他会话更新，请重新加载后比较修改。",
            )
        return read_curation_state(connection, _read_current_page(connection, page_id))


def complete_source_review(
    settings: Settings, *, page_id: str, actor_id: str, snapshot_id: str
) -> dict[str, Any]:
    with transaction(settings) as connection:
        page = _read_current_page(connection, page_id)
        _assert_current_pending_snapshot(page, snapshot_id)
        if _read_confirmation(connection, snapshot_id) is None:
            raise CurationRequestError(
                409, "source_unconfirmed", "请先显式确认文字来源。"
            )
        state = read_curation_state(connection, page)
        image_blockers = [
            blocker
            for blocker in state["blockers"]
            if str(blocker["code"]).startswith("image_")
        ]
        if image_blockers:
            raise CurationRequestError(
                409,
                "image_sources_unresolved",
                "图片来源尚待逐项处置，暂时不能完成来源审核。",
            )
        if _read_review(connection, snapshot_id) is None:
            connection.execute(
                """
                INSERT INTO curation_source_reviews (
                    review_id, snapshot_id, actor_id, completed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (uuid.uuid4().hex, snapshot_id, actor_id, timestamp()),
            )
        return read_curation_state(connection, page)


def _copy_snapshot_gate_records(
    connection: sqlite3.Connection,
    *,
    source_snapshot_id: str,
    target_snapshot_id: str,
) -> None:
    confirmation = _read_confirmation(connection, source_snapshot_id)
    if confirmation is not None:
        connection.execute(
            """
            INSERT INTO curation_source_confirmations (
                confirmation_id, snapshot_id, actor_id, confirmed_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                target_snapshot_id,
                confirmation["actor_id"],
                confirmation["confirmed_at"],
            ),
        )
    review = _read_review(connection, source_snapshot_id)
    if review is not None:
        connection.execute(
            """
            INSERT INTO curation_source_reviews (
                review_id, snapshot_id, actor_id, completed_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                target_snapshot_id,
                review["actor_id"],
                review["completed_at"],
            ),
        )


def _normalized_capture_bounds(bounds: dict[str, float]) -> dict[str, float]:
    normalized = {
        key: float(bounds[key]) for key in ("left", "top", "width", "height")
    }
    left = normalized["left"]
    top = normalized["top"]
    width = normalized["width"]
    height = normalized["height"]
    if (
        not all(math.isfinite(value) for value in normalized.values())
        or left < 0
        or top < 0
        or width <= 0
        or height <= 0
        or left + width > 1
        or top + height > 1
    ):
        raise CurationRequestError(
            422,
            "invalid_visual_bounds",
            "框选范围必须是整页内具有有效面积的归一化坐标。",
        )
    return normalized


def _crop_standard_render(
    settings: Settings,
    page: sqlite3.Row,
    bounds: dict[str, float],
) -> tuple[bytes, int, int]:
    render_sha256 = page["render_sha256"]
    if render_sha256 is None or page["render_media_type"] != "image/png":
        raise CurationRequestError(
            409, "standard_render_unavailable", "标准页渲染结果暂不可用于框选裁图。"
        )
    try:
        render_path = LocalObjectStore(settings.object_store_path).path_for(
            str(render_sha256)
        )
    except ValueError as error:
        raise CurationRequestError(
            409, "standard_render_unavailable", "标准页渲染结果暂不可用于框选裁图。"
        ) from error
    if not render_path.is_file():
        raise CurationRequestError(
            409, "standard_render_unavailable", "标准页渲染结果暂不可用于框选裁图。"
        )
    try:
        with Image.open(render_path) as rendered:
            rendered.load()
            if rendered.format != "PNG":
                raise CurationRequestError(
                    409,
                    "standard_render_unavailable",
                    "标准页渲染结果不是可裁剪的 PNG。",
                )
            render_width, render_height = rendered.size
            left_px = max(
                0,
                math.floor(bounds["left"] * render_width) - CAPTURE_PADDING_PX,
            )
            top_px = max(
                0,
                math.floor(bounds["top"] * render_height) - CAPTURE_PADDING_PX,
            )
            right_px = min(
                render_width,
                math.ceil((bounds["left"] + bounds["width"]) * render_width)
                + CAPTURE_PADDING_PX,
            )
            bottom_px = min(
                render_height,
                math.ceil((bounds["top"] + bounds["height"]) * render_height)
                + CAPTURE_PADDING_PX,
            )
            cropped = rendered.crop((left_px, top_px, right_px, bottom_px))
            output = BytesIO()
            cropped.save(output, format="PNG")
    except (OSError, UnidentifiedImageError) as error:
        raise CurationRequestError(
            409, "standard_render_unavailable", "标准页渲染结果暂不可用于框选裁图。"
        ) from error
    return output.getvalue(), right_px - left_px, bottom_px - top_px


def _read_capture_visual(
    connection: sqlite3.Connection, *, snapshot_id: str, visual_ref: str
) -> sqlite3.Row:
    visual = connection.execute(
        """
        SELECT visual_ref, position, summary, visual_type, bounds_json
        FROM curation_snapshot_visuals
        WHERE snapshot_id = ? AND visual_ref = ?
          AND source_kind = 'capture' AND disposition = 'included'
        """,
        (snapshot_id, visual_ref),
    ).fetchone()
    if visual is None:
        raise CurationRequestError(404, "visual_not_found", "未找到指定的人工截图视觉对象。")
    return cast(sqlite3.Row, visual)


def _create_derived_visual_snapshot(
    connection: sqlite3.Connection,
    *,
    page: sqlite3.Row,
    source_snapshot: dict[str, Any],
    actor_id: str,
    capture_required: bool,
    excluded_visual_ref: str | None = None,
) -> str:
    source_snapshot_id = str(source_snapshot["snapshot_id"])
    snapshot_id = uuid.uuid4().hex
    connection.execute(
        """
        INSERT INTO curation_snapshots (
            snapshot_id, page_version_id, snapshot_kind, source_snapshot_id,
            overview, source_content_json, capture_required, created_by, created_at
        ) VALUES (?, ?, 'formal', ?, NULL, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            page["page_version_id"],
            source_snapshot_id,
            json.dumps(
                source_snapshot["source_content"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            int(capture_required),
            actor_id,
            timestamp(),
        ),
    )
    _copy_image_source_records(
        connection,
        source_snapshot_id=source_snapshot_id,
        target_snapshot_id=snapshot_id,
        excluded_visual_ref=excluded_visual_ref,
    )
    _copy_snapshot_gate_records(
        connection,
        source_snapshot_id=source_snapshot_id,
        target_snapshot_id=snapshot_id,
    )
    return snapshot_id


def _activate_snapshot(
    connection: sqlite3.Connection,
    *,
    page: sqlite3.Row,
    base_snapshot_id: str,
    snapshot_id: str,
) -> None:
    updated = connection.execute(
        """
        UPDATE page_versions SET current_snapshot_id = ?
        WHERE page_version_id = ? AND current_snapshot_id = ?
        """,
        (snapshot_id, page["page_version_id"], base_snapshot_id),
    )
    if updated.rowcount != 1:
        raise CurationRequestError(
            409,
            "curation_snapshot_stale",
            "此页已被其他会话更新，请重新加载后继续。",
        )


def _store_capture_asset(
    connection: sqlite3.Connection,
    settings: Settings,
    *,
    page: sqlite3.Row,
    bounds: dict[str, float],
) -> tuple[str, int, int, int]:
    asset_bytes, width_px, height_px = _crop_standard_render(settings, page, bounds)
    stored = LocalObjectStore(settings.object_store_path).put(asset_bytes)
    connection.execute(
        """
        INSERT INTO stored_objects (sha256, size_bytes, media_type, verified_at)
        VALUES (?, ?, 'image/png', ?)
        ON CONFLICT(sha256) DO UPDATE SET
            size_bytes = excluded.size_bytes,
            media_type = excluded.media_type,
            verified_at = excluded.verified_at
        """,
        (stored.sha256, stored.size_bytes, timestamp()),
    )
    return stored.sha256, stored.size_bytes, width_px, height_px


def save_capture_visual(
    settings: Settings,
    *,
    page_id: str,
    actor_id: str,
    base_snapshot_id: str,
    summary: str,
    visual_type: str | None,
    bounds: dict[str, float],
) -> dict[str, Any]:
    normalized_summary = summary.strip()
    if not normalized_summary:
        raise CurationRequestError(
            422, "visual_summary_required", "视觉对象必须填写自足 summary。"
        )
    if visual_type is not None and visual_type not in VISUAL_TYPES:
        raise CurationRequestError(
            422, "invalid_visual_type", "视觉对象类型不在初版可选集合中。"
        )
    normalized_bounds = _normalized_capture_bounds(bounds)
    with transaction(settings) as connection:
        page = _read_current_page(connection, page_id)
        _assert_current_pending_snapshot(page, base_snapshot_id)
        snapshot = _read_snapshot(connection, base_snapshot_id)
        if snapshot is None:
            raise CurationRequestError(409, "source_unsaved", "请先保存并审核来源。")
        if _read_review(connection, base_snapshot_id) is None:
            raise CurationRequestError(
                409, "source_review_incomplete", "请先完成来源审核再框选视觉对象。"
            )
        asset_bytes, width_px, height_px = _crop_standard_render(
            settings, page, normalized_bounds
        )
        stored = LocalObjectStore(settings.object_store_path).put(asset_bytes)
        now = timestamp()
        connection.execute(
            """
            INSERT INTO stored_objects (sha256, size_bytes, media_type, verified_at)
            VALUES (?, ?, 'image/png', ?)
            ON CONFLICT(sha256) DO UPDATE SET
                size_bytes = excluded.size_bytes,
                media_type = excluded.media_type,
                verified_at = excluded.verified_at
            """,
            (stored.sha256, stored.size_bytes, now),
        )
        snapshot_id = uuid.uuid4().hex
        connection.execute(
            """
            INSERT INTO curation_snapshots (
                snapshot_id, page_version_id, snapshot_kind, source_snapshot_id,
                overview, source_content_json, created_by, created_at
            ) VALUES (?, ?, 'formal', ?, NULL, ?, ?, ?)
            """,
            (
                snapshot_id,
                page["page_version_id"],
                base_snapshot_id,
                json.dumps(
                    snapshot["source_content"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                actor_id,
                now,
            ),
        )
        _copy_image_source_records(
            connection,
            source_snapshot_id=base_snapshot_id,
            target_snapshot_id=snapshot_id,
        )
        _copy_snapshot_gate_records(
            connection,
            source_snapshot_id=base_snapshot_id,
            target_snapshot_id=snapshot_id,
        )
        position = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(position), -1) + 1
                FROM (
                    SELECT position
                    FROM curation_snapshot_visuals
                    WHERE snapshot_id = ?
                    UNION ALL
                    SELECT position
                    FROM curation_snapshot_image_sources
                    WHERE snapshot_id = ?
                )
                """,
                (snapshot_id, snapshot_id),
            ).fetchone()[0]
        )
        visual_ref = uuid.uuid4().hex
        connection.execute(
            """
            INSERT INTO visual_objects (visual_ref, page_id, created_at)
            VALUES (?, ?, ?)
            """,
            (visual_ref, page["page_id"], now),
        )
        connection.execute(
            """
            INSERT INTO curation_snapshot_visuals (
                snapshot_id, visual_ref, position, source_kind, disposition,
                summary, visual_type, bounds_json, source_visual_ref, confirmed,
                source_image_ref, asset_sha256, asset_media_type, asset_size_bytes,
                asset_width_px, asset_height_px
            ) VALUES (?, ?, ?, 'capture', 'included', ?, ?, ?, NULL, 1,
                      NULL, ?, 'image/png', ?, ?, ?)
            """,
            (
                snapshot_id,
                visual_ref,
                position,
                normalized_summary,
                visual_type,
                json.dumps(normalized_bounds, sort_keys=True, separators=(",", ":")),
                stored.sha256,
                stored.size_bytes,
                width_px,
                height_px,
            ),
        )
        updated = connection.execute(
            """
            UPDATE page_versions SET current_snapshot_id = ?
            WHERE page_version_id = ? AND current_snapshot_id = ?
            """,
            (snapshot_id, page["page_version_id"], base_snapshot_id),
        )
        if updated.rowcount != 1:
            raise CurationRequestError(
                409,
                "curation_snapshot_stale",
                "此页已被其他会话更新，请重新加载后继续。",
            )
        return read_curation_state(connection, _read_current_page(connection, page_id))


def update_capture_visual(
    settings: Settings,
    *,
    page_id: str,
    visual_ref: str,
    actor_id: str,
    base_snapshot_id: str,
    summary: str,
    visual_type: str | None,
    bounds: dict[str, float],
) -> dict[str, Any]:
    normalized_summary = summary.strip()
    if not normalized_summary:
        raise CurationRequestError(
            422, "visual_summary_required", "视觉对象必须填写自足 summary。"
        )
    if visual_type is not None and visual_type not in VISUAL_TYPES:
        raise CurationRequestError(
            422, "invalid_visual_type", "视觉对象类型不在初版可选集合中。"
        )
    normalized_bounds = _normalized_capture_bounds(bounds)
    with transaction(settings) as connection:
        page = _read_current_page(connection, page_id)
        _assert_current_pending_snapshot(page, base_snapshot_id)
        snapshot = _read_snapshot(connection, base_snapshot_id)
        if snapshot is None:
            raise CurationRequestError(409, "source_unsaved", "请先保存并审核来源。")
        _read_capture_visual(
            connection, snapshot_id=base_snapshot_id, visual_ref=visual_ref
        )
        asset_sha256, asset_size_bytes, width_px, height_px = _store_capture_asset(
            connection, settings, page=page, bounds=normalized_bounds
        )
        snapshot_id = _create_derived_visual_snapshot(
            connection,
            page=page,
            source_snapshot=snapshot,
            actor_id=actor_id,
            capture_required=False,
        )
        connection.execute(
            """
            UPDATE curation_snapshot_visuals
            SET summary = ?, visual_type = ?, bounds_json = ?, confirmed = 1,
                asset_sha256 = ?, asset_media_type = 'image/png',
                asset_size_bytes = ?, asset_width_px = ?, asset_height_px = ?
            WHERE snapshot_id = ? AND visual_ref = ?
            """,
            (
                normalized_summary,
                visual_type,
                json.dumps(normalized_bounds, sort_keys=True, separators=(",", ":")),
                asset_sha256,
                asset_size_bytes,
                width_px,
                height_px,
                snapshot_id,
                visual_ref,
            ),
        )
        _activate_snapshot(
            connection,
            page=page,
            base_snapshot_id=base_snapshot_id,
            snapshot_id=snapshot_id,
        )
        return read_curation_state(connection, _read_current_page(connection, page_id))


def delete_capture_visual(
    settings: Settings,
    *,
    page_id: str,
    visual_ref: str,
    actor_id: str,
    base_snapshot_id: str,
) -> dict[str, Any]:
    with transaction(settings) as connection:
        page = _read_current_page(connection, page_id)
        _assert_current_pending_snapshot(page, base_snapshot_id)
        snapshot = _read_snapshot(connection, base_snapshot_id)
        if snapshot is None:
            raise CurationRequestError(409, "source_unsaved", "请先保存并审核来源。")
        _read_capture_visual(
            connection, snapshot_id=base_snapshot_id, visual_ref=visual_ref
        )
        remaining = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM curation_snapshot_visuals
                WHERE snapshot_id = ? AND source_kind = 'capture'
                  AND disposition = 'included' AND visual_ref <> ?
                """,
                (base_snapshot_id, visual_ref),
            ).fetchone()[0]
        )
        snapshot_id = _create_derived_visual_snapshot(
            connection,
            page=page,
            source_snapshot=snapshot,
            actor_id=actor_id,
            capture_required=remaining == 0,
            excluded_visual_ref=visual_ref,
        )
        _activate_snapshot(
            connection,
            page=page,
            base_snapshot_id=base_snapshot_id,
            snapshot_id=snapshot_id,
        )
        return read_curation_state(connection, _read_current_page(connection, page_id))


def move_capture_visual(
    settings: Settings,
    *,
    page_id: str,
    visual_ref: str,
    actor_id: str,
    base_snapshot_id: str,
    direction: str,
) -> dict[str, Any]:
    if direction not in {"up", "down"}:
        raise CurationRequestError(
            422, "invalid_move_direction", "排序方向必须是上移或下移。"
        )
    with transaction(settings) as connection:
        page = _read_current_page(connection, page_id)
        _assert_current_pending_snapshot(page, base_snapshot_id)
        snapshot = _read_snapshot(connection, base_snapshot_id)
        if snapshot is None:
            raise CurationRequestError(409, "source_unsaved", "请先保存并审核来源。")
        captures = connection.execute(
            """
            SELECT visual_ref, position
            FROM curation_snapshot_visuals
            WHERE snapshot_id = ? AND source_kind = 'capture'
              AND disposition = 'included'
            ORDER BY position
            """,
            (base_snapshot_id,),
        ).fetchall()
        target_index = next(
            (
                index
                for index, visual in enumerate(captures)
                if str(visual["visual_ref"]) == visual_ref
            ),
            None,
        )
        if target_index is None:
            raise CurationRequestError(
                404, "visual_not_found", "未找到指定的人工截图视觉对象。"
            )
        adjacent_index = target_index - 1 if direction == "up" else target_index + 1
        if adjacent_index < 0 or adjacent_index >= len(captures):
            raise CurationRequestError(
                409,
                "visual_move_unavailable",
                "视觉对象已位于该方向的边界位置。",
            )
        snapshot_id = _create_derived_visual_snapshot(
            connection,
            page=page,
            source_snapshot=snapshot,
            actor_id=actor_id,
            capture_required=False,
        )
        target_position = int(captures[target_index]["position"])
        adjacent = captures[adjacent_index]
        adjacent_position = int(adjacent["position"])
        temporary_position = int(
            connection.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 "
                "FROM curation_snapshot_visuals WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            UPDATE curation_snapshot_visuals SET position = ?
            WHERE snapshot_id = ? AND visual_ref = ?
            """,
            (temporary_position, snapshot_id, visual_ref),
        )
        connection.execute(
            """
            UPDATE curation_snapshot_visuals SET position = ?
            WHERE snapshot_id = ? AND visual_ref = ?
            """,
            (target_position, snapshot_id, adjacent["visual_ref"]),
        )
        connection.execute(
            """
            UPDATE curation_snapshot_visuals SET position = ?
            WHERE snapshot_id = ? AND visual_ref = ?
            """,
            (adjacent_position, snapshot_id, visual_ref),
        )
        _activate_snapshot(
            connection,
            page=page,
            base_snapshot_id=base_snapshot_id,
            snapshot_id=snapshot_id,
        )
        return read_curation_state(connection, _read_current_page(connection, page_id))


def mark_capture_source_complete(
    settings: Settings,
    *,
    page_id: str,
    actor_id: str,
    snapshot_id: str,
) -> dict[str, Any]:
    with transaction(settings) as connection:
        page = _read_current_page(connection, page_id)
        _assert_current_pending_snapshot(page, snapshot_id)
        snapshot = _read_snapshot(connection, snapshot_id)
        if snapshot is None:
            raise CurationRequestError(409, "source_unsaved", "请先保存并审核来源。")
        derived_snapshot_id = _create_derived_visual_snapshot(
            connection,
            page=page,
            source_snapshot=snapshot,
            actor_id=actor_id,
            capture_required=False,
        )
        _activate_snapshot(
            connection,
            page=page,
            base_snapshot_id=snapshot_id,
            snapshot_id=derived_snapshot_id,
        )
        return read_curation_state(connection, _read_current_page(connection, page_id))


def _assert_current_pending_snapshot(page: sqlite3.Row, snapshot_id: str) -> None:
    if page["review_status"] != "pending":
        raise CurationRequestError(409, "page_not_pending", "此页已不再是待处理状态。")
    if page["current_snapshot_id"] != snapshot_id:
        raise CurationRequestError(
            409,
            "curation_snapshot_stale",
            "此页已被其他会话更新，请重新加载后继续。",
        )


def approve_page(
    settings: Settings, *, page_id: str, actor_id: str, snapshot_id: str
) -> dict[str, Any]:
    with transaction(settings) as connection:
        page = _read_current_page(connection, page_id)
        _assert_current_pending_snapshot(page, snapshot_id)
        state = read_curation_state(connection, page)
        if state["blockers"]:
            raise CurationRequestError(
                409,
                "approval_blocked",
                "页面仍有结构性阻塞，暂时不能批准。",
            )
        snapshot = state["current_snapshot"]
        assert snapshot is not None
        chunk_body = build_chunk_body(snapshot["source_content"])
        now = timestamp()
        updated = connection.execute(
            """
            UPDATE page_versions
            SET review_status = 'approved', reviewed_by = ?, reviewed_at = ?,
                review_source_version_id = ?, exclusion_reason = NULL,
                exclusion_note = NULL
            WHERE page_version_id = ? AND review_status = 'pending'
              AND current_snapshot_id = ?
            """,
            (
                actor_id,
                now,
                page["version_id"],
                page["page_version_id"],
                snapshot_id,
            ),
        )
        if updated.rowcount != 1:
            raise CurationRequestError(
                409,
                "curation_state_changed",
                "页面状态已被其他会话改变，请重新加载。",
            )
        connection.execute(
            """
            INSERT INTO page_review_events (
                event_id, page_version_id, event_type, actor_id, occurred_at,
                source_version_id, source_page_version_id, snapshot_id,
                reason, note
            ) VALUES (?, ?, 'approved', ?, ?, ?, NULL, ?, NULL, NULL)
            """,
            (
                uuid.uuid4().hex,
                page["page_version_id"],
                actor_id,
                now,
                page["version_id"],
                snapshot_id,
            ),
        )
        return {
            "review": {
                "status": "approved",
                "reviewed_by": actor_id,
                "reviewed_at": now,
                "source_version_id": page["version_id"],
                "snapshot_id": snapshot_id,
            },
            "chunk_body": chunk_body,
        }
