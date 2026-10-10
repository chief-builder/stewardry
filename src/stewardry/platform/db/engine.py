"""SQLite engine with the pragmas every connection needs (ADR 0004)."""

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event


def _on_connect(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.execute("PRAGMA busy_timeout = 5000")
    cursor.close()


def make_engine(db_path: Path) -> Engine:
    # Owner-only. SQLite gives its -wal and -shm files the same permissions.
    if not db_path.exists():
        db_path.touch(mode=0o600)
        db_path.chmod(0o600)  # touch's mode is filtered by the umask
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    event.listen(engine, "connect", _on_connect)
    return engine
