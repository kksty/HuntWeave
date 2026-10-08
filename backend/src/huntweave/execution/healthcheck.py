from huntweave.execution.client import get_capabilities


def main() -> int:
    try:
        get_capabilities("http://127.0.0.1:8001")
    except Exception:
        print('{"reason_code":"runner_unavailable"}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
