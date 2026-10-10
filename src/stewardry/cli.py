"""Command-line entry point. Commands are added as milestone steps land."""

import argparse
import sys
from collections.abc import Sequence

from stewardry import __version__
from stewardry.config import DataDirError, database_path, ensure_data_dir, resolve_data_dir
from stewardry.platform.db import migrate
from stewardry.platform.db.engine import make_engine


def _db_upgrade(_args: argparse.Namespace) -> int:
    try:
        data_dir = ensure_data_dir(resolve_data_dir())
    except DataDirError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    db_path = database_path(data_dir)
    engine = make_engine(db_path)
    try:
        migrate.upgrade(engine)
    finally:
        engine.dispose()
    print(f"database is at the latest schema: {db_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stewardry")
    parser.add_argument("--version", action="version", version=f"stewardry {__version__}")
    commands = parser.add_subparsers(title="commands")

    db = commands.add_parser("db", help="database commands")
    db_commands = db.add_subparsers(title="db commands", required=True)
    upgrade = db_commands.add_parser(
        "upgrade", help="create or migrate the database (data dir: $STEWARDRY_DATA_DIR)"
    )
    upgrade.set_defaults(handler=_db_upgrade)

    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0
    result: int = args.handler(args)
    return result
