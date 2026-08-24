from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, cast

from pptextract.config import Settings
from pptextract.db import transaction
from pptextract.jobs import timestamp


class CurationRequestError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _read_current_page(
    connection: sqlite3.Connection, page_id: str
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT pv.page_version_id, pv.page_id, pv.document_id, pv.version_id,
               pv.page_number, pv.review_status, pv.source_content_json,
               pv.current_snapshot_id
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


def read_curation_state(
    connection: sqlite3.Connection, page: sqlite3.Row
) -> dict[str, Any]:
    original = json.loads(str(page["source_content_json"]))
    snapshot = _read_snapshot(connection, page["current_snapshot_id"])
    effective = original if snapshot is None else snapshot["source_content"]
    image_count = _source_image_count(effective)
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
    if image_count:
        blockers.append(
            {
                "code": "image_sources_unresolved",
                "message": f"{image_count} 个图片来源尚待逐项处置。",
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
        "image_sources": {"total": image_count, "unresolved": image_count},
        "chunk_body": {"nonempty": chunk_nonempty},
        "blockers": blockers,
        "can_confirm_source": pending and snapshot is not None,
        "can_complete_source_review": (
            pending and snapshot is not None and confirmation is not None and image_count == 0
        ),
        "can_approve": pending and snapshot is not None and not blockers,
    }


def read_page_curation(
    connection: sqlite3.Connection, page_id: str
) -> dict[str, Any]:
    return read_curation_state(connection, _read_current_page(connection, page_id))


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
        snapshot_id = uuid.uuid4().hex
        now = timestamp()
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
                json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                actor_id,
                now,
            ),
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
        original = json.loads(str(page["source_content_json"]))
        if _source_image_count(original):
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
