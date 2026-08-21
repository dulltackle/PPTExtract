from __future__ import annotations

import json
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

from tests.support.synthetic_pptx import build_plain_text_presentation


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
            "progress": {
                "phase": "activation",
                "completed_pages": 1,
                "total_pages": 1,
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
