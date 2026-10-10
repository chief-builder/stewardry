"""Command-line entry point. Commands are added as milestone steps land."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import Engine

from stewardry import __version__
from stewardry.config import DataDirError, database_path, ensure_data_dir, resolve_data_dir
from stewardry.platform import audit
from stewardry.platform.db import migrate
from stewardry.platform.db.engine import make_engine
from stewardry.platform.redaction import install_log_redaction

EXIT_OK, EXIT_PROBLEMS, EXIT_USAGE = 0, 1, 2


def _data_dir() -> Path | None:
    try:
        return ensure_data_dir(resolve_data_dir())
    except DataDirError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _existing_engine(data_dir: Path) -> Engine | None:
    db_path = database_path(data_dir)
    if not db_path.exists():
        print(f"error: no database at {db_path}; run: stewardry db upgrade", file=sys.stderr)
        return None
    return make_engine(db_path)


def _anchor_file(args: argparse.Namespace, data_dir: Path) -> Path:
    return Path(args.anchor_file) if args.anchor_file else data_dir / "audit-anchors.jsonl"


def _db_upgrade(_args: argparse.Namespace) -> int:
    data_dir = _data_dir()
    if data_dir is None:
        return EXIT_USAGE
    db_path = database_path(data_dir)
    engine = make_engine(db_path)
    try:
        migrate.upgrade(engine)
    finally:
        engine.dispose()
    print(f"database is at the latest schema: {db_path}")
    return EXIT_OK


def _audit_verify(args: argparse.Namespace) -> int:
    data_dir = _data_dir()
    engine = _existing_engine(data_dir) if data_dir else None
    if data_dir is None or engine is None:
        return EXIT_USAGE
    anchor_file = _anchor_file(args, data_dir)
    try:
        with engine.connect() as conn:
            result = audit.verify(conn, audit.read_anchors(anchor_file))
    finally:
        engine.dispose()
    for problem in result.problems:
        print(f"problem: {problem}")
    print(
        f"audit: {result.events} event(s), head seq {result.head_seq}, "
        f"{result.anchors_checked} anchor(s) checked from {anchor_file}"
    )
    if result.unanchored_tail:
        print(
            f"note: the last {result.unanchored_tail} event(s) are after the newest anchor; "
            "truncation there cannot be detected. Run: stewardry audit anchor"
        )
    print("audit: OK" if result.ok else "audit: FAILED")
    return EXIT_OK if result.ok else EXIT_PROBLEMS


def _audit_anchor(args: argparse.Namespace) -> int:
    data_dir = _data_dir()
    engine = _existing_engine(data_dir) if data_dir else None
    if data_dir is None or engine is None:
        return EXIT_USAGE
    anchor_file = _anchor_file(args, data_dir)
    try:
        with engine.connect() as conn:
            anchor = audit.write_anchor(conn, anchor_file)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        engine.dispose()
    print(f"anchored seq {anchor.seq} ({anchor.event_hash}) in {anchor_file}")
    print("Keep a copy of this file outside the data directory.")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    install_log_redaction()
    parser = argparse.ArgumentParser(prog="stewardry")
    parser.add_argument("--version", action="version", version=f"stewardry {__version__}")
    commands = parser.add_subparsers(title="commands")

    db = commands.add_parser("db", help="database commands")
    db_commands = db.add_subparsers(title="db commands", required=True)
    upgrade = db_commands.add_parser(
        "upgrade", help="create or migrate the database (data dir: $STEWARDRY_DATA_DIR)"
    )
    upgrade.set_defaults(handler=_db_upgrade)

    audit_parser = commands.add_parser("audit", help="audit log commands")
    audit_commands = audit_parser.add_subparsers(title="audit commands", required=True)
    for name, handler, help_text in [
        ("verify", _audit_verify, "recompute the hash chain and check it against anchors"),
        ("anchor", _audit_anchor, "append the current chain head to the anchor file"),
    ]:
        sub = audit_commands.add_parser(name, help=help_text)
        sub.add_argument(
            "--anchor-file", help="anchor file (default: <data dir>/audit-anchors.jsonl)"
        )
        sub.set_defaults(handler=handler)

    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return EXIT_OK
    result: int = args.handler(args)
    return result
