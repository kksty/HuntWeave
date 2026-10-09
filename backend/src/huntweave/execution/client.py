import os
from datetime import datetime
from uuid import UUID

import httpx

from huntweave.config import runner_token
from huntweave.contracts.capabilities import Capabilities
from huntweave.contracts.execution import ExecutionRecord, ExecutionRequest


class RunnerClient:
    def __init__(self, url: str | None = None, token: str | None = None):
        self.url = url or os.environ.get("HUNTWEAVE_RUNNER_URL", "http://runner:8001")
        self.token = token or runner_token()

    def _request(
        self, method: str, path: str, data: dict[str, object] | None = None
    ) -> httpx.Response:
        response = httpx.request(
            method,
            self.url + path,
            json=data,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=3,
            trust_env=False,
        )
        if response.status_code != 404:
            response.raise_for_status()
        return response

    def capabilities(self) -> Capabilities:
        return Capabilities.model_validate(self._request("GET", "/v1/capabilities").json())

    def submit(self, request: ExecutionRequest) -> ExecutionRecord:
        return ExecutionRecord.model_validate(
            self._request("POST", "/v1/calls", request.model_dump(mode="json")).json()
        )

    def query(self, call_id: UUID) -> ExecutionRecord | None:
        response = self._request("GET", f"/v1/calls/{call_id}")
        return (
            None if response.status_code == 404 else ExecutionRecord.model_validate(response.json())
        )

    def renew(self, call_id: UUID, generation: int, expires_at: datetime) -> ExecutionRecord:
        return ExecutionRecord.model_validate(
            self._request(
                "POST",
                f"/v1/calls/{call_id}/renew",
                {"lease_generation": generation, "lease_expires_at": expires_at.isoformat()},
            ).json()
        )

    def cancel(self, call_id: UUID, generation: int) -> ExecutionRecord:
        return ExecutionRecord.model_validate(
            self._request(
                "POST", f"/v1/calls/{call_id}/cancel", {"lease_generation": generation}
            ).json()
        )


def get_capabilities(url: str | None = None) -> Capabilities:
    base = url or os.environ.get("HUNTWEAVE_RUNNER_URL", "http://runner:8001")
    response = httpx.get(
        f"{base}/v1/capabilities",
        headers={"Authorization": f"Bearer {runner_token()}"},
        timeout=3,
        trust_env=False,
    )
    response.raise_for_status()
    return Capabilities.model_validate(response.json())
