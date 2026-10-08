from sqlalchemy import Engine, create_engine, text

from huntweave.config import database_url

BUSINESS_REVISION = "0001_runtime"


def connect_engine(role: str = "app") -> Engine:
    return create_engine(
        database_url(role),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )


def verify_business_schema(engine: Engine) -> None:
    with engine.connect() as connection:
        revision = connection.scalar(text("SELECT version_num FROM huntweave.alembic_version"))
        if revision != BUSINESS_REVISION:
            raise RuntimeError("Business schema migration is required")


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
