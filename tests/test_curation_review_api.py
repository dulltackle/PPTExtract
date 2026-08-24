from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pptextract.api import create_app
from pptextract.config import Settings
from pptextract.conversion import NormalizedImage, NormalizedPageContent
from pptextract.pptx_projection import SourcePage
from pptextract.rendering import StandardPageRender
from pptextract.worker import run_once
from tests.support.synthetic_pptx import build_plain_text_presentation

PPTX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@pytest.fixture
def system(tmp_path: Path) -> Iterator[tuple[TestClient, Settings]]:
    settings = replace(Settings.for_test(tmp_path), job_retry_base_seconds=0)
    with TestClient(create_app(settings)) as client:
        yield client, settings


def _ingest_plain_text_page(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    *,
    content: NormalizedPageContent | None = None,
) -> dict[str, object]:
    normalized = content or NormalizedPageContent(
        titles=("公开来源标题",),
        body=("公开来源正文。",),
        tables=(),
        images=(),
        speaker_notes=(),
    )

    monkeypatch.setattr(
        "pptextract.ingest_workflow.convert_page",
        lambda _source, _page: normalized,
    )

    def render(
        _source: bytes, *, toolchain: object, pages: tuple[SourcePage, ...]
    ) -> tuple[StandardPageRender, ...]:
        del toolchain
        return tuple(
            StandardPageRender(
                page_number=page.page_number,
                media_type="image/png",
                dpi=144,
                width_px=100,
                height_px=56,
                data=f"render-{page.page_number}".encode(),
            )
            for page in pages
        )

    monkeypatch.setattr("pptextract.ingest_workflow.render_standard_pages", render)
    accepted = client.post(
        "/api/v1/documents",
        headers={"Idempotency-Key": "curation-review-source"},
        files={
            "file": (
                "public-curation-review.pptx",
                build_plain_text_presentation(),
                PPTX_MEDIA_TYPE,
            )
        },
    )
    assert accepted.status_code == 202
    assert run_once(settings) is True
    page = client.get("/api/v1/curation/pages").json()["pages"][0]
    assert page["page_id"]
    return page


def test_plain_text_source_can_be_saved_confirmed_reviewed_and_approved_without_annotations(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    page = _ingest_plain_text_page(client, settings, monkeypatch)
    page_id = str(page["page_id"])

    initial = client.get(f"/api/v1/pages/{page_id}")
    assert initial.status_code == 200
    assert initial.json()["curation"] == {
        "current_snapshot": None,
        "image_sources": {"total": 0, "unresolved": 0},
        "chunk_body": {"nonempty": True},
        "blockers": [
            {"code": "source_unsaved", "message": "文字修改尚未保存。"},
            {"code": "source_unconfirmed", "message": "文字来源尚未确认。"},
            {"code": "source_review_incomplete", "message": "来源审核尚未完成。"},
        ],
        "can_confirm_source": False,
        "can_complete_source_review": False,
        "can_approve": False,
    }

    saved = client.post(
        f"/api/v1/pages/{page_id}/curation/snapshots",
        headers={"X-Actor-ID": "curator-zhang"},
        json={
            "base_snapshot_id": None,
            "titles": ["人工修订标题"],
            "body": ["人工修订后的正文。"],
        },
    )
    assert saved.status_code == 201
    snapshot = saved.json()["curation"]["current_snapshot"]
    assert snapshot["snapshot_id"]
    assert snapshot["source_content"]["titles"] == ["人工修订标题"]
    assert snapshot["source_content"]["body"] == ["人工修订后的正文。"]
    assert snapshot["created_by"] == "curator-zhang"
    assert snapshot["created_at"]
    assert snapshot["source_confirmation"] is None
    assert snapshot["source_review"] is None

    confirmed = client.post(
        f"/api/v1/pages/{page_id}/curation/source-confirmation",
        headers={"X-Actor-ID": "curator-zhang"},
        json={"snapshot_id": snapshot["snapshot_id"]},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["curation"]["current_snapshot"]["source_confirmation"][
        "actor_id"
    ] == "curator-zhang"
    assert confirmed.json()["curation"]["can_complete_source_review"] is True

    reviewed = client.post(
        f"/api/v1/pages/{page_id}/curation/source-review",
        headers={"X-Actor-ID": "curator-li"},
        json={"snapshot_id": snapshot["snapshot_id"]},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["curation"]["current_snapshot"]["source_review"][
        "actor_id"
    ] == "curator-li"
    assert reviewed.json()["curation"]["blockers"] == []
    assert reviewed.json()["curation"]["can_approve"] is True

    approved = client.post(
        f"/api/v1/pages/{page_id}/approve",
        headers={"X-Actor-ID": "curator-wang"},
        json={"snapshot_id": snapshot["snapshot_id"]},
    )
    assert approved.status_code == 200
    payload = approved.json()
    assert payload["review"] == {
        "status": "approved",
        "reviewed_by": "curator-wang",
        "reviewed_at": payload["review"]["reviewed_at"],
        "source_version_id": page["version_id"],
        "snapshot_id": snapshot["snapshot_id"],
    }
    assert payload["chunk_body"] == "人工修订标题\n\n人工修订后的正文。"
    assert client.get("/api/v1/curation/pages").json()["pages"] == []

    frozen = client.get(f"/api/v1/pages/{page_id}").json()
    assert frozen["review"]["reviewed_by"] == "curator-wang"
    assert frozen["review"]["source_version_id"] == page["version_id"]
    assert frozen["annotation"]["snapshot_id"] == snapshot["snapshot_id"]
    assert frozen["annotation"]["overview"] is None
    assert frozen["annotation"]["visuals"] == []


def test_saving_changed_source_creates_a_new_current_snapshot_and_invalidates_confirmation(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    page = _ingest_plain_text_page(client, settings, monkeypatch)
    page_id = str(page["page_id"])
    first = client.post(
        f"/api/v1/pages/{page_id}/curation/snapshots",
        json={
            "base_snapshot_id": None,
            "titles": ["第一版标题"],
            "body": ["第一版正文。"],
        },
    ).json()["curation"]["current_snapshot"]
    for route in ("source-confirmation", "source-review"):
        response = client.post(
            f"/api/v1/pages/{page_id}/curation/{route}",
            json={"snapshot_id": first["snapshot_id"]},
        )
        assert response.status_code == 200

    second_response = client.post(
        f"/api/v1/pages/{page_id}/curation/snapshots",
        headers={"X-Actor-ID": "curator-li"},
        json={
            "base_snapshot_id": first["snapshot_id"],
            "titles": ["第二版标题"],
            "body": ["第二版正文。"],
        },
    )
    assert second_response.status_code == 201
    state = second_response.json()["curation"]
    second = state["current_snapshot"]
    assert second["snapshot_id"] != first["snapshot_id"]
    assert second["source_snapshot_id"] == first["snapshot_id"]
    assert second["source_confirmation"] is None
    assert second["source_review"] is None
    assert [blocker["code"] for blocker in state["blockers"]] == [
        "source_unconfirmed",
        "source_review_incomplete",
    ]

    stale_confirmation = client.post(
        f"/api/v1/pages/{page_id}/curation/source-confirmation",
        json={"snapshot_id": first["snapshot_id"]},
    )
    assert stale_confirmation.status_code == 409
    assert stale_confirmation.json()["error"]["code"] == "curation_snapshot_stale"
    detail = client.get(f"/api/v1/pages/{page_id}").json()
    assert detail["curation"]["current_snapshot"]["source_content"]["titles"] == [
        "第二版标题"
    ]


def test_image_sources_remain_read_only_blockers_until_each_is_disposed(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    page = _ingest_plain_text_page(
        client,
        settings,
        monkeypatch,
        content=NormalizedPageContent(
            titles=("含图片来源",),
            body=("文字来源已完整。",),
            tables=(),
            images=(
                NormalizedImage(
                    reference_index=0,
                    alt_text="公开图片",
                    media_type="image/png",
                    origin_part="ppt/media/image1.png",
                    data=b"public-image",
                ),
            ),
            speaker_notes=(),
        ),
    )
    page_id = str(page["page_id"])
    saved = client.post(
        f"/api/v1/pages/{page_id}/curation/snapshots",
        json={
            "base_snapshot_id": None,
            "titles": ["含图片来源"],
            "body": ["文字来源已完整。"],
        },
    ).json()["curation"]
    snapshot_id = saved["current_snapshot"]["snapshot_id"]
    assert saved["image_sources"] == {"total": 1, "unresolved": 1}
    assert [blocker["code"] for blocker in saved["blockers"]] == [
        "source_unconfirmed",
        "source_review_incomplete",
        "image_sources_unresolved",
    ]

    confirmed = client.post(
        f"/api/v1/pages/{page_id}/curation/source-confirmation",
        json={"snapshot_id": snapshot_id},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["curation"]["can_complete_source_review"] is False
    reviewed = client.post(
        f"/api/v1/pages/{page_id}/curation/source-review",
        json={"snapshot_id": snapshot_id},
    )
    assert reviewed.status_code == 409
    assert reviewed.json()["error"]["code"] == "image_sources_unresolved"
    approved = client.post(
        f"/api/v1/pages/{page_id}/approve",
        json={"snapshot_id": snapshot_id},
    )
    assert approved.status_code == 409
    assert approved.json()["error"]["code"] == "approval_blocked"


def test_confirmed_empty_source_cannot_be_approved(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    page = _ingest_plain_text_page(
        client,
        settings,
        monkeypatch,
        content=NormalizedPageContent(
            titles=(), body=(), tables=(), images=(), speaker_notes=()
        ),
    )
    page_id = str(page["page_id"])
    saved = client.post(
        f"/api/v1/pages/{page_id}/curation/snapshots",
        json={"base_snapshot_id": None, "titles": [], "body": []},
    ).json()["curation"]
    snapshot_id = saved["current_snapshot"]["snapshot_id"]
    assert saved["chunk_body"] == {"nonempty": False}
    assert saved["blockers"][-1] == {
        "code": "chunk_body_empty",
        "message": "已确认来源无法生成非空 Chunk 正文。",
    }
    assert client.post(
        f"/api/v1/pages/{page_id}/curation/source-confirmation",
        json={"snapshot_id": snapshot_id},
    ).status_code == 200
    reviewed = client.post(
        f"/api/v1/pages/{page_id}/curation/source-review",
        json={"snapshot_id": snapshot_id},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["curation"]["blockers"] == [
        {
            "code": "chunk_body_empty",
            "message": "已确认来源无法生成非空 Chunk 正文。",
        }
    ]
    assert reviewed.json()["curation"]["can_approve"] is False
