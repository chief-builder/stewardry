"""Stack tier: a throwaway Keycloak from the broker's pinned image.

Runs only with STEWARDRY_STACK=1 (`make test-stack`). It never touches a
`vtb-*` stack: the container has its own name and port, imports the broker's
realm file read-only from the pinned checkout, and gets a random admin
password generated for this container only. It is removed afterwards.
"""

import os
import re
import secrets
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest

ROOT = Path(__file__).resolve().parents[2]
BROKER = ROOT.parent / "vendor-token-broker"
CONTAINER = "stewardry-stack-keycloak"
PORT = 18180
BASE = f"http://localhost:{PORT}"
ISSUER = f"{BASE}/realms/mcp"
LOCAL = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True)
class Hub:
    base: str
    issuer: str
    admin_user: str
    admin_password: str


def keycloak_image() -> str:
    """The Keycloak image pinned by the broker's own stack (read, never changed)."""
    compose = (BROKER / "tests/stack/docker-compose.yml").read_text(encoding="utf-8")
    match = re.search(r"image:\s*(quay\.io/keycloak/keycloak:\S+@sha256:[0-9a-f]{64})", compose)
    assert match, "the broker's compose file no longer pins a Keycloak image"
    return match[1]


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed docker arguments
        ["docker", *args],  # noqa: S607 - docker from PATH, as in the broker's own tests
        capture_output=True,
        text=True,
        check=check,
    )


@pytest.fixture(autouse=True)
def _local_network_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stack tests may reach localhost only."""
    original = socket.getaddrinfo

    def local(host: object, *args: object, **kwargs: object) -> object:
        if str(host) not in LOCAL:
            raise RuntimeError(f"stack tests may only reach localhost, not {host}")
        return original(host, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "getaddrinfo", local)


@pytest.fixture(scope="session")
def hub() -> Iterator[Hub]:
    if os.environ.get("STEWARDRY_STACK") != "1":
        pytest.skip("stack tier: set STEWARDRY_STACK=1 (make test-stack)")
    if _docker("info", check=False).returncode != 0:
        pytest.fail("Docker is not available")
    if _docker("ps", "-a", "--filter", f"name=^{CONTAINER}$", "-q").stdout.strip():
        pytest.fail(f"container {CONTAINER} already exists; remove it first")
    password = secrets.token_urlsafe(24)
    _docker(
        "run",
        "-d",
        "--rm",
        "--name",
        CONTAINER,
        "-p",
        f"127.0.0.1:{PORT}:{PORT}",
        "-e",
        "KC_BOOTSTRAP_ADMIN_USERNAME=admin",
        "-e",
        f"KC_BOOTSTRAP_ADMIN_PASSWORD={password}",
        "-v",
        f"{BROKER / 'tests/stack/keycloak'}:/opt/keycloak/data/import:ro",
        keycloak_image(),
        "start-dev",
        "--import-realm",
        f"--http-port={PORT}",
        f"--hostname={BASE}",
    )
    try:
        deadline = time.monotonic() + 180
        while True:
            try:
                if httpx2.get(f"{ISSUER}/.well-known/openid-configuration").status_code == 200:
                    break
            except httpx2.HTTPError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("Keycloak did not become ready in 180 s")
            time.sleep(2)
        yield Hub(BASE, ISSUER, "admin", password)
    finally:
        _docker("rm", "-f", CONTAINER, check=False)
