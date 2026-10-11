"""Offline tier: every test runs with outbound network blocked.

Tests that need a network opt out with their tier's marker (`live_model`,
`stack`).
"""

import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine


class NetworkBlockedError(RuntimeError):
    pass


def _blocked(*args: Any, **kwargs: Any) -> None:
    raise NetworkBlockedError("network access is blocked in offline tests")


@pytest.fixture(autouse=True)
def _block_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    if request.node.get_closest_marker("live_model") or request.node.get_closest_marker("stack"):
        yield  # these tiers need the network and run only when asked for; the stack
        return  # tier limits itself to localhost (tests/stack/conftest.py)
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    yield


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """A fresh SQLite database migrated to the latest schema."""
    from stewardry.platform.db import migrate
    from stewardry.platform.db.engine import make_engine

    eng = make_engine(tmp_path / "test.db")
    migrate.upgrade(eng)
    yield eng
    eng.dispose()
