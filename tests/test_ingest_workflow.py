from dataclasses import replace
from pathlib import Path

from pptextract.config import Settings
from pptextract.db import connect, initialize_database, transaction
from pptextract.ingest_workflow import fail_ingestion_job
from pptextract.jobs import ClaimedJob, timestamp


def test_worker_that_lost_its_lease_cannot_fail_the_version(tmp_path: Path) -> None:
    first_worker = Settings.for_test(tmp_path)
    second_worker = replace(first_worker, worker_id="second-worker")
    initialize_database(first_worker)
    now = timestamp()
    with transaction(first_worker) as connection:
        connection.execute(
            """
            INSERT INTO stored_objects (sha256, size_bytes, media_type, verified_at)
            VALUES (?, 1, 'application/test', ?)
            """,
            ("a" * 64, now),
        )
        connection.execute(
            "INSERT INTO documents (document_id, created_at) VALUES ('doc', ?)",
            (now,),
        )
        connection.execute(
            """
            INSERT INTO document_versions (
                version_id, document_id, source_sha256, source_filename,
                source_size_bytes, status, created_at
            ) VALUES ('version', 'doc', ?, 'source.pptx', 1, 'processing', ?)
            """,
            ("a" * 64, now),
        )
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, kind, payload_json, status, actor_id, idempotency_key,
                lease_owner, lease_expires_at, created_at, updated_at
            ) VALUES (
                'job', 'document.ingest', '{}', 'running', 'actor', 'key',
                ?, ?, ?, ?
            )
            """,
            (second_worker.worker_id, now, now, now),
        )

    stale_claim = ClaimedJob(
        job_id="job",
        kind="document.ingest",
        payload={"version_id": "version"},
    )
    fail_ingestion_job(first_worker, stale_claim, RuntimeError("stale worker"))

    with connect(first_worker) as connection:
        version = connection.execute(
            "SELECT status FROM document_versions WHERE version_id = 'version'"
        ).fetchone()
        job = connection.execute(
            "SELECT status, lease_owner FROM jobs WHERE job_id = 'job'"
        ).fetchone()
    assert dict(version) == {"status": "processing"}
    assert dict(job) == {"status": "running", "lease_owner": second_worker.worker_id}
