from __future__ import annotations

import json
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
            ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
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
    with transaction(settings) as connection:
        row = connection.execute(
            """
            SELECT job_id, kind, payload_json
            FROM jobs
            WHERE status = 'pending'
               OR (status = 'running' AND lease_expires_at < ?)
            ORDER BY created_at, job_id
            LIMIT 1
            """,
            (now.isoformat(),),
        ).fetchone()
        if row is None:
            return None
        updated = connection.execute(
            """
            UPDATE jobs
            SET status = 'running', lease_owner = ?, lease_expires_at = ?,
                attempts = attempts + 1, updated_at = ?
            WHERE job_id = ?
              AND (status = 'pending' OR (status = 'running' AND lease_expires_at < ?))
            """,
            (
                settings.worker_id,
                lease_expires_at,
                now.isoformat(),
                row["job_id"],
                now.isoformat(),
            ),
        )
        if updated.rowcount != 1:
            return None
    return ClaimedJob(
        job_id=str(row["job_id"]),
        kind=str(row["kind"]),
        payload=json.loads(row["payload_json"]),
    )


def finish_job(settings: Settings, job_id: str, *, succeeded: bool) -> None:
    status = "completed" if succeeded else "failed"
    with transaction(settings) as connection:
        updated = connection.execute(
            """
            UPDATE jobs
            SET status = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
            WHERE job_id = ? AND status = 'running' AND lease_owner = ?
            """,
            (status, timestamp(), job_id, settings.worker_id),
        )
        if updated.rowcount != 1:
            raise RuntimeError(f"worker 无法完成未持有的任务：{job_id}")
