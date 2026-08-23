from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any

from pptextract.rendering import RenderingWarning


def warning_details(warning: RenderingWarning) -> dict[str, Any]:
    if warning.code == "missing_font":
        return {
            "requested_font": warning.font_family,
            "replacement_font": warning.replacement_font,
        }
    if warning.code == "animation_flattened":
        return {"timeline_count": warning.timeline_count}
    raise ValueError(f"未知渲染警告类型：{warning.code}")


def replace_active_warnings(
    connection: sqlite3.Connection,
    *,
    version_id: str,
    render_config_version: str,
    warnings: tuple[RenderingWarning, ...],
    page_numbers: tuple[int, ...],
    observed_at: str,
) -> None:
    """用一次完整审计替换版本的当前警告集合，同时保留旧配置审计。"""
    if page_numbers:
        placeholders = ",".join("?" for _ in page_numbers)
        connection.execute(
            f"""
            UPDATE rendering_warnings SET active = 0
            WHERE version_id = ? AND active = 1 AND page_number IN ({placeholders})
            """,
            (version_id, *page_numbers),
        )
    for warning in warnings:
        details_json = json.dumps(
            warning_details(warning), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        warning_id = hashlib.sha256(
            "\0".join(
                (
                    version_id,
                    str(warning.page_number),
                    render_config_version,
                    warning.code,
                    details_json,
                )
            ).encode("utf-8")
        ).hexdigest()
        connection.execute(
            """
            INSERT INTO rendering_warnings (
                warning_id, version_id, page_number, code, details_json,
                render_config_version, observed_at, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(warning_id) DO UPDATE SET
                observed_at = excluded.observed_at,
                active = 1
            """,
            (
                warning_id,
                version_id,
                warning.page_number,
                warning.code,
                details_json,
                render_config_version,
                observed_at,
            ),
        )


def summarize_rows(rows: list[sqlite3.Row]) -> dict[str, int]:
    total_pages = {
        (str(row["version_id"]), int(row["page_number"])) for row in rows
    }
    unconfirmed = [row for row in rows if row["confirmed_at"] is None]
    return {
        "total": len(rows),
        "pages": len(total_pages),
        "unconfirmed": len(unconfirmed),
        "unconfirmed_pages": len(
            {
                (str(row["version_id"]), int(row["page_number"]))
                for row in unconfirmed
            }
        ),
    }


def read_warning_rows(
    connection: sqlite3.Connection, *, version_id: str | None = None
) -> list[sqlite3.Row]:
    where = "warnings.active = 1"
    parameters: tuple[str, ...] = ()
    if version_id is not None:
        where += " AND warnings.version_id = ?"
        parameters = (version_id,)
    return list(
        connection.execute(
            f"""
            SELECT warnings.warning_id, warnings.version_id, warnings.page_number,
                   warnings.code, warnings.details_json,
                   warnings.render_config_version, warnings.observed_at,
                   confirmations.actor_id AS confirmed_by,
                   confirmations.confirmed_at
            FROM rendering_warnings AS warnings
            LEFT JOIN rendering_warning_confirmations AS confirmations
              ON confirmations.warning_id = warnings.warning_id
            WHERE {where}
            ORDER BY warnings.page_number, warnings.code, warnings.warning_id
            """,
            parameters,
        ).fetchall()
    )


def serialize_warning(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "warning_id": row["warning_id"],
        "page_number": row["page_number"],
        "code": row["code"],
        "details": json.loads(row["details_json"]),
        "render_config_version": row["render_config_version"],
        "observed_at": row["observed_at"],
        "status": "confirmed" if row["confirmed_at"] is not None else "unconfirmed",
        "confirmed_by": row["confirmed_by"],
        "confirmed_at": row["confirmed_at"],
    }


def confirm_warning(
    connection: sqlite3.Connection,
    *,
    warning_id: str,
    version_id: str,
    actor_id: str,
    confirmed_at: str,
) -> bool:
    warning = connection.execute(
        """
        SELECT warning_id, details_json, render_config_version
        FROM rendering_warnings
        WHERE warning_id = ? AND version_id = ? AND active = 1
        """,
        (warning_id, version_id),
    ).fetchone()
    if warning is None:
        return False
    connection.execute(
        """
        INSERT INTO rendering_warning_confirmations (
            confirmation_id, warning_id, actor_id, confirmed_at,
            warning_details_json, render_config_version
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(warning_id) DO NOTHING
        """,
        (
            uuid.uuid4().hex,
            warning_id,
            actor_id,
            confirmed_at,
            warning["details_json"],
            warning["render_config_version"],
        ),
    )
    return True
