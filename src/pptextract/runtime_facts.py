from __future__ import annotations

import csv
import io
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from pptextract.config import Settings
from pptextract.db import connect, transaction
from pptextract.jobs import timestamp

STAGES = ("source_review", "capture_annotation", "page_decision")


class RuntimeFactError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def record_action(
    connection: sqlite3.Connection,
    *,
    page_version_id: str,
    actor_id: str,
    action_type: str,
    occurred_at: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO curation_action_events (
            event_id, page_version_id, actor_id, action_type, occurred_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (uuid.uuid4().hex, page_version_id, actor_id, action_type, occurred_at or timestamp()),
    )


def _milliseconds_since(value: str, *, now: datetime) -> int:
    try:
        started_at = datetime.fromisoformat(value)
    except ValueError:
        return 0
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return max(0, int((now - started_at).total_seconds() * 1000))


def _queue_snapshot(
    connection: sqlite3.Connection, *, recorded_at: str | None = None
) -> dict[str, int]:
    pending_rows = connection.execute(
        """
        SELECT pv.created_at,
               MAX(CASE WHEN events.event_type = 'reopened' THEN events.occurred_at END)
                   AS reopened_at
        FROM page_versions AS pv
        JOIN documents AS d
          ON d.document_id = pv.document_id
         AND d.current_version_id = pv.version_id
        LEFT JOIN page_review_events AS events
          ON events.page_version_id = pv.page_version_id
        WHERE d.deleted_at IS NULL AND pv.review_status = 'pending'
        GROUP BY pv.page_version_id, pv.created_at
        """
    ).fetchall()
    now = datetime.fromisoformat(recorded_at) if recorded_at is not None else datetime.now(UTC)
    longest_wait_ms = max(
        (
            _milliseconds_since(str(row["reopened_at"] or row["created_at"]), now=now)
            for row in pending_rows
        ),
        default=0,
    )
    return {"pending_count": len(pending_rows), "longest_wait_ms": longest_wait_ms}


def record_timing_sample(
    settings: Settings,
    *,
    sample_id: str,
    page_id: str,
    version_id: str,
    actor_id: str,
    stage: str,
    duration_ms: int,
) -> dict[str, Any]:
    if stage not in STAGES:
        raise RuntimeFactError(422, "invalid_stage", "策展计时阶段无效。")
    if duration_ms < 0:
        raise RuntimeFactError(422, "invalid_duration", "策展计时不能为负数。")
    recorded_at = timestamp()
    with transaction(settings) as connection:
        page = connection.execute(
            """
            SELECT pv.page_version_id
            FROM page_versions AS pv
            JOIN documents AS d ON d.document_id = pv.document_id
            WHERE d.deleted_at IS NULL AND pv.page_id = ? AND pv.version_id = ?
            """,
            (page_id, version_id),
        ).fetchone()
        if page is None:
            raise RuntimeFactError(404, "not_found", "未找到请求的策展页。")
        page_version_id = str(page["page_version_id"])
        existing = connection.execute(
            """
            SELECT sample_id, page_version_id, actor_id, stage, duration_ms,
                   pending_count, longest_wait_ms, recorded_at
            FROM curation_timing_samples WHERE sample_id = ?
            """,
            (sample_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["page_version_id"]) != page_version_id
                or str(existing["actor_id"]) != actor_id
                or str(existing["stage"]) != stage
                or int(existing["duration_ms"]) != duration_ms
            ):
                raise RuntimeFactError(
                    409,
                    "sample_conflict",
                    "同一计时样本标识已用于不同内容。",
                )
            return {"status": "duplicate", "sample": _serialize_sample(existing)}
        queue = _queue_snapshot(connection, recorded_at=recorded_at)
        connection.execute(
            """
            INSERT INTO curation_timing_samples (
                sample_id, page_version_id, actor_id, stage, duration_ms,
                pending_count, longest_wait_ms, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sample_id,
                page_version_id,
                actor_id,
                stage,
                duration_ms,
                queue["pending_count"],
                queue["longest_wait_ms"],
                recorded_at,
            ),
        )
        row = connection.execute(
            "SELECT * FROM curation_timing_samples WHERE sample_id = ?", (sample_id,)
        ).fetchone()
        assert row is not None
        return {"status": "recorded", "sample": _serialize_sample(row)}


def _serialize_sample(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "sample_id": str(row["sample_id"]),
        "stage": str(row["stage"]),
        "duration_ms": int(row["duration_ms"]),
        "pending_count": int(row["pending_count"]),
        "longest_wait_ms": int(row["longest_wait_ms"]),
        "recorded_at": str(row["recorded_at"]),
    }


def read_runtime_facts(settings: Settings) -> dict[str, Any]:
    with connect(settings) as connection:
        page_rows = connection.execute(
            """
            SELECT pv.page_id, pv.page_version_id, pv.document_id, pv.version_id,
                   pv.page_number, pv.review_status, pv.current_snapshot_id
            FROM page_versions AS pv
            JOIN documents AS d ON d.document_id = pv.document_id
            WHERE (d.deleted_at IS NULL AND d.current_version_id = pv.version_id)
               OR EXISTS (
                    SELECT 1 FROM curation_timing_samples AS timing
                    WHERE timing.page_version_id = pv.page_version_id
               )
               OR EXISTS (
                    SELECT 1 FROM curation_action_events AS actions
                    WHERE actions.page_version_id = pv.page_version_id
               )
            ORDER BY pv.document_id, pv.page_number, pv.page_id
            """
        ).fetchall()
        pages = [_read_page_fact(connection, row) for row in page_rows]
        queue = _queue_snapshot(connection)
        queue_samples = [
            {
                "recorded_at": str(row["recorded_at"]),
                "pending_count": int(row["pending_count"]),
                "longest_wait_ms": int(row["longest_wait_ms"]),
            }
            for row in connection.execute(
                """
                SELECT recorded_at, pending_count, longest_wait_ms
                FROM curation_timing_samples ORDER BY recorded_at, rowid
                """
            )
        ]
    return {
        "generated_at": timestamp(),
        "queue": queue,
        "pages": pages,
        "queue_samples": queue_samples,
    }


def runtime_facts_csv(facts: dict[str, Any]) -> str:
    output = io.StringIO(newline="")
    fieldnames = [
        "record_type",
        "page_id",
        "page_version_id",
        "document_id",
        "version_id",
        "page_number",
        "review_conclusion",
        "source_review_ms",
        "capture_annotation_ms",
        "page_decision_ms",
        "total_ms",
        "source_image_count",
        "source_image_disposed_count",
        "capture_visual_count",
        "actions",
        "sampled_pending_count",
        "sampled_longest_wait_ms",
        "sample_recorded_at",
        "generated_at",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for page in facts["pages"]:
        durations = page["durations_ms"]
        source_images = page["source_images"]
        actions = ";".join(
            f"{action}={count}" for action, count in sorted(page["actions"].items())
        )
        writer.writerow(
            {
                "record_type": "page",
                "page_id": page["page_id"],
                "page_version_id": page["page_version_id"],
                "document_id": page["document_id"],
                "version_id": page["version_id"],
                "page_number": page["page_number"],
                "review_conclusion": page["review_conclusion"],
                "source_review_ms": durations["source_review"],
                "capture_annotation_ms": durations["capture_annotation"],
                "page_decision_ms": durations["page_decision"],
                "total_ms": durations["total"],
                "source_image_count": source_images["total"],
                "source_image_disposed_count": source_images["disposed"],
                "capture_visual_count": page["capture_visuals"],
                "actions": actions,
                "generated_at": facts["generated_at"],
            }
        )
    for sample in facts["queue_samples"]:
        writer.writerow(
            {
                "record_type": "queue_sample",
                "sampled_pending_count": sample["pending_count"],
                "sampled_longest_wait_ms": sample["longest_wait_ms"],
                "sample_recorded_at": sample["recorded_at"],
                "generated_at": facts["generated_at"],
            }
        )
    return output.getvalue()


def _read_page_fact(connection: sqlite3.Connection, page: sqlite3.Row) -> dict[str, Any]:
    page_version_id = str(page["page_version_id"])
    duration_rows = connection.execute(
        """
        SELECT stage, SUM(duration_ms) AS duration_ms
        FROM curation_timing_samples
        WHERE page_version_id = ? GROUP BY stage
        """,
        (page_version_id,),
    ).fetchall()
    duration_by_stage = {str(row["stage"]): int(row["duration_ms"]) for row in duration_rows}
    durations = {stage: duration_by_stage.get(stage, 0) for stage in STAGES}
    snapshot_id = cast(str | None, page["current_snapshot_id"])
    source_total = int(
        connection.execute(
            "SELECT COUNT(*) FROM page_version_image_sources WHERE page_version_id = ?",
            (page_version_id,),
        ).fetchone()[0]
    )
    disposed = 0
    capture_visuals = 0
    if snapshot_id is not None:
        disposed = int(
            connection.execute(
                "SELECT COUNT(*) FROM curation_snapshot_image_sources WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()[0]
        )
        capture_visuals = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM curation_snapshot_visuals
                WHERE snapshot_id = ? AND source_kind = 'capture'
                  AND disposition = 'included'
                """,
                (snapshot_id,),
            ).fetchone()[0]
        )
    action_rows = connection.execute(
        """
        SELECT action_type, COUNT(*) AS action_count
        FROM curation_action_events
        WHERE page_version_id = ? GROUP BY action_type ORDER BY action_type
        """,
        (page_version_id,),
    ).fetchall()
    return {
        "page_id": str(page["page_id"]),
        "page_version_id": page_version_id,
        "document_id": str(page["document_id"]),
        "version_id": str(page["version_id"]),
        "page_number": int(page["page_number"]),
        "review_conclusion": str(page["review_status"]),
        "durations_ms": {"total": sum(durations.values()), **durations},
        "source_images": {"total": source_total, "disposed": disposed},
        "capture_visuals": capture_visuals,
        "actions": {
            str(row["action_type"]): int(row["action_count"]) for row in action_rows
        },
    }
