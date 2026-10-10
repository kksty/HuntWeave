"""Outbox dispatcher and Runner reconciliation outside database transactions."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx

from huntweave.contracts.execution import (
    ExecutionObservation,
    ExecutionRecord,
    ExecutionRequest,
)
from huntweave.execution.client import RunnerClient
from huntweave.runs.orchestration import OrchestrationService


def read_observation(runner: RunnerClient, call_id: UUID) -> ExecutionObservation | None:
    """Read what the execution side can prove about a call, outside any transaction.

    An unreachable ledger yields no observation, which is not the same as a proof of
    non-execution: a verdict that needs proof must be refused instead.
    """
    try:
        record = runner.query(call_id)
    except (httpx.HTTPError, OSError, TimeoutError):
        return None
    return record.observation if record is not None else None


class ExecutionDispatcher:
    def __init__(
        self,
        business: OrchestrationService,
        runner: RunnerClient,
        *,
        real_ready: Callable[[], bool] | None = None,
    ):
        self.business = business
        self.runner = runner
        # Read fresh from the execution side: a real Run whose readiness lapsed stops offering new
        # calls and stops renewing what is running, instead of hardening into a fake one.
        self.real_ready = real_ready

    def _ready_for(self, ticket: ExecutionRequest) -> bool:
        if ticket.execution_profile == "fake-p0-v1":
            return True
        return self.real_ready is not None and self.real_ready()

    def reconcile(self, run_id: UUID) -> None:
        """Converge every pending call with the Runner's authenticated durable ledger.

        Lease generations are per role session and the Runner fences them itself, so this
        pass never compares a ticket with the claim that triggered it: re-leasing a task
        after a restart must not invalidate another role's live lease. A call the Runner
        already accepted is maintained, never re-submitted, and an unavailable control
        interface leaves every local status alone instead of guessing an outcome.
        """
        state = self.business.snapshot(run_id)["run"]
        for call in self.business.reconcilable(run_id):
            ticket = ExecutionRequest.model_validate(call["ticket"])
            try:
                record = self.runner.query(ticket.call_id)
                if record is None:
                    if call["status"] != "planned":
                        # The durable ledger answers for a call the control plane already
                        # dispatched; that verdict is recorded, never second-guessed.
                        self.business.unknown(run_id, ticket.call_id)
                        continue
                    if not self._ready_for(ticket):
                        # A real Run does not fall back to the demonstration side: the call keeps
                        # its reservation and its place, and the Run says what it is waiting for.
                        self.business.hold_for_readiness(
                            run_id, ticket.call_id, "real_execution_not_ready"
                        )
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
                elif record.status == "unknown" and state["status"] in {"cancelling", "pausing"}:
                    # The Run is ending: have the execution side confirm what the call left
                    # running instead of waiting for an outcome it will never give. A pause
                    # needs it too, or the Run can never leave 停止中/待核对.
                    record = self._confirm_stop(ticket) or record
                self.business.accept(record)
            except (httpx.HTTPError, OSError, TimeoutError):
                # Stop dispatching and renewing this call for now: a call the Runner is
                # already running stays bounded by its control lease, and this call_id is
                # reconciled once the Runner answers. Declaring it unknown here would
                # invent a verdict the execution ledger never gave.
                self.business.unreachable(run_id, ticket.call_id)

    def sweep(self, limit: int = 20, offset: int = 0) -> int:
        """Re-reconcile Runs the scheduler itself cannot claim.

        An unconfirmed call is never retried, but its ledger must still be read again: this is
        where a Runner that answers later releases the Run, and where a Run an operator ended
        gets its stop confirmed. Returns how many Runs were swept so the caller can rotate a
        bounded window.
        """
        run_ids = self.business.reconcilable_runs(limit, offset)
        for run_id in run_ids:
            self.reconcile(run_id)
        return len(run_ids)

    def _settle_undispatched(
        self, run_id: UUID, ticket: ExecutionRequest, reason: str = "cancelled_before_dispatch"
    ) -> None:
        # The Runner's ledger confirms this call_id was never accepted, so recording the
        # refusal releases the run without replaying or duplicating any effect.
        self.business.accept(
            ExecutionRecord(
                request=ticket,
                status="cancelled",
                reason_code=reason,
                observation=ExecutionObservation(
                    started=False,
                    process_active=False,
                    connection_open=False,
                    observed_at=datetime.now(UTC),
                ),
            )
        )

    def _confirm_stop(self, ticket: ExecutionRequest) -> ExecutionRecord | None:
        # The Runner stops and records the stop; a refusal leaves its own verdict standing.
        try:
            return self.runner.cancel(ticket.call_id, ticket.lease_generation)
        except httpx.HTTPStatusError as rejection:
            if rejection.response.status_code >= 500:
                raise
            return None

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
            if not self._ready_for(ticket):
                # Readiness lapsed while this call was running: stop renewing its lease, so the
                # execution side's own watchdog ends it and says so, rather than the control plane
                # pretending it is still a healthy real execution.
                return None
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
