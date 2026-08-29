from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from pptextract.config import Settings
from pptextract.db import connect, transaction
from pptextract.jobs import ClaimedJob, lease_expiration, timestamp
from pptextract.object_store import LocalObjectStore
from pptextract.rendering import render_configuration_version

ARCHIVE_MEDIA_TYPE = "application/zip"


class PublicationRequestError(Exception):
    def __init__(
        self, status_code: int, code: str, message: str, details: Any | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True, slots=True)
class PublicationArtifact:
    publication_seq: int
    candidate_id: str
    snapshot_id: str
    content_set_hash: str
    sha256: str
    media_type: str
    size_bytes: int
    chunk_count: int
    asset_count: int
    published_at: str
    path: Path


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _asset_extension(media_type: str) -> str:
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
    }.get(media_type, "bin")


def publication_preflight(connection: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    current_config = render_configuration_version(settings.render_image)
    stale = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM documents AS d
            JOIN document_versions AS v ON v.version_id = d.current_version_id
            WHERE d.deleted_at IS NULL AND v.status = 'ready'
              AND COALESCE(v.render_config_version, '') <> ?
            """,
            (current_config,),
        ).fetchone()[0]
    )
    warning = connection.execute(
        """
        SELECT COUNT(*) AS total,
               COUNT(DISTINCT w.version_id || ':' || w.page_number) AS pages,
               SUM(CASE WHEN c.confirmation_id IS NULL THEN 1 ELSE 0 END) AS warning_count,
               COUNT(DISTINCT CASE WHEN c.confirmation_id IS NULL
                   THEN w.version_id || ':' || w.page_number END) AS page_count
        FROM documents AS d
        JOIN document_versions AS v ON v.version_id = d.current_version_id
        JOIN rendering_warnings AS w ON w.version_id = v.version_id AND w.active = 1
        LEFT JOIN rendering_warning_confirmations AS c ON c.warning_id = w.warning_id
        WHERE d.deleted_at IS NULL AND v.status = 'ready'
        """
    ).fetchone()
    unconfirmed = int(warning["warning_count"] or 0)
    unconfirmed_pages = int(warning["page_count"] or 0)
    first_unconfirmed = connection.execute(
        """
        SELECT d.document_id, v.version_id, w.page_number, w.warning_id
        FROM documents AS d
        JOIN document_versions AS v ON v.version_id = d.current_version_id
        JOIN rendering_warnings AS w ON w.version_id = v.version_id AND w.active = 1
        LEFT JOIN rendering_warning_confirmations AS c ON c.warning_id = w.warning_id
        WHERE d.deleted_at IS NULL AND v.status = 'ready' AND c.confirmation_id IS NULL
        ORDER BY v.version_id, w.page_number, w.warning_id LIMIT 1
        """
    ).fetchone()
    href = None
    if first_unconfirmed is not None:
        href = (
            "/curation?filter=rendering-warnings"
            f"&document={first_unconfirmed['document_id']}"
            f"&version={first_unconfirmed['version_id']}"
            f"&page={first_unconfirmed['page_number']}"
            f"&warning={first_unconfirmed['warning_id']}"
        )
    return {
        "can_publish": not stale and not unconfirmed,
        "stale_render_versions": stale,
        "unconfirmed_warnings": unconfirmed,
        "unconfirmed_warning_pages": unconfirmed_pages,
        "summary": {
            "total": int(warning["total"]),
            "pages": int(warning["pages"]),
            "unconfirmed": unconfirmed,
            "unconfirmed_pages": unconfirmed_pages,
        },
        "href": href,
    }


def _table_part(table: dict[str, Any]) -> dict[str, Any]:
    grid: list[list[dict[str, Any]]] = []
    gfm_rows: list[list[str]] = []
    for row in table.get("grid", []):
        normalized: list[dict[str, Any]] = []
        display: list[str] = []
        for slot in row:
            if slot.get("kind") != "origin":
                continue
            cell = slot.get("cell") or {}
            text = str(cell.get("text", "")).strip()
            normalized.append(
                {
                    "text": text,
                    "row_span": int(cell.get("row_span", 1)),
                    "col_span": int(cell.get("col_span", 1)),
                }
            )
            display.append(text.replace("|", "\\|"))
        grid.append(normalized)
        gfm_rows.append(display)
    width = max((len(row) for row in gfm_rows), default=0)
    if not width:
        gfm = ""
    else:
        padded = [row + [""] * (width - len(row)) for row in gfm_rows]
        header_rows = max(0, int(table.get("header_rows", 0)))
        if header_rows == 0:
            padded.insert(0, [""] * width)
        lines = ["| " + " | ".join(row) + " |" for row in padded]
        lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
        gfm = "\n".join(lines)
    return {"kind": "table", "text": gfm, "data": {"grid": grid}}


def _repeated_footer_indexes(
    connection: sqlite3.Connection, page_version_id: str
) -> tuple[set[int], list[dict[str, Any]]]:
    rows = connection.execute(
        """
        SELECT sources.source_index, sources.source_ref, sources.source_text,
               confirmations.confirmation_id, confirmations.rule_version,
               confirmations.actor_id, confirmations.confirmed_at
        FROM repeated_footer_noise_sources AS sources
        JOIN repeated_footer_noise_confirmations AS confirmations
          ON confirmations.confirmation_id = sources.confirmation_id
        LEFT JOIN repeated_footer_noise_events AS revoked
          ON revoked.confirmation_id = confirmations.confirmation_id
         AND revoked.event_type = 'revoked'
        WHERE sources.page_version_id = ? AND sources.source_kind = 'body'
          AND revoked.event_id IS NULL
        ORDER BY sources.source_index, sources.source_ref
        """,
        (page_version_id,),
    ).fetchall()
    metadata = [
        {
            "confirmation_id": str(row["confirmation_id"]),
            "source_ref": str(row["source_ref"]),
            "source_text": str(row["source_text"]),
            "rule_version": str(row["rule_version"]),
            "confirmed_by": str(row["actor_id"]),
            "confirmed_at": str(row["confirmed_at"]),
        }
        for row in rows
    ]
    return {int(row["source_index"]) for row in rows}, metadata


def _visuals(
    connection: sqlite3.Connection, snapshot_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = connection.execute(
        """
        SELECT visual_ref, position, source_kind, summary, visual_type,
               asset_sha256, asset_media_type, asset_size_bytes,
               asset_width_px, asset_height_px
        FROM curation_snapshot_visuals
        WHERE snapshot_id = ? AND disposition = 'included' AND confirmed = 1
        ORDER BY position, visual_ref
        """,
        (snapshot_id,),
    ).fetchall()
    visuals: list[dict[str, Any]] = []
    assets_by_sha: dict[str, dict[str, Any]] = {}
    for row in rows:
        sha256 = str(row["asset_sha256"] or "")
        media_type = str(row["asset_media_type"] or "")
        summary = str(row["summary"] or "").strip()
        if not sha256 or not media_type or not summary:
            raise PublicationRequestError(
                409,
                "publication_input_invalid",
                "批准页包含不完整的视觉资产或标注，无法创建发布候选。",
            )
        byte_contract = (
            "anydoc_original" if row["source_kind"] == "source_image" else "standard_render_crop"
        )
        path = f"assets/{sha256}.{_asset_extension(media_type)}"
        asset = {
            "sha256": sha256,
            "path": path,
            "media_type": media_type,
            "size_bytes": int(row["asset_size_bytes"]),
            "byte_contract": byte_contract,
        }
        if row["asset_width_px"] is not None:
            asset["width_px"] = int(row["asset_width_px"])
        if row["asset_height_px"] is not None:
            asset["height_px"] = int(row["asset_height_px"])
        visual: dict[str, Any] = {
            "visual_ref": str(row["visual_ref"]),
            "summary": summary,
            "asset": asset,
        }
        if row["visual_type"]:
            visual["visual_type"] = str(row["visual_type"])
        visuals.append(visual)
        assets_by_sha[sha256] = asset
    return visuals, list(assets_by_sha.values())


def _chunk_from_row(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    content = json.loads(str(row["source_content_json"]))
    excluded_indexes, noise = _repeated_footer_indexes(connection, str(row["page_version_id"]))
    visuals, assets = _visuals(connection, str(row["snapshot_id"]))
    overview = str(row["overview"] or "").strip()
    annotation: dict[str, Any] = {"visuals": visuals}
    if overview:
        annotation["overview"] = overview
    annotation_text = "\n\n".join(
        fragment for fragment in [overview, *(item["summary"] for item in visuals)] if fragment
    )
    parts: list[dict[str, Any]] = [{"kind": "document_title", "text": str(row["source_filename"])}]
    titles = [str(value).strip() for value in content.get("titles", []) if str(value).strip()]
    page_title = titles[0] if titles else f"第 {row['page_number']} 页"
    if titles:
        parts.append({"kind": "page_title", "text": "\n".join(titles)})
    if annotation_text:
        parts.append({"kind": "annotation", "text": annotation_text, "data": annotation})
    for index, value in enumerate(content.get("body", [])):
        text = str(value).strip()
        if text and index not in excluded_indexes:
            parts.append({"kind": "body", "text": text})
    for table in content.get("tables", []):
        part = _table_part(table)
        if part["text"]:
            parts.append(part)
    for image in content.get("images", []):
        text = str(image.get("alt_text", "")).strip()
        if text:
            parts.append({"kind": "image_alt", "text": text})
    for value in content.get("speaker_notes", []):
        text = str(value).strip()
        if text:
            parts.append({"kind": "speaker_notes", "text": text})
    text = "\n\n".join(str(part["text"]) for part in parts if str(part["text"]).strip())
    if not text.strip():
        raise PublicationRequestError(
            409, "publication_input_invalid", "批准页无法生成非空 Chunk 正文。"
        )
    content_hash = _digest({"text": text, "parts": parts, "annotation": annotation})
    chunk = {
        "schema_version": 1,
        "chunk_id": str(row["chunk_id"]),
        "content_hash": content_hash,
        "text": text,
        "parts": parts,
        "annotation": annotation,
        "metadata": {
            "document_id": str(row["document_id"]),
            "document_version_id": str(row["version_id"]),
            "page_id": str(row["page_id"]),
            "page_version_id": str(row["page_version_id"]),
            "page_number": int(row["page_number"]),
            "page_fingerprint": str(row["fingerprint_sha256"]),
            "document_title": str(row["source_filename"]),
            "page_title": page_title,
            "approved_by": str(row["reviewed_by"]),
            "approved_at": str(row["reviewed_at"]),
            "approval_source_version_id": str(row["review_source_version_id"]),
            "snapshot_id": str(row["snapshot_id"]),
            "text_characters": len(text),
            "excluded_repeated_footer_noise": noise,
        },
    }
    return chunk, assets


def _business_state_token(connection: sqlite3.Connection) -> str:
    state: dict[str, Any] = {}
    state["documents"] = [
        list(row)
        for row in connection.execute(
            """
            SELECT document_id, current_version_id, deleted_at
            FROM documents ORDER BY document_id
            """
        )
    ]
    state["pages"] = [
        list(row)
        for row in connection.execute(
            """
            SELECT pv.page_version_id, pv.version_id, pv.review_status,
                   pv.current_snapshot_id, pv.reviewed_by, pv.reviewed_at,
                   p.deleted_at
            FROM page_versions AS pv
            JOIN pages AS p ON p.page_id = pv.page_id
            JOIN documents AS d ON d.document_id = pv.document_id
            WHERE pv.version_id = d.current_version_id
            ORDER BY pv.page_version_id
            """
        )
    ]
    state["hidden"] = [
        list(row)
        for row in connection.execute(
            """
            SELECT r.version_id, r.page_number, r.hidden, r.enabled
            FROM ingestion_page_results AS r
            JOIN documents AS d ON d.current_version_id = r.version_id
            ORDER BY r.version_id, r.page_number
            """
        )
    ]
    state["warnings"] = [
        list(row)
        for row in connection.execute(
            """
            SELECT w.warning_id, w.active, c.confirmation_id
            FROM rendering_warnings AS w
            LEFT JOIN rendering_warning_confirmations AS c ON c.warning_id = w.warning_id
            ORDER BY w.warning_id
            """
        )
    ]
    state["repeated_footer_noise"] = [
        list(row)
        for row in connection.execute(
            """
            SELECT confirmations.confirmation_id, confirmations.version_id,
                   confirmations.normalized_text, confirmations.rule_version,
                   events.event_id, events.event_type, events.occurred_at
            FROM repeated_footer_noise_confirmations AS confirmations
            JOIN documents AS documents
              ON documents.current_version_id = confirmations.version_id
            LEFT JOIN repeated_footer_noise_events AS events
              ON events.confirmation_id = confirmations.confirmation_id
            WHERE documents.deleted_at IS NULL
            ORDER BY confirmations.confirmation_id, events.event_type, events.event_id
            """
        )
    ]
    return _digest(state)


def _collect_scope(connection: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    preflight = publication_preflight(connection, settings)
    if not preflight["can_publish"]:
        raise PublicationRequestError(
            409,
            "publication_preflight_blocked",
            "发布前置校验尚未通过，不能创建或确认发布候选。",
            preflight,
        )
    rows = connection.execute(
        """
        SELECT d.document_id, v.version_id, v.source_filename,
               p.page_id, p.chunk_id, pv.page_version_id, pv.page_number,
               pv.fingerprint_sha256, pv.current_snapshot_id AS snapshot_id,
               pv.reviewed_by, pv.reviewed_at, pv.review_source_version_id,
               s.overview, s.source_content_json
        FROM documents AS d
        JOIN document_versions AS v ON v.version_id = d.current_version_id
        JOIN page_versions AS pv ON pv.version_id = v.version_id
        JOIN pages AS p ON p.page_id = pv.page_id
        JOIN ingestion_page_results AS r
          ON r.version_id = v.version_id AND r.page_number = pv.page_number
        JOIN curation_snapshots AS s ON s.snapshot_id = pv.current_snapshot_id
        WHERE d.deleted_at IS NULL AND v.status = 'ready'
          AND p.deleted_at IS NULL AND pv.review_status = 'approved'
          AND r.hidden = 0 AND r.enabled = 1
        ORDER BY d.document_id, pv.page_number, p.page_id
        """
    ).fetchall()
    current_hashes = {
        str(row["chunk_id"]): str(row["content_hash"])
        for row in connection.execute(
            """
            SELECT chunks.chunk_id, chunks.content_hash
            FROM current_publication AS current
            JOIN publication_artifacts AS artifacts
              ON artifacts.publication_seq = current.publication_seq
            JOIN publication_frozen_chunks AS chunks
              ON chunks.candidate_id = artifacts.candidate_id
            WHERE current.singleton_id = 1
            """
        ).fetchall()
    }
    chunks: list[dict[str, Any]] = []
    assets_by_sha: dict[str, dict[str, Any]] = {}
    documents: dict[str, dict[str, Any]] = {}
    for row in rows:
        chunk, assets = _chunk_from_row(connection, row)
        old_hash = current_hashes.get(str(row["chunk_id"]))
        change = (
            "added"
            if old_hash is None
            else "unchanged"
            if old_hash == chunk["content_hash"]
            else "updated"
        )
        internal = {
            "page_id": str(row["page_id"]),
            "page_version_id": str(row["page_version_id"]),
            "snapshot_id": str(row["snapshot_id"]),
            "chunk": chunk,
        }
        chunks.append(internal)
        for asset in assets:
            assets_by_sha[asset["sha256"]] = asset
        document = documents.setdefault(
            str(row["document_id"]),
            {
                "document_id": str(row["document_id"]),
                "version_id": str(row["version_id"]),
                "title": str(row["source_filename"]),
                "pages": [],
            },
        )
        document["pages"].append(
            {
                "page_number": int(row["page_number"]),
                "title": chunk["metadata"]["page_title"],
                "page_id": str(row["page_id"]),
                "chunk_id": str(row["chunk_id"]),
                "snapshot_id": str(row["snapshot_id"]),
                "reviewed_by": str(row["reviewed_by"]),
                "reviewed_at": str(row["reviewed_at"]),
                "change": change,
            }
        )
    new_hashes = {item["chunk"]["chunk_id"]: item["chunk"]["content_hash"] for item in chunks}
    diff = {
        "added": sum(key not in current_hashes for key in new_hashes),
        "updated": sum(
            key in current_hashes and value != current_hashes[key]
            for key, value in new_hashes.items()
        ),
        "removed": sum(key not in new_hashes for key in current_hashes),
        "unchanged": sum(
            key in current_hashes and value == current_hashes[key]
            for key, value in new_hashes.items()
        ),
    }
    counts = connection.execute(
        """
        SELECT
          SUM(CASE WHEN pv.review_status = 'pending' AND p.deleted_at IS NULL
              THEN 1 ELSE 0 END) AS pending,
          SUM(CASE WHEN pv.review_status = 'excluded' AND p.deleted_at IS NULL
              THEN 1 ELSE 0 END) AS excluded
        FROM documents AS d
        JOIN page_versions AS pv ON pv.version_id = d.current_version_id
        JOIN pages AS p ON p.page_id = pv.page_id
        WHERE d.deleted_at IS NULL
        """
    ).fetchone()
    hidden = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM documents AS d
            JOIN ingestion_page_results AS r ON r.version_id = d.current_version_id
            WHERE d.deleted_at IS NULL AND r.hidden = 1 AND r.enabled = 0
            """
        ).fetchone()[0]
    )
    deleted = int(
        connection.execute(
            "SELECT COUNT(*) FROM documents WHERE deleted_at IS NOT NULL"
        ).fetchone()[0]
    )
    excluded = {
        "pending_pages": int(counts["pending"] or 0),
        "excluded_pages": int(counts["excluded"] or 0),
        "disabled_hidden_pages": hidden,
        "soft_deleted_documents": deleted,
    }
    content_set_hash = _digest(
        {
            "chunks": sorted(new_hashes.items()),
            "assets": sorted(assets_by_sha),
        }
    )
    return {
        "business_state_token": _business_state_token(connection),
        "content_set_hash": content_set_hash,
        "diff": diff,
        "excluded": excluded,
        "documents": list(documents.values()),
        "chunks": chunks,
        "assets": list(assets_by_sha.values()),
    }


def _candidate_content(row: sqlite3.Row) -> dict[str, Any]:
    scope = json.loads(str(row["scope_json"]))
    return {
        "candidate_id": str(row["candidate_id"]),
        "status": str(row["status"]),
        "business_state_token": str(row["business_state_token"]),
        "content_set_hash": str(row["content_set_hash"]),
        "created_by": str(row["created_by"]),
        "created_at": str(row["created_at"]),
        "confirmed_by": row["confirmed_by"],
        "confirmed_at": row["confirmed_at"],
        "publication_seq": row["publication_seq"],
        "frozen_input_hash": row["frozen_input_hash"],
        "diff": scope["diff"],
        "excluded": scope["excluded"],
        "documents": scope["documents"],
        "chunk_count": len(scope["chunks"]),
        "asset_count": len(scope["assets"]),
    }


def create_candidate(settings: Settings, *, actor_id: str) -> dict[str, Any]:
    with transaction(settings) as connection:
        scope = _collect_scope(connection, settings)
        candidate_id = uuid.uuid4().hex
        now = timestamp()
        connection.execute(
            """
            INSERT INTO publication_candidates (
                candidate_id, business_state_token, content_set_hash, scope_json,
                status, created_by, created_at
            ) VALUES (?, ?, ?, ?, 'ready', ?, ?)
            """,
            (
                candidate_id,
                scope["business_state_token"],
                scope["content_set_hash"],
                _canonical(scope),
                actor_id,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM publication_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
    assert row is not None
    return _candidate_content(row)


def read_candidate(settings: Settings, candidate_id: str) -> dict[str, Any] | None:
    with connect(settings) as connection:
        row = connection.execute(
            "SELECT * FROM publication_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
    return None if row is None else _candidate_content(row)


def _reserve_sequence(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT next_value FROM publication_sequences WHERE singleton_id = 1"
    ).fetchone()
    assert row is not None
    value = int(row["next_value"])
    connection.execute(
        "UPDATE publication_sequences SET next_value = ? WHERE singleton_id = 1",
        (value + 1,),
    )
    return value


def confirm_candidate(
    settings: Settings, *, candidate_id: str, actor_id: str
) -> tuple[int, dict[str, Any]]:
    with transaction(settings) as connection:
        row = connection.execute(
            "SELECT * FROM publication_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise PublicationRequestError(404, "candidate_not_found", "未找到发布候选。")
        if row["status"] != "ready":
            raise PublicationRequestError(409, "candidate_not_ready", "发布候选已不可确认。")
        current_scope = _collect_scope(connection, settings)
        if (
            current_scope["business_state_token"] != row["business_state_token"]
            or current_scope["content_set_hash"] != row["content_set_hash"]
        ):
            connection.execute(
                "UPDATE publication_candidates SET status = 'stale' WHERE candidate_id = ?",
                (candidate_id,),
            )
            stale = True
        else:
            stale = False
        if stale:
            # 事务提交状态后再由事务外抛错。
            pass
        else:
            active = connection.execute(
                "SELECT job_id FROM jobs WHERE kind = 'publication.build' "
                "AND status IN ('queued', 'running') LIMIT 1"
            ).fetchone()
            if active is not None:
                raise PublicationRequestError(
                    409, "publication_busy", "已有发布任务正在构建，请等待完成后再确认。"
                )
            current = connection.execute(
                """
                SELECT artifacts.content_set_hash
                FROM current_publication AS current
                JOIN publication_artifacts AS artifacts
                  ON artifacts.publication_seq = current.publication_seq
                WHERE current.singleton_id = 1
                """
            ).fetchone()
            if current is not None and current["content_set_hash"] == row["content_set_hash"]:
                connection.execute(
                    "UPDATE publication_candidates SET status = 'no_change', "
                    "confirmed_by = ?, confirmed_at = ? WHERE candidate_id = ?",
                    (actor_id, timestamp(), candidate_id),
                )
                return 200, {
                    "candidate_id": candidate_id,
                    "status": "no_change",
                    "publication_seq": None,
                    "job_id": None,
                }
            scope = json.loads(str(row["scope_json"]))
            frozen_input_hash = _digest({"chunks": scope["chunks"], "assets": scope["assets"]})
            for position, item in enumerate(scope["chunks"]):
                chunk = item["chunk"]
                connection.execute(
                    """
                    INSERT INTO publication_frozen_chunks (
                        candidate_id, position, chunk_id, page_id, page_version_id,
                        snapshot_id, content_hash, chunk_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        position,
                        chunk["chunk_id"],
                        item["page_id"],
                        item["page_version_id"],
                        item["snapshot_id"],
                        chunk["content_hash"],
                        _canonical(chunk),
                    ),
                )
            for asset in scope["assets"]:
                connection.execute(
                    """
                    INSERT INTO publication_frozen_assets (
                        candidate_id, asset_sha256, path, media_type,
                        size_bytes, byte_contract
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        asset["sha256"],
                        asset["path"],
                        asset["media_type"],
                        asset["size_bytes"],
                        asset["byte_contract"],
                    ),
                )
            publication_seq = _reserve_sequence(connection)
            now = timestamp()
            job_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, kind, payload_json, status, actor_id, idempotency_key,
                    checkpoint_json, created_at, updated_at
                ) VALUES (?, 'publication.build', ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    _canonical({"candidate_id": candidate_id, "publication_seq": publication_seq}),
                    actor_id,
                    f"publication:{candidate_id}",
                    _canonical(
                        {
                            "phase": "frozen_input",
                            "completed_pages": 0,
                            "total_pages": len(scope["chunks"]),
                        }
                    ),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE publication_candidates
                SET status = 'confirmed', confirmed_by = ?, confirmed_at = ?,
                    publication_seq = ?, frozen_input_hash = ?
                WHERE candidate_id = ?
                """,
                (actor_id, now, publication_seq, frozen_input_hash, candidate_id),
            )
            return 202, {
                "candidate_id": candidate_id,
                "status": "queued",
                "publication_seq": publication_seq,
                "job_id": job_id,
                "frozen_input_hash": frozen_input_hash,
            }
    if stale:
        raise PublicationRequestError(
            409,
            "publication_candidate_stale",
            "业务状态已变化，此发布候选已失效，请重新创建。",
        )
    raise AssertionError("unreachable")


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    return info


def _archive_bytes(
    settings: Settings,
    *,
    job: ClaimedJob,
    candidate: sqlite3.Row,
    chunks: list[sqlite3.Row],
    assets: list[sqlite3.Row],
) -> bytes:
    chunks_bytes = (
        "\n".join(str(row["chunk_json"]) for row in chunks) + ("\n" if chunks else "")
    ).encode("utf-8")
    asset_manifest = [
        {
            "path": str(row["path"]),
            "sha256": str(row["asset_sha256"]),
            "size_bytes": int(row["size_bytes"]),
            "media_type": str(row["media_type"]),
            "byte_contract": str(row["byte_contract"]),
        }
        for row in assets
    ]
    manifest = {
        "schema_version": 1,
        "snapshot_id": str(candidate["candidate_id"]),
        "publication_seq": int(candidate["publication_seq"]),
        "captured_at": str(candidate["confirmed_at"]),
        "content_set_hash": str(candidate["content_set_hash"]),
        "chunk_count": len(chunks),
        "asset_count": len(assets),
        "chunks": {
            "path": "chunks.jsonl",
            "sha256": hashlib.sha256(chunks_bytes).hexdigest(),
            "size_bytes": len(chunks_bytes),
        },
        "assets": asset_manifest,
    }
    output = BytesIO()
    store = LocalObjectStore(settings.object_store_path)
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(_zip_info("manifest.json"), _canonical(manifest).encode("utf-8"))
        archive.writestr(_zip_info("chunks.jsonl"), chunks_bytes)
        for position, row in enumerate(assets, start=1):
            _checkpoint(settings, job, "build", len(chunks), position - 1, len(assets))
            sha256 = str(row["asset_sha256"])
            path = store.path_for(sha256)
            if not store.verify(sha256) or path.stat().st_size != int(row["size_bytes"]):
                raise ValueError(f"冻结视觉资产校验失败：{sha256}")
            archive.writestr(_zip_info(str(row["path"])), path.read_bytes())
        _checkpoint(settings, job, "build", len(chunks), len(assets), len(assets))
    return output.getvalue()


def validate_publication_archive(payload: bytes) -> None:
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ValueError("ZIP 含重复路径")
        for info in infos:
            archive_path = PurePosixPath(info.filename)
            if (
                archive_path.is_absolute()
                or ".." in archive_path.parts
                or (info.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("ZIP 含不安全路径或符号链接")
        if not {"manifest.json", "chunks.jsonl"} <= set(names):
            raise ValueError("ZIP 缺少 manifest 或 Chunk JSONL")
        manifest = json.loads(archive.read("manifest.json"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise ValueError("manifest Schema 版本不受支持")
        if (
            not isinstance(manifest.get("publication_seq"), int)
            or manifest["publication_seq"] <= 0
            or not str(manifest.get("snapshot_id", ""))
            or len(str(manifest.get("content_set_hash", ""))) != 64
        ):
            raise ValueError("manifest 身份字段不完整")
        chunks_bytes = archive.read("chunks.jsonl")
        if hashlib.sha256(chunks_bytes).hexdigest() != manifest["chunks"]["sha256"]:
            raise ValueError("Chunk JSONL 哈希不一致")
        if len(chunks_bytes) != int(manifest["chunks"]["size_bytes"]):
            raise ValueError("Chunk JSONL 字节数不一致")
        chunks = [json.loads(line) for line in chunks_bytes.splitlines() if line]
        for chunk in chunks:
            if chunk.get("schema_version") != 1:
                raise ValueError("Chunk Schema 版本不受支持")
            expected_content_hash = _digest(
                {
                    "text": chunk.get("text"),
                    "parts": chunk.get("parts"),
                    "annotation": chunk.get("annotation"),
                }
            )
            if chunk.get("content_hash") != expected_content_hash:
                raise ValueError("Chunk 内容哈希不一致")
            metadata = chunk.get("metadata")
            if not isinstance(metadata, dict) or any(
                not str(metadata.get(key, ""))
                for key in (
                    "document_id",
                    "document_version_id",
                    "page_id",
                    "page_version_id",
                    "snapshot_id",
                )
            ):
                raise ValueError("Chunk 身份字段不完整")
        chunk_ids = [str(chunk["chunk_id"]) for chunk in chunks]
        if len(chunk_ids) != len(set(chunk_ids)) or len(chunks) != int(manifest["chunk_count"]):
            raise ValueError("Chunk ID 重复或计数不一致")
        references = [
            visual["asset"]
            for chunk in chunks
            for visual in chunk.get("annotation", {}).get("visuals", [])
        ]
        referenced = {str(asset["path"]) for asset in references}
        declared = {str(asset["path"]): asset for asset in manifest["assets"]}
        if referenced != set(declared) or int(manifest["asset_count"]) != len(declared):
            raise ValueError("视觉资产引用不完整")
        asset_fields = ("path", "sha256", "size_bytes", "media_type", "byte_contract")
        for reference in references:
            declaration = declared[str(reference["path"])]
            if any(reference.get(field) != declaration.get(field) for field in asset_fields):
                raise ValueError("视觉资产引用描述与 manifest 不一致")
        for path, asset in declared.items():
            data = archive.read(path)
            if hashlib.sha256(data).hexdigest() != asset["sha256"] or len(data) != int(
                asset["size_bytes"]
            ):
                raise ValueError("视觉资产哈希或字节数不一致")
        if set(names) != {"manifest.json", "chunks.jsonl", *declared}:
            raise ValueError("ZIP 含未声明文件")


def _checkpoint(
    settings: Settings,
    job: ClaimedJob,
    phase: str,
    total: int,
    completed_assets: int = 0,
    total_assets: int = 0,
) -> None:
    now = timestamp()
    with transaction(settings) as connection:
        updated = connection.execute(
            """
            UPDATE jobs SET checkpoint_json = ?, lease_expires_at = ?, updated_at = ?
            WHERE job_id = ? AND status = 'running'
              AND lease_owner = ? AND lease_token = ? AND lease_expires_at > ?
            """,
            (
                _canonical(
                    {
                        "phase": phase,
                        "completed_pages": 0,
                        "total_pages": total,
                        "completed_assets": completed_assets,
                        "total_assets": total_assets,
                    }
                ),
                lease_expiration(),
                now,
                job.job_id,
                settings.worker_id,
                job.lease_token,
                now,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("worker 无法保存未持有发布任务的进度")


def process_publication_job(settings: Settings, job: ClaimedJob) -> None:
    candidate_id = str(job.payload["candidate_id"])
    with connect(settings) as connection:
        candidate = connection.execute(
            "SELECT * FROM publication_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        chunks = connection.execute(
            "SELECT * FROM publication_frozen_chunks WHERE candidate_id = ? ORDER BY position",
            (candidate_id,),
        ).fetchall()
        assets = connection.execute(
            "SELECT * FROM publication_frozen_assets WHERE candidate_id = ? ORDER BY path",
            (candidate_id,),
        ).fetchall()
    if candidate is None or int(candidate["publication_seq"]) != int(
        job.payload["publication_seq"]
    ):
        raise ValueError("发布任务冻结身份不一致")
    existing = read_artifact(settings, int(candidate["publication_seq"]))
    if existing is not None:
        with transaction(settings) as connection:
            now = timestamp()
            updated = connection.execute(
                """
                UPDATE jobs SET status = 'succeeded', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                  AND lease_token = ? AND lease_expires_at > ?
                """,
                (now, job.job_id, settings.worker_id, job.lease_token, now),
            )
            if updated.rowcount != 1:
                raise RuntimeError("worker 无法完成未持有的发布任务")
            connection.execute(
                "UPDATE publication_candidates SET status = 'succeeded' "
                "WHERE candidate_id = ? AND status <> 'succeeded'",
                (candidate_id,),
            )
        return
    _checkpoint(settings, job, "build", len(chunks))
    payload = _archive_bytes(
        settings, job=job, candidate=candidate, chunks=chunks, assets=assets
    )
    _checkpoint(settings, job, "validate", len(chunks))
    validate_publication_archive(payload)
    _checkpoint(settings, job, "store", len(chunks))
    stored = LocalObjectStore(settings.object_store_path).put(payload)
    if not LocalObjectStore(settings.object_store_path).verify(stored.sha256):
        raise ValueError("发布 ZIP 写入后校验失败")
    _checkpoint(settings, job, "switch_pointer", len(chunks))
    published_at = timestamp()
    with transaction(settings) as connection:
        connection.execute(
            """
            INSERT INTO stored_objects (sha256, size_bytes, media_type, verified_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET verified_at = excluded.verified_at
            """,
            (stored.sha256, stored.size_bytes, ARCHIVE_MEDIA_TYPE, published_at),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO publication_artifacts (
                publication_seq, candidate_id, snapshot_id, content_set_hash,
                artifact_sha256, media_type, size_bytes, chunk_count,
                asset_count, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(candidate["publication_seq"]),
                candidate_id,
                candidate_id,
                candidate["content_set_hash"],
                stored.sha256,
                ARCHIVE_MEDIA_TYPE,
                stored.size_bytes,
                len(chunks),
                len(assets),
                published_at,
            ),
        )
        current = connection.execute(
            "SELECT publication_seq FROM current_publication WHERE singleton_id = 1"
        ).fetchone()
        if current is None or int(current["publication_seq"]) < int(candidate["publication_seq"]):
            connection.execute(
                """
                INSERT INTO current_publication (singleton_id, publication_seq)
                VALUES (1, ?)
                ON CONFLICT(singleton_id) DO UPDATE SET publication_seq = excluded.publication_seq
                """,
                (int(candidate["publication_seq"]),),
            )
        connection.execute(
            "UPDATE publication_candidates SET status = 'succeeded' WHERE candidate_id = ?",
            (candidate_id,),
        )
        updated = connection.execute(
            """
            UPDATE jobs SET status = 'succeeded', checkpoint_json = ?, error_json = NULL,
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                next_attempt_at = NULL, updated_at = ?
            WHERE job_id = ? AND status = 'running' AND lease_owner = ? AND lease_token = ?
            """,
            (
                _canonical(
                    {
                        "phase": "succeeded",
                        "completed_pages": len(chunks),
                        "total_pages": len(chunks),
                    }
                ),
                published_at,
                job.job_id,
                settings.worker_id,
                job.lease_token,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("worker 无法原子完成发布任务")


def fail_publication_job(settings: Settings, job: ClaimedJob, error: Exception) -> None:
    now = timestamp()
    with transaction(settings) as connection:
        row = connection.execute(
            "SELECT checkpoint_json FROM jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()
        checkpoint = (
            json.loads(str(row["checkpoint_json"])) if row and row["checkpoint_json"] else {}
        )
        updated = connection.execute(
            """
            UPDATE jobs SET status = 'failed', error_json = ?, lease_owner = NULL,
                lease_token = NULL, lease_expires_at = NULL, next_attempt_at = NULL,
                updated_at = ?
            WHERE job_id = ? AND status = 'running' AND lease_owner = ?
              AND lease_token = ? AND lease_expires_at > ?
            """,
            (
                _canonical(
                    {
                        "attempt": job.attempts,
                        "code": "publication_build_failed",
                        "message": str(error),
                        "phase": checkpoint.get("phase", "unknown"),
                        "retryable": True,
                    }
                ),
                now,
                job.job_id,
                settings.worker_id,
                job.lease_token,
                now,
            ),
        )
        if updated.rowcount != 1:
            return
        connection.execute(
            "UPDATE publication_candidates SET status = 'failed' "
            "WHERE candidate_id = ? AND status = 'confirmed'",
            (str(job.payload["candidate_id"]),),
        )


def retry_publication_job(
    settings: Settings, *, failed_job_id: str, actor_id: str
) -> dict[str, Any]:
    with transaction(settings) as connection:
        failed = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ? AND kind = 'publication.build'",
            (failed_job_id,),
        ).fetchone()
        if failed is None:
            raise PublicationRequestError(404, "publication_task_not_found", "未找到发布任务。")
        if failed["status"] != "failed":
            raise PublicationRequestError(
                409, "publication_task_not_failed", "只有失败任务可以重试。"
            )
        payload = json.loads(str(failed["payload_json"]))
        candidate = connection.execute(
            "SELECT * FROM publication_candidates WHERE candidate_id = ?",
            (payload["candidate_id"],),
        ).fetchone()
        assert candidate is not None
        artifact = connection.execute(
            "SELECT 1 FROM publication_artifacts WHERE candidate_id = ?",
            (payload["candidate_id"],),
        ).fetchone()
        if candidate["status"] == "succeeded" or artifact is not None:
            raise PublicationRequestError(
                409,
                "publication_already_succeeded",
                "该冻结候选已有不可变产物，不能再次重试旧失败任务。",
            )
        active = connection.execute(
            "SELECT 1 FROM jobs WHERE kind = 'publication.build' "
            "AND status IN ('queued', 'running')"
        ).fetchone()
        if active is not None:
            raise PublicationRequestError(409, "publication_busy", "已有发布任务正在构建。")
        job_id = uuid.uuid4().hex
        now = timestamp()
        total = int(
            connection.execute(
                "SELECT COUNT(*) FROM publication_frozen_chunks WHERE candidate_id = ?",
                (payload["candidate_id"],),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, kind, payload_json, status, actor_id, idempotency_key,
                checkpoint_json, created_at, updated_at
            ) VALUES (?, 'publication.build', ?, 'queued', ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                _canonical(payload),
                actor_id,
                f"publication-retry:{job_id}",
                _canonical({"phase": "frozen_input", "completed_pages": 0, "total_pages": total}),
                now,
                now,
            ),
        )
        connection.execute(
            "UPDATE publication_candidates SET status = 'confirmed' WHERE candidate_id = ?",
            (payload["candidate_id"],),
        )
    return {
        "candidate_id": str(candidate["candidate_id"]),
        "status": "queued",
        "publication_seq": int(candidate["publication_seq"]),
        "job_id": job_id,
        "frozen_input_hash": str(candidate["frozen_input_hash"]),
    }


def _artifact_from_row(settings: Settings, row: sqlite3.Row) -> PublicationArtifact:
    sha256 = str(row["artifact_sha256"])
    return PublicationArtifact(
        publication_seq=int(row["publication_seq"]),
        candidate_id=str(row["candidate_id"]),
        snapshot_id=str(row["snapshot_id"]),
        content_set_hash=str(row["content_set_hash"]),
        sha256=sha256,
        media_type=str(row["media_type"]),
        size_bytes=int(row["size_bytes"]),
        chunk_count=int(row["chunk_count"]),
        asset_count=int(row["asset_count"]),
        published_at=str(row["published_at"]),
        path=LocalObjectStore(settings.object_store_path).path_for(sha256),
    )


def read_artifact(settings: Settings, publication_seq: int) -> PublicationArtifact | None:
    with connect(settings) as connection:
        row = connection.execute(
            "SELECT * FROM publication_artifacts WHERE publication_seq = ?",
            (publication_seq,),
        ).fetchone()
    return None if row is None else _artifact_from_row(settings, row)


def read_current_artifact(settings: Settings) -> PublicationArtifact | None:
    with connect(settings) as connection:
        row = connection.execute(
            """
            SELECT artifacts.* FROM current_publication AS current
            JOIN publication_artifacts AS artifacts
              ON artifacts.publication_seq = current.publication_seq
            WHERE current.singleton_id = 1
            """
        ).fetchone()
    return None if row is None else _artifact_from_row(settings, row)


def artifact_content(artifact: PublicationArtifact) -> dict[str, Any]:
    return {
        "publication_seq": artifact.publication_seq,
        "candidate_id": artifact.candidate_id,
        "snapshot_id": artifact.snapshot_id,
        "published_at": artifact.published_at,
        "chunk_count": artifact.chunk_count,
        "asset_count": artifact.asset_count,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "media_type": artifact.media_type,
        "download_url": f"/api/v1/publications/{artifact.publication_seq}/artifact",
    }


def iter_file(path: Path, *, start: int = 0, end: int | None = None) -> Iterator[bytes]:
    remaining = None if end is None else end - start + 1
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining is None or remaining > 0:
            block = handle.read(1024 * 1024 if remaining is None else min(1024 * 1024, remaining))
            if not block:
                break
            yield block
            if remaining is not None:
                remaining -= len(block)


def read_publication_workspace(settings: Settings) -> dict[str, Any]:
    current = read_current_artifact(settings)
    with connect(settings) as connection:
        candidate = connection.execute(
            "SELECT * FROM publication_candidates "
            "ORDER BY created_at DESC, candidate_id DESC LIMIT 1"
        ).fetchone()
        task = None
        if candidate is not None:
            task = connection.execute(
                """
                SELECT job_id, status, checkpoint_json, error_json, attempts, updated_at
                FROM jobs WHERE kind = 'publication.build'
                  AND json_extract(payload_json, '$.candidate_id') = ?
                ORDER BY created_at DESC, job_id DESC LIMIT 1
                """,
                (candidate["candidate_id"],),
            ).fetchone()
        preflight = publication_preflight(connection, settings)
    task_content = None
    if task is not None:
        task_content = {
            "job_id": str(task["job_id"]),
            "status": str(task["status"]),
            "progress": json.loads(str(task["checkpoint_json"]))
            if task["checkpoint_json"]
            else None,
            "error": json.loads(str(task["error_json"])) if task["error_json"] else None,
            "attempts": int(task["attempts"]),
            "updated_at": str(task["updated_at"]),
        }
    return {
        "preflight": preflight,
        "current": None if current is None else artifact_content(current),
        "candidate": None if candidate is None else _candidate_content(candidate),
        "task": task_content,
    }
