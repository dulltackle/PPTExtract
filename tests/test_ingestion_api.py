from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from threading import Barrier
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from pptextract.api import create_app
from pptextract.config import Settings
from pptextract.db import transaction
from pptextract.jobs import timestamp
from pptextract.pptx_projection import MAX_XML_PART_BYTES
from pptextract.worker import run_once
from tests.support.synthetic_pptx import (
    build_minimal_presentation,
    build_plain_text_presentation,
)

PPTX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@pytest.fixture
def system(tmp_path: Path) -> Iterator[tuple[TestClient, Settings]]:
    settings = Settings.for_test(tmp_path)
    with TestClient(create_app(settings)) as test_client:
        yield test_client, settings


@pytest.fixture
def client(system: tuple[TestClient, Settings]) -> TestClient:
    return system[0]


def set_version_state(
    settings: Settings,
    identity: dict[str, str | None],
    *,
    status: str,
    current: bool,
) -> None:
    now = timestamp()
    with transaction(settings) as connection:
        connection.execute(
            "UPDATE jobs SET status = 'succeeded', updated_at = ? WHERE job_id = ?",
            (now, identity["job_id"]),
        )
        connection.execute(
            "UPDATE document_versions SET status = ?, ready_at = ? WHERE version_id = ?",
            (status, now if status == "ready" else None, identity["version_id"]),
        )
        connection.execute(
            "UPDATE documents SET current_version_id = ? WHERE document_id = ?",
            (identity["version_id"] if current else None, identity["document_id"]),
        )


def test_upload_validation_returns_stable_errors_at_the_http_boundary(
    tmp_path: Path, system: tuple[TestClient, Settings]
) -> None:
    client, settings = system
    unsupported = client.post(
        "/api/v1/documents",
        headers={"Idempotency-Key": "unsupported"},
        files={"file": ("notes.txt", b"not a presentation", "text/plain")},
    )
    disguised = client.post(
        "/api/v1/documents",
        headers={"Idempotency-Key": "disguised"},
        files={"file": ("notes.pptx", b"not an OOXML package", PPTX_MEDIA_TYPE)},
    )
    incomplete_package = BytesIO()
    with ZipFile(incomplete_package, "w", compression=ZIP_DEFLATED) as package:
        package.writestr(
            "ppt/presentation.xml",
            b"""<?xml version="1.0" encoding="UTF-8"?>
            <p:presentation
                xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>
            </p:presentation>""",
        )
        package.writestr(
            "ppt/_rels/presentation.xml.rels",
            b"""<?xml version="1.0" encoding="UTF-8"?>
            <Relationships
                xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Target="slides/slide1.xml"
                Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"/>
            </Relationships>""",
        )
        package.writestr(
            "ppt/slides/slide1.xml",
            b"""<?xml version="1.0" encoding="UTF-8"?>
            <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>""",
        )
    incomplete = client.post(
        "/api/v1/documents",
        headers={"Idempotency-Key": "incomplete-package"},
        files={"file": ("incomplete.pptx", incomplete_package.getvalue(), PPTX_MEDIA_TYPE)},
    )

    limited_settings = replace(
        Settings.for_test(tmp_path / "limited"),
        max_source_upload_bytes=64,
    )
    with TestClient(create_app(limited_settings)) as limited_client:
        oversized = limited_client.post(
            "/api/v1/documents",
            headers={"Idempotency-Key": "oversized"},
            files={
                "file": (
                    "oversized.pptx",
                    build_plain_text_presentation(),
                    PPTX_MEDIA_TYPE,
                )
            },
        )

    source = build_minimal_presentation()
    expanded = BytesIO()
    with (
        ZipFile(BytesIO(source)) as package,
        ZipFile(expanded, "w", compression=ZIP_DEFLATED) as rewritten,
    ):
        for entry in package.infolist():
            content = package.read(entry.filename)
            if entry.filename == "ppt/presentation.xml":
                content = b" " * (MAX_XML_PART_BYTES + 1)
            rewritten.writestr(entry, content)
    expanded_over_limit = client.post(
        "/api/v1/documents",
        headers={"Idempotency-Key": "expanded-over-limit"},
        files={"file": ("expanded.pptx", expanded.getvalue(), PPTX_MEDIA_TYPE)},
    )

    assert unsupported.status_code == 415
    assert unsupported.json()["error"]["code"] == "unsupported_presentation"
    assert disguised.status_code == 422
    assert disguised.json()["error"]["code"] == "invalid_pptx"
    assert incomplete.status_code == 422
    assert incomplete.json()["error"]["code"] == "invalid_pptx"
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "source_too_large"
    assert expanded_over_limit.status_code == 413
    assert expanded_over_limit.json()["error"]["code"] == "source_too_large"
    assert run_once(settings) is False
    assert run_once(limited_settings) is False


def test_first_upload_idempotency_is_scoped_to_actor_and_the_exact_request(
    client: TestClient,
) -> None:
    source = build_plain_text_presentation()

    def upload(*, actor: str, filename: str) -> Response:
        return client.post(
            "/api/v1/documents",
            headers={"X-Actor-ID": actor, "Idempotency-Key": "create-quarterly-deck"},
            files={"file": (filename, source, PPTX_MEDIA_TYPE)},
        )

    first = upload(actor="operator-zhang", filename="quarterly.pptx")
    replayed = upload(actor="operator-zhang", filename="quarterly.pptx")
    conflicting = upload(actor="operator-zhang", filename="renamed.pptx")
    other_actor = upload(actor="operator-li", filename="quarterly.pptx")

    assert first.status_code == 202
    assert replayed.status_code == 202
    assert replayed.json() == first.json()
    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["code"] == "idempotency_conflict"
    assert other_actor.status_code == 202
    assert other_actor.json()["document_id"] != first.json()["document_id"]


def test_active_document_uploads_coalesce_or_report_document_busy(
    client: TestClient,
) -> None:
    source = build_plain_text_presentation()
    headers = {"X-Actor-ID": "operator-zhang", "Idempotency-Key": "shared-key"}
    created = client.post(
        "/api/v1/documents",
        headers=headers,
        files={"file": ("quarterly.pptx", source, PPTX_MEDIA_TYPE)},
    )
    document_id = created.json()["document_id"]

    coalesced = client.post(
        f"/api/v1/documents/{document_id}/versions",
        headers=headers,
        files={"file": ("quarterly.pptx", source, PPTX_MEDIA_TYPE)},
    )
    replayed = client.post(
        f"/api/v1/documents/{document_id}/versions",
        headers=headers,
        files={"file": ("quarterly.pptx", source, PPTX_MEDIA_TYPE)},
    )
    conflicting = client.post(
        f"/api/v1/documents/{document_id}/versions",
        headers=headers,
        files={"file": ("changed.pptx", build_minimal_presentation(), PPTX_MEDIA_TYPE)},
    )
    busy = client.post(
        f"/api/v1/documents/{document_id}/versions",
        headers={"X-Actor-ID": "operator-zhang", "Idempotency-Key": "different-key"},
        files={"file": ("changed.pptx", build_minimal_presentation(), PPTX_MEDIA_TYPE)},
    )

    assert created.status_code == 202
    assert coalesced.status_code == 202
    assert coalesced.json() == {
        **created.json(),
        "status": "coalesced",
    }
    assert replayed.json() == coalesced.json()
    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["code"] == "idempotency_conflict"
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "document_busy"


def test_only_current_ready_content_is_no_change_while_history_creates_a_version(
    system: tuple[TestClient, Settings],
) -> None:
    client, settings = system
    original_source = build_plain_text_presentation()
    changed_source = build_minimal_presentation()
    created = client.post(
        "/api/v1/documents",
        headers={"Idempotency-Key": "create-original"},
        files={"file": ("original.pptx", original_source, PPTX_MEDIA_TYPE)},
    ).json()
    set_version_state(settings, created, status="ready", current=True)

    no_change = client.post(
        f"/api/v1/documents/{created['document_id']}/versions",
        headers={"Idempotency-Key": "original-again"},
        files={"file": ("original.pptx", original_source, PPTX_MEDIA_TYPE)},
    )
    changed = client.post(
        f"/api/v1/documents/{created['document_id']}/versions",
        headers={"Idempotency-Key": "changed-current"},
        files={"file": ("changed.pptx", changed_source, PPTX_MEDIA_TYPE)},
    )
    set_version_state(settings, changed.json(), status="ready", current=True)
    historical = client.post(
        f"/api/v1/documents/{created['document_id']}/versions",
        headers={"Idempotency-Key": "restore-historical-bytes"},
        files={"file": ("original.pptx", original_source, PPTX_MEDIA_TYPE)},
    )
    independent_document = client.post(
        "/api/v1/documents",
        headers={"Idempotency-Key": "same-bytes-new-document"},
        files={"file": ("original.pptx", original_source, PPTX_MEDIA_TYPE)},
    )

    assert no_change.status_code == 200
    assert no_change.json() == {
        "document_id": created["document_id"],
        "version_id": created["version_id"],
        "job_id": None,
        "status": "no_change",
    }
    assert changed.status_code == 202
    assert historical.status_code == 202
    assert historical.json()["status"] == "accepted"
    assert historical.json()["version_id"] not in {
        created["version_id"],
        changed.json()["version_id"],
    }
    assert independent_document.status_code == 202
    assert independent_document.json()["document_id"] != created["document_id"]


@pytest.mark.parametrize("terminal_status", ["failed", "voided"])
def test_failed_or_voided_content_never_becomes_a_no_change_target(
    system: tuple[TestClient, Settings], terminal_status: str
) -> None:
    client, settings = system
    source = build_plain_text_presentation()
    created = client.post(
        "/api/v1/documents",
        headers={"Idempotency-Key": f"create-{terminal_status}"},
        files={"file": ("source.pptx", source, PPTX_MEDIA_TYPE)},
    ).json()
    set_version_state(settings, created, status=terminal_status, current=False)

    retried_content = client.post(
        f"/api/v1/documents/{created['document_id']}/versions",
        headers={"Idempotency-Key": f"retry-{terminal_status}-bytes"},
        files={"file": ("source.pptx", source, PPTX_MEDIA_TYPE)},
    )

    assert retried_content.status_code == 202
    assert retried_content.json()["status"] == "accepted"
    assert retried_content.json()["version_id"] != created["version_id"]
    assert retried_content.json()["job_id"] is not None


def test_awaiting_mapping_keeps_the_document_serial_slot(
    system: tuple[TestClient, Settings],
) -> None:
    client, settings = system
    created = client.post(
        "/api/v1/documents",
        headers={"Idempotency-Key": "create-awaiting-mapping"},
        files={
            "file": (
                "source.pptx",
                build_plain_text_presentation(),
                PPTX_MEDIA_TYPE,
            )
        },
    ).json()
    with transaction(settings) as connection:
        connection.execute(
            "UPDATE document_versions SET status = 'awaiting_mapping' WHERE version_id = ?",
            (created["version_id"],),
        )

    busy = client.post(
        f"/api/v1/documents/{created['document_id']}/versions",
        headers={"Idempotency-Key": "blocked-by-mapping"},
        files={
            "file": (
                "changed.pptx",
                build_minimal_presentation(),
                PPTX_MEDIA_TYPE,
            )
        },
    )

    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "document_busy"


def test_concurrent_uploads_do_not_create_duplicate_versions_or_jobs(
    system: tuple[TestClient, Settings],
) -> None:
    client, settings = system
    first_source = build_plain_text_presentation(title="并发基线")
    created = client.post(
        "/api/v1/documents",
        headers={"Idempotency-Key": "create-concurrency-baseline"},
        files={"file": ("baseline.pptx", first_source, PPTX_MEDIA_TYPE)},
    ).json()
    set_version_state(settings, created, status="ready", current=True)
    document_id = str(created["document_id"])

    def upload_pair(
        left: tuple[str, str, bytes], right: tuple[str, str, bytes]
    ) -> tuple[Response, Response]:
        barrier = Barrier(2)

        def upload(request: tuple[str, str, bytes]) -> Response:
            key, filename, source = request
            barrier.wait(timeout=5)
            return client.post(
                f"/api/v1/documents/{document_id}/versions",
                headers={"Idempotency-Key": key},
                files={"file": (filename, source, PPTX_MEDIA_TYPE)},
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            left_future = executor.submit(upload, left)
            right_future = executor.submit(upload, right)
            return left_future.result(timeout=10), right_future.result(timeout=10)

    retry_source = build_plain_text_presentation(title="并发同键重试")
    retry_left, retry_right = upload_pair(
        ("same-command", "retry.pptx", retry_source),
        ("same-command", "retry.pptx", retry_source),
    )
    assert retry_left.status_code == retry_right.status_code == 202
    assert retry_left.json() == retry_right.json()
    set_version_state(settings, retry_left.json(), status="ready", current=True)

    coalesced_source = build_plain_text_presentation(title="并发同内容合并")
    coalesced_left, coalesced_right = upload_pair(
        ("same-bytes-left", "coalesced.pptx", coalesced_source),
        ("same-bytes-right", "coalesced.pptx", coalesced_source),
    )
    assert {coalesced_left.status_code, coalesced_right.status_code} == {202}
    assert {coalesced_left.json()["status"], coalesced_right.json()["status"]} == {
        "accepted",
        "coalesced",
    }
    assert coalesced_left.json()["version_id"] == coalesced_right.json()["version_id"]
    assert coalesced_left.json()["job_id"] == coalesced_right.json()["job_id"]
    accepted = (
        coalesced_left.json()
        if coalesced_left.json()["status"] == "accepted"
        else coalesced_right.json()
    )
    set_version_state(settings, accepted, status="ready", current=True)

    different_left, different_right = upload_pair(
        (
            "different-left",
            "left.pptx",
            build_plain_text_presentation(title="竞争内容甲"),
        ),
        (
            "different-right",
            "right.pptx",
            build_plain_text_presentation(title="竞争内容乙"),
        ),
    )
    assert sorted([different_left.status_code, different_right.status_code]) == [202, 409]
    busy = different_left if different_left.status_code == 409 else different_right
    assert busy.json()["error"]["code"] == "document_busy"
