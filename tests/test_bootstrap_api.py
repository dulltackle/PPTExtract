from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pptextract.api import create_app
from pptextract.config import Settings


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings.for_test(tmp_path)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_bootstrap_exposes_stable_actor_and_honest_empty_runways(client: TestClient) -> None:
    response = client.get("/api/v1/app/bootstrap", headers={"X-Actor-ID": "operator-zhang"})

    assert response.status_code == 200
    assert response.json() == {
        "actor": {"actor_id": "operator-zhang", "display_name": "操作者 operator-zhang"},
        "runways": [
            {"id": "pending", "label": "待处理", "documents": []},
            {"id": "processing", "label": "处理中", "documents": []},
            {"id": "curatable", "label": "可策展", "documents": []},
        ],
    }


def test_api_errors_use_operator_safe_envelope(client: TestClient) -> None:
    response = client.get("/api/v1/missing")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "未找到请求的资源。",
        }
    }
    assert "traceback" not in response.text.lower()


def test_unexpected_api_error_does_not_expose_stack(tmp_path: Path) -> None:
    app = create_app(Settings.for_test(tmp_path))

    @app.get("/api/v1/explode")
    async def explode() -> None:
        raise RuntimeError("secret infrastructure detail")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/api/v1/explode")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "系统暂时无法完成请求，请稍后重试。",
        }
    }
    assert "secret infrastructure detail" not in response.text


def test_request_validation_uses_same_error_envelope(tmp_path: Path) -> None:
    app = create_app(Settings.for_test(tmp_path))

    @app.get("/api/v1/validated")
    async def validated(count: int) -> dict[str, int]:
        return {"count": count}

    with TestClient(app) as test_client:
        response = test_client.get("/api/v1/validated", params={"count": "not-a-number"})

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_request",
            "message": "请求参数无效。",
            "details": [
                {
                    "field": "count",
                    "message": (
                        "Input should be a valid integer, unable to parse string as an integer"
                    ),
                }
            ],
        }
    }
