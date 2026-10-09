from huntweave.execution.client import get_capabilities


def main() -> int:
    try:
        # A reachable endpoint is not enough: an execution side whose ledger cannot be opened
        # must not be reported healthy to the deployment.
        if not get_capabilities("http://127.0.0.1:8001").fake_execution_ready:
            print('{"reason_code":"runner_state_unavailable"}')
            return 1
    except Exception:
        print('{"reason_code":"runner_unavailable"}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
