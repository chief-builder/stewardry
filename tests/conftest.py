"""Offline tier: every test runs with outbound network blocked.

Tests that need a network (stack, live-model, connected tiers) will opt out
with their own marker when those tiers are added.
"""

import socket
from collections.abc import Iterator
from typing import Any

import pytest


class NetworkBlockedError(RuntimeError):
    pass


def _blocked(*args: Any, **kwargs: Any) -> None:
    raise NetworkBlockedError("network access is blocked in offline tests")


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    yield
