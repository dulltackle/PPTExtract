from __future__ import annotations

import signal
import threading
from datetime import UTC, datetime, timedelta

from pptextract.config import Settings
from pptextract.db import connect, initialize_database, transaction
from pptextract.ingest_workflow import (
    enqueue_stale_render_jobs,
    fail_hidden_page_job,
    fail_ingestion_job,
    fail_rerender_job,
    process_hidden_page_job,
    process_ingestion_job,
    process_rerender_job,
)
from pptextract.jobs import claim_next_job, finish_job
from pptextract.object_store import LocalObjectStore

HEARTBEAT_INTERVAL_SECONDS = 2.0
HEARTBEAT_STALE_AFTER = timedelta(seconds=10)


def utc_now() -> datetime:
    return datetime.now(UTC)


def record_heartbeat(settings: Settings, *, at: datetime | None = None) -> None:
    heartbeat_at = (at or utc_now()).isoformat()
    with transaction(settings) as connection:
        connection.execute(
            """
            INSERT INTO worker_heartbeats (worker_id, config_version, heartbeat_at)
            VALUES (?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                config_version = excluded.config_version,
                heartbeat_at = excluded.heartbeat_at
            """,
            (settings.worker_id, settings.config_version, heartbeat_at),
        )


def worker_is_fresh(settings: Settings, *, at: datetime | None = None) -> bool:
    with connect(settings) as connection:
        row = connection.execute(
            """
            SELECT config_version, heartbeat_at
            FROM worker_heartbeats
            WHERE worker_id = ?
            """,
            (settings.worker_id,),
        ).fetchone()
    if row is None or row["config_version"] != settings.config_version:
        return False
    heartbeat_at = datetime.fromisoformat(row["heartbeat_at"])
    return (at or utc_now()) - heartbeat_at <= HEARTBEAT_STALE_AFTER


def run_once(settings: Settings) -> bool:
    record_heartbeat(settings)
    enqueue_stale_render_jobs(settings)
    job = claim_next_job(settings)
    if job is None:
        return False
    if job.kind == "system.noop":
        finish_job(settings, job, succeeded=True)
    elif job.kind == "document.ingest":
        try:
            process_ingestion_job(settings, job)
        except Exception as error:
            fail_ingestion_job(settings, job, error)
    elif job.kind == "page.enable":
        try:
            process_hidden_page_job(settings, job)
        except Exception as error:
            fail_hidden_page_job(settings, job, error)
    elif job.kind == "version.rerender":
        try:
            process_rerender_job(settings, job)
        except Exception as error:
            fail_rerender_job(settings, job, error)
        else:
            finish_job(settings, job, succeeded=True)
    else:
        finish_job(settings, job, succeeded=False)
    return True


def run(settings: Settings | None = None, stop_event: threading.Event | None = None) -> None:
    resolved = settings or Settings.from_env()
    initialize_database(resolved)
    LocalObjectStore(resolved.object_store_path)
    stopped = stop_event or threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stopped.set()

    if stop_event is None:
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)

    while not stopped.is_set():
        run_once(resolved)
        stopped.wait(HEARTBEAT_INTERVAL_SECONDS)


def main() -> None:
    run()


if __name__ == "__main__":
    main()
