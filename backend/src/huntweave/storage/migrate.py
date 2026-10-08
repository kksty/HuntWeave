from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from huntweave.harness.checkpoints import migrate_checkpoints
from huntweave.storage.database import connect_engine


def migrate() -> None:
    engine = connect_engine("migrator")
    try:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[3] / "migrations")
        )
        with engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(486735942)"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        # Separate migration ownership and transaction semantics from the business schema.
        migrate_checkpoints()
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        migrate()
    except Exception as error:
        print(f'{{"reason_code":"migration_failed","error_type":"{type(error).__name__}"}}')
        raise SystemExit(1) from None
    print('{"event":"migrations_complete"}')
