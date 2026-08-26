from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from pptextract.api import create_app
from pptextract.config import Settings
from pptextract.conversion import NormalizedImage, NormalizedPageContent
from pptextract.db import connect
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
    idempotency_key: str = "curation-review-source",
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
        image = Image.new("RGB", (100, 56))
        image.putdata(
            [(x, y, (x + y) % 256) for y in range(56) for x in range(100)]
        )
        encoded = BytesIO()
        image.save(encoded, format="PNG")
        return tuple(
            StandardPageRender(
                page_number=page.page_number,
                media_type="image/png",
                dpi=144,
                width_px=100,
                height_px=56,
                data=encoded.getvalue(),
            )
            for page in pages
        )

    monkeypatch.setattr("pptextract.ingest_workflow.render_standard_pages", render)
    accepted = client.post(
        "/api/v1/documents",
        headers={"Idempotency-Key": idempotency_key},
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
    page = next(
        candidate
        for candidate in client.get("/api/v1/curation/pages").json()["pages"]
        if candidate["document_id"] == accepted.json()["document_id"]
    )
    assert page["page_id"]
    return page


def test_pending_page_can_be_excluded_without_source_review_and_reopened_with_audit_history(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    page = _ingest_plain_text_page(client, settings, monkeypatch)
    page_id = str(page["page_id"])

    excluded = client.post(
        f"/api/v1/pages/{page_id}/exclude",
        headers={"X-Actor-ID": "curator-exclude"},
        json={"reason": "irrelevant", "note": "与当前知识库无关。"},
    )

    assert excluded.status_code == 200
    assert excluded.json()["review"] == {
        "status": "excluded",
        "reviewed_by": "curator-exclude",
        "reviewed_at": excluded.json()["review"]["reviewed_at"],
        "source_version_id": page["version_id"],
        "inherited_from_page_version_id": None,
        "snapshot_id": None,
        "exclusion_reason": "irrelevant",
        "exclusion_note": "与当前知识库无关。",
    }
    detail = client.get(f"/api/v1/pages/{page_id}").json()
    assert detail["review"]["status"] == "excluded"
    assert detail["review"]["exclusion_reason"] == "irrelevant"

    frozen = client.post(
        f"/api/v1/pages/{page_id}/exclude",
        json={"reason": "duplicate", "note": None},
    )
    assert frozen.status_code == 409
    assert frozen.json()["error"]["code"] == "page_not_pending"

    reopened = client.post(
        f"/api/v1/pages/{page_id}/reopen",
        headers={"X-Actor-ID": "curator-reopen"},
    )
    assert reopened.status_code == 200
    assert reopened.json()["review"] == {
        "status": "pending",
        "reviewed_by": None,
        "reviewed_at": None,
        "source_version_id": None,
        "inherited_from_page_version_id": None,
        "snapshot_id": None,
        "exclusion_reason": None,
        "exclusion_note": None,
    }
    assert client.get(f"/api/v1/pages/{page_id}").json()["review"]["status"] == "pending"

    with connect(settings) as connection:
        events = connection.execute(
            """
            SELECT event_type, actor_id, reason, note
            FROM page_review_events
            WHERE page_version_id = (
                SELECT page_version_id FROM page_versions WHERE page_id = ?
            )
            ORDER BY occurred_at, rowid
            """,
            (page_id,),
        ).fetchall()
    assert [dict(event) for event in events] == [
        {
            "event_type": "excluded",
            "actor_id": "curator-exclude",
            "reason": "irrelevant",
            "note": "与当前知识库无关。",
        },
        {
            "event_type": "reopened",
            "actor_id": "curator-reopen",
            "reason": None,
            "note": None,
        },
    ]


def test_batch_exclusion_reports_each_page_and_keeps_conflicts_visible(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    first = _ingest_plain_text_page(
        client,
        settings,
        monkeypatch,
        idempotency_key="curation-batch-first",
    )
    second = _ingest_plain_text_page(
        client,
        settings,
        monkeypatch,
        idempotency_key="curation-batch-second",
    )
    first_id = str(first["page_id"])
    second_id = str(second["page_id"])
    already_frozen = client.post(
        f"/api/v1/pages/{second_id}/exclude",
        json={"reason": "duplicate", "note": None},
    )
    assert already_frozen.status_code == 200

    batched = client.post(
        "/api/v1/pages/batch-exclude",
        headers={"X-Actor-ID": "curator-batch"},
        json={
            "page_ids": [first_id, second_id],
            "reason": "no_meaningful_content",
            "note": "统一批次处置。",
        },
    )

    assert batched.status_code == 207
    assert batched.json() == {
        "requested": 2,
        "excluded": [first_id],
        "failed": [
            {
                "page_id": second_id,
                "code": "page_not_pending",
                "message": "此页已不再是待处理状态。",
            }
        ],
        "complete": False,
    }
    with connect(settings) as connection:
        rows = connection.execute(
            """
            SELECT pv.page_id, events.event_type, events.actor_id, events.reason
            FROM page_review_events AS events
            JOIN page_versions AS pv ON pv.page_version_id = events.page_version_id
            WHERE pv.page_id IN (?, ?)
            ORDER BY pv.page_id, events.occurred_at, events.rowid
            """,
            (first_id, second_id),
        ).fetchall()
    by_page = {
        page_id: [dict(row) for row in rows if row["page_id"] == page_id]
        for page_id in (first_id, second_id)
    }
    assert by_page[first_id] == [
        {
            "page_id": first_id,
            "event_type": "excluded",
            "actor_id": "curator-batch",
            "reason": "no_meaningful_content",
        }
    ]
    assert len(by_page[second_id]) == 1
    assert by_page[second_id][0]["reason"] == "duplicate"


def _review_plain_text_page(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, dict[str, object]]:
    page = _ingest_plain_text_page(client, settings, monkeypatch)
    page_id = str(page["page_id"])
    state = client.post(
        f"/api/v1/pages/{page_id}/curation/snapshots",
        json={
            "base_snapshot_id": None,
            "titles": ["公开来源标题"],
            "body": ["公开来源正文。"],
        },
    ).json()["curation"]
    state = client.post(
        f"/api/v1/pages/{page_id}/curation/source-confirmation",
        json={"snapshot_id": state["current_snapshot"]["snapshot_id"]},
    ).json()["curation"]
    state = client.post(
        f"/api/v1/pages/{page_id}/curation/source-review",
        json={"snapshot_id": state["current_snapshot"]["snapshot_id"]},
    ).json()["curation"]
    return page_id, state


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
        "image_sources": {"total": 0, "unresolved": 0, "items": []},
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
    assert saved["image_sources"]["total"] == 1
    assert saved["image_sources"]["unresolved"] == 1
    assert saved["image_sources"]["items"][0]["disposition"] is None
    assert [blocker["code"] for blocker in saved["blockers"]] == [
        "source_unconfirmed",
        "source_review_incomplete",
        "image_disposition_required",
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


def test_page_detail_exposes_stable_ordered_image_source_identity_and_duplicates(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    shared_bytes = b"shared-public-image"
    active_svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    page = _ingest_plain_text_page(
        client,
        settings,
        monkeypatch,
        content=NormalizedPageContent(
            titles=("含重复图片来源",),
            body=("逐项处置契约。",),
            tables=(),
            images=(
                NormalizedImage(
                    reference_index=0,
                    alt_text="第一处引用",
                    media_type="image/png",
                    origin_part="ppt/media/shared.png",
                    data=shared_bytes,
                ),
                NormalizedImage(
                    reference_index=1,
                    alt_text="第二处引用",
                    media_type="image/png",
                    origin_part="ppt/media/shared.png",
                    data=shared_bytes,
                ),
                NormalizedImage(
                    reference_index=2,
                    alt_text="独立引用",
                    media_type="image/svg+xml",
                    origin_part="ppt/media/active.svg",
                    data=active_svg,
                ),
            ),
            speaker_notes=(),
        ),
    )
    page_id = str(page["page_id"])

    first = client.get(f"/api/v1/pages/{page_id}").json()["curation"][
        "image_sources"
    ]
    second = client.get(f"/api/v1/pages/{page_id}").json()["curation"][
        "image_sources"
    ]

    assert first["total"] == 3
    assert first["unresolved"] == 3
    assert [item["reference_index"] for item in first["items"]] == [0, 1, 2]
    assert [item["source_ref"] for item in first["items"]] == [
        item["source_ref"] for item in second["items"]
    ]
    assert len(set(item["source_ref"] for item in first["items"])) == 3
    assert first["items"][0]["object_sha256"] == first["items"][1][
        "object_sha256"
    ]
    assert first["items"][0]["duplicate_object"] is True
    assert first["items"][1]["duplicate_object"] is True
    assert first["items"][2]["duplicate_object"] is False
    assert first["items"][0]["origin_part"] == "ppt/media/shared.png"
    assert first["items"][0]["size_bytes"] == len(shared_bytes)
    assert first["items"][0]["preview_url"].endswith(
        f"/source-images/{first['items'][0]['source_ref']}"
    )
    preview = client.get(first["items"][0]["preview_url"])
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/png"
    assert "sandbox" in preview.headers["content-security-policy"]
    assert "default-src 'none'" in preview.headers["content-security-policy"]
    assert preview.content == shared_bytes
    active_preview = client.get(first["items"][2]["preview_url"])
    assert active_preview.status_code == 200
    assert active_preview.headers["content-type"] == "image/svg+xml"
    assert "sandbox" in active_preview.headers["content-security-policy"]
    assert active_preview.content == active_svg
    with connect(settings) as connection:
        source_rows = connection.execute(
            """
            SELECT reference_index, object_sha256, size_bytes
            FROM page_version_image_sources ORDER BY position
            """
        ).fetchall()
    assert [row["reference_index"] for row in source_rows] == [0, 1, 2]
    assert source_rows[0]["object_sha256"] == source_rows[1]["object_sha256"]
    assert source_rows[2]["size_bytes"] == len(active_svg)


def test_included_image_source_keeps_visual_identity_and_original_object_bytes(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    original_bytes = b"public-original-image-bytes"
    page = _ingest_plain_text_page(
        client,
        settings,
        monkeypatch,
        content=NormalizedPageContent(
            titles=("保留原始图片",),
            body=("正文已完整。",),
            tables=(),
            images=(
                NormalizedImage(
                    reference_index=0,
                    alt_text="公开原始图片",
                    media_type="image/jpeg",
                    origin_part="ppt/media/image1.jpg",
                    data=original_bytes,
                ),
            ),
            speaker_notes=(),
        ),
    )
    page_id = str(page["page_id"])
    state = client.post(
        f"/api/v1/pages/{page_id}/curation/snapshots",
        json={
            "base_snapshot_id": None,
            "titles": ["保留原始图片"],
            "body": ["正文已完整。"],
        },
    ).json()["curation"]
    snapshot_id = state["current_snapshot"]["snapshot_id"]
    source_ref = state["image_sources"]["items"][0]["source_ref"]
    confirmed = client.post(
        f"/api/v1/pages/{page_id}/curation/source-confirmation",
        headers={"X-Actor-ID": "curator-text"},
        json={"snapshot_id": snapshot_id},
    ).json()["curation"]

    incomplete = client.post(
        f"/api/v1/pages/{page_id}/curation/image-sources/{source_ref}",
        headers={"X-Actor-ID": "curator-image"},
        json={
            "base_snapshot_id": confirmed["current_snapshot"]["snapshot_id"],
            "disposition": "included",
            "summary": " ",
        },
    )
    assert incomplete.status_code == 422
    assert incomplete.json()["error"]["code"] == "image_summary_required"

    completed = client.post(
        f"/api/v1/pages/{page_id}/curation/image-sources/{source_ref}",
        headers={"X-Actor-ID": "curator-image"},
        json={
            "base_snapshot_id": confirmed["current_snapshot"]["snapshot_id"],
            "disposition": "included",
            "summary": "蓝色包装正面展示公开产品名称与容量信息。",
        },
    )
    assert completed.status_code == 201
    completed_state = completed.json()["curation"]
    completed_item = completed_state["image_sources"]["items"][0]
    assert completed_item["visual_ref"]
    assert completed_item["summary"] == "蓝色包装正面展示公开产品名称与容量信息。"
    assert completed_state["image_sources"]["unresolved"] == 0
    assert [blocker["code"] for blocker in completed_state["blockers"]] == [
        "source_review_incomplete"
    ]

    revised = client.post(
        f"/api/v1/pages/{page_id}/curation/image-sources/{source_ref}",
        headers={"X-Actor-ID": "curator-image"},
        json={
            "base_snapshot_id": completed_state["current_snapshot"]["snapshot_id"],
            "disposition": "included",
            "summary": "蓝色包装正面完整展示公开产品名称、类别与容量信息。",
        },
    )
    assert revised.status_code == 201
    completed_state = revised.json()["curation"]
    revised_item = completed_state["image_sources"]["items"][0]
    assert revised_item["visual_ref"] == completed_item["visual_ref"]
    assert revised_item["summary"] == "蓝色包装正面完整展示公开产品名称、类别与容量信息。"

    object_path = settings.object_store_path / revised_item["object_sha256"][:2] / (
        revised_item["object_sha256"]
    )
    assert object_path.read_bytes() == original_bytes

    with connect(settings) as connection:
        row = connection.execute(
            "SELECT source_content_json FROM page_versions WHERE page_id = ?",
            (page_id,),
        ).fetchone()
        assert row is not None
        corrupted = json.loads(str(row["source_content_json"]))
        corrupted["images"][0]["data_base64"] = base64.b64encode(
            b"corrupted-after-decision"
        ).decode("ascii")
        connection.execute(
            "UPDATE page_versions SET source_content_json = ? WHERE page_id = ?",
            (json.dumps(corrupted), page_id),
        )

    corrupted_state = client.get(f"/api/v1/pages/{page_id}").json()["curation"]
    corrupted_item = corrupted_state["image_sources"]["items"][0]
    assert corrupted_item["integrity"] == "hash_mismatch"
    assert corrupted_state["blockers"][-1] == {
        "code": "image_hash_mismatch",
        "message": "图片来源 01：原始字节与已记录哈希不一致。",
        "source_ref": source_ref,
    }
    rejected = client.post(
        f"/api/v1/pages/{page_id}/curation/image-sources/{source_ref}",
        json={
            "base_snapshot_id": corrupted_state["current_snapshot"]["snapshot_id"],
            "disposition": "included",
            "summary": "不得用已篡改字节覆盖原始资产。",
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "source_image_hash_mismatch"


def test_mixed_image_dispositions_require_explicit_review_and_freeze_auditable_sources(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    kept_bytes = b"kept-original-png"
    ignored_bytes = b"ignored-original-png"
    page = _ingest_plain_text_page(
        client,
        settings,
        monkeypatch,
        content=NormalizedPageContent(
            titles=("混合图片处置",),
            body=("正文可形成 Chunk。",),
            tables=(),
            images=(
                NormalizedImage(0, "保留项", "image/png", "ppt/media/keep.png", kept_bytes),
                NormalizedImage(1, "忽略项", "image/png", "ppt/media/ignore.png", ignored_bytes),
            ),
            speaker_notes=(),
        ),
    )
    page_id = str(page["page_id"])
    state = client.post(
        f"/api/v1/pages/{page_id}/curation/snapshots",
        json={
            "base_snapshot_id": None,
            "titles": ["混合图片处置"],
            "body": ["正文可形成 Chunk。"],
        },
    ).json()["curation"]
    state = client.post(
        f"/api/v1/pages/{page_id}/curation/source-confirmation",
        headers={"X-Actor-ID": "curator-text"},
        json={"snapshot_id": state["current_snapshot"]["snapshot_id"]},
    ).json()["curation"]
    kept_ref, ignored_ref = [
        item["source_ref"] for item in state["image_sources"]["items"]
    ]
    state = client.post(
        f"/api/v1/pages/{page_id}/curation/image-sources/{kept_ref}",
        headers={"X-Actor-ID": "curator-keep"},
        json={
            "base_snapshot_id": state["current_snapshot"]["snapshot_id"],
            "disposition": "included",
            "summary": "公开示意图展示两个阶段之间的单向关系。",
        },
    ).json()["curation"]
    incomplete = client.post(
        f"/api/v1/pages/{page_id}/curation/image-sources/{ignored_ref}",
        headers={"X-Actor-ID": "curator-ignore"},
        json={
            "base_snapshot_id": state["current_snapshot"]["snapshot_id"],
            "disposition": "ignored",
            "ignore_reason": "other",
            "ignore_note": "",
        },
    )
    assert incomplete.status_code == 422
    assert incomplete.json()["error"]["code"] == "image_other_note_required"
    assert client.post(
        f"/api/v1/pages/{page_id}/curation/source-review",
        json={"snapshot_id": state["current_snapshot"]["snapshot_id"]},
    ).status_code == 409

    completed = client.post(
        f"/api/v1/pages/{page_id}/curation/image-sources/{ignored_ref}",
        headers={"X-Actor-ID": "curator-ignore"},
        json={
            "base_snapshot_id": state["current_snapshot"]["snapshot_id"],
            "disposition": "ignored",
            "ignore_reason": "other",
            "ignore_note": "图片已损坏，无法在浏览器或源文件中核验。",
        },
    ).json()["curation"]
    assert completed["image_sources"]["unresolved"] == 0
    assert completed["current_snapshot"]["source_review"] is None
    assert completed["can_approve"] is False

    reviewed = client.post(
        f"/api/v1/pages/{page_id}/curation/source-review",
        headers={"X-Actor-ID": "curator-review"},
        json={"snapshot_id": completed["current_snapshot"]["snapshot_id"]},
    )
    assert reviewed.status_code == 200
    reviewed_state = reviewed.json()["curation"]
    assert reviewed_state["blockers"] == []
    approved = client.post(
        f"/api/v1/pages/{page_id}/approve",
        headers={"X-Actor-ID": "curator-approve"},
        json={"snapshot_id": reviewed_state["current_snapshot"]["snapshot_id"]},
    )
    assert approved.status_code == 200

    frozen = client.get(f"/api/v1/pages/{page_id}").json()
    decisions = frozen["curation"]["current_snapshot"]["image_source_decisions"]
    assert [decision["disposition"] for decision in decisions] == ["included", "ignored"]
    assert decisions[0]["decided_by"] == "curator-keep"
    assert decisions[1]["ignore_reason"] == "other"
    assert decisions[1]["decided_by"] == "curator-ignore"
    assert frozen["annotation"]["visuals"] == [
        {
            "visual_ref": decisions[0]["visual_ref"],
            "position": 0,
            "source_kind": "source_image",
            "disposition": "included",
            "summary": "公开示意图展示两个阶段之间的单向关系。",
            "visual_type": None,
            "bounds": None,
            "source_visual_ref": None,
            "confirmed": True,
            "source_image_ref": kept_ref,
            "asset": {
                "sha256": decisions[0]["object_sha256"],
                "media_type": "image/png",
                "size_bytes": len(kept_bytes),
                "byte_contract": "anydoc_original",
            },
        }
    ]


def test_text_change_invalidates_confirmations_without_losing_image_disposition(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    page = _ingest_plain_text_page(
        client,
        settings,
        monkeypatch,
        content=NormalizedPageContent(
            titles=("修改前标题",),
            body=("修改前正文。",),
            tables=(),
            images=(NormalizedImage(0, "来源图", "image/png", "ppt/media/a.png", b"a"),),
            speaker_notes=(),
        ),
    )
    page_id = str(page["page_id"])
    state = client.post(
        f"/api/v1/pages/{page_id}/curation/snapshots",
        json={
            "base_snapshot_id": None,
            "titles": ["修改前标题"],
            "body": ["修改前正文。"],
        },
    ).json()["curation"]
    state = client.post(
        f"/api/v1/pages/{page_id}/curation/source-confirmation",
        json={"snapshot_id": state["current_snapshot"]["snapshot_id"]},
    ).json()["curation"]
    source_ref = state["image_sources"]["items"][0]["source_ref"]
    state = client.post(
        f"/api/v1/pages/{page_id}/curation/image-sources/{source_ref}",
        json={
            "base_snapshot_id": state["current_snapshot"]["snapshot_id"],
            "disposition": "included",
            "summary": "一幅可公开验证的来源图。",
        },
    ).json()["curation"]
    visual_ref = state["image_sources"]["items"][0]["visual_ref"]
    state = client.post(
        f"/api/v1/pages/{page_id}/curation/source-review",
        json={"snapshot_id": state["current_snapshot"]["snapshot_id"]},
    ).json()["curation"]
    assert state["current_snapshot"]["source_review"] is not None

    changed = client.post(
        f"/api/v1/pages/{page_id}/curation/snapshots",
        json={
            "base_snapshot_id": state["current_snapshot"]["snapshot_id"],
            "titles": ["修改后标题"],
            "body": ["修改后正文。"],
        },
    )
    assert changed.status_code == 201
    changed_state = changed.json()["curation"]
    changed_snapshot = changed_state["current_snapshot"]
    assert changed_snapshot["source_confirmation"] is None
    assert changed_snapshot["source_review"] is None
    assert changed_state["image_sources"]["unresolved"] == 0
    assert changed_state["image_sources"]["items"][0]["disposition"] == "included"
    assert changed_state["image_sources"]["items"][0]["visual_ref"] == visual_ref


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


def test_first_capture_visual_is_cropped_from_standard_render_and_frozen_in_new_snapshot(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    page = _ingest_plain_text_page(client, settings, monkeypatch)
    page_id = str(page["page_id"])
    state = client.post(
        f"/api/v1/pages/{page_id}/curation/snapshots",
        json={
            "base_snapshot_id": None,
            "titles": ["公开来源标题"],
            "body": ["公开来源正文。"],
        },
    ).json()["curation"]
    state = client.post(
        f"/api/v1/pages/{page_id}/curation/source-confirmation",
        json={"snapshot_id": state["current_snapshot"]["snapshot_id"]},
    ).json()["curation"]
    state = client.post(
        f"/api/v1/pages/{page_id}/curation/source-review",
        json={"snapshot_id": state["current_snapshot"]["snapshot_id"]},
    ).json()["curation"]
    reviewed_snapshot_id = state["current_snapshot"]["snapshot_id"]

    missing_summary = client.post(
        f"/api/v1/pages/{page_id}/curation/visuals",
        json={
            "base_snapshot_id": reviewed_snapshot_id,
            "summary": "  ",
            "visual_type": "chart",
            "bounds": {"left": 0.105, "top": 0.241, "width": 0.199, "height": 0.268},
        },
    )
    assert missing_summary.status_code == 422
    assert missing_summary.json()["error"]["code"] == "visual_summary_required"

    invalid_type = client.post(
        f"/api/v1/pages/{page_id}/curation/visuals",
        json={
            "base_snapshot_id": reviewed_snapshot_id,
            "summary": "折线展示公开指标随月份稳步上升。",
            "visual_type": "future_unknown_type",
            "bounds": {"left": 0.105, "top": 0.241, "width": 0.199, "height": 0.268},
        },
    )
    assert invalid_type.status_code == 422
    assert invalid_type.json()["error"]["code"] == "invalid_visual_type"

    saved = client.post(
        f"/api/v1/pages/{page_id}/curation/visuals",
        headers={"X-Actor-ID": "curator-capture"},
        json={
            "base_snapshot_id": reviewed_snapshot_id,
            "summary": "折线展示公开指标随月份稳步上升。",
            "visual_type": "chart",
            "bounds": {"left": 0.105, "top": 0.241, "width": 0.199, "height": 0.268},
        },
    )
    assert saved.status_code == 201
    saved_state = saved.json()["curation"]
    assert saved_state["current_snapshot"]["snapshot_id"] != reviewed_snapshot_id
    assert saved_state["current_snapshot"]["source_snapshot_id"] == reviewed_snapshot_id
    assert saved_state["current_snapshot"]["source_confirmation"] is not None
    assert saved_state["current_snapshot"]["source_review"] is not None
    assert saved_state["blockers"] == []
    assert saved_state["can_approve"] is True

    detail = client.get(f"/api/v1/pages/{page_id}").json()
    assert detail["annotation"]["visuals"] == [
        {
            "visual_ref": detail["annotation"]["visuals"][0]["visual_ref"],
            "position": 0,
            "source_kind": "capture",
            "disposition": "included",
            "summary": "折线展示公开指标随月份稳步上升。",
            "visual_type": "chart",
            "bounds": {"height": 0.268, "left": 0.105, "top": 0.241, "width": 0.199},
            "source_visual_ref": None,
            "confirmed": True,
            "asset": {
                "sha256": detail["annotation"]["visuals"][0]["asset"]["sha256"],
                "media_type": "image/png",
                "size_bytes": detail["annotation"]["visuals"][0]["asset"]["size_bytes"],
                "width_px": 37,
                "height_px": 32,
                "byte_contract": "standard_render_crop",
            },
        }
    ]
    visual = detail["annotation"]["visuals"][0]
    assert len(visual["visual_ref"]) == 32
    assert visual["visual_ref"] != "01"
    asset_sha256 = visual["asset"]["sha256"]
    asset_path = settings.object_store_path / asset_sha256[:2] / asset_sha256
    asset_bytes = asset_path.read_bytes()
    assert hashlib.sha256(asset_bytes).hexdigest() == asset_sha256
    with Image.open(BytesIO(asset_bytes)) as cropped:
        assert cropped.format == "PNG"
        assert cropped.size == (37, 32)
        assert cropped.getpixel((0, 0)) == (2, 5, 7)
        assert cropped.getpixel((36, 31)) == (38, 36, 74)

    with connect(settings) as connection:
        connection.execute(
            """
            UPDATE curation_snapshot_visuals
            SET summary = ''
            WHERE snapshot_id = ? AND visual_ref = ?
            """,
            (
                saved_state["current_snapshot"]["snapshot_id"],
                visual["visual_ref"],
            ),
        )

    corrupted_state = client.get(f"/api/v1/pages/{page_id}").json()["curation"]
    assert corrupted_state["can_approve"] is False
    assert corrupted_state["blockers"] == [
        {
            "code": "visual_summary_required",
            "message": "视觉对象 01：缺少 summary。",
            "visual_ref": visual["visual_ref"],
        }
    ]
    blocked_approval = client.post(
        f"/api/v1/pages/{page_id}/approve",
        headers={"X-Actor-ID": "curator-approver"},
        json={"snapshot_id": saved_state["current_snapshot"]["snapshot_id"]},
    )
    assert blocked_approval.status_code == 409
    assert blocked_approval.json()["error"]["code"] == "approval_blocked"


def test_capture_visuals_can_be_added_and_edited_without_changing_identity(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    page_id, state = _review_plain_text_page(client, settings, monkeypatch)
    reviewed_snapshot_id = str(state["current_snapshot"]["snapshot_id"])

    first = client.post(
        f"/api/v1/pages/{page_id}/curation/visuals",
        json={
            "base_snapshot_id": reviewed_snapshot_id,
            "summary": "第一幅图展示公开指标的月度趋势。",
            "visual_type": "chart",
            "bounds": {"left": 0.1, "top": 0.1, "width": 0.3, "height": 0.3},
        },
    )
    assert first.status_code == 201
    first_payload = first.json()
    first_snapshot_id = first_payload["curation"]["current_snapshot"]["snapshot_id"]
    assert first_payload["annotation"]["snapshot_id"] == first_snapshot_id
    first_visual = client.get(f"/api/v1/pages/{page_id}").json()["annotation"][
        "visuals"
    ][0]

    second = client.post(
        f"/api/v1/pages/{page_id}/curation/visuals",
        json={
            "base_snapshot_id": first_snapshot_id,
            "summary": "第二幅图展示公开地区分布。",
            "visual_type": "map",
            "bounds": {"left": 0.5, "top": 0.2, "width": 0.3, "height": 0.4},
        },
    )
    assert second.status_code == 201
    second_payload = second.json()
    second_snapshot_id = second_payload["curation"]["current_snapshot"]["snapshot_id"]
    assert second_payload["annotation"]["snapshot_id"] == second_snapshot_id
    before_edit = client.get(f"/api/v1/pages/{page_id}").json()["annotation"][
        "visuals"
    ]
    assert [visual["position"] for visual in before_edit] == [0, 1]

    edited = client.patch(
        f"/api/v1/pages/{page_id}/curation/visuals/{first_visual['visual_ref']}",
        headers={"X-Actor-ID": "curator-editor"},
        json={
            "base_snapshot_id": second_snapshot_id,
            "summary": "第一幅图展示公开指标逐月稳步增长。",
            "visual_type": "diagram",
            "bounds": {"left": 0.12, "top": 0.14, "width": 0.28, "height": 0.26},
        },
    )
    assert edited.status_code == 201
    edited_payload = edited.json()
    edited_snapshot_id = edited_payload["curation"]["current_snapshot"]["snapshot_id"]
    assert edited_payload["annotation"]["snapshot_id"] == edited_snapshot_id
    assert edited_snapshot_id not in {
        reviewed_snapshot_id,
        first_snapshot_id,
        second_snapshot_id,
    }
    current_visuals = client.get(f"/api/v1/pages/{page_id}").json()["annotation"][
        "visuals"
    ]
    assert current_visuals[0]["visual_ref"] == first_visual["visual_ref"]
    assert current_visuals[0]["summary"] == "第一幅图展示公开指标逐月稳步增长。"
    assert current_visuals[0]["visual_type"] == "diagram"
    assert current_visuals[0]["bounds"] == {
        "height": 0.26,
        "left": 0.12,
        "top": 0.14,
        "width": 0.28,
    }
    assert current_visuals[1] == before_edit[1]

    stale_edit = client.patch(
        f"/api/v1/pages/{page_id}/curation/visuals/{first_visual['visual_ref']}",
        json={
            "base_snapshot_id": second_snapshot_id,
            "summary": "不应覆盖已保存修改。",
            "visual_type": None,
            "bounds": {"left": 0.2, "top": 0.2, "width": 0.2, "height": 0.2},
        },
    )
    assert stale_edit.status_code == 409
    assert stale_edit.json()["error"]["code"] == "curation_snapshot_stale"
    assert client.get(f"/api/v1/pages/{page_id}").json()["annotation"]["visuals"] == current_visuals

    with connect(settings) as connection:
        historical = connection.execute(
            """
            SELECT summary, visual_type, bounds_json
            FROM curation_snapshot_visuals
            WHERE snapshot_id = ? AND visual_ref = ?
            """,
            (second_snapshot_id, first_visual["visual_ref"]),
        ).fetchone()
    assert historical is not None
    assert historical["summary"] == "第一幅图展示公开指标的月度趋势。"
    assert historical["visual_type"] == "chart"

    approved = client.post(
        f"/api/v1/pages/{page_id}/approve",
        json={"snapshot_id": edited_snapshot_id},
    )
    assert approved.status_code == 200
    frozen = client.patch(
        f"/api/v1/pages/{page_id}/curation/visuals/{first_visual['visual_ref']}",
        json={
            "base_snapshot_id": edited_snapshot_id,
            "summary": "冻结后不得修改。",
            "visual_type": None,
            "bounds": {"left": 0.1, "top": 0.1, "width": 0.2, "height": 0.2},
        },
    )
    assert frozen.status_code == 409
    assert frozen.json()["error"]["code"] == "page_not_pending"


def test_capture_visuals_can_be_reordered_and_failed_move_keeps_saved_numbering(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    page_id, state = _review_plain_text_page(client, settings, monkeypatch)
    snapshot_id = str(state["current_snapshot"]["snapshot_id"])
    for summary in ("第一幅人工截图。", "第二幅人工截图。", "第三幅人工截图。"):
        response = client.post(
            f"/api/v1/pages/{page_id}/curation/visuals",
            json={
                "base_snapshot_id": snapshot_id,
                "summary": summary,
                "visual_type": None,
                "bounds": {"left": 0.1, "top": 0.1, "width": 0.2, "height": 0.2},
            },
        )
        assert response.status_code == 201
        snapshot_id = response.json()["curation"]["current_snapshot"]["snapshot_id"]

    before = client.get(f"/api/v1/pages/{page_id}").json()["annotation"]["visuals"]
    moved_ref = before[2]["visual_ref"]
    moved = client.post(
        f"/api/v1/pages/{page_id}/curation/visuals/{moved_ref}/move",
        json={"base_snapshot_id": snapshot_id, "direction": "up"},
    )
    assert moved.status_code == 201
    moved_payload = moved.json()
    moved_snapshot_id = moved_payload["curation"]["current_snapshot"]["snapshot_id"]
    assert moved_payload["annotation"]["snapshot_id"] == moved_snapshot_id
    after = client.get(f"/api/v1/pages/{page_id}").json()["annotation"]["visuals"]
    assert [visual["visual_ref"] for visual in after] == [
        before[0]["visual_ref"],
        before[2]["visual_ref"],
        before[1]["visual_ref"],
    ]
    assert [visual["position"] for visual in after] == [0, 1, 2]

    failed = client.post(
        f"/api/v1/pages/{page_id}/curation/visuals/{moved_ref}/move",
        json={"base_snapshot_id": snapshot_id, "direction": "up"},
    )
    assert failed.status_code == 409
    assert failed.json()["error"]["code"] == "curation_snapshot_stale"
    unchanged = client.get(f"/api/v1/pages/{page_id}").json()["annotation"]["visuals"]
    assert unchanged == after
    assert unchanged[1]["visual_ref"] == moved_ref
    assert moved_snapshot_id != snapshot_id


def test_deleting_last_capture_keeps_gap_blocked_until_source_is_marked_complete(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    page_id, state = _review_plain_text_page(client, settings, monkeypatch)
    reviewed_snapshot_id = str(state["current_snapshot"]["snapshot_id"])
    created = client.post(
        f"/api/v1/pages/{page_id}/curation/visuals",
        json={
            "base_snapshot_id": reviewed_snapshot_id,
            "summary": "将被删除的人工截图。",
            "visual_type": "screenshot",
            "bounds": {"left": 0.2, "top": 0.2, "width": 0.4, "height": 0.4},
        },
    ).json()["curation"]
    created_snapshot_id = str(created["current_snapshot"]["snapshot_id"])
    visual_ref = client.get(f"/api/v1/pages/{page_id}").json()["annotation"][
        "visuals"
    ][0]["visual_ref"]

    deleted = client.request(
        "DELETE",
        f"/api/v1/pages/{page_id}/curation/visuals/{visual_ref}",
        headers={"X-Actor-ID": "curator-delete"},
        json={"base_snapshot_id": created_snapshot_id},
    )
    assert deleted.status_code == 201
    deleted_payload = deleted.json()
    deleted_state = deleted_payload["curation"]
    deleted_snapshot_id = deleted_state["current_snapshot"]["snapshot_id"]
    assert deleted_payload["annotation"]["snapshot_id"] == deleted_snapshot_id
    assert deleted_snapshot_id != created_snapshot_id
    assert deleted_state["can_approve"] is False
    assert deleted_state["blockers"] == [
        {
            "code": "capture_required",
            "message": "来源仍有缺口：请重新框选视觉对象，或明确改选来源完整。",
        }
    ]
    assert client.get(f"/api/v1/pages/{page_id}").json()["annotation"]["visuals"] == []

    stale_delete = client.request(
        "DELETE",
        f"/api/v1/pages/{page_id}/curation/visuals/{visual_ref}",
        json={"base_snapshot_id": created_snapshot_id},
    )
    assert stale_delete.status_code == 409
    assert stale_delete.json()["error"]["code"] == "curation_snapshot_stale"

    completed = client.post(
        f"/api/v1/pages/{page_id}/curation/source-completeness",
        json={"snapshot_id": deleted_snapshot_id},
    )
    assert completed.status_code == 201
    completed_payload = completed.json()
    completed_state = completed_payload["curation"]
    assert completed_payload["annotation"]["snapshot_id"] == completed_state[
        "current_snapshot"
    ]["snapshot_id"]
    assert completed_state["current_snapshot"]["snapshot_id"] != deleted_snapshot_id
    assert completed_state["blockers"] == []
    assert completed_state["can_approve"] is True

    with connect(settings) as connection:
        history = connection.execute(
            """
            SELECT snapshot_id, source_snapshot_id, capture_required
            FROM curation_snapshots
            WHERE page_version_id = (
                SELECT page_version_id FROM page_versions WHERE page_id = ?
            ) ORDER BY created_at, snapshot_id
            """,
            (page_id,),
        ).fetchall()
    assert len(history) == 4
    assert [row["capture_required"] for row in history[-2:]] == [1, 0]


def test_capture_position_does_not_conflict_when_an_ignored_image_is_later_included(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    page = _ingest_plain_text_page(
        client,
        settings,
        monkeypatch,
        content=NormalizedPageContent(
            titles=("先忽略后保留图片",),
            body=("框选视觉应保留独立顺序。",),
            tables=(),
            images=(
                NormalizedImage(
                    0,
                    "可恢复的来源图片",
                    "image/png",
                    "ppt/media/restored.png",
                    b"restored-source-image",
                ),
            ),
            speaker_notes=(),
        ),
    )
    page_id = str(page["page_id"])
    state = client.post(
        f"/api/v1/pages/{page_id}/curation/snapshots",
        json={
            "base_snapshot_id": None,
            "titles": ["先忽略后保留图片"],
            "body": ["框选视觉应保留独立顺序。"],
        },
    ).json()["curation"]
    state = client.post(
        f"/api/v1/pages/{page_id}/curation/source-confirmation",
        json={"snapshot_id": state["current_snapshot"]["snapshot_id"]},
    ).json()["curation"]
    source_ref = state["image_sources"]["items"][0]["source_ref"]
    state = client.post(
        f"/api/v1/pages/{page_id}/curation/image-sources/{source_ref}",
        json={
            "base_snapshot_id": state["current_snapshot"]["snapshot_id"],
            "disposition": "ignored",
            "ignore_reason": "decorative",
        },
    ).json()["curation"]
    state = client.post(
        f"/api/v1/pages/{page_id}/curation/source-review",
        json={"snapshot_id": state["current_snapshot"]["snapshot_id"]},
    ).json()["curation"]
    state = client.post(
        f"/api/v1/pages/{page_id}/curation/visuals",
        json={
            "base_snapshot_id": state["current_snapshot"]["snapshot_id"],
            "summary": "页面中的关键示意图补充了正文未表达的结构关系。",
            "visual_type": "diagram",
            "bounds": {"left": 0.1, "top": 0.1, "width": 0.4, "height": 0.4},
        },
    ).json()["curation"]

    restored = client.post(
        f"/api/v1/pages/{page_id}/curation/image-sources/{source_ref}",
        json={
            "base_snapshot_id": state["current_snapshot"]["snapshot_id"],
            "disposition": "included",
            "summary": "来源图片展示了可验证的公开产品外观。",
        },
    )
    assert restored.status_code == 201
    visuals = client.get(f"/api/v1/pages/{page_id}").json()["annotation"][
        "visuals"
    ]
    assert [(visual["position"], visual["source_kind"]) for visual in visuals] == [
        (0, "source_image"),
        (1, "capture"),
    ]
