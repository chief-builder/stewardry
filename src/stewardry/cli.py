"""Command-line entry point. Commands are added as milestone steps land."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from stewardry import __version__
from stewardry.config import DataDirError, database_path, ensure_data_dir, resolve_data_dir
from stewardry.platform import audit, registry
from stewardry.platform.audit import AuditLog
from stewardry.platform.db import migrate
from stewardry.platform.db.engine import make_engine
from stewardry.platform.policy import PolicyError, json_schema, load_agent_policy, load_baseline
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


AS_HELP = (
    "hub subject (sub) performing the action. The CLI is a local admin tool: this is "
    "asserted by the operator, checked against Stewardry's role mapping, and audited"
)


def _with_engine(args: argparse.Namespace, action: str) -> int:
    data_dir = _data_dir()
    engine = _existing_engine(data_dir) if data_dir else None
    if engine is None:
        return EXIT_USAGE
    log = AuditLog()
    try:
        if action == "apply":
            agent_file = load_agent_policy(Path(args.file))
            baseline = load_baseline(Path(args.baseline))
            result = registry.apply_policy(engine, log, args.actor, agent_file, baseline)
            state = "new version" if result.created else "unchanged"
            print(
                f"{result.agent_id}: policy {result.policy_hash[:12]} ({state}, "
                f"version {result.policy_version_id}); re-evaluation required: "
                f"{result.reevaluation_required}"
            )
        elif action == "show":
            with engine.connect() as conn:
                agent = registry.get_agent(conn, args.agent_id)
                version = registry.current_policy_version(conn, args.agent_id)
            if agent is None:
                print(f"error: agent {args.agent_id} is not registered", file=sys.stderr)
                return EXIT_USAGE
            print(f"{agent.agent_id}: {agent.engagement_status}, {agent.operational_state}")
            if version is not None:
                print(
                    f"policy {version.policy_hash} (version {version.id}); "
                    f"re-evaluation required: {version.reevaluation_required}"
                )
        elif action == "transition":
            registry.transition_status(
                engine, log, args.actor, args.agent_id, args.status, args.evidence
            )
            print(f"{args.agent_id}: moved to {args.status}")
        elif action == "accept-eval":
            registry.accept_evaluation(
                engine, log, args.actor, args.agent_id, args.policy_hash, args.report
            )
            print(f"{args.agent_id}: evaluation accepted for {args.policy_hash[:12]}")
        else:
            operation = {
                "pause": registry.pause,
                "resume": registry.resume,
                "retire": registry.retire,
            }[action]
            operation(engine, log, args.actor, args.agent_id, args.reason)
            print(f"{args.agent_id}: {action}d")
    except (registry.RegistryError, PolicyError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_PROBLEMS
    finally:
        engine.dispose()
    return EXIT_OK


def _add_registry_commands(commands: Any) -> None:
    parser = commands.add_parser("registry", help="agent registry and lifecycle")
    sub = parser.add_subparsers(title="registry commands", required=True)

    schema = sub.add_parser("schema", help="print the JSON Schema for agent policy files")
    schema.set_defaults(handler=lambda _a: print(json_schema(), end="") or EXIT_OK)

    apply = sub.add_parser("apply", help="register an agent or apply a new policy version")
    apply.add_argument("file", help="agent policy file (policies/agents/*.yaml)")
    apply.add_argument("--baseline", default="policies/platform-baseline.yaml")

    show = sub.add_parser("show", help="show an agent's status and current policy version")
    show.add_argument("agent_id")

    transition = sub.add_parser("transition", help="move an agent through a rollout gate")
    transition.add_argument("agent_id")
    transition.add_argument("status")
    transition.add_argument("--evidence", required=True, help="gate evidence reference")

    accept = sub.add_parser("accept-eval", help="accept an eval report for the current policy")
    accept.add_argument("agent_id")
    accept.add_argument("--policy-hash", required=True)
    accept.add_argument("--report", required=True, help="eval report reference")

    operations = {}
    for name in ("pause", "resume", "retire"):
        operations[name] = sub.add_parser(name, help=f"{name} an agent (reason required)")
        operations[name].add_argument("agent_id")
        operations[name].add_argument("--reason", required=True)

    for name, command in [
        ("apply", apply),
        ("show", show),
        ("transition", transition),
        ("accept-eval", accept),
        *operations.items(),
    ]:
        if name != "show":
            command.add_argument("--as", dest="actor", required=True, help=AS_HELP)
        command.set_defaults(handler=lambda a, action=name: _with_engine(a, action))


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

    _add_registry_commands(commands)

    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return EXIT_OK
    result: int = args.handler(args)
    return result
