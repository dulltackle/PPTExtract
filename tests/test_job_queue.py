from pathlib import Path

from pptextract.config import Settings
from pptextract.db import connect, initialize_database
from pptextract.jobs import enqueue_job
from pptextract.worker import run_once


def test_worker_completes_persistent_noop_job(tmp_path: Path) -> None:
    settings = Settings.for_test(tmp_path)
    initialize_database(settings)
    job_id = enqueue_job(
        settings,
        kind="system.noop",
        payload={},
        actor_id="operator-zhang",
        idempotency_key="blackbox-worker-check",
    )

    assert run_once(settings) is True

    with connect(settings) as connection:
        row = connection.execute(
            "SELECT status, attempts, actor_id FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    assert dict(row) == {
        "status": "completed",
        "attempts": 1,
        "actor_id": "operator-zhang",
    }


def test_actor_scopes_idempotent_job_submission(tmp_path: Path) -> None:
    settings = Settings.for_test(tmp_path)
    initialize_database(settings)

    first = enqueue_job(
        settings,
        kind="system.noop",
        payload={},
        actor_id="operator-zhang",
        idempotency_key="same-request",
    )
    repeated = enqueue_job(
        settings,
        kind="system.noop",
        payload={},
        actor_id="operator-zhang",
        idempotency_key="same-request",
    )
    other_actor = enqueue_job(
        settings,
        kind="system.noop",
        payload={},
        actor_id="operator-li",
        idempotency_key="same-request",
    )

    assert repeated == first
    assert other_actor != first
