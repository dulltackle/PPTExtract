from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
import uuid
from typing import Any, cast

from pptextract.config import Settings
from pptextract.db import transaction
from pptextract.jobs import timestamp

RULE_VERSION = "manual-exact-text-v1"


class RepeatedFooterNoiseError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def normalize_source_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def text_source_ref(page_version_id: str, source_kind: str, source_index: int) -> str:
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        (
            "https://pptextract.local/text-sources/"
            f"{page_version_id}/{source_kind}/{source_index}"
        ),
    ).hex


def _current_page(connection: sqlite3.Connection, page_id: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT pv.page_version_id, pv.page_id, pv.document_id, pv.version_id,
               pv.page_number, pv.source_content_json
        FROM page_versions AS pv
        JOIN documents AS d
          ON d.document_id = pv.document_id AND d.current_version_id = pv.version_id
        WHERE d.deleted_at IS NULL AND pv.page_id = ?
        """,
        (page_id,),
    ).fetchone()
    if row is None:
        raise RepeatedFooterNoiseError(404, "not_found", "未找到请求的资源。")
    return cast(sqlite3.Row, row)


def _body_sources(page: sqlite3.Row) -> list[dict[str, Any]]:
    content = json.loads(str(page["source_content_json"]))
    return [
        {
            "source_ref": text_source_ref(str(page["page_version_id"]), "body", index),
            "source_kind": "body",
            "source_index": index,
            "text": str(value),
        }
        for index, value in enumerate(content.get("body", []))
        if str(value).strip()
    ]


def _candidate_id(document_id: str, version_id: str, normalized_text: str) -> str:
    payload = json.dumps(
        {
            "document_id": document_id,
            "version_id": version_id,
            "normalized_text": normalized_text,
            "rule_version": RULE_VERSION,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def preview_candidate(
    connection: sqlite3.Connection, *, page_id: str, source_ref: str
) -> dict[str, Any]:
    page = _current_page(connection, page_id)
    source = next(
        (item for item in _body_sources(page) if item["source_ref"] == source_ref),
        None,
    )
    if source is None:
        raise RepeatedFooterNoiseError(
            404, "text_source_not_found", "未找到可确认的正文来源引用。"
        )
    normalized_text = normalize_source_text(str(source["text"]))
    rows = connection.execute(
        """
        SELECT pv.page_version_id, pv.page_id, pv.page_number, pv.source_content_json
        FROM page_versions AS pv
        JOIN documents AS d
          ON d.document_id = pv.document_id AND d.current_version_id = pv.version_id
        WHERE pv.document_id = ? AND pv.version_id = ? AND d.deleted_at IS NULL
        ORDER BY pv.page_number, pv.page_id
        """,
        (page["document_id"], page["version_id"]),
    ).fetchall()
    affected: list[dict[str, Any]] = []
    for candidate_page in rows:
        for item in _body_sources(candidate_page):
            if normalize_source_text(str(item["text"])) != normalized_text:
                continue
            affected.append(
                {
                    "page_id": str(candidate_page["page_id"]),
                    "page_version_id": str(candidate_page["page_version_id"]),
                    "page_number": int(candidate_page["page_number"]),
                    "source_ref": str(item["source_ref"]),
                    "source_kind": str(item["source_kind"]),
                    "source_index": int(item["source_index"]),
                    "source_text": str(item["text"]),
                    "standard_render": {
                        "url": f"/api/v1/pages/{candidate_page['page_id']}/render"
                    },
                }
            )
    if len({item["page_id"] for item in affected}) < 2:
        raise RepeatedFooterNoiseError(
            422,
            "text_not_repeated_across_pages",
            "此正文来源未在至少两页中重复，不能确认为重复页脚噪声。",
        )
    return {
        "candidate_id": _candidate_id(
            str(page["document_id"]), str(page["version_id"]), normalized_text
        ),
        "document_id": str(page["document_id"]),
        "version_id": str(page["version_id"]),
        "source_text": str(source["text"]),
        "normalized_text": normalized_text,
        "rule_version": RULE_VERSION,
        "affected_pages": affected,
    }


def _is_active(connection: sqlite3.Connection, confirmation_id: str) -> bool:
    return connection.execute(
        """
        SELECT 1
        FROM repeated_footer_noise_events
        WHERE confirmation_id = ? AND event_type = 'confirmed'
          AND NOT EXISTS (
              SELECT 1 FROM repeated_footer_noise_events AS revoked
              WHERE revoked.confirmation_id = ? AND revoked.event_type = 'revoked'
          )
        """,
        (confirmation_id, confirmation_id),
    ).fetchone() is not None


def _serialize_confirmation(
    connection: sqlite3.Connection, confirmation_id: str
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT confirmation_id, document_id, version_id, source_text,
               normalized_text, rule_version, note, actor_id, confirmed_at
        FROM repeated_footer_noise_confirmations WHERE confirmation_id = ?
        """,
        (confirmation_id,),
    ).fetchone()
    if row is None:
        raise RepeatedFooterNoiseError(404, "not_found", "未找到重复页脚噪声确认。")
    affected = connection.execute(
        """
        SELECT sources.page_id, sources.page_version_id, sources.page_number,
               sources.source_ref, sources.source_kind, sources.source_index,
               sources.source_text, pv.review_status
        FROM repeated_footer_noise_sources AS sources
        JOIN page_versions AS pv ON pv.page_version_id = sources.page_version_id
        WHERE confirmation_id = ? ORDER BY sources.page_number, source_index, source_ref
        """,
        (confirmation_id,),
    ).fetchall()
    return {
        "confirmation_id": str(row["confirmation_id"]),
        "document_id": str(row["document_id"]),
        "version_id": str(row["version_id"]),
        "source_text": str(row["source_text"]),
        "normalized_text": str(row["normalized_text"]),
        "rule_version": str(row["rule_version"]),
        "note": row["note"],
        "actor_id": str(row["actor_id"]),
        "confirmed_at": str(row["confirmed_at"]),
        "status": "active" if _is_active(connection, confirmation_id) else "revoked",
        "affected_pages": [
            {
                "page_id": str(item["page_id"]),
                "page_version_id": str(item["page_version_id"]),
                "page_number": int(item["page_number"]),
                "review_status": str(item["review_status"]),
                "source_ref": str(item["source_ref"]),
                "source_kind": str(item["source_kind"]),
                "source_index": int(item["source_index"]),
                "source_text": str(item["source_text"]),
            }
            for item in affected
        ],
    }


def confirm_candidate(
    settings: Settings,
    *,
    page_id: str,
    actor_id: str,
    candidate_id: str,
    source_ref: str,
    note: str | None,
) -> dict[str, Any]:
    normalized_note = None if note is None or not note.strip() else note.strip()
    with transaction(settings) as connection:
        candidate = preview_candidate(connection, page_id=page_id, source_ref=source_ref)
        if candidate["candidate_id"] != candidate_id:
            raise RepeatedFooterNoiseError(
                409,
                "footer_noise_candidate_stale",
                "受影响页已变化，请重新查看候选后再确认。",
            )
        active = connection.execute(
            """
            SELECT confirmations.confirmation_id
            FROM repeated_footer_noise_confirmations AS confirmations
            JOIN repeated_footer_noise_events AS confirmed
              ON confirmed.confirmation_id = confirmations.confirmation_id
             AND confirmed.event_type = 'confirmed'
            WHERE confirmations.version_id = ?
              AND confirmations.normalized_text = ?
              AND confirmations.rule_version = ?
              AND NOT EXISTS (
                  SELECT 1 FROM repeated_footer_noise_events AS revoked
                  WHERE revoked.confirmation_id = confirmations.confirmation_id
                    AND revoked.event_type = 'revoked'
              )
            """,
            (
                candidate["version_id"],
                candidate["normalized_text"],
                candidate["rule_version"],
            ),
        ).fetchone()
        if active is not None:
            raise RepeatedFooterNoiseError(
                409, "footer_noise_already_confirmed", "这组重复页脚噪声已经确认。"
            )
        confirmation_id = uuid.uuid4().hex
        now = timestamp()
        connection.execute(
            """
            INSERT INTO repeated_footer_noise_confirmations (
                confirmation_id, document_id, version_id, source_text,
                normalized_text, rule_version, note, actor_id, confirmed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                confirmation_id,
                candidate["document_id"],
                candidate["version_id"],
                candidate["source_text"],
                candidate["normalized_text"],
                candidate["rule_version"],
                normalized_note,
                actor_id,
                now,
            ),
        )
        connection.executemany(
            """
            INSERT INTO repeated_footer_noise_sources (
                confirmation_id, page_id, page_version_id, page_number,
                source_ref, source_kind, source_index, source_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    confirmation_id,
                    item["page_id"],
                    item["page_version_id"],
                    item["page_number"],
                    item["source_ref"],
                    item["source_kind"],
                    item["source_index"],
                    item["source_text"],
                )
                for item in candidate["affected_pages"]
            ],
        )
        connection.execute(
            """
            INSERT INTO repeated_footer_noise_events (
                event_id, confirmation_id, event_type, actor_id, note, occurred_at
            ) VALUES (?, ?, 'confirmed', ?, ?, ?)
            """,
            (uuid.uuid4().hex, confirmation_id, actor_id, normalized_note, now),
        )
        return _serialize_confirmation(connection, confirmation_id)


def revoke_confirmation(
    settings: Settings,
    *,
    confirmation_id: str,
    actor_id: str,
    note: str | None,
) -> dict[str, Any]:
    normalized_note = None if note is None or not note.strip() else note.strip()
    with transaction(settings) as connection:
        confirmation = _serialize_confirmation(connection, confirmation_id)
        frozen = [
            page for page in confirmation["affected_pages"]
            if page["review_status"] != "pending"
        ]
        if frozen:
            numbers = "、".join(str(page["page_number"]) for page in frozen)
            raise RepeatedFooterNoiseError(
                409, "footer_noise_pages_frozen",
                f"撤销影响整组来源；请先逐页重新打开第 {numbers} 页，再撤销排除。",
            )
        if not _is_active(connection, confirmation_id):
            raise RepeatedFooterNoiseError(
                409, "footer_noise_already_revoked", "这项重复页脚噪声确认已经撤销。"
            )
        connection.execute(
            """
            INSERT INTO repeated_footer_noise_events (
                event_id, confirmation_id, event_type, actor_id, note, occurred_at
            ) VALUES (?, ?, 'revoked', ?, ?, ?)
            """,
            (uuid.uuid4().hex, confirmation_id, actor_id, normalized_note, timestamp()),
        )
        return _serialize_confirmation(connection, confirmation_id)


def read_page_noise_state(
    connection: sqlite3.Connection, page: sqlite3.Row
) -> dict[str, Any]:
    sources = _body_sources(page)
    active_rows = connection.execute(
        """
        SELECT sources.source_ref, sources.source_kind, sources.source_index,
               sources.source_text, confirmations.confirmation_id,
               confirmations.rule_version, confirmations.actor_id,
               confirmations.confirmed_at
        FROM repeated_footer_noise_sources AS sources
        JOIN repeated_footer_noise_confirmations AS confirmations
          ON confirmations.confirmation_id = sources.confirmation_id
        WHERE sources.page_version_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM repeated_footer_noise_events AS revoked
              WHERE revoked.confirmation_id = confirmations.confirmation_id
                AND revoked.event_type = 'revoked'
          )
        ORDER BY sources.source_index, confirmations.confirmed_at
        """,
        (page["page_version_id"],),
    ).fetchall()
    by_source = {str(row["source_ref"]): row for row in active_rows}
    for source in sources:
        active = by_source.get(str(source["source_ref"]))
        source["active_confirmation_id"] = (
            None if active is None else str(active["confirmation_id"])
        )
    exclusions = [
        {
            "confirmation_id": str(row["confirmation_id"]),
            "source_ref": str(row["source_ref"]),
            "source_kind": str(row["source_kind"]),
            "source_index": int(row["source_index"]),
            "source_text": str(row["source_text"]),
            "rule_version": str(row["rule_version"]),
            "confirmed_by": str(row["actor_id"]),
            "confirmed_at": str(row["confirmed_at"]),
        }
        for row in active_rows
    ]
    history_rows = connection.execute(
        """
        SELECT sources.source_ref, sources.source_text,
               confirmations.confirmation_id, confirmations.rule_version,
               confirmations.note AS confirmation_note,
               confirmations.actor_id AS confirmed_by,
               confirmations.confirmed_at,
               revoked.actor_id AS revoked_by,
               revoked.occurred_at AS revoked_at,
               revoked.note AS revoke_note
        FROM repeated_footer_noise_sources AS sources
        JOIN repeated_footer_noise_confirmations AS confirmations
          ON confirmations.confirmation_id = sources.confirmation_id
        LEFT JOIN repeated_footer_noise_events AS revoked
          ON revoked.confirmation_id = confirmations.confirmation_id
         AND revoked.event_type = 'revoked'
        WHERE sources.page_version_id = ?
        ORDER BY confirmations.confirmed_at DESC, confirmations.confirmation_id DESC
        """,
        (page["page_version_id"],),
    ).fetchall()
    history = [
        {
            "confirmation_id": str(row["confirmation_id"]),
            "source_ref": str(row["source_ref"]),
            "source_text": str(row["source_text"]),
            "rule_version": str(row["rule_version"]),
            "confirmation_note": row["confirmation_note"],
            "affected_pages": _serialize_confirmation(
                connection, str(row["confirmation_id"])
            )["affected_pages"],
            "confirmed_by": str(row["confirmed_by"]),
            "confirmed_at": str(row["confirmed_at"]),
            "status": "active" if row["revoked_at"] is None else "revoked",
            "revoked_by": row["revoked_by"],
            "revoked_at": row["revoked_at"],
            "revoke_note": row["revoke_note"],
        }
        for row in history_rows
    ]
    return {"sources": sources, "exclusions": exclusions, "history": history}
