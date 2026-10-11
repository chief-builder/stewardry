"""Command-line entry point. Commands are added as milestone steps land."""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from stewardry import __version__
from stewardry.config import (
    DataDirError,
    artifacts_path,
    database_path,
    ensure_data_dir,
    resolve_data_dir,
)
from stewardry.platform import access, audit, registry
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


def _roles(args: argparse.Namespace) -> int:
    data_dir = _data_dir()
    engine = _existing_engine(data_dir) if data_dir else None
    if engine is None:
        return EXIT_USAGE
    try:
        if args.roles_action == "apply":
            mapping = access.load_access_mapping(Path(args.file))
            summary = access.apply_access_mapping(
                engine,
                AuditLog(),
                args.actor,
                mapping,
                bootstrap=args.bootstrap,
                remap=frozenset(args.remap or []),
            )
            for name, values in summary.__dict__.items():
                if values:
                    print(f"{name.replace('_', ' ')}: {', '.join(values)}")
            print("access mapping applied")
        else:
            with engine.connect() as conn:
                people = access.list_people(conn)
            for person in people:
                print(
                    f"{person.username} ({person.sub}): "
                    f"roles={','.join(sorted(person.roles)) or '-'} "
                    f"customers={','.join(sorted(person.customers)) or '-'}"
                )
    except access.AccessError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_PROBLEMS
    finally:
        engine.dispose()
    return EXIT_OK


HUB_ADMIN_VARIABLES = ("STEWARDRY_HUB_ISSUER", "KEYCLOAK_ADMIN_USER", "KEYCLOAK_ADMIN_PASSWORD")


def _hub_bootstrap(args: argparse.Namespace) -> int:
    import httpx2
    import yaml

    from stewardry.platform import hub_bootstrap as hb

    missing = [name for name in HUB_ADMIN_VARIABLES if not os.environ.get(name)]
    if missing:
        print("error: set " + ", ".join(missing) + " (values are never printed)", file=sys.stderr)
        return EXIT_USAGE
    additions = json.loads(Path(args.additions).read_text(encoding="utf-8"))
    issuer = os.environ["STEWARDRY_HUB_ISSUER"].rstrip("/")
    try:
        base = hb.check_issuer(issuer, additions["realm"])
        with httpx2.Client(timeout=15) as http:
            admin = hb.KeycloakAdmin(base, additions["realm"], http)
            admin.login(
                issuer, os.environ["KEYCLOAK_ADMIN_USER"], os.environ["KEYCLOAK_ADMIN_PASSWORD"]
            )
            result = hb.bootstrap(
                admin,
                issuer,
                additions,
                Path(args.secrets_file),
                args.include_sales2,
                args.with_stewardry_gateway,
            )
    except hb.BootstrapError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_PROBLEMS
    mapping = hb.proposed_mapping(Path(args.example_mapping), result.subjects)
    Path(args.mapping_out).write_text(yaml.safe_dump(mapping, sort_keys=False), encoding="utf-8")
    print(f"client stewardry: {result.client}")
    if result.gateway_client:
        print(f"stewardry-gateway scope: {result.gateway_scope}, client: {result.gateway_client}")
    print(f"users created: {', '.join(result.created_users) or 'none'}")
    print(f"users already present: {', '.join(result.existing_users) or 'none'}")
    for username, kept in sorted(result.fixed_ids_kept.items()):
        if not kept:
            print(f"warning: {username} did not keep its fixed ID; re-map after a recreate")
    for line in result.drift:
        print(f"warning: {line}")
    if result.entries_written:
        print(f"wrote {args.secrets_file} (0600), entries: {', '.join(result.entries_written)}")
    print("broker clients and scopes: unchanged")
    print(f"proposed access mapping: {args.mapping_out} (review, then `stewardry roles apply`)")
    return EXIT_OK


def _add_hub_commands(commands: Any) -> None:
    parser = commands.add_parser("hub", help="hub (Keycloak) setup for Stewardry (ADR 0008)")
    sub = parser.add_subparsers(title="hub commands", required=True, dest="hub_action")
    boot = sub.add_parser(
        "bootstrap",
        help="add the stewardry client and persona users to the local hub realm (idempotent)",
    )
    boot.add_argument("--additions", default="deploy/hub/stewardry-realm-additions.json")
    boot.add_argument("--secrets-file", default="deploy/hub/.secrets.env")
    boot.add_argument("--example-mapping", default="deploy/access/access-mapping.example.yaml")
    boot.add_argument("--mapping-out", default="deploy/hub/proposed-access-mapping.yaml")
    boot.add_argument("--include-sales2", action="store_true", help="also add the sales2 user")
    boot.add_argument(
        "--with-stewardry-gateway",
        action="store_true",
        help="also add the Stewardry-only gateway's scope and exchange client "
        "(stack tier only in Milestone 1; ADR 0009 option 2)",
    )
    boot.set_defaults(handler=_hub_bootstrap)


def _add_roles_commands(commands: Any) -> None:
    parser = commands.add_parser("roles", help="users, roles, and customer assignments")
    sub = parser.add_subparsers(title="roles commands", required=True, dest="roles_action")
    apply = sub.add_parser("apply", help="apply an access mapping file (declarative)")
    apply.add_argument("file", help="access mapping YAML (see deploy/access/)")
    apply.add_argument("--as", dest="actor", required=True, help=AS_HELP)
    apply.add_argument(
        "--bootstrap",
        action="store_true",
        help="first apply only: allowed while no platform engineer exists",
    )
    apply.add_argument(
        "--remap",
        action="append",
        metavar="USERNAME",
        help="allow this username's hub subject to change (repeatable)",
    )
    apply.set_defaults(handler=_roles)
    show = sub.add_parser("show", help="list users with their active roles and customers")
    show.set_defaults(handler=_roles)


def _serve(args: argparse.Namespace) -> int:
    import httpx2
    import uvicorn

    from stewardry.platform.artifacts import ArtifactStore
    from stewardry.platform.credentials import CredentialStore
    from stewardry.web.app import Services, create_app
    from stewardry.web.hub import HubClient
    from stewardry.web.sessions import SessionStore
    from stewardry.web.settings import SettingsError, WebSettings

    try:
        settings = WebSettings.from_environment()
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    data_dir = _data_dir()
    engine = _existing_engine(data_dir) if data_dir else None
    if data_dir is None or engine is None:
        return EXIT_USAGE
    hub = HubClient(
        settings.hub_issuer,
        settings.hub_client_id,
        settings.hub_client_secret,
        settings.redirect_uri,
        httpx2.Client(timeout=15),
        settings.hub_scope,
    )
    services = Services(
        settings,
        engine,
        hub,
        AuditLog(),
        SessionStore(),
        CredentialStore(),
        ArtifactStore(artifacts_path(data_dir)),
    )
    print(f"Stewardry on http://{args.host}:{args.port} (public URL {settings.public_url})")
    uvicorn.run(create_app(services), host=args.host, port=args.port, log_level="info")
    engine.dispose()
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

    _add_registry_commands(commands)
    _add_roles_commands(commands)
    _add_hub_commands(commands)
    serve = commands.add_parser("serve", help="run the web app (localhost only by default)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8700)
    serve.set_defaults(handler=_serve)

    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return EXIT_OK
    result: int = args.handler(args)
    return result
