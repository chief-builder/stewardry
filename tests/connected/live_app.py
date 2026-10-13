"""Stewardry's web app, running against the real hub and gateway, for the
connected harnesses (S15 compatibility, S16 shadow run).

It serves http://localhost:8700 from a temporary database with the reviewed
access mapping and agent policy applied. People sign in through their own
browser windows. Each persona's hub credential is renewed in the background
so it does not lapse while other people take their turn.
"""

import datetime as dt
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import uvicorn
import yaml
from sqlalchemy import Engine

from stewardry.platform import registry
from stewardry.platform.access import apply_access_mapping, load_access_mapping
from stewardry.platform.artifacts import ArtifactStore
from stewardry.platform.atlassian.adapter import AtlassianReads, AtlassianSettings
from stewardry.platform.atlassian.client import ToolSuccess
from stewardry.platform.atlassian.mcp_client import McpGatewayClient
from stewardry.platform.audit import AuditLog
from stewardry.platform.catalog import Catalogs
from stewardry.platform.credentials import CredentialStore, GatewayCredential
from stewardry.platform.db import migrate
from stewardry.platform.db.engine import make_engine
from stewardry.platform.policy import load_agent_policy, load_baseline
from stewardry.web.app import Services, create_app
from stewardry.web.hub import HubClient, HubError
from stewardry.web.sessions import SessionStore
from stewardry.web.settings import WebSettings

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "deploy/connected/connected.yaml"
MAPPING = ROOT / "deploy/hub/proposed-access-mapping.yaml"
AGENT_POLICY = ROOT / "policies/agents/questionnaire-agent.yaml"
BASELINE = ROOT / "policies/platform-baseline.yaml"
CATALOG = "policies/catalogs/secpol.yaml"
APP = "http://localhost:8700"
WAIT_FOR_PEOPLE = dt.timedelta(minutes=30)


def say(text: str) -> None:
    print(f"\n>>> {text}", flush=True)


def ask(question: str) -> str:
    say(question)
    return input("> ").strip()


def broker_revision(broker: Path) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(broker), "rev-parse", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


@dataclass
class LiveApp:
    settings: WebSettings
    config: dict[str, Any]
    credentials: CredentialStore
    subjects: dict[str, str]  # persona role -> hub sub
    gateway: McpGatewayClient
    reads: AtlassianReads
    engine: Engine
    log: AuditLog
    catalogs: Catalogs
    owner: str  # the engagement owner's sub

    def credential(self, role: str) -> GatewayCredential:
        found = self.credentials.get(self.subjects[role], dt.datetime.now(dt.UTC))
        assert found is not None, f"{role} is not signed in"
        return found

    def wait_until_connected(self, roles: list[str]) -> None:
        names = ", ".join(f"{self.config['personas'][r]} ({r})" for r in roles)
        say(
            f"Sign in at {APP} as each of: {names}. Use a separate private browser window "
            "for each person. In each window, click 'Connect Atlassian' and approve with "
            "that person's own Atlassian account. Waiting..."
        )
        deadline = dt.datetime.now(dt.UTC) + WAIT_FOR_PEOPLE
        pending = set(roles)
        while pending and dt.datetime.now(dt.UTC) < deadline:
            for role in sorted(pending):
                cred = self.credentials.get(self.subjects[role], dt.datetime.now(dt.UTC))
                if cred is None:
                    continue
                # Any successful call means the person is connected.
                outcome = self.gateway.call_tool(
                    cred, "atlassian_atlassianUserInfo", {}, interactive=False
                )
                if isinstance(outcome, ToolSuccess):
                    say(f"{role} is signed in and connected.")
                    pending.discard(role)
            time.sleep(15)
        assert not pending, f"not connected in time: {sorted(pending)}"


@contextmanager
def live_app(
    wrap: Callable[[McpGatewayClient], Any] = lambda client: client,
    data_dir: Path | None = None,
) -> Iterator[LiveApp]:
    """Serve Stewardry on port 8700 until the block ends. `wrap` lets a harness
    record the gateway calls the adapter makes. The database and uploads go to
    `data_dir` when given (kept), otherwise to a temporary directory."""
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    settings = WebSettings.from_environment()
    mapping = load_access_mapping(MAPPING)
    by_name = {u.username: u for u in mapping.users}
    subjects = {role: by_name[name].sub for role, name in config["personas"].items()}
    platform = next(u.sub for u in mapping.users if "platform_engineer" in u.roles)
    owner = next(u.sub for u in mapping.users if "engagement_owner" in u.roles)

    tmp = data_dir or Path(tempfile.mkdtemp(prefix="stewardry-connected-"))
    tmp.mkdir(parents=True, exist_ok=True)
    engine = make_engine(tmp / "db.sqlite")
    migrate.upgrade(engine)
    log = AuditLog()
    apply_access_mapping(engine, log, platform, mapping, bootstrap=True)
    registry.apply_policy(
        engine, log, platform, load_agent_policy(AGENT_POLICY), load_baseline(BASELINE)
    )
    registry.transition_status(engine, log, owner, "questionnaire-agent", "build", "docs/brief.md")

    field_keys = (
        {"customer": config["jira"]["customer_field"]}
        if config["jira"].get("customer_field")
        else {}
    )
    gateway = McpGatewayClient(settings.gateway_url)
    reads = AtlassianReads(
        wrap(gateway),
        AtlassianSettings(config["cloud_id"], settings.consent_url_prefix, field_keys),
    )
    credentials = CredentialStore()
    http = httpx2.Client(timeout=30)
    hub = HubClient(
        settings.hub_issuer,
        settings.hub_client_id,
        settings.hub_client_secret,
        settings.redirect_uri,
        http,
        settings.hub_scope,
    )
    services = Services(
        settings,
        engine,
        hub,
        log,
        SessionStore(),
        credentials,
        ArtifactStore(tmp / "artifacts"),
        reads,
    )
    stop = threading.Event()

    def keep_fresh() -> None:
        """Renew each persona's credential before it lapses, as the web app does on
        each request. People sign in one at a time, so a browser that has been
        closed would otherwise let its 5-minute hub token expire mid-run."""
        while not stop.wait(30):
            now = dt.datetime.now(dt.UTC)
            for sub in subjects.values():
                for sid in credentials.sessions_of(sub):
                    token = credentials.needs_refresh(sid, now, dt.timedelta(minutes=2))
                    if token is None:
                        continue
                    try:
                        renewed = hub.refresh(token)
                    except HubError:
                        continue  # the hub session ended; that person signs in again
                    credentials.put(
                        sid,
                        sub,
                        GatewayCredential(renewed.access_token, renewed.expires_at),
                        renewed.refresh_token or token,
                    )

    threading.Thread(target=keep_fresh, daemon=True).start()
    server = uvicorn.Server(
        uvicorn.Config(create_app(services), host="127.0.0.1", port=8700, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    catalogs = Catalogs(ROOT, {CATALOG: ROOT / config["catalog_file"]})
    try:
        yield LiveApp(
            settings, config, credentials, subjects, gateway, reads, engine, log, catalogs, owner
        )
    finally:
        stop.set()
        server.should_exit = True
        thread.join(timeout=10)
        http.close()
        engine.dispose()
