"""Outbox dispatcher and Runner reconciliation outside database transactions."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx

from huntweave.contracts.execution import ExecutionRecord, ExecutionRequest
from huntweave.execution.client import RunnerClient
from huntweave.runs.orchestration import OrchestrationService


class ExecutionDispatcher:
    def __init__(self, business: OrchestrationService, runner: RunnerClient):
        self.business = business
        self.runner = runner

    def reconcile(self, run_id: UUID) -> None:
        """Converge every pending call with the Runner's authenticated durable ledger.

        Lease generations are per role session and the Runner fences them itself, so this
        pass never compares a ticket with the claim that triggered it: re-leasing a task
        after a restart must not invalidate another role's live lease. A call the Runner
        already accepted is maintained, never re-submitted, and an unavailable control
        interface leaves every local status alone instead of guessing an outcome.
        """
        state = self.business.snapshot(run_id)["run"]
        for call in self.business.pending(run_id):
            ticket = ExecutionRequest.model_validate(call["ticket"])
            try:
                record = self.runner.query(ticket.call_id)
                if record is None:
                    if call["status"] != "planned":
                        # The durable ledger answers for a call the control plane already
                        # dispatched; that verdict is recorded, never second-guessed.
                        self.business.unknown(run_id, ticket.call_id)
                        continue
                    if state["status"] in {"pausing", "cancelling"} or (
                        ticket.lease_expires_at <= datetime.now(UTC)
                    ):
                        self._settle_undispatched(run_id, ticket)
                        continue
                    if state["status"] != "running":
                        continue
                    record = self._submit(run_id, ticket)
                    if record is None:
                        # No side effect was accepted: the same call_id stays in the outbox
                        # and is retried once the control interface answers again.
                        continue
                if record.status in {"accepted", "running"}:
                    record = self._maintain(state["status"], ticket)
                    if record is None:
                        continue
                self.business.accept(record)
            except (httpx.HTTPError, OSError, TimeoutError):
                # Stop dispatching and renewing this call for now: a call the Runner is
                # already running stays bounded by its control lease, and this call_id is
                # reconciled once the Runner answers. Declaring it unknown here would
                # invent a verdict the execution ledger never gave.
                self.business.unreachable(run_id, ticket.call_id)

    def sweep(self, limit: int = 20, offset: int = 0) -> int:
        """Re-reconcile Runs the scheduler itself cannot claim.

        An unconfirmed call is never retried, but its ledger must still be read again:
        this is where a Runner that answers later releases the Run. Returns how many Runs
        were swept so the caller can rotate a bounded window.
        """
        run_ids = self.business.pending_runs(limit, offset)
        for run_id in run_ids:
            self.reconcile(run_id)
        return len(run_ids)

    def _settle_undispatched(
        self, run_id: UUID, ticket: ExecutionRequest, reason: str = "cancelled_before_dispatch"
    ) -> None:
        # The Runner's ledger confirms this call_id was never accepted, so recording the
        # refusal releases the run without replaying or duplicating any effect.
        self.business.accept(
            ExecutionRecord(request=ticket, status="cancelled", reason_code=reason)
        )

    def _submit(self, run_id: UUID, ticket: ExecutionRequest) -> ExecutionRecord | None:
        try:
            return self.runner.submit(ticket)
        except httpx.HTTPStatusError as rejection:
            if rejection.response.status_code < 500:
                # The Runner refused the intent outright; nothing was accepted, so the Run
                # is released with the Runner's own reason instead of waiting forever.
                self._settle_undispatched(run_id, ticket, self._refusal_reason(rejection))
                return None
            raise

    @staticmethod
    def _refusal_reason(rejection: httpx.HTTPStatusError) -> str:
        try:
            body = rejection.response.json()
        except ValueError:
            return "cancelled_before_dispatch"
        if not isinstance(body, dict):
            return "cancelled_before_dispatch"
        reason = body.get("reason_code")
        return reason if isinstance(reason, str) and reason else "cancelled_before_dispatch"

    def _maintain(self, run_status: str, ticket: ExecutionRequest) -> ExecutionRecord | None:
        # The Runner owns the call it accepted: keep its control lease alive so a bounded
        # action survives a scheduling gap, or stop it when the Run was cancelled.
        try:
            if run_status == "cancelling":
                return self.runner.cancel(ticket.call_id, ticket.lease_generation)
            return self.runner.renew(
                ticket.call_id,
                ticket.lease_generation,
                min(
                    datetime.now(UTC) + timedelta(seconds=15),
                    ticket.deadline_at,
                    ticket.authorized_until,
                ),
            )
        except httpx.HTTPStatusError as rejection:
            if rejection.response.status_code >= 500:
                raise
            # The ledger finished, expired or already stopped this call; read its verdict.
            return self.runner.query(ticket.call_id)
