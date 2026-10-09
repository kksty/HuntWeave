from fastapi.testclient import TestClient

from huntweave.api.app import create_app
from huntweave.config import AppSettings


def test_missing_access_key_keeps_liveness_but_blocks_business() -> None:
    client = TestClient(create_app(AppSettings(access_key=None)))
    assert client.get("/health/live").json() == {"status": "alive"}
    for path in ("/", "/api/v1/system/capabilities", "/docs", "/openapi.json", "/evidence/x"):
        response = client.get(path)
        assert response.status_code == 503
        assert response.json()["reason_code"] == "access_key_missing"


def test_configured_secret_never_grants_implicit_access() -> None:
    secret = "a" * 64
    client = TestClient(create_app(AppSettings(access_key=secret)))
    for path in ("/", "/api/v1/system/capabilities", "/docs", "/openapi.json"):
        response = client.get(path, headers={"Authorization": f"Bearer {secret}"})
        assert response.status_code == 401
        assert secret not in response.text


def test_missing_key_also_blocks_mutations() -> None:
    client = TestClient(create_app(AppSettings(access_key=None)))
    response = client.post("/api/v1/runs", json={"targets": ["192.0.2.1"]})
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
