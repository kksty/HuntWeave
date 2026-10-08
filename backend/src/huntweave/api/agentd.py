import signal
import threading
from types import FrameType
from uuid import uuid4

from sqlalchemy import text

from huntweave.harness.checkpoints import verify_checkpoint_schema
from huntweave.storage.database import connect_engine, verify_business_schema


def main() -> int:
    stopping = threading.Event()

    def stop(signum: int, frame: FrameType | None) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    engine = connect_engine()
    instance_id = uuid4()
    try:
        verify_business_schema(engine)
        verify_checkpoint_schema()
        while not stopping.is_set():
            # This P0-A daemon proves process supervision and storage readiness only.
            # Queue claiming and graph advancement are introduced with P0-C.
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO huntweave.runtime_processes (name, instance_id, heartbeat_at) "
                        "VALUES ('agentd', :instance_id, clock_timestamp()) "
                        "ON CONFLICT (name) DO UPDATE SET instance_id=EXCLUDED.instance_id, "
                        "heartbeat_at=EXCLUDED.heartbeat_at"
                    ),
                    {"instance_id": instance_id},
                )
            stopping.wait(2)
    except Exception as error:
        print(f'{{"reason_code":"storage_unavailable","error_type":"{type(error).__name__}"}}')
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
