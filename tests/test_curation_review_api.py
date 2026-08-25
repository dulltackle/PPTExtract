from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
