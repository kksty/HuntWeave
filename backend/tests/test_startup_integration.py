import os

import httpx
import pytest
from huntweave.execution.client import get_capabilities
from huntweave.harness.checkpoints import checkpoint_connection, verify_checkpoint_schema
from huntweave.storage.database import connect_engine, verify_agentd, verify_business_schema
from huntweave.storage.migrate import migrate
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

pytestmark = pytest.mark.integration


def test_live_stack_requires_authentication_at_every_business_entry() -> None:
    base = os.environ["HUNTWEAVE_APP_URL"]
    with httpx.Client(base_url=base, trust_env=False) as client:
        assert client.get("/health/live").json() == {"status": "alive"}
        for path in ("/", "/docs", "/openapi.json", "/api/v1/system/capabilities", "/evidence/x"):
            assert client.get(path).status_code == 401


def test_migrations_can_repeat_and_process_remains_ready() -> None:
    migrate()
    migrate()
    engine = connect_engine()
    try:
        verify_business_schema(engine)
        verify_agentd(engine)
        verify_checkpoint_schema()
    finally:
        engine.dispose()


def test_business_runtime_cannot_create_tables_or_read_checkpoints() -> None:
    engine = connect_engine()
    try:
        for sql in (
            "CREATE TABLE huntweave.forbidden (id integer)",
            "SELECT * FROM huntweave_checkpoint.checkpoints",
        ):
            with engine.connect() as connection:
                with pytest.raises(ProgrammingError) as error:
                    connection.execute(text(sql))
                assert getattr(error.value.orig, "sqlstate", None) == "42501"
    finally:
        engine.dispose()


def test_checkpoint_runtime_is_isolated_from_business_schema() -> None:
    from psycopg.errors import InsufficientPrivilege

    with checkpoint_connection() as connection:
        with pytest.raises(InsufficientPrivilege):
            connection.execute("SELECT * FROM huntweave.runtime_processes")


def test_runner_reports_environment_unsupported_through_real_interface() -> None:
    capability = get_capabilities()
    assert capability.real_execution_ready is False
    assert capability.fake_execution_ready is False
    assert capability.reason_code == "environment_unsupported"
