from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row

from huntweave.config import database_url


@contextmanager
def checkpoint_connection(role: str = "checkpoint") -> Iterator[Connection[dict[str, object]]]:
    url = database_url(role).set(drivername="postgresql")
    with Connection.connect(
        url.render_as_string(hide_password=False),
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        options="-c search_path=huntweave_checkpoint,pg_catalog",
        connect_timeout=3,
    ) as connection:
        yield connection


def migrate_checkpoints() -> None:
    with checkpoint_connection("migrator") as connection:
        connection.execute("SELECT pg_advisory_lock(486735943)")
        try:
            PostgresSaver(connection).setup()
        finally:
            connection.execute("SELECT pg_advisory_unlock(486735943)")


def verify_checkpoint_schema() -> None:
    with checkpoint_connection() as connection:
        row = connection.execute("SELECT max(v) AS v FROM checkpoint_migrations").fetchone()
        if row is None or row["v"] != len(PostgresSaver.MIGRATIONS) - 1:
            raise RuntimeError("Checkpoint schema migration is required")
