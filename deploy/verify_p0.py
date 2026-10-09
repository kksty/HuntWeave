"""Exercise P0 recovery through a disposable local Compose stack and public APIs.

This script intentionally refuses the daily development Compose project. It leaves
its demonstration records intact for inspection and restores injected faults in
finally blocks. No network probes or user commands are sent to a target.
"""

import argparse
import http.cookiejar
import json
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

REPOSITORY = Path(__file__).resolve().parent.parent
DISPOSABLE_PROJECT = "huntweave-p0-checks"


class ServiceUnavailable(RuntimeError):
    """A bounded poll may retry a temporary control-plane outage."""


class Probe:
    def __init__(self, project: str, base_url: str):
        parsed = urlsplit(base_url)
        if project != DISPOSABLE_PROJECT:
            raise ValueError(f"Failure injection requires --project {DISPOSABLE_PROJECT}")
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.port in {None, 8000} or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("Use an isolated http://127.0.0.1:<port> origin, never port 8000")
        self.base_url = base_url
        self.environment = {**os.environ, "HUNTWEAVE_WEB_PORT": str(parsed.port),
                            "HUNTWEAVE_PUBLIC_ORIGIN": base_url}
        self.command = ["docker", "compose", "--project-name", project,
                        "-f", str(REPOSITORY / "deploy" / "compose.yaml")]
        self.browser = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = ""

    def compose(self, *arguments: str) -> str:
        result = subprocess.run([*self.command, *arguments], cwd=REPOSITORY,
                                env=self.environment, text=True, capture_output=True)
        if result.returncode:
            # The command may hold credentials in a Python expression; never echo it.
            raise RuntimeError(f"Compose operation failed (exit {result.returncode})")
        return result.stdout.strip()

    def sql(self, statement: str) -> None:
        self.compose("exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "huntweave",
                     "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-c", statement)

    def app_restart_count(self) -> int:
        container = self.compose("ps", "-a", "-q", "app")
        result = subprocess.run(["docker", "inspect", container], text=True,
                                capture_output=True, check=True)
        return json.loads(result.stdout)[0]["RestartCount"]

    def request(self, path: str, body: dict | None = None, *, expected: int = 200,
                extra_headers: dict | None = None) -> dict | list:
        headers = {"Origin": self.base_url, "X-CSRF-Token": self.csrf,
                   "Content-Type": "application/json", **(extra_headers or {})}
        request = Request(self.base_url + path, headers=headers,
                          data=json.dumps(body).encode() if body is not None else None)
        try:
            response = self.browser.open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            payload = response.read()
            if response.status != expected:
                if response.status == 503:
                    raise ServiceUnavailable(f"{path}: control plane temporarily unavailable")
                reason = json.loads(payload).get("reason_code", "unexpected_response")
                raise AssertionError(f"{path}: expected {expected}, got {response.status}: {reason}")
            return json.loads(payload) if payload else {}

    def login(self) -> None:
        key = (REPOSITORY / "runtime" / "secrets" / "access_key").read_text().strip()
        session = self.request("/auth/login", {"access_key": key})
        self.csrf = session["csrf_token"]

    def wait(self, get, condition, *, timeout: float = 90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = get()
                if condition(value):
                    return value
            except (URLError, TimeoutError, ConnectionError, ServiceUnavailable):
                pass
            time.sleep(0.25)
        raise AssertionError("Timed out waiting for observable P0 state")

    def ready(self) -> None:
        self.compose("up", "-d", "--wait", "--wait-timeout", "120")
        published = self.compose("port", "app", "8000")
        assert published == self.base_url.removeprefix("http://"), (
            "Public API origin does not belong to the disposable Compose app"
        )

    def create_run(self, scenario: str = "positive", duration_ms: int = 1500) -> str:
        project = self.request("/api/v1/projects", {"name": f"P0 fault probe {uuid.uuid4().hex[:8]}"},
                               expected=201)
        now = datetime.now(UTC)
        scope = self.request("/api/v1/scopes", {
            "project_id": project["id"], "targets_text": "192.0.2.1",
            "ports": {"profile": "custom-tcp-v1", "custom": "80"},
            "starts_at": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "authorization": "Disposable local fixed-action demo; no target connections",
        }, expected=201)
        run = self.request("/api/v1/runs", {"scope_id": scope["id"], "scope_version": 1,
                                          "demonstration_scenario": scenario,
                                          "demonstration_duration_ms": duration_ms},
                           expected=201, extra_headers={"Idempotency-Key": uuid.uuid4().hex})
        self.request(f"/api/v1/runs/{run['id']}/start", {"version": run["version"]})
        return run["id"]

    def ledger(self, run_id: str) -> list[dict]:
        # Read only fixed fake Runner records, never secrets or container logs.
        run_id = str(uuid.UUID(run_id))
        script = ("import json; from pathlib import Path; "
                  "path=Path('/runner-state/ledger.json'); "
                  "data=json.loads(path.read_text()) if path.exists() else {}; "
                  f"print(json.dumps([v['record'] for v in data.values() "
                  f"if v['record']['request']['run_id']=='{run_id}']))")
        return json.loads(self.compose("exec", "-T", "runner", "python", "-c", script))

    def running(self, run_id: str) -> dict:
        records = self.wait(lambda: self.ledger(run_id),
                            lambda items: any(item["status"] == "running" for item in items))
        return next(item for item in records if item["status"] == "running")

    @staticmethod
    def assert_once(records: list[dict]) -> None:
        ids = [record["request"]["call_id"] for record in records]
        assert len(ids) == len(set(ids)), "Duplicate durable call IDs"
        for record in records:
            starts = [event for event in record["events"] if event["type"] == "execution_started"]
            assert len(starts) <= 1, "Fixed action executed more than once"

    def restart_fault(self) -> None:
        run_id = self.create_run(duration_ms=30000)
        active = self.running(run_id)
        call_id = active["request"]["call_id"]
        self.compose("restart", "runner")
        self.ready()
        records = self.wait(lambda: self.ledger(run_id),
                            lambda items: any(item["status"] == "unknown" for item in items))
        unknown = next(item for item in records if item["request"]["call_id"] == call_id)
        assert unknown["status"] == "unknown"
        assert unknown["reason_code"] == "execution_unknown"
        time.sleep(3)
        records = self.ledger(run_id)
        assert len(records) == 1, "Unknown action was followed by a new action"
        self.assert_once(records)
        print("PASS: Runner restart preserves unknown execution without replay")

    def database_outage_fault(self) -> None:
        run_id = self.create_run(duration_ms=30000)
        self.running(run_id)
        try:
            self.compose("stop", "postgres")
            records = self.wait(lambda: self.ledger(run_id),
                                lambda items: any(item["status"] == "cancelled" for item in items),
                                timeout=25)
            assert len(records) == 1, "New dispatch during business database outage"
            assert records[0]["reason_code"] == "control_lease_expired"
        finally:
            self.ready()
        time.sleep(3)
        records = self.ledger(run_id)
        self.assert_once(records)
        assert records[0]["status"] == "cancelled"
        print("PASS: PostgreSQL outage stops execution at lease expiry and preserves ledger")

    def app_restart_fault(self) -> str:
        run_id = self.create_run(duration_ms=6000)
        active = self.running(run_id)
        first_id = active["request"]["call_id"]
        self.compose("restart", "app")
        self.ready()
        self.wait(lambda: self.request(f"/api/v1/runs/{run_id}"),
                  lambda run: run["status"] == "waiting" and run["phase"] == "awaiting_human")
        records = self.ledger(run_id)
        # The ledger is keyed by call id, so its order is not creation order: the restart
        # must reuse the accepted call exactly once rather than dispatch a second action.
        call_ids = [record["request"]["call_id"] for record in records]
        assert call_ids.count(first_id) == 1
        self.assert_once(records)
        assert all(record["status"] == "completed" for record in records)
        print("PASS: app restart resumes the same Run and reuses accepted actions")
        return run_id

    def snapshot(self, run_id: str) -> dict:
        return self.request(f"/api/v1/runs/{run_id}/snapshot")

    @staticmethod
    def reconciliation_path(run_id: str, call_id: str) -> str:
        return f"/api/v1/runs/{run_id}/calls/{call_id}/reconciliation"

    def call(self, run_id: str, call_id: str) -> dict:
        calls = [item for item in self.snapshot(run_id)["calls"] if item["id"] == str(call_id)]
        assert len(calls) == 1
        return calls[0]

    def unknown_call_after_runner_restart(self, run_id: str) -> str:
        """Restart the Runner around one fixed action and return its unconfirmed call id."""
        call_id = self.running(run_id)["request"]["call_id"]
        self.compose("restart", "runner")
        self.ready()
        self.wait(
            lambda: self.call(run_id, call_id),
            lambda item: item["status"] == "unknown" and item["observation"] is not None,
        )
        return str(call_id)

    def reconciliation_flow(self) -> str:
        """An operator rules on an unknown call, and the Run converges on the facts.

        The Runner restart leaves the outcome unconfirmed while proving nothing about the
        process it left behind: the verdict settles the outcome, the stop still has to come
        from the execution side, and the Run ends marked as limited rather than normal.
        """
        run_id = self.create_run(duration_ms=30000)
        call_id = self.unknown_call_after_runner_restart(run_id)
        call = self.call(run_id, call_id)
        assert call["observation"]["started"] is True
        assert call["observation"]["process_active"] is None, "Ledger claimed an unproven stop"
        assert call["conditions"] == ["outcome_unsettled", "stop_unconfirmed"]

        # The ledger proves the action started, so an operator's click cannot call it back.
        contradicted = self.request(
            self.reconciliation_path(run_id, call_id),
            {"outcome": "not_executed", "version": self.snapshot(run_id)["run"]["version"],
             "evidence_ids": [], "note": "probe: must be refused"},
            expected=409,
        )
        assert contradicted["reason_code"] == "reconciliation_evidence_contradicted"

        # Confirming execution settles the outcome without inventing a result or a stop.
        verified = self.request(
            self.reconciliation_path(run_id, call_id),
            {"outcome": "executed", "version": self.snapshot(run_id)["run"]["version"],
             "evidence_ids": [], "note": "probe: fixed action started, result never arrived"},
        )
        assert verified["call"]["status"] == "incomplete"
        assert verified["call"]["result"] is None
        assert verified["call"]["reconciliation"]["outcome"] == "executed"
        assert verified["call"]["reconciliation"]["redispatch_authorized"] is False
        assert verified["call"]["observation"]["stop_confirmed"] is False

        preview = self.request(f"/api/v1/runs/{run_id}/resume-preview")
        assert preview["can_resume"] is False
        assert preview["reason_code"] == "execution_stop_unconfirmed"
        assert preview["pending_calls"][0]["conditions"] == ["stop_unconfirmed"]

        # Cancelling stays available, and only the execution side's own stop confirmation
        # lets the Run converge - as a limited end, not as a normal one.
        self.request(
            f"/api/v1/runs/{run_id}/cancel",
            {"version": self.snapshot(run_id)["run"]["version"]},
        )
        settled = self.wait(
            lambda: self.request(f"/api/v1/runs/{run_id}"),
            lambda run: run["status"] == "cancelled",
        )
        assert settled["reason_code"] == "result_incomplete"
        events = self.request(f"/api/v1/runs/{run_id}/event-history?after=0&limit=500")["events"]
        types = [event["type"] for event in events]
        assert types.count("reconciliation_recorded") == 1
        assert types.count("execution_stopped") == 1
        closed = [event for event in events if event["type"] == "run_cancelled"][-1]["payload"]
        assert closed["limited"] is True and closed["incomplete_calls"] == [call_id]
        print("PASS: operator reconciliation settles an unknown call and converges limited")
        return run_id

    def unknown_run_for_console(self) -> str:
        """Leave one unknown call for the browser flow, where the operator rules on it."""
        run_id = self.create_run(duration_ms=30000)
        self.unknown_call_after_runner_restart(run_id)
        print(f"CONSOLE_RECONCILIATION_RUN={run_id}")
        return run_id

    def checkpoint_fault(self) -> None:
        run_id = self.create_run(duration_ms=6000)
        self.running(run_id)
        restart_count = self.app_restart_count()
        try:
            self.sql("REVOKE INSERT, UPDATE ON ALL TABLES IN SCHEMA huntweave_checkpoint "
                     "FROM huntweave_checkpoint")
            settled = self.wait(lambda: self.snapshot(run_id),
                                lambda snapshot: snapshot["budget"]["settled_tool_calls"] >= 1)
            assert settled["budget"]["settled_tool_calls"] <= len(settled["calls"])
            self.wait(self.app_restart_count, lambda count: count > restart_count, timeout=30)
        finally:
            self.sql("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
                     "huntweave_checkpoint TO huntweave_checkpoint")
        self.wait(lambda: self.request(f"/api/v1/runs/{run_id}"),
                  lambda run: run["status"] == "waiting" and run["phase"] == "awaiting_human")
        snapshot = self.snapshot(run_id)
        self.assert_once(self.ledger(run_id))
        assert snapshot["budget"]["reserved_tool_calls"] == len(snapshot["calls"])
        assert snapshot["budget"]["settled_tool_calls"] == len(snapshot["calls"])
        assert len({item["id"] for item in snapshot["decisions"]}) == len(snapshot["decisions"])
        print("PASS: checkpoint write failure recovers without duplicate actions or settlement")

    def evidence_loss_fault(self, run_id: str) -> None:
        records = self.ledger(run_id)
        evidence = records[0]["result"]["evidence"][0]
        evidence_id = str(uuid.UUID(evidence["id"]))
        before = self.request(f"/api/v1/evidence/{evidence_id}")
        assert before["available"] and before["content"]
        # Resolve and contain the archive path before the reversible rename.
        script = ("from pathlib import Path; root=Path('/evidence').resolve(); "
                  f"path=(root / {evidence['relative_path']!r}).resolve(); "
                  "assert path.is_relative_to(root) and path.is_file(); "
                  "path.rename(path.with_suffix('.fault-backup'))")
        restore = ("from pathlib import Path; root=Path('/evidence').resolve(); "
                   f"path=(root / {evidence['relative_path']!r}).resolve(); "
                   "assert path.is_relative_to(root); "
                   "path.with_suffix('.fault-backup').rename(path)")
        try:
            self.compose("exec", "-T", "runner", "python", "-c", script)
            missing = self.request(f"/api/v1/evidence/{evidence_id}")
            assert not missing["available"] and not missing["content"]
            assert missing["missing_reason"] == "archive_missing"
        finally:
            self.compose("exec", "-T", "runner", "python", "-c", restore)
        restored = self.request(f"/api/v1/evidence/{evidence_id}")
        assert restored["available"] and restored["sha256"] == before["sha256"]
        print("PASS: missing archive reports evidence failure and restoration retains hash")

    def history(self, run_id: str) -> None:
        cursor = 0
        events = []
        while True:
            page = self.request(f"/api/v1/runs/{run_id}/event-history?after={cursor}&limit=2")
            assert not page["gap"]
            events.extend(page["events"])
            next_cursor = page["next_cursor"]
            if next_cursor <= cursor or not page["events"]:
                break
            cursor = next_cursor
        cursors = [event["cursor"] for event in events]
        assert cursors == list(range(1, cursor + 1)), "Event history has gaps or duplicate cursors"
        assert cursor == self.snapshot(run_id)["cursor"]
        print("PASS: paginated committed event history matches the final snapshot")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    options = parser.parse_args()
    try:
        probe = Probe(options.project, options.base_url)
    except ValueError as error:
        parser.error(str(error))
    probe.ready()
    probe.login()
    run_id = probe.app_restart_fault()
    probe.history(run_id)
    probe.evidence_loss_fault(run_id)
    probe.restart_fault()
    probe.reconciliation_flow()
    probe.unknown_run_for_console()
    probe.database_outage_fault()
    probe.checkpoint_fault()
    print("Live recovery probes passed; use backend tests and Playwright for remaining P0 contracts")


if __name__ == "__main__":
    main()
