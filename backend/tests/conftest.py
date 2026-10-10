"""Fixture state for the checks that own PostgreSQL.

Physical execution capacity is counted from the calls themselves: a call holds a global slot and its
target address's slot until the execution side confirms that nothing it started is still running
(spec 0006 section 7, ADR-0016). That makes the Run-scoped business tables part of the *fixture*
state rather than a private detail of each check. A check that records a call and never offers it to
an execution side leaves a slot held — exactly as a real deployment would — and a suite that
accumulated four of them would then be measuring its own leftovers instead of the behaviour under
test.

`HUNTWEAVE_DISPOSABLE_TEST_DATABASE=1` is the deployment saying this database may be thrown away, so
the Run-scoped tables are cleared before each check that runs. Identity is *not* cleared: the access
key state, the console's sessions and the scheduler's heartbeat belong to the deployment the checks
run against rather than to any one check's fixture, and clearing them would mean testing a different
deployment than the one that is up.
"""

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import text

from huntweave.storage.database import connect_engine
from huntweave.storage.models import Base

DISPOSABLE = "HUNTWEAVE_DISPOSABLE_TEST_DATABASE"

# Tables the deployment owns rather than a check's fixture. `runtime_processes` is not modelled and
# is therefore never touched: the scheduler's heartbeat is the running deployment's, and
# `verify_agentd` reads it.
DEPLOYMENT_OWNED = frozenset({"access_key_state", "web_sessions"})

RUN_SCOPED = tuple(
    table.name for table in Base.metadata.sorted_tables if table.name not in DEPLOYMENT_OWNED
)


@pytest.fixture(autouse=True)
def disposable_database() -> Iterator[None]:
    """Start every check from an empty Run-scoped ledger, when the database is disposable."""
    if os.environ.get(DISPOSABLE) != "1":
        yield
        return
    engine = connect_engine("migrator")
    try:
        targets = ", ".join(f"huntweave.{name}" for name in RUN_SCOPED)
        with engine.begin() as connection:
            # One statement, so the foreign keys between them are handled by the truncation itself
            # instead of by an order this list would have to keep matching.
            connection.execute(text(f"TRUNCATE {targets} CASCADE"))
    finally:
        engine.dispose()
    yield
