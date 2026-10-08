import httpx

from huntweave.config import AppSettings
from huntweave.execution.client import get_capabilities
from huntweave.harness.checkpoints import verify_checkpoint_schema
from huntweave.storage.database import connect_engine, verify_agentd, verify_business_schema


def main() -> int:
    engine = None
    try:
        if not AppSettings.from_env().access_key:
            print('{"reason_code":"access_key_missing"}')
            return 1
        response = httpx.get("http://127.0.0.1:8000/health/live", timeout=2, trust_env=False)
        response.raise_for_status()
        engine = connect_engine()
        verify_business_schema(engine)
        verify_checkpoint_schema()
        verify_agentd(engine)
        get_capabilities()
    except Exception as error:
        print(f'{{"reason_code":"readiness_failed","error_type":"{type(error).__name__}"}}')
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
