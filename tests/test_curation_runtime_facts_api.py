from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pptextract.api import create_app
from pptextract.config import Settings
from pptextract.conversion import NormalizedImage, NormalizedPageContent
from pptextract.worker import run_once
from tests.test_curation_review_api import _ingest_plain_text_page, _review_plain_text_page
from tests.test_version_inheritance import (
    _install_toolchain,
    _presentation,
    _replace_title,
    _upload,
)


@pytest.fixture
def system(tmp_path: Path) -> Iterator[tuple[TestClient, Settings]]:
    settings = Settings.for_test(tmp_path)
    with TestClient(create_app(settings)) as client:
        yield client, settings


def test_runtime_samples_are_idempotent_and_accumulate_nonnegative_stage_time(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    page = _ingest_plain_text_page(client, settings, monkeypatch)
    page_id = str(page["page_id"])

    first = client.post(
        "/api/v1/curation/runtime-facts/samples",
        headers={"X-Actor-ID": "curator-facts"},
        json={
            "sample_id": "source-review-visit-1",
            "page_id": page_id,
            "version_id": page["version_id"],
            "stage": "source_review",
            "duration_ms": 1_250,
        },
    )
    assert first.status_code == 201
    assert first.json()["status"] == "recorded"
    assert first.json()["sample"]["pending_count"] == 1
    assert first.json()["sample"]["longest_wait_ms"] >= 0

    duplicate = client.post(
        "/api/v1/curation/runtime-facts/samples",
        headers={"X-Actor-ID": "curator-facts"},
        json={
            "sample_id": "source-review-visit-1",
            "page_id": page_id,
            "version_id": page["version_id"],
            "stage": "source_review",
            "duration_ms": 1_250,
        },
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"

    second = client.post(
        "/api/v1/curation/runtime-facts/samples",
        headers={"X-Actor-ID": "curator-facts"},
        json={
            "sample_id": "capture-visit-1",
            "page_id": page_id,
            "version_id": page["version_id"],
            "stage": "capture_annotation",
            "duration_ms": 750,
        },
    )
    assert second.status_code == 201

    facts = client.get("/api/v1/curation/runtime-facts")
    assert facts.status_code == 200
    payload = facts.json()
    assert payload["queue"]["pending_count"] == 1
    assert payload["queue"]["longest_wait_ms"] >= 0
    assert payload["pages"] == [
        {
            "page_id": page_id,
            "page_version_id": payload["pages"][0]["page_version_id"],
            "document_id": page["document_id"],
            "version_id": page["version_id"],
            "page_number": 1,
            "review_conclusion": "pending",
            "durations_ms": {
                "total": 2_000,
                "source_review": 1_250,
                "capture_annotation": 750,
                "page_decision": 0,
            },
            "source_images": {"total": 0, "disposed": 0},
            "capture_visuals": 0,
            "actions": {},
        }
    ]
    assert payload["queue_samples"] == [
        {
            "recorded_at": payload["queue_samples"][0]["recorded_at"],
            "pending_count": 1,
            "longest_wait_ms": payload["queue_samples"][0]["longest_wait_ms"],
        },
        {
            "recorded_at": payload["queue_samples"][1]["recorded_at"],
            "pending_count": 1,
            "longest_wait_ms": payload["queue_samples"][1]["longest_wait_ms"],
        },
    ]
    exported = client.get("/api/v1/curation/runtime-facts?format=csv").text
    assert exported.count("queue_sample") == 2

    serialized = facts.text
    for forbidden in (
        "source_filename",
        "source_content",
        "公开合成正文",
        "alert_threshold",
        "productivity_target",
        "budget",
        "sla",
        "autoscaling",
    ):
        assert forbidden not in serialized
        assert forbidden not in exported


def test_runtime_sample_rejects_negative_duration(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    page = _ingest_plain_text_page(client, settings, monkeypatch)

    response = client.post(
        "/api/v1/curation/runtime-facts/samples",
        json={
            "sample_id": "invalid-negative",
            "page_id": page["page_id"],
            "version_id": page["version_id"],
            "stage": "page_decision",
            "duration_ms": -1,
        },
    )

    assert response.status_code == 422


def test_delayed_sample_stays_attached_to_its_historical_page_version(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    _install_toolchain(monkeypatch)
    first_source = _presentation("第一版标题")
    first = _upload(client, first_source, key="runtime-facts-first")
    assert run_once(settings)
    first_page = client.get(
        "/api/v1/curation/pages", params={"review_status": "all"}
    ).json()["pages"][0]

    second = _upload(
        client,
        _replace_title(first_source, "第一版标题", "第二版标题"),
        key="runtime-facts-second",
        document_id=first["document_id"],
    )
    assert run_once(settings)
    assert second["version_id"] != first["version_id"]

    delayed = client.post(
        "/api/v1/curation/runtime-facts/samples",
        json={
            "sample_id": "delayed-old-tab-sample",
            "page_id": first_page["page_id"],
            "version_id": first["version_id"],
            "stage": "page_decision",
            "duration_ms": 321,
        },
    )
    assert delayed.status_code == 201

    pages = client.get("/api/v1/curation/runtime-facts").json()["pages"]
    old_fact = next(page for page in pages if page["version_id"] == first["version_id"])
    new_fact = next(page for page in pages if page["version_id"] == second["version_id"])
    assert old_fact["durations_ms"]["page_decision"] == 321
    assert new_fact["durations_ms"]["page_decision"] == 0


def test_runtime_facts_count_persisted_curation_actions_and_export_safe_csv(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    page = _ingest_plain_text_page(
        client,
        settings,
        monkeypatch,
        content=NormalizedPageContent(
            titles=("不要导出此标题",),
            body=("不要导出此正文",),
            tables=(),
            images=(
                NormalizedImage(
                    reference_index=0,
                    alt_text="不要导出此替代文字",
                    media_type="image/png",
                    origin_part="ppt/media/private-name.png",
                    data=b"public-test-image",
                ),
            ),
            speaker_notes=(),
        ),
    )
    page_id = str(page["page_id"])
    saved = client.post(
        f"/api/v1/pages/{page_id}/curation/snapshots",
        headers={"X-Actor-ID": "curator-actions"},
        json={
            "base_snapshot_id": None,
            "titles": ["人工确认标题"],
            "body": ["人工确认正文"],
        },
    ).json()["curation"]
    snapshot_id = saved["current_snapshot"]["snapshot_id"]
    source_ref = saved["image_sources"]["items"][0]["source_ref"]
    assert client.post(
        f"/api/v1/pages/{page_id}/curation/source-confirmation",
        headers={"X-Actor-ID": "curator-actions"},
        json={"snapshot_id": snapshot_id},
    ).status_code == 200
    disposed = client.post(
        f"/api/v1/pages/{page_id}/curation/image-sources/{source_ref}",
        headers={"X-Actor-ID": "curator-actions"},
        json={
            "base_snapshot_id": snapshot_id,
            "disposition": "ignored",
            "summary": None,
            "ignore_reason": "decorative",
            "ignore_note": None,
        },
    )
    assert disposed.status_code == 201
    snapshot_id = disposed.json()["curation"]["current_snapshot"]["snapshot_id"]
    assert client.post(
        f"/api/v1/pages/{page_id}/curation/source-review",
        headers={"X-Actor-ID": "curator-actions"},
        json={"snapshot_id": snapshot_id},
    ).status_code == 200
    assert client.post(
        f"/api/v1/pages/{page_id}/approve",
        headers={"X-Actor-ID": "curator-actions"},
        json={"snapshot_id": snapshot_id},
    ).status_code == 200

    facts = client.get("/api/v1/curation/runtime-facts").json()
    assert facts["queue"] == {"pending_count": 0, "longest_wait_ms": 0}
    assert facts["pages"][0]["review_conclusion"] == "approved"
    assert facts["pages"][0]["source_images"] == {"total": 1, "disposed": 1}
    assert facts["pages"][0]["actions"] == {
        "page_approved": 1,
        "source_confirmed": 1,
        "source_image_disposed": 1,
        "source_review_completed": 1,
        "source_saved": 1,
    }

    exported = client.get("/api/v1/curation/runtime-facts?format=csv")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert "page_id,page_version_id,document_id,version_id,page_number" in exported.text
    assert "source_review_ms,capture_annotation_ms,page_decision_ms,total_ms" in exported.text
    assert "source_saved=1" in exported.text
    for sensitive in (
        "不要导出此标题",
        "不要导出此正文",
        "不要导出此替代文字",
        "private-name.png",
    ):
        assert sensitive not in exported.text


def test_runtime_facts_remain_explainable_after_visual_edits_and_reopen(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    page_id, state = _review_plain_text_page(client, settings, monkeypatch)
    snapshot_id = str(state["current_snapshot"]["snapshot_id"])
    version_id = client.get(f"/api/v1/pages/{page_id}").json()["version_id"]
    first_sample = {
        "sample_id": "decision-before-edit",
        "page_id": page_id,
        "version_id": version_id,
        "stage": "page_decision",
        "duration_ms": 400,
    }
    assert client.post(
        "/api/v1/curation/runtime-facts/samples", json=first_sample
    ).status_code == 201

    created = client.post(
        f"/api/v1/pages/{page_id}/curation/visuals",
        headers={"X-Actor-ID": "curator-visual"},
        json={
            "base_snapshot_id": snapshot_id,
            "summary": "公开指标按月增长。",
            "visual_type": "chart",
            "bounds": {"left": 0.1, "top": 0.1, "width": 0.3, "height": 0.3},
        },
    )
    assert created.status_code == 201
    snapshot_id = created.json()["curation"]["current_snapshot"]["snapshot_id"]
    visual_ref = created.json()["annotation"]["visuals"][0]["visual_ref"]
    edited = client.patch(
        f"/api/v1/pages/{page_id}/curation/visuals/{visual_ref}",
        headers={"X-Actor-ID": "curator-visual"},
        json={
            "base_snapshot_id": snapshot_id,
            "summary": "公开指标在第一季度按月增长。",
            "visual_type": "chart",
            "bounds": {"left": 0.12, "top": 0.12, "width": 0.28, "height": 0.28},
        },
    )
    assert edited.status_code == 201
    snapshot_id = edited.json()["curation"]["current_snapshot"]["snapshot_id"]
    assert client.post(
        f"/api/v1/pages/{page_id}/approve",
        headers={"X-Actor-ID": "curator-visual"},
        json={"snapshot_id": snapshot_id},
    ).status_code == 200
    assert client.post(
        f"/api/v1/pages/{page_id}/reopen",
        headers={"X-Actor-ID": "curator-visual"},
    ).status_code == 200
    assert client.post(
        "/api/v1/curation/runtime-facts/samples",
        json={
            "sample_id": "decision-after-reopen",
            "page_id": page_id,
            "version_id": version_id,
            "stage": "page_decision",
            "duration_ms": 600,
        },
    ).status_code == 201

    page_fact = client.get("/api/v1/curation/runtime-facts").json()["pages"][0]
    assert page_fact["review_conclusion"] == "pending"
    assert page_fact["capture_visuals"] == 1
    assert page_fact["durations_ms"] == {
        "total": 1_000,
        "source_review": 0,
        "capture_annotation": 0,
        "page_decision": 1_000,
    }
    assert page_fact["actions"]["capture_created"] == 1
    assert page_fact["actions"]["capture_updated"] == 1
    assert page_fact["actions"]["page_approved"] == 1
    assert page_fact["actions"]["page_reopened"] == 1
