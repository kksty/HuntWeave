from datetime import datetime
from typing import cast

from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.orm import Session

from huntweave.config import database_url

# The business revisions this build may serve. `alembic upgrade head` always leaves the newest
# revision behind, so checking for one exact value fails the moment a new migration lands — even
# when the build and the schema are perfectly in step. What the gate has to catch is the opposite
# direction: a schema this build does not know, either too old to have the columns it reads or
# newer than what it was tested against. Adding a migration means adding its id here, in the same
# commit as the migration itself.
BUSINESS_REVISIONS = ("0008_retention_decisions", "0009_planning_attempt_identity")


def database_now(session: Session) -> datetime:
    return cast(datetime, session.scalar(select(func.clock_timestamp())))


def connect_engine(role: str = "app") -> Engine:
    return create_engine(
        database_url(role),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )


def verify_business_schema(engine: Engine) -> None:
    with engine.connect() as connection:
        revision = connection.scalar(text("SELECT version_num FROM huntweave.alembic_version"))
        if revision not in BUSINESS_REVISIONS:
            raise RuntimeError(
                "Business schema revision is not one this build serves: "
                f"{revision!r} not in {BUSINESS_REVISIONS}"
            )


def verify_agentd(engine: Engine) -> None:
    with engine.connect() as connection:
        alive = connection.scalar(
            text(
                "SELECT heartbeat_at > clock_timestamp() - interval '10 seconds' "
                "FROM huntweave.runtime_processes WHERE name = 'agentd'"
            )
        )
        if alive is not True:
            raise RuntimeError("agentd heartbeat is missing or expired")
