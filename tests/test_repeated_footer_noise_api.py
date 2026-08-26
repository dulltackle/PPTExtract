from __future__ import annotations

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
from pptextract.conversion import NormalizedPageContent
from pptextract.db import connect
from pptextract.pptx_projection import SourcePage
from pptextract.rendering import StandardPageRender
from pptextract.worker import run_once
from tests.support.synthetic_pptx import build_repeated_footer_presentation

PPTX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@pytest.fixture
def system(tmp_path: Path) -> Iterator[tuple[TestClient, Settings]]:
    settings = replace(Settings.for_test(tmp_path), job_retry_base_seconds=0)
    with TestClient(create_app(settings)) as client:
        yield client, settings


def _ingest_repeated_footer_document(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, object]]:
    def convert(_source: bytes, page: SourcePage) -> NormalizedPageContent:
        return NormalizedPageContent(
            titles=(f"公开页 {page.page_number}",),
            body=(f"第 {page.page_number} 页独有正文", " 公开合成重复页脚\n"),
            tables=(),
            images=(),
            speaker_notes=(),
        )

    monkeypatch.setattr("pptextract.ingest_workflow.convert_page", convert)

    def render(
        _source: bytes, *, toolchain: object, pages: tuple[SourcePage, ...]
    ) -> tuple[StandardPageRender, ...]:
        del toolchain
        encoded = BytesIO()
        Image.new("RGB", (100, 56), "white").save(encoded, format="PNG")
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
        headers={"Idempotency-Key": "repeated-footer-noise"},
        files={
            "file": (
                "repeated-footer-noise.pptx",
                build_repeated_footer_presentation(),
                PPTX_MEDIA_TYPE,
            )
        },
    )
    assert accepted.status_code == 202
    assert run_once(settings) is True
    document_id = accepted.json()["document_id"]
    pages = client.get(
        "/api/v1/curation/pages", params={"review_status": "all"}
    ).json()["pages"]
    return [page for page in pages if page["document_id"] == document_id]


def test_candidate_confirmation_and_revoke_only_change_chunk_composition(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    pages = _ingest_repeated_footer_document(client, settings, monkeypatch)
    assert [page["page_number"] for page in pages] == [1, 2, 3]
    first_id = str(pages[0]["page_id"])
    detail_before = client.get(f"/api/v1/pages/{first_id}").json()
    footer_source = next(
        item
        for item in detail_before["curation"]["repeated_footer_noise"]["sources"]
        if item["text"].strip() == "公开合成重复页脚"
    )

    candidate = client.get(
        f"/api/v1/pages/{first_id}/repeated-footer-noise/candidates/"
        f"{footer_source['source_ref']}"
    )

    assert candidate.status_code == 200
    candidate_payload = candidate.json()
    assert candidate_payload["candidate"]["rule_version"] == "manual-exact-text-v1"
    assert candidate_payload["candidate"]["source_text"] == " 公开合成重复页脚\n"
    assert [item["page_number"] for item in candidate_payload["candidate"]["affected_pages"]] == [
        1,
        2,
        3,
    ]
    assert all(item["source_ref"] for item in candidate_payload["candidate"]["affected_pages"])

    # 候选只能供人查看，不能改变来源、页指纹、审核状态或正文。
    previewed = client.get(f"/api/v1/pages/{first_id}").json()
    assert previewed["source_content"] == detail_before["source_content"]
    assert previewed["fingerprint"] == detail_before["fingerprint"]
    assert previewed["review"] == detail_before["review"]
    assert previewed["curation"]["chunk_body"]["preview"].endswith("公开合成重复页脚")
    assert previewed["curation"]["chunk_metadata"]["excluded_repeated_footer_noise"] == []

    confirmed = client.post(
        f"/api/v1/pages/{first_id}/repeated-footer-noise/confirmations",
        headers={"X-Actor-ID": "curator-footer"},
        json={
            "candidate_id": candidate_payload["candidate"]["candidate_id"],
            "source_ref": footer_source["source_ref"],
            "note": "已在三页标准页渲染结果中核对。",
        },
    )

    assert confirmed.status_code == 201
    fact = confirmed.json()["confirmation"]
    assert fact["status"] == "active"
    assert fact["source_text"] == " 公开合成重复页脚\n"
    assert fact["normalized_text"] == "公开合成重复页脚"
    assert fact["actor_id"] == "curator-footer"
    assert fact["confirmed_at"]
    assert fact["rule_version"] == "manual-exact-text-v1"
    assert fact["note"] == "已在三页标准页渲染结果中核对。"
    assert [item["page_number"] for item in fact["affected_pages"]] == [1, 2, 3]

    for page in pages:
        detail = client.get(f"/api/v1/pages/{page['page_id']}").json()
        assert detail["review"] == {
            **detail_before["review"],
            "status": "pending",
        }
        assert detail["curation"]["chunk_body"]["preview"] == (
            f"公开页 {page['page_number']}\n\n第 {page['page_number']} 页独有正文"
        )
        metadata = detail["curation"]["chunk_metadata"][
            "excluded_repeated_footer_noise"
        ]
        assert metadata == [
            {
                "confirmation_id": fact["confirmation_id"],
                "source_ref": metadata[0]["source_ref"],
                "source_text": " 公开合成重复页脚\n",
                "rule_version": "manual-exact-text-v1",
                "confirmed_by": "curator-footer",
                "confirmed_at": fact["confirmed_at"],
            }
        ]
        assert detail["source_content"]["body"][-1] == " 公开合成重复页脚\n"

    saved = client.post(
        f"/api/v1/pages/{first_id}/curation/snapshots",
        json={
            "base_snapshot_id": None,
            "titles": ["公开页 1"],
            "body": ["第 1 页独有正文", " 公开合成重复页脚\n"],
        },
    ).json()["curation"]
    snapshot_id = saved["current_snapshot"]["snapshot_id"]
    for action in ("source-confirmation", "source-review"):
        response = client.post(
            f"/api/v1/pages/{first_id}/curation/{action}",
            json={"snapshot_id": snapshot_id},
        )
        assert response.status_code == 200
    approved = client.post(
        f"/api/v1/pages/{first_id}/approve",
        json={"snapshot_id": snapshot_id},
    )
    assert approved.status_code == 200
    assert approved.json()["chunk_body"] == "公开页 1\n\n第 1 页独有正文"
    assert approved.json()["chunk_metadata"]["excluded_repeated_footer_noise"][0][
        "confirmation_id"
    ] == fact["confirmation_id"]

    revoked = client.post(
        f"/api/v1/repeated-footer-noise/confirmations/{fact['confirmation_id']}/revoke",
        headers={"X-Actor-ID": "curator-revoke"},
        json={"note": "材料范围调整，恢复正文。"},
    )

    assert revoked.status_code == 200
    assert revoked.json()["confirmation"]["status"] == "revoked"
    restored = client.get(f"/api/v1/pages/{first_id}").json()
    assert restored["review"]["status"] == "approved"
    assert restored["curation"]["chunk_body"]["preview"].endswith("公开合成重复页脚")
    assert restored["curation"]["chunk_metadata"]["excluded_repeated_footer_noise"] == []
    history = restored["curation"]["repeated_footer_noise"]["history"]
    assert history == [
        {
            "confirmation_id": fact["confirmation_id"],
            "source_ref": history[0]["source_ref"],
            "source_text": " 公开合成重复页脚\n",
            "rule_version": "manual-exact-text-v1",
            "confirmation_note": "已在三页标准页渲染结果中核对。",
            "confirmed_by": "curator-footer",
            "confirmed_at": fact["confirmed_at"],
            "status": "revoked",
            "revoked_by": "curator-revoke",
            "revoked_at": history[0]["revoked_at"],
            "revoke_note": "材料范围调整，恢复正文。",
        }
    ]
    assert history[0]["revoked_at"]
    with connect(settings) as connection:
        events = connection.execute(
            """
            SELECT event_type, actor_id, note, occurred_at
            FROM repeated_footer_noise_events
            WHERE confirmation_id = ? ORDER BY occurred_at, rowid
            """,
            (fact["confirmation_id"],),
        ).fetchall()
        immutable_source = connection.execute(
            "SELECT source_content_json FROM page_versions WHERE page_id = ?",
            (first_id,),
        ).fetchone()["source_content_json"]
    assert [(event["event_type"], event["actor_id"], event["note"]) for event in events] == [
        ("confirmed", "curator-footer", "已在三页标准页渲染结果中核对。"),
        ("revoked", "curator-revoke", "材料范围调整，恢复正文。"),
    ]
    assert all(event["occurred_at"] for event in events)
    assert json.loads(immutable_source)["body"][-1] == " 公开合成重复页脚\n"
