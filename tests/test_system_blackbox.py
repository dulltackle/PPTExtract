from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from PIL import Image

from tests.support.synthetic_pptx import (
    build_image_curation_presentation,
    build_minimal_presentation,
    build_plain_text_presentation,
)


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture(scope="module")
def running_system() -> Iterator[tuple[str, Path]]:
    project_root = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["npm", "run", "build"],
        cwd=project_root / "web",
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    data_root = project_root / "var" / "blackbox" / uuid.uuid4().hex
    port = available_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment.update(
        {
            "PPTEXTRACT_CONFIG_VERSION": "1",
            "PPTEXTRACT_DATA_ROOT": str(data_root),
            "PPTEXTRACT_ACTOR_ID": "blackbox-operator",
            "PPTEXTRACT_WORKER_ID": "blackbox-worker",
            "PPTEXTRACT_WEB_DIST": str(project_root / "web" / "dist"),
            "PPTEXTRACT_RENDER_IMAGE": "pptextract/document-toolchain:1",
        }
    )
    worker = subprocess.Popen(
        [sys.executable, "-m", "pptextract.worker"],
        cwd=project_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    api = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "pptextract.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=project_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        last_error = "系统尚未响应"
        while time.monotonic() < deadline:
            if worker.poll() is not None or api.poll() is not None:
                break
            try:
                with httpx.Client(trust_env=False, timeout=0.5) as client:
                    response = client.get(f"{base_url}/api/v1/health")
                if response.status_code == 200 and response.json()["status"] == "ready":
                    yield base_url, data_root
                    return
                last_error = response.text
            except httpx.HTTPError as error:
                last_error = str(error)
            time.sleep(0.1)
        worker_error = worker.stderr.read() if worker.poll() is not None and worker.stderr else ""
        api_error = api.stderr.read() if api.poll() is not None and api.stderr else ""
        pytest.fail(
            f"黑盒系统未就绪：{last_error}\nworker: {worker_error}\napi: {api_error}"
        )
    finally:
        for process in (api, worker):
            if process.poll() is None:
                process.terminate()
        for process in (api, worker):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if data_root.exists():
            shutil.rmtree(data_root)


def test_external_system_spine_and_browser_shell(
    running_system: tuple[str, Path],
) -> None:
    base_url, data_root = running_system
    project_root = Path(__file__).resolve().parents[1]

    with httpx.Client(trust_env=False, timeout=2) as client:
        health = client.get(f"{base_url}/api/v1/health")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ready",
        "config_version": 1,
        "components": {
            "api": {"status": "ready"},
            "database": {"status": "ready"},
            "object_store": {"status": "ready"},
            "worker": {"status": "ready", "worker_id": "blackbox-worker"},
        },
    }

    with sqlite3.connect(data_root / "pptextract.sqlite3") as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys = ON").fetchone() is None
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
    assert (data_root / "objects" / ".staging").is_dir()

    browser_result = subprocess.run(
        ["node", "tests/blackbox.mjs", base_url],
        cwd=project_root / "web",
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    report = json.loads(browser_result.stdout)
    assert report == {
        "ok": True,
        "checks": ["viewport-1440", "viewport-1280", "operator-safe-error"],
    }


def test_first_plain_text_upload_becomes_a_curatable_ready_page(
    running_system: tuple[str, Path],
) -> None:
    base_url, _data_root = running_system
    headers = {
        "X-Actor-ID": "blackbox-operator",
        "Idempotency-Key": "first-public-presentation",
    }

    with httpx.Client(trust_env=False, timeout=10) as client:
        accepted = client.post(
            f"{base_url}/api/v1/documents",
            headers=headers,
            files={
                "file": (
                    "public-first-upload.pptx",
                    build_plain_text_presentation(),
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                )
            },
        )

        assert accepted.status_code == 202
        identity = accepted.json()
        assert identity["status"] == "accepted"
        assert all(identity[key] for key in ("document_id", "version_id", "job_id"))

        deadline = time.monotonic() + 45
        task = client.get(f"{base_url}/api/v1/jobs/{identity['job_id']}").json()
        while task["status"] not in {"succeeded", "failed"} and time.monotonic() < deadline:
            time.sleep(0.1)
            task = client.get(f"{base_url}/api/v1/jobs/{identity['job_id']}").json()

        assert task == {
            "job_id": identity["job_id"],
            "kind": "document.ingest",
            "status": "succeeded",
            "attempts": 1,
            "next_retry_at": None,
            "progress": {
                "phase": "activation",
                "completed_pages": 1,
                "total_pages": 1,
                "pages": [
                    {
                        "page_number": 1,
                        "phase": "page_fingerprint",
                        "status": "completed",
                    }
                ],
            },
            "error": None,
        }

        document = client.get(f"{base_url}/api/v1/documents/{identity['document_id']}")
        assert document.status_code == 200
        assert document.json()["current_version_id"] == identity["version_id"]

        version = client.get(
            f"{base_url}/api/v1/documents/{identity['document_id']}"
            f"/versions/{identity['version_id']}"
        )
        assert version.status_code == 200
        assert version.json()["status"] == "ready"
        assert version.json()["source"]["filename"] == "public-first-upload.pptx"
        assert len(version.json()["source"]["sha256"]) == 64

        pending = client.get(
            f"{base_url}/api/v1/curation/pages", params={"review_status": "pending"}
        )
        assert pending.status_code == 200
        assert len(pending.json()["pages"]) == 1
        pending_page = pending.json()["pages"][0]
        assert pending_page["document_id"] == identity["document_id"]
        assert pending_page["version_id"] == identity["version_id"]
        assert pending_page["page_number"] == 1
        assert pending_page["review_status"] == "pending"
        assert pending_page["page_id"] != pending_page["chunk_id"]

        detail = client.get(f"{base_url}/api/v1/pages/{pending_page['page_id']}")
        assert detail.status_code == 200
        assert detail.json()["source_content"]["titles"] == ["公开首次摄取"]
        assert detail.json()["source_content"]["body"] == [
            "这是可公开验证的单页纯文字内容。"
        ]
        render = detail.json()["standard_render"]
        assert render["media_type"] == "image/png"
        assert render["dpi"] == 144
        assert render["width_px"] == 1921
        assert render["height_px"] == 1080

        rendered_page = client.get(f"{base_url}{render['url']}")
        assert rendered_page.status_code == 200
        assert rendered_page.headers["content-type"] == "image/png"
        assert rendered_page.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_synthetic_hidden_page_can_be_enabled_in_a_real_browser(
    running_system: tuple[str, Path],
) -> None:
    base_url, _data_root = running_system
    project_root = Path(__file__).resolve().parents[1]
    with httpx.Client(trust_env=False, timeout=10) as client:
        accepted = client.post(
            f"{base_url}/api/v1/documents",
            headers={
                "X-Actor-ID": "blackbox-operator",
                "Idempotency-Key": "browser-hidden-page-fixture",
            },
            files={
                "file": (
                    "public-hidden-page.pptx",
                    build_minimal_presentation(),
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                )
            },
        )
        assert accepted.status_code == 202
        identity = accepted.json()
        deadline = time.monotonic() + 45
        task = client.get(f"{base_url}/api/v1/jobs/{identity['job_id']}").json()
        while task["status"] not in {"succeeded", "failed"} and time.monotonic() < deadline:
            time.sleep(0.1)
            task = client.get(f"{base_url}/api/v1/jobs/{identity['job_id']}").json()
        assert task["status"] == "succeeded"

    browser_result = subprocess.run(
        ["node", "tests/hidden-page-blackbox.mjs", base_url],
        cwd=project_root / "web",
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert json.loads(browser_result.stdout) == {
        "ok": True,
        "checks": [
            "pending-excludes-hidden",
            "viewport-1440x1024",
            "viewport-1280x900",
            "persistent-enable-to-pending",
        ],
    }


def test_review_queue_supports_single_and_batch_conclusions_in_browser(
    running_system: tuple[str, Path],
) -> None:
    base_url, _data_root = running_system
    project_root = Path(__file__).resolve().parents[1]
    with httpx.Client(trust_env=False, timeout=10) as client:
        accepted = client.post(
            f"{base_url}/api/v1/documents",
            headers={
                "X-Actor-ID": "blackbox-operator",
                "Idempotency-Key": "browser-curation-review-fixture",
            },
            files={
                "file": (
                    "public-curation-review.pptx",
                    build_plain_text_presentation(
                        title="公开浏览器策展页",
                        body_text="这是公开浏览器策展正文。",
                    ),
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                )
            },
        )
        assert accepted.status_code == 202
        identities = [accepted.json()]
        for index, title in enumerate(
            ("公开批量策展页二", "公开批量策展页三"), start=2
        ):
            extra = client.post(
                f"{base_url}/api/v1/documents",
                headers={
                    "X-Actor-ID": "blackbox-operator",
                    "Idempotency-Key": f"browser-review-queue-{index}",
                },
                files={
                    "file": (
                        f"public-review-queue-{index}.pptx",
                        build_plain_text_presentation(title=title),
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    )
                },
            )
            assert extra.status_code == 202
            identities.append(extra.json())
        for current in identities:
            deadline = time.monotonic() + 45
            task = client.get(f"{base_url}/api/v1/jobs/{current['job_id']}").json()
            while task["status"] not in {"succeeded", "failed"} and time.monotonic() < deadline:
                time.sleep(0.1)
                task = client.get(f"{base_url}/api/v1/jobs/{current['job_id']}").json()
            assert task["status"] == "succeeded"
        identity = identities[0]

    route = (
        "/curation?document="
        f"{identity['document_id']}&version={identity['version_id']}&page=1"
    )
    browser_result = subprocess.run(
        ["node", "tests/curation-review-blackbox.mjs", base_url, route],
        cwd=project_root / "web",
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert browser_result.returncode == 0, browser_result.stderr
    assert json.loads(browser_result.stdout) == {
        "ok": True,
        "checks": [
            "viewport-1280-three-columns",
            "wcag22aa-1280",
            "zoom-125%-reachable",
            "zoom-200%-reachable",
            "text-zoom-200%-reachable",
            "plain-text-zero-capture-approved",
            "keyboard-a-approved",
            "wcag22aa-1440",
            "keyboard-r-reopen-and-x-exclude",
            "pending-only-batch-exclusion",
            "forbidden-batch-actions-absent",
        ],
    }

    with httpx.Client(trust_env=False, timeout=5) as client:
        pages = client.get(
            f"{base_url}/api/v1/curation/pages", params={"review_status": "all"}
        ).json()["pages"]
        reviewed = [
            page for page in pages
            if page["document_id"] in {item["document_id"] for item in identities}
        ]
        assert len(reviewed) == 3
        assert {page["review_status"] for page in reviewed} == {"excluded"}
        primary = next(page for page in reviewed if page["document_id"] == identity["document_id"])
        detail = client.get(f"{base_url}/api/v1/pages/{primary['page_id']}").json()
        assert detail["review"]["reviewed_by"] == "blackbox-operator"
        assert detail["review"]["source_version_id"] == identity["version_id"]
        assert detail["review"]["exclusion_reason"] == "irrelevant"
        assert detail["annotation"]["overview"] is None
        assert detail["annotation"]["visuals"] == []


def test_first_capture_visual_is_created_and_verified_in_real_browser(
    running_system: tuple[str, Path],
) -> None:
    base_url, data_root = running_system
    project_root = Path(__file__).resolve().parents[1]
    identities: list[dict[str, object]] = []
    with httpx.Client(trust_env=False, timeout=60) as client:
        for index, width in enumerate((1280, 1440), start=1):
            accepted = client.post(
                f"{base_url}/api/v1/documents",
                headers={
                    "X-Actor-ID": "blackbox-operator",
                    "Idempotency-Key": f"browser-capture-visual-{width}",
                },
                files={
                    "file": (
                        f"public-capture-{width}.pptx",
                        build_plain_text_presentation(
                            title=f"公开框选验收页 {index}",
                            body_text="文字来源不足，需要从标准页补充公开视觉结论。",
                        ),
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    )
                },
            )
            assert accepted.status_code == 202
            identity = accepted.json()
            deadline = time.monotonic() + 45
            task = client.get(f"{base_url}/api/v1/jobs/{identity['job_id']}").json()
            while task["status"] not in {"succeeded", "failed"} and time.monotonic() < deadline:
                time.sleep(0.1)
                task = client.get(f"{base_url}/api/v1/jobs/{identity['job_id']}").json()
            assert task["status"] == "succeeded"
            identities.append(identity)

    routes = [
        "/curation?document="
        f"{identity['document_id']}&version={identity['version_id']}&page=1"
        for identity in identities
    ]
    browser_result = subprocess.run(
        ["node", "tests/capture-visual-blackbox.mjs", base_url, *routes],
        cwd=project_root / "web",
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert browser_result.returncode == 0, browser_result.stderr
    report = json.loads(browser_result.stdout)
    assert report["ok"] is True
    assert report["checks"] == [
        "capture-viewport-1280",
        "keyboard-flow-1280",
        "capture-viewport-1440",
        "keyboard-flow-1440",
    ]
    assert len(report["savedBounds"]) == 2
    assert len(report["mutationSnapshots"]) == 10
    assert len(set(report["mutationSnapshots"])) == 10
    for bounds in report["savedBounds"]:
        assert bounds == pytest.approx(
            {"left": 0.18, "top": 0.22, "width": 0.44, "height": 0.44},
            abs=0.002,
        )

    visual_refs: list[str] = []
    with httpx.Client(trust_env=False, timeout=10) as client:
        pages = client.get(
            f"{base_url}/api/v1/curation/pages", params={"review_status": "all"}
        ).json()["pages"]
        for identity in identities:
            page = next(
                candidate
                for candidate in pages
                if candidate["document_id"] == identity["document_id"]
                and candidate["version_id"] == identity["version_id"]
            )
            detail = client.get(f"{base_url}/api/v1/pages/{page['page_id']}").json()
            visual = next(
                item
                for item in detail["annotation"]["visuals"]
                if item["source_kind"] == "capture"
            )
            visual_refs.append(visual["visual_ref"])
            assert len(visual["visual_ref"]) == 32
            assert visual["visual_ref"] != "01"
            assert visual["summary"].startswith("公开折线展示")
            assert visual["visual_type"] == "chart"
            assert visual["confirmed"] is True
            asset = visual["asset"]
            assert asset["media_type"] == "image/png"
            assert asset["byte_contract"] == "standard_render_crop"
            render = detail["standard_render"]
            bounds = visual["bounds"]
            expected_left = max(0, math.floor(bounds["left"] * render["width_px"]) - 8)
            expected_top = max(0, math.floor(bounds["top"] * render["height_px"]) - 8)
            expected_right = min(
                render["width_px"],
                math.ceil((bounds["left"] + bounds["width"]) * render["width_px"]) + 8,
            )
            expected_bottom = min(
                render["height_px"],
                math.ceil((bounds["top"] + bounds["height"]) * render["height_px"]) + 8,
            )
            assert asset["width_px"] == expected_right - expected_left
            assert asset["height_px"] == expected_bottom - expected_top
            object_path = data_root / "objects" / asset["sha256"][:2] / asset["sha256"]
            payload = object_path.read_bytes()
            assert hashlib.sha256(payload).hexdigest() == asset["sha256"]
            assert len(payload) == asset["size_bytes"]
            with Image.open(object_path) as image:
                assert image.format == "PNG"
                assert image.size == (asset["width_px"], asset["height_px"])
    assert len(set(visual_refs)) == 2


def test_anydoc_image_sources_are_disposed_in_a_real_browser(
    running_system: tuple[str, Path],
) -> None:
    base_url, _data_root = running_system
    project_root = Path(__file__).resolve().parents[1]
    with httpx.Client(trust_env=False, timeout=10) as client:
        accepted = client.post(
            f"{base_url}/api/v1/documents",
            headers={
                "X-Actor-ID": "blackbox-operator",
                "Idempotency-Key": "browser-source-image-fixture",
            },
            files={
                "file": (
                    "public-source-image-review.pptx",
                    build_image_curation_presentation(),
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                )
            },
        )
        assert accepted.status_code == 202
        identity = accepted.json()
        deadline = time.monotonic() + 60
        task = client.get(f"{base_url}/api/v1/jobs/{identity['job_id']}").json()
        while task["status"] not in {"succeeded", "failed"} and time.monotonic() < deadline:
            time.sleep(0.1)
            task = client.get(f"{base_url}/api/v1/jobs/{identity['job_id']}").json()
        assert task["status"] == "succeeded"

    route_prefix = (
        "/curation?document="
        f"{identity['document_id']}&version={identity['version_id']}"
    )
    browser_result = subprocess.run(
        ["node", "tests/source-image-review-blackbox.mjs", base_url, route_prefix],
        cwd=project_root / "web",
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert json.loads(browser_result.stdout) == {
        "ok": True,
        "checks": [
            "image-viewport-1280-three-columns",
            "single-image-included",
            "single-image-ignored",
            "mixed-duplicate-dispositions",
            "preview-and-save-recovery",
            "review-invalidated-and-leave-warning",
            "image-viewport-1440",
            "source-load-recovery",
        ],
    }
