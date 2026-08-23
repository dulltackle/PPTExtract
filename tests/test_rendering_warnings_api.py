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
from pptextract.fingerprint import fingerprint_page as real_fingerprint_page
from pptextract.ingest_workflow import enqueue_stale_render_jobs
from pptextract.jobs import claim_next_job
from pptextract.pptx_projection import SourcePage
from pptextract.rendering import (
    DockerRenderingToolchain,
    RenderingWarning,
    StandardPageRender,
)
from pptextract.rendering_warnings import replace_active_warnings
from pptextract.worker import run_once
from tests.support.synthetic_pptx import (
    build_minimal_presentation,
    build_rendering_warning_presentation,
)

PPTX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@pytest.fixture
def system(tmp_path: Path) -> Iterator[tuple[TestClient, Settings]]:
    settings = replace(Settings.for_test(tmp_path), job_retry_base_seconds=0)
    with TestClient(create_app(settings)) as client:
        yield client, settings


def _stub_ingestion(monkeypatch: pytest.MonkeyPatch) -> None:
    def convert(_source: bytes, page: SourcePage) -> NormalizedPageContent:
        return NormalizedPageContent(
            titles=(f"公开渲染风险页 {page.page_number}",),
            body=("公开正文",),
            tables=(),
            images=(),
        )

    def render(
        _source: bytes, *, toolchain: object, pages: tuple[SourcePage, ...]
    ) -> tuple[StandardPageRender, ...]:
        del toolchain
        page = pages[0]
        return (
            StandardPageRender(
                page_number=page.page_number,
                media_type="image/png",
                dpi=144,
                width_px=100,
                height_px=56,
                data=f"render-{page.page_number}".encode(),
            ),
        )

    monkeypatch.setattr("pptextract.ingest_workflow.convert_page", convert)
    monkeypatch.setattr("pptextract.ingest_workflow.render_standard_pages", render)
    monkeypatch.setattr(
        "pptextract.ingest_workflow.audit_rendering_warnings",
        lambda *_args, **_kwargs: (
            RenderingWarning(
                code="missing_font",
                page_number=1,
                font_family="PPTExtract Missing Contract Font",
                replacement_font="Noto Sans",
            ),
            RenderingWarning(
                code="animation_flattened",
                page_number=2,
                timeline_count=1,
            ),
        ),
    )


def _ingest(client: TestClient, settings: Settings) -> dict[str, str]:
    response = client.post(
        "/api/v1/documents",
        headers={"Idempotency-Key": "rendering-warning-contract"},
        files={
            "file": (
                "rendering-warning-contract.pptx",
                build_rendering_warning_presentation(),
                PPTX_MEDIA_TYPE,
            )
        },
    )
    assert response.status_code == 202
    accepted = response.json()
    assert run_once(settings) is True
    return accepted


def test_warnings_remain_visible_and_auditable_without_blocking_ready(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    _stub_ingestion(monkeypatch)
    accepted = _ingest(client, settings)

    version = client.get(
        f"/api/v1/documents/{accepted['document_id']}/versions/{accepted['version_id']}"
    )
    assert version.json()["status"] == "ready"

    warning_url = (
        f"/api/v1/documents/{accepted['document_id']}/versions/"
        f"{accepted['version_id']}/rendering-warnings"
    )
    response = client.get(warning_url)
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"] == {
        "total": 2,
        "pages": 2,
        "unconfirmed": 2,
        "unconfirmed_pages": 2,
    }
    assert payload["render_config_version"]
    assert [warning["code"] for warning in payload["warnings"]] == [
        "missing_font",
        "animation_flattened",
    ]
    assert payload["warnings"][0]["details"] == {
        "requested_font": "PPTExtract Missing Contract Font",
        "replacement_font": "Noto Sans",
    }
    assert payload["warnings"][1]["details"] == {"timeline_count": 1}
    assert all(warning["status"] == "unconfirmed" for warning in payload["warnings"])

    bootstrap = client.get("/api/v1/app/bootstrap").json()
    curatable = next(runway for runway in bootstrap["runways"] if runway["id"] == "curatable")
    assert curatable["documents"][0]["rendering_warnings"] == payload["summary"]
    assert curatable["documents"][0]["action"]["href"] == (
        "/curation?filter=rendering-warnings"
        f"&document={accepted['document_id']}&version={accepted['version_id']}"
    )

    curation = client.get("/api/v1/curation/pages", params={"review_status": "all"}).json()
    assert [page["rendering_warnings"]["total"] for page in curation["pages"]] == [1, 1]

    blocked = client.post("/api/v1/publications/preflight")
    assert blocked.status_code == 409
    blocked_error = blocked.json()["error"]
    assert blocked_error["code"] == "rendering_warnings_unconfirmed"
    assert blocked_error["message"] == "仍有 2 页 / 2 条渲染警告未确认，发布被阻止。"
    assert blocked_error["details"]["unconfirmed"] == 2
    assert blocked_error["details"]["unconfirmed_pages"] == 2
    assert blocked_error["details"]["href"].startswith(
        "/curation?filter=rendering-warnings&document="
    )
    assert "&page=1&warning=" in blocked_error["details"]["href"]

    first = payload["warnings"][0]
    confirmed = client.post(
        f"{warning_url}/{first['warning_id']}/confirm",
        headers={"X-Actor-ID": "curator-wang"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert confirmed.json()["confirmed_by"] == "curator-wang"
    assert confirmed.json()["confirmed_at"]

    after_one = client.get(warning_url).json()
    assert after_one["summary"]["unconfirmed"] == 1
    assert after_one["warnings"][0]["details"] == first["details"]
    assert after_one["warnings"][0]["render_config_version"] == first[
        "render_config_version"
    ]

    confirmed_all = client.post(
        f"{warning_url}/confirm-all",
        headers={"X-Actor-ID": "curator-li"},
        json={
            "render_config_version": payload["render_config_version"],
            "warning_ids": [payload["warnings"][1]["warning_id"]],
        },
    )
    assert confirmed_all.status_code == 200
    assert confirmed_all.json()["confirmed_count"] == 1
    assert confirmed_all.json()["summary"]["unconfirmed"] == 0
    assert client.post("/api/v1/publications/preflight").json()["can_publish"] is True


def test_zero_warnings_allow_publication_preflight(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    _stub_ingestion(monkeypatch)
    monkeypatch.setattr(
        "pptextract.ingest_workflow.audit_rendering_warnings",
        lambda *_args, **_kwargs: (),
    )
    _ingest(client, settings)

    response = client.post("/api/v1/publications/preflight")
    assert response.status_code == 200
    assert response.json() == {
        "can_publish": True,
        "href": None,
        "stale_render_versions": 0,
        "summary": {
            "total": 0,
            "pages": 0,
            "unconfirmed": 0,
            "unconfirmed_pages": 0,
        },
    }


def test_confirm_all_rejects_a_warning_added_after_the_dialog_snapshot(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    _stub_ingestion(monkeypatch)
    accepted = _ingest(client, settings)
    warning_url = (
        f"/api/v1/documents/{accepted['document_id']}/versions/"
        f"{accepted['version_id']}/rendering-warnings"
    )
    snapshot = client.get(warning_url).json()
    with transaction(settings) as connection:
        replace_active_warnings(
            connection,
            version_id=accepted["version_id"],
            render_config_version=snapshot["render_config_version"],
            warnings=(
                RenderingWarning(
                    code="missing_font",
                    page_number=1,
                    font_family="PPTExtract Missing Contract Font",
                    replacement_font="Noto Sans",
                ),
                RenderingWarning(
                    code="missing_font",
                    page_number=1,
                    font_family="PPTExtract Concurrent Font",
                    replacement_font="Liberation Sans",
                ),
            ),
            page_numbers=(1,),
            observed_at="2026-08-22T10:10:00+00:00",
        )

    response = client.post(
        f"{warning_url}/confirm-all",
        json={
            "render_config_version": snapshot["render_config_version"],
            "warning_ids": [warning["warning_id"] for warning in snapshot["warnings"]],
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "rendering_warnings_stale"
    assert client.get(warning_url).json()["summary"]["unconfirmed"] == 3


def test_ingestion_workspace_keeps_discovered_warnings_visible_during_render_retry(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    _stub_ingestion(monkeypatch)

    def unavailable_render(*_args: object, **_kwargs: object) -> object:
        raise OSError("temporary renderer outage")

    monkeypatch.setattr(
        "pptextract.ingest_workflow.render_standard_pages", unavailable_render
    )
    response = client.post(
        "/api/v1/documents",
        headers={"Idempotency-Key": "visible-during-render-retry"},
        files={
            "file": (
                "visible-during-render-retry.pptx",
                build_rendering_warning_presentation(),
                PPTX_MEDIA_TYPE,
            )
        },
    )
    accepted = response.json()
    assert run_once(settings) is True

    version = client.get(
        f"/api/v1/documents/{accepted['document_id']}/versions/{accepted['version_id']}"
    ).json()
    bootstrap = client.get("/api/v1/app/bootstrap").json()
    processing = next(
        runway for runway in bootstrap["runways"] if runway["id"] == "processing"
    )

    assert version["status"] == "processing"
    assert processing["documents"][0]["status"] == "queued"
    assert processing["documents"][0]["rendering_warnings"] == {
        "total": 2,
        "pages": 2,
        "unconfirmed": 2,
        "unconfirmed_pages": 2,
    }
    warning_url = (
        f"/api/v1/documents/{accepted['document_id']}/versions/"
        f"{accepted['version_id']}/rendering-warnings"
    )
    warning_id = client.get(warning_url).json()["warnings"][0]["warning_id"]
    rejected = client.post(f"{warning_url}/{warning_id}/confirm")
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "version_not_current"


def test_render_config_change_rebuilds_renders_without_reusing_warning_confirmation(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings = system
    conversion_calls: list[int] = []
    rendering_calls: list[tuple[str, int]] = []
    fingerprint_calls: list[int] = []
    fail_changed_page_two_once = True

    def convert(_source: bytes, page: SourcePage) -> NormalizedPageContent:
        conversion_calls.append(page.page_number)
        return NormalizedPageContent(
            titles=(f"配置变化页 {page.page_number}",),
            body=("公开正文",),
            tables=(),
            images=(),
        )

    def render(
        _source: bytes,
        *,
        toolchain: DockerRenderingToolchain,
        pages: tuple[SourcePage, ...],
    ) -> tuple[StandardPageRender, ...]:
        nonlocal fail_changed_page_two_once
        image = toolchain.image
        page_number = pages[0].page_number
        rendering_calls.append((image, page_number))
        if image.endswith(":2") and page_number == 2 and fail_changed_page_two_once:
            fail_changed_page_two_once = False
            raise OSError("temporary rerender outage")
        return (
            StandardPageRender(
                page_number=page_number,
                media_type="image/png",
                dpi=144,
                width_px=100,
                height_px=56,
                data=f"{image}-render-{page_number}".encode(),
            ),
        )

    def fingerprint(content: NormalizedPageContent):  # type: ignore[no-untyped-def]
        fingerprint_calls.append(int(content.titles[0].rsplit(" ", 1)[-1]))
        return real_fingerprint_page(content)

    monkeypatch.setattr("pptextract.ingest_workflow.convert_page", convert)
    monkeypatch.setattr("pptextract.ingest_workflow.render_standard_pages", render)
    monkeypatch.setattr("pptextract.ingest_workflow.fingerprint_page", fingerprint)
    monkeypatch.setattr(
        "pptextract.ingest_workflow.audit_rendering_warnings",
        lambda *_args, **_kwargs: (
            RenderingWarning(
                code="missing_font",
                page_number=1,
                font_family="PPTExtract Missing Contract Font",
                replacement_font="Noto Sans",
            ),
        ),
    )

    accepted = client.post(
        "/api/v1/documents",
        headers={"Idempotency-Key": "render-config-change"},
        files={
            "file": (
                "render-config-change.pptx",
                build_rendering_warning_presentation(),
                PPTX_MEDIA_TYPE,
            )
        },
    ).json()
    assert run_once(settings) is True

    warning_url = (
        f"/api/v1/documents/{accepted['document_id']}/versions/"
        f"{accepted['version_id']}/rendering-warnings"
    )
    first_warning = client.get(warning_url).json()["warnings"][0]
    assert client.post(
        f"{warning_url}/{first_warning['warning_id']}/confirm",
        headers={"X-Actor-ID": "test-operator"},
    ).status_code == 200
    with transaction(settings) as connection:
        before = connection.execute(
            """
            SELECT results.fingerprint_sha256, results.conversion_key,
                   versions.review_status, versions.render_sha256
            FROM ingestion_page_results AS results
            JOIN page_versions AS versions
              ON versions.version_id = results.version_id
             AND versions.page_number = results.page_number
            WHERE results.version_id = ? AND results.page_number = 1
            """,
            (accepted["version_id"],),
        ).fetchone()
    assert before is not None

    changed_settings = replace(
        settings,
        render_generation=settings.render_generation + 1,
        render_image="pptextract/document-toolchain:2",
    )
    assert enqueue_stale_render_jobs(changed_settings) == 1
    page_id = client.get(
        "/api/v1/curation/pages", params={"review_status": "all"}
    ).json()["pages"][0]["page_id"]
    with TestClient(create_app(changed_settings)) as changed_client:
        stale = changed_client.post("/api/v1/publications/preflight")
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "render_configuration_stale"
        assert changed_client.post(
            f"{warning_url}/{first_warning['warning_id']}/confirm"
        ).status_code == 409
        assert changed_client.get(f"/api/v1/pages/{page_id}/render").status_code == 409

        # 第一轮在第 2 页失败；第 1 页暂存结果不得提前切换。
        assert run_once(changed_settings) is True
        with transaction(settings) as connection:
            partial = connection.execute(
                "SELECT render_sha256 FROM page_versions "
                "WHERE version_id = ? AND page_number = 1",
                (accepted["version_id"],),
            ).fetchone()
        assert partial is not None
        assert partial["render_sha256"] == before["render_sha256"]
        assert changed_client.post("/api/v1/publications/preflight").status_code == 409

        assert run_once(changed_settings) is True
        current = changed_client.get(warning_url).json()["warnings"][0]
        assert current["render_config_version"] != first_warning["render_config_version"]
        assert current["warning_id"] != first_warning["warning_id"]
        assert current["status"] == "unconfirmed"
        assert changed_client.post(
            f"{warning_url}/confirm-all",
            json={
                "render_config_version": current["render_config_version"],
                "warning_ids": [current["warning_id"]],
            },
        ).status_code == 200
        assert changed_client.post("/api/v1/publications/preflight").status_code == 200

    # 滚动升级期间旧 worker 不得再排队并覆盖已完成的新代次。
    assert enqueue_stale_render_jobs(settings) == 0
    with transaction(settings) as connection:
        after = connection.execute(
            """
            SELECT results.fingerprint_sha256, results.conversion_key,
                   versions.review_status, versions.render_sha256
            FROM ingestion_page_results AS results
            JOIN page_versions AS versions
              ON versions.version_id = results.version_id
             AND versions.page_number = results.page_number
            WHERE results.version_id = ? AND results.page_number = 1
            """,
            (accepted["version_id"],),
        ).fetchone()
    assert after is not None
    assert after["fingerprint_sha256"] == before["fingerprint_sha256"]
    assert after["conversion_key"] == before["conversion_key"]
    assert after["review_status"] == before["review_status"]
    assert after["render_sha256"] != before["render_sha256"]
    assert conversion_calls == [1, 2]
    assert fingerprint_calls == [1, 2]
    assert rendering_calls == [
        (settings.render_image, 1),
        (settings.render_image, 2),
        (changed_settings.render_image, 1),
        (changed_settings.render_image, 2),
        (changed_settings.render_image, 1),
        (changed_settings.render_image, 2),
    ]


def test_old_render_generation_cannot_downgrade_a_newly_ingested_ready_version(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    _client, old_settings = system
    _stub_ingestion(monkeypatch)
    new_settings = replace(
        old_settings,
        render_generation=old_settings.render_generation + 1,
        render_image="pptextract/document-toolchain:2",
    )
    with TestClient(create_app(new_settings)) as new_client:
        accepted = new_client.post(
            "/api/v1/documents",
            headers={"Idempotency-Key": "new-generation-hidden-page"},
            files={
                "file": (
                    "new-generation-hidden-page.pptx",
                    build_minimal_presentation(),
                    PPTX_MEDIA_TYPE,
                )
            },
        ).json()
        assert run_once(new_settings) is True
        enablement = new_client.post(
            f"/api/v1/documents/{accepted['document_id']}/versions/"
            f"{accepted['version_id']}/source-pages/2/enable",
            headers={"Idempotency-Key": "new-generation-hidden-enable"},
        )
        assert enablement.status_code == 202

    assert enqueue_stale_render_jobs(old_settings) == 0
    assert claim_next_job(old_settings) is None
    claimed_by_new_generation = claim_next_job(new_settings)
    assert claimed_by_new_generation is not None
    assert claimed_by_new_generation.job_id == enablement.json()["job_id"]
    with transaction(old_settings) as connection:
        version = connection.execute(
            "SELECT render_generation FROM document_versions WHERE version_id = ?",
            (accepted["version_id"],),
        ).fetchone()
        downgrade_jobs = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind = 'version.rerender'"
        ).fetchone()[0]
    assert version is not None
    assert version["render_generation"] == new_settings.render_generation
    assert downgrade_jobs == 0


def test_new_generation_supersedes_a_queued_hidden_page_job(
    system: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, old_settings = system
    _stub_ingestion(monkeypatch)
    accepted = client.post(
        "/api/v1/documents",
        headers={"Idempotency-Key": "old-generation-hidden-source"},
        files={
            "file": (
                "old-generation-hidden-source.pptx",
                build_minimal_presentation(),
                PPTX_MEDIA_TYPE,
            )
        },
    ).json()
    assert run_once(old_settings) is True
    enable_url = (
        f"/api/v1/documents/{accepted['document_id']}/versions/"
        f"{accepted['version_id']}/source-pages/2/enable"
    )
    old_enablement = client.post(
        enable_url,
        headers={"Idempotency-Key": "old-generation-hidden-enable"},
    ).json()

    new_settings = replace(
        old_settings,
        render_generation=old_settings.render_generation + 1,
        render_image="pptextract/document-toolchain:2",
    )
    assert run_once(new_settings) is True
    with TestClient(create_app(new_settings)) as new_client:
        replacement = new_client.post(
            enable_url,
            headers={"Idempotency-Key": "new-generation-hidden-enable"},
        )
    assert replacement.status_code == 202
    assert replacement.json()["job_id"] != old_enablement["job_id"]
    with transaction(old_settings) as connection:
        old_status = connection.execute(
            "SELECT status FROM jobs WHERE job_id = ?",
            (old_enablement["job_id"],),
        ).fetchone()["status"]
    assert old_status == "cancelled"
    claimed = claim_next_job(new_settings)
    assert claimed is not None
    assert claimed.job_id == replacement.json()["job_id"]
