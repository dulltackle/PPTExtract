from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pptextract.api import create_app
from pptextract.config import Settings
from pptextract.conversion import NormalizedPageContent
from pptextract.db import transaction
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


def _install_successful_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    def convert(_source: bytes, page: SourcePage) -> NormalizedPageContent:
        return NormalizedPageContent(
            titles=(f"公开生命周期页 {page.page_number}",),
            body=("用于验证版本与文档生命周期。",),
            tables=(),
            images=(),
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
                width_px=10,
                height_px=10,
                data=f"render-{page.page_number}".encode(),
            )
            for page in pages
        )

    monkeypatch.setattr("pptextract.ingest_workflow.convert_page", convert)
    monkeypatch.setattr("pptextract.ingest_workflow.render_standard_pages", render)


def _upload(
    client: TestClient,
    *,
    key: str,
    title: str,
    document_id: str | None = None,
) -> dict[str, str]:
    route = (
        "/api/v1/documents"
        if document_id is None
        else f"/api/v1/documents/{document_id}/versions"
    )
    response = client.post(
        route,
        headers={"Idempotency-Key": key},
        files={
            "file": (
                f"{key}.pptx",
                build_plain_text_presentation(title=title),
                PPTX_MEDIA_TYPE,
            )
        },
    )
    assert response.status_code == 202
    return response.json()


def _ready_version(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    *,
    key: str,
    title: str,
    document_id: str | None = None,
) -> dict[str, str]:
    _install_successful_toolchain(monkeypatch)
    accepted = _upload(client, key=key, title=title, document_id=document_id)
    assert run_once(settings) is True
    assert client.get(f"/api/v1/jobs/{accepted['job_id']}").json()["status"] == "succeeded"
    return accepted


def _command_headers(key: str, *, actor: str = "operator-zhang") -> dict[str, str]:
    return {"Idempotency-Key": key, "X-Actor-ID": actor}


def test_failed_version_retry_creates_a_new_version_and_preserves_provenance(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    failed = _upload(client, key="failed-source", title="失败来源")

    def invalid_conversion(*_args: object, **_kwargs: object) -> object:
        raise ValueError("synthetic permanent failure")

    monkeypatch.setattr("pptextract.ingest_workflow.convert_page", invalid_conversion)
    assert run_once(settings) is True
    failed_job = client.get(f"/api/v1/jobs/{failed['job_id']}").json()
    assert failed_job["status"] == "failed"
    assert failed_job["error"]["code"] == "conversion_failed"

    route = (
        f"/api/v1/documents/{failed['document_id']}"
        f"/versions/{failed['version_id']}/retry"
    )
    retried = client.post(
        route,
        headers=_command_headers("retry-failed"),
        json={"reason": "依赖已恢复，重新摄取"},
    )
    replayed = client.post(
        route,
        headers=_command_headers("retry-failed"),
        json={"reason": "依赖已恢复，重新摄取"},
    )
    conflicting = client.post(
        route,
        headers=_command_headers("retry-failed"),
        json={"reason": "同键但不同原因"},
    )

    assert retried.status_code == 202
    assert replayed.status_code == 202
    assert replayed.json() == retried.json()
    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["code"] == "idempotency_conflict"
    assert retried.json()["version_id"] != failed["version_id"]
    assert retried.json()["job_id"] != failed["job_id"]
    assert retried.json()["source_relation"] == {
        "operation": "retry",
        "source_version_id": failed["version_id"],
    }
    original = client.get(
        f"/api/v1/documents/{failed['document_id']}/versions/{failed['version_id']}"
    ).json()
    assert original["status"] == "failed"

    _install_successful_toolchain(monkeypatch)
    assert run_once(settings) is True
    document = client.get(f"/api/v1/documents/{failed['document_id']}").json()
    assert document["current_version_id"] == retried.json()["version_id"]

    events = client.get(f"/api/v1/documents/{failed['document_id']}/events").json()
    assert events["events"] == [
        {
            "event_id": events["events"][0]["event_id"],
            "type": "version_retried",
            "actor_id": "operator-zhang",
            "reason": "依赖已恢复，重新摄取",
            "version_id": retried.json()["version_id"],
            "source_version_id": failed["version_id"],
            "created_at": events["events"][0]["created_at"],
        }
    ]


def test_voiding_versions_cancels_active_work_and_falls_back_through_ready_history(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    first = _ready_version(
        client, settings, monkeypatch, key="ready-one", title="历史版本一"
    )
    second = _ready_version(
        client,
        settings,
        monkeypatch,
        key="ready-two",
        title="历史版本二",
        document_id=first["document_id"],
    )

    void_second = client.post(
        f"/api/v1/documents/{first['document_id']}/versions/{second['version_id']}/void",
        headers=_command_headers("void-second"),
        json={"reason": "版本归属错误"},
    )
    assert void_second.status_code == 200
    assert void_second.json()["status"] == "voided"
    assert void_second.json()["current_version_id"] == first["version_id"]

    void_first = client.post(
        f"/api/v1/documents/{first['document_id']}/versions/{first['version_id']}/void",
        headers=_command_headers("void-first"),
        json={"reason": "基线也不可用"},
    )
    assert void_first.status_code == 200
    assert void_first.json()["current_version_id"] is None
    assert client.get("/api/v1/curation/pages").json()["pages"] == []

    processing = _upload(
        client,
        key="processing-version",
        title="处理中版本",
        document_id=first["document_id"],
    )
    void_processing = client.post(
        f"/api/v1/documents/{first['document_id']}"
        f"/versions/{processing['version_id']}/void",
        headers=_command_headers("void-processing"),
        json={"reason": "上传到了错误文档"},
    )
    assert void_processing.status_code == 200
    assert client.get(f"/api/v1/jobs/{processing['job_id']}").json()["status"] == "cancelled"

    awaiting = _upload(
        client,
        key="awaiting-version",
        title="待对应版本",
        document_id=first["document_id"],
    )
    with transaction(settings) as connection:
        connection.execute(
            "UPDATE document_versions SET status = 'awaiting_mapping' WHERE version_id = ?",
            (awaiting["version_id"],),
        )
        connection.execute(
            "UPDATE jobs SET status = 'requires_action' WHERE job_id = ?",
            (awaiting["job_id"],),
        )
    void_awaiting = client.post(
        f"/api/v1/documents/{first['document_id']}/versions/{awaiting['version_id']}/void",
        headers=_command_headers("void-awaiting"),
        json={"reason": "对应目标不正确"},
    )
    assert void_awaiting.status_code == 200
    assert client.get(f"/api/v1/jobs/{awaiting['job_id']}").json()["status"] == "cancelled"


def test_rollback_creates_a_new_version_that_runs_the_normal_ingestion_flow(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    historical = _ready_version(
        client, settings, monkeypatch, key="rollback-source", title="回滚来源"
    )
    current = _ready_version(
        client,
        settings,
        monkeypatch,
        key="rollback-current",
        title="当前版本",
        document_id=historical["document_id"],
    )

    rolled_back = client.post(
        f"/api/v1/documents/{historical['document_id']}"
        f"/versions/{historical['version_id']}/rollback",
        headers=_command_headers("rollback-history", actor="operator-li"),
        json={"reason": "恢复已验证的历史内容"},
    )

    assert rolled_back.status_code == 202
    identity = rolled_back.json()
    assert identity["version_id"] not in {historical["version_id"], current["version_id"]}
    assert identity["source_relation"] == {
        "operation": "rollback",
        "source_version_id": historical["version_id"],
    }
    assert client.get(f"/api/v1/documents/{historical['document_id']}").json()[
        "current_version_id"
    ] == current["version_id"]

    _install_successful_toolchain(monkeypatch)
    assert run_once(settings) is True
    assert client.get(f"/api/v1/documents/{historical['document_id']}").json()[
        "current_version_id"
    ] == identity["version_id"]
    source = client.get(
        f"/api/v1/documents/{historical['document_id']}/versions/{historical['version_id']}"
    ).json()
    replacement = client.get(
        f"/api/v1/documents/{historical['document_id']}/versions/{identity['version_id']}"
    ).json()
    assert replacement["source"] == source["source"]
    assert replacement["source_relation"] == identity["source_relation"]


def test_soft_delete_and_restore_preserve_identity_current_version_and_audit_history(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    ready = _ready_version(
        client, settings, monkeypatch, key="delete-source", title="待软删文档"
    )
    page = client.get("/api/v1/curation/pages").json()["pages"][0]
    route = f"/api/v1/documents/{ready['document_id']}"
    pending_source = build_plain_text_presentation(title="软删前已接受的下一版本")
    pending_response = client.post(
        f"{route}/versions",
        headers={"Idempotency-Key": "pending-before-delete"},
        files={"file": ("pending.pptx", pending_source, PPTX_MEDIA_TYPE)},
    )
    assert pending_response.status_code == 202
    pending = pending_response.json()

    deleted = client.request(
        "DELETE",
        route,
        headers=_command_headers("delete-document"),
        json={"reason": "该文档暂不参与策展"},
    )
    replayed = client.request(
        "DELETE",
        route,
        headers=_command_headers("delete-document"),
        json={"reason": "该文档暂不参与策展"},
    )
    conflict = client.request(
        "DELETE",
        route,
        headers=_command_headers("delete-document"),
        json={"reason": "同键不同原因"},
    )

    assert deleted.status_code == 200
    assert replayed.json() == deleted.json()
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert deleted.json()["deleted"] is True
    assert deleted.json()["current_version_id"] == ready["version_id"]
    assert client.get("/api/v1/curation/pages").json()["pages"] == []
    assert client.get(f"/api/v1/pages/{page['page_id']}").status_code == 404
    assert client.get(f"/api/v1/jobs/{pending['job_id']}").json()["status"] == "cancelled"
    pending_version = client.get(
        f"{route}/versions/{pending['version_id']}"
    ).json()
    assert pending_version["status"] == "failed"
    assert run_once(settings) is False

    replayed_upload = client.post(
        f"{route}/versions",
        headers={"Idempotency-Key": "pending-before-delete"},
        files={"file": ("pending.pptx", pending_source, PPTX_MEDIA_TYPE)},
    )
    assert replayed_upload.status_code == 202
    assert replayed_upload.json() == pending

    rejected_upload = client.post(
        f"{route}/versions",
        headers={"Idempotency-Key": "upload-deleted"},
        files={
            "file": (
                "rejected.pptx",
                build_plain_text_presentation(title="不应接受"),
                PPTX_MEDIA_TYPE,
            )
        },
    )
    assert rejected_upload.status_code == 409
    assert rejected_upload.json()["error"]["code"] == "document_deleted"

    restored = client.post(
        f"{route}/restore",
        headers=_command_headers("restore-document", actor="operator-li"),
        json={"reason": "重新纳入策展"},
    )
    assert restored.status_code == 200
    assert restored.json()["deleted"] is False
    assert restored.json()["current_version_id"] == ready["version_id"]
    restored_page = client.get("/api/v1/curation/pages").json()["pages"][0]
    assert restored_page["page_id"] == page["page_id"]
    assert restored_page["chunk_id"] == page["chunk_id"]

    events = client.get(f"{route}/events").json()["events"]
    assert [(event["type"], event["actor_id"], event["reason"]) for event in events] == [
        ("document_deleted", "operator-zhang", "该文档暂不参与策展"),
        ("document_restored", "operator-li", "重新纳入策展"),
    ]


def test_lifecycle_commands_reject_invalid_states_and_missing_reasons(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    ready = _ready_version(
        client, settings, monkeypatch, key="state-ready", title="状态冲突"
    )
    retry_ready = client.post(
        f"/api/v1/documents/{ready['document_id']}/versions/{ready['version_id']}/retry",
        headers=_command_headers("retry-ready"),
        json={"reason": "ready 不能重试"},
    )
    rollback_current = client.post(
        f"/api/v1/documents/{ready['document_id']}"
        f"/versions/{ready['version_id']}/rollback",
        headers=_command_headers("rollback-current"),
        json={"reason": "当前版本不是历史版本"},
    )
    missing_reason = client.post(
        f"/api/v1/documents/{ready['document_id']}/versions/{ready['version_id']}/void",
        headers=_command_headers("void-without-reason"),
        json={"reason": ""},
    )

    assert retry_ready.status_code == 409
    assert retry_ready.json()["error"]["code"] == "version_state_conflict"
    assert rollback_current.status_code == 409
    assert rollback_current.json()["error"]["code"] == "version_state_conflict"
    assert missing_reason.status_code == 422
    assert missing_reason.json()["error"]["code"] == "invalid_request"
