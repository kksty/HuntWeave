import os
from datetime import datetime
from typing import Literal
from uuid import UUID

import httpx

from huntweave.config import runner_token
from huntweave.contracts.capabilities import Capabilities
from huntweave.contracts.execution import ExecutionRecord, ExecutionRequest, RunRuntimeView
from huntweave.contracts.retention import (
    RetentionReport,
    RetentionRequest,
    RetentionView,
)

RetentionTargetKind = Literal["versions", "artifacts"]
RetentionAction = Literal["pin", "unpin", "delete"]


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
        # Every refusal, including 404, surfaces as a status error: no caller may parse a
        # refusal body as if it were a record.
        response.raise_for_status()
        return response

    def capabilities(self) -> Capabilities:
        return Capabilities.model_validate(self._request("GET", "/v1/capabilities").json())

    def run_runtime(self, run_id: UUID) -> RunRuntimeView:
        """The execution side's own account of this Run's containers and gateways.

        Read-only: this asks for a projection of the manager's records and grants no capability. A
        refusal (404 on an older Runner, a 5xx, an unreachable side) is the caller's to turn into an
        observation gap — never into "this Run has nothing running".
        """
        return RunRuntimeView.model_validate(
            self._request("GET", f"/v1/runs/{run_id}/sandbox-state").json()
        )

    def submit(self, request: ExecutionRequest) -> ExecutionRecord:
        return ExecutionRecord.model_validate(
            self._request("POST", "/v1/calls", request.model_dump(mode="json")).json()
        )

    def retention(self) -> RetentionView:
        """The execution side's retention answer: candidates, held artifacts and the preview.

        Read-only. A deployment without management says so in the view instead of failing the read,
        so the console can state the gap rather than show an empty cache.
        """
        return RetentionView.model_validate(self._request("GET", "/v1/retention").json())

    def retention_action(
        self,
        *,
        target_kind: RetentionTargetKind,
        target: str,
        action: RetentionAction,
        request: RetentionRequest,
    ) -> RetentionView | RetentionReport:
        """Pin, unpin or delete one version or artifact, and return what the execution side did.

        The reaction is not assumed: a pin answers with the new view, a delete answers with its own
        report, and either is validated as the contract it claims to be.
        """
        response = self._request(
            "POST",
            f"/v1/retention/{target_kind}/{target}/{action}",
            request.model_dump(mode="json"),
        ).json()
        if action == "delete":
            return RetentionReport.model_validate(response)
        return RetentionView.model_validate(response)

    def sweep_retention(self, request: RetentionRequest) -> RetentionReport:
        """Apply the reclaim preview on the execution side and report what it did."""
        return RetentionReport.model_validate(
            self._request(
                "POST", "/v1/retention/sweep", request.model_dump(mode="json")
            ).json()
        )

    def query(self, call_id: UUID) -> ExecutionRecord | None:
        try:
            response = self._request("GET", f"/v1/calls/{call_id}")
        except httpx.HTTPStatusError as error:
            # Only the ledger's explicit "no such call" answer means the ID was never accepted.
            if error.response.status_code == 404:
                return None
            raise
        return ExecutionRecord.model_validate(response.json())

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
