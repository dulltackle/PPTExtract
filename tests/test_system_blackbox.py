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
