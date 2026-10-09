from fastapi.testclient import TestClient

from huntweave.execution.server import create_runner


def test_runner_requires_its_own_token() -> None:
    token = "b" * 64
    client = TestClient(create_runner(token=token))
    assert client.get("/health/live").status_code == 200
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer " + "a" * 64}):
        assert client.get("/v1/capabilities", headers=headers).status_code == 401
    response = client.get("/v1/capabilities", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["real_execution_ready"] is False
    assert response.json()["reason_code"] == "environment_unsupported"
    assert token not in response.text


def test_execution_requires_a_strict_ticket() -> None:
    client = TestClient(create_runner(token="b" * 64))
    response = client.post("/v1/calls", headers={"Authorization": "Bearer " + "b" * 64})
    assert response.status_code == 422
