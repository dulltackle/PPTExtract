from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pptextract.config import Settings
from pptextract.db import transaction

JOB_LEASE_DURATION = timedelta(minutes=5)


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def lease_expiration(*, at: datetime | None = None) -> str:
    return ((at or datetime.now(UTC)) + JOB_LEASE_DURATION).isoformat()


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    job_id: str
    kind: str
    payload: dict[str, Any]
    lease_token: str = ""
    attempts: int = 0
    max_attempts: int = 3


def enqueue_job(
    settings: Settings,
    *,
    kind: str,
    payload: dict[str, Any],
    actor_id: str,
    idempotency_key: str,
) -> str:
    job_id = uuid.uuid4().hex
    now = timestamp()
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with transaction(settings) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO jobs (
                job_id, kind, payload_json, status, actor_id, idempotency_key,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)
            """,
            (job_id, kind, payload_json, actor_id, idempotency_key, now, now),
        )
        row = connection.execute(
            "SELECT job_id FROM jobs WHERE actor_id = ? AND idempotency_key = ?",
            (actor_id, idempotency_key),
        ).fetchone()
    if row is None:
        raise RuntimeError("无法持久化任务")
    return str(row["job_id"])


def claim_next_job(settings: Settings) -> ClaimedJob | None:
    now = datetime.now(UTC)
    lease_expires_at = lease_expiration(at=now)
    lease_token = uuid.uuid4().hex
    with transaction(settings) as connection:
        _fail_exhausted_expired_jobs(connection, now=now.isoformat())
        row = connection.execute(
            """
            SELECT job_id, kind, payload_json, attempts, max_attempts
            FROM jobs
            WHERE (
                (status = 'queued'
                 AND attempts < max_attempts
                 AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                OR (status = 'running' AND attempts < max_attempts
                    AND lease_expires_at <= ?)
            )
              AND (
                kind NOT IN ('page.enable', 'version.rerender')
                OR COALESCE(
                    CAST(json_extract(payload_json, '$.render_generation') AS INTEGER),
                    ?
                ) = ?
              )
            ORDER BY created_at, job_id
            LIMIT 1
            """,
            (
                now.isoformat(),
                now.isoformat(),
                settings.render_generation,
                settings.render_generation,
            ),
        ).fetchone()
        if row is None:
            return None
        updated = connection.execute(
            """
            UPDATE jobs
            SET status = 'running', lease_owner = ?, lease_token = ?,
                lease_expires_at = ?, next_attempt_at = NULL,
                attempts = attempts + 1, updated_at = ?
            WHERE job_id = ?
              AND (
                  (status = 'queued' AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                  OR (status = 'running' AND lease_expires_at <= ?)
              )
              AND attempts < max_attempts
            """,
            (
                settings.worker_id,
                lease_token,
                lease_expires_at,
                now.isoformat(),
                row["job_id"],
                now.isoformat(),
                now.isoformat(),
            ),
        )
        if updated.rowcount != 1:
            return None
    return ClaimedJob(
        job_id=str(row["job_id"]),
        kind=str(row["kind"]),
        payload=json.loads(row["payload_json"]),
        lease_token=lease_token,
        attempts=int(row["attempts"]) + 1,
        max_attempts=int(row["max_attempts"]),
    )


def finish_job(settings: Settings, job: ClaimedJob, *, succeeded: bool) -> None:
    status = "succeeded" if succeeded else "failed"
    now = timestamp()
    with transaction(settings) as connection:
        updated = connection.execute(
            """
            UPDATE jobs
            SET status = ?, lease_owner = NULL, lease_token = NULL,
                lease_expires_at = NULL, next_attempt_at = NULL, updated_at = ?
            WHERE job_id = ? AND status = 'running'
              AND lease_owner = ? AND lease_token = ? AND lease_expires_at > ?
            """,
            (status, now, job.job_id, settings.worker_id, job.lease_token, now),
        )
        if updated.rowcount != 1:
            raise RuntimeError(f"worker 无法完成未持有的任务：{job.job_id}")


def _fail_exhausted_expired_jobs(connection: sqlite3.Connection, *, now: str) -> None:
    rows = connection.execute(
        """
        SELECT job_id, kind, payload_json, version_id, attempts, checkpoint_json
        FROM jobs
        WHERE status = 'running' AND attempts >= max_attempts
          AND lease_expires_at <= ?
        """,
        (now,),
    ).fetchall()
    for row in rows:
        checkpoint = json.loads(row["checkpoint_json"]) if row["checkpoint_json"] else {}
        error = json.dumps(
            {
                "attempt": int(row["attempts"]),
                "code": "lease_expired",
                "message": "任务租约过期且已耗尽尝试次数。",
                "phase": str(checkpoint.get("phase", "unknown")),
                "retryable": True,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        failed = connection.execute(
            """
            UPDATE jobs
            SET status = 'failed', lease_owner = NULL, lease_token = NULL,
                lease_expires_at = NULL, next_attempt_at = NULL,
                error_json = ?, updated_at = ?
            WHERE job_id = ? AND status = 'running'
              AND attempts >= max_attempts AND lease_expires_at <= ?
            """,
            (error, now, row["job_id"], now),
        )
        if (
            failed.rowcount == 1
            and row["kind"] == "document.ingest"
            and row["version_id"] is not None
        ):
            connection.execute(
                """
                UPDATE document_versions SET status = 'failed'
                WHERE version_id = ? AND status = 'processing'
                """,
                (row["version_id"],),
            )
        elif failed.rowcount == 1 and row["kind"] == "page.enable":
            payload = json.loads(row["payload_json"])
            connection.execute(
                """
                UPDATE ingestion_page_results
                SET enabled = 0, source_content_json = NULL, conversion_key = NULL,
                    fingerprint_version = NULL, fingerprint_sha256 = NULL,
                    fingerprint_key = NULL, render_sha256 = NULL,
                    render_media_type = NULL, render_dpi = NULL,
                    render_width_px = NULL, render_height_px = NULL, render_key = NULL
                WHERE version_id = ? AND page_number = ? AND enable_job_id = ?
                """,
                (payload["version_id"], int(payload["page_number"]), row["job_id"]),
            )
