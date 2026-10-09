from pathlib import Path

from fastapi.testclient import TestClient

from huntweave.execution.server import create_runner


def test_runner_requires_its_own_token(tmp_path: Path) -> None:
    token = "b" * 64
    client = TestClient(
        create_runner(token=token, state_dir=tmp_path / "state", evidence_dir=tmp_path / "evidence")
    )
    assert client.get("/health/live").status_code == 200
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer " + "a" * 64}):
        assert client.get("/v1/capabilities", headers=headers).status_code == 401
    response = client.get("/v1/capabilities", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["real_execution_ready"] is False
    assert response.json()["reason_code"] == "environment_unsupported"
    assert token not in response.text


def test_runner_reports_its_own_readiness_instead_of_a_constant(tmp_path: Path) -> None:
    token = "b" * 64
    headers = {"Authorization": f"Bearer {token}"}
    # A state directory that cannot be opened leaves the fixed fake chain unusable, and the
    # endpoint says so instead of reporting a ready platform.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    unusable = TestClient(
        create_runner(
            token=token, state_dir=blocked / "state", evidence_dir=blocked / "evidence"
        )
    )
    body = unusable.get("/v1/capabilities", headers=headers).json()
    assert body["fake_execution_ready"] is False
    assert body["reason_code"] == "runner_state_unavailable"
    assert body["real_execution_ready"] is False

    working = TestClient(
        create_runner(
            token=token, state_dir=tmp_path / "state", evidence_dir=tmp_path / "evidence"
        )
    )
    body = working.get("/v1/capabilities", headers=headers).json()
    assert body["fake_execution_ready"] is True
    assert body["observed_at"] is not None
    # Every ADR-0010 gate is reported, and the ones that are unmet carry their own condition.
    assert [gate["gate"] for gate in body["gates"]] == [
        "profile_revalidation",
        "contract_expressiveness",
        "console_consumption",
        "deployment_revert",
    ]
    assert all(gate["reason_code"] for gate in body["gates"] if not gate["ready"])


def test_execution_requires_a_strict_ticket(tmp_path: Path) -> None:
    client = TestClient(
        create_runner(
            token="b" * 64, state_dir=tmp_path / "state", evidence_dir=tmp_path / "evidence"
        )
    )
    response = client.post("/v1/calls", headers={"Authorization": "Bearer " + "b" * 64})
    assert response.status_code == 422
