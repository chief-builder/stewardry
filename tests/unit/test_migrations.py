import stat
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, text

from stewardry.platform.db import migrate
from stewardry.platform.db.engine import make_engine
from stewardry.platform.db.schema import metadata


def test_upgrade_creates_every_table(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert tables == set(metadata.tables) | {"alembic_version"}


def test_migrations_match_the_schema_exactly(engine: Engine) -> None:
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), metadata)
    assert diffs == []


def test_downgrade_to_base_removes_every_table(engine: Engine) -> None:
    migrate.downgrade(engine, "base")
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}


def test_upgrade_is_repeatable(engine: Engine) -> None:
    migrate.upgrade(engine)
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version == migrate.head_revision() == "0008"


def test_round_trip_down_and_up(tmp_path: Path) -> None:
    eng = make_engine(tmp_path / "roundtrip.db")
    migrate.upgrade(eng)
    migrate.downgrade(eng, "base")
    migrate.upgrade(eng)
    assert set(inspect(eng).get_table_names()) == set(metadata.tables) | {"alembic_version"}
    eng.dispose()


def test_every_connection_enforces_foreign_keys_and_uses_wal(engine: Engine) -> None:
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"


def test_database_files_are_owner_only(tmp_path: Path, engine: Engine) -> None:
    with engine.connect() as conn:  # keep a connection open so WAL files exist
        conn.execute(text("SELECT 1"))
        assert (tmp_path / "test.db-wal").exists()
    for name in ("test.db", "test.db-wal", "test.db-shm"):
        path = tmp_path / name
        if path.exists():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, name
