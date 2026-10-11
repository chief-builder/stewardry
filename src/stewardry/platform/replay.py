"""Recording and offline replay of model responses (ADR 0007).

- `RecordingTransport` wraps a live transport and saves each request and
  response under its request hash, after redaction.
- `ReplayTransport` serves only recorded responses. A request it has not seen
  is a hard failure (`ReplayMiss`), never a live call.
- `replay_guard` makes a process safe for offline replay: it refuses to start
  while model or service credentials are configured, and blocks outbound
  network access for as long as it is active.

A replay shows that the workflow is reproducible. It does not show live
vendor access or current model quality (spec section 1).
"""

import json
import os
import socket
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from stewardry.platform.db.schema import utcnow
from stewardry.platform.model import Transport, TransportFatal
from stewardry.platform.redaction import Redactor

# Any of these set means live credentials are present in the environment.
CREDENTIAL_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_PROFILE",
    "STEWARDRY_HUB_CLIENT_SECRET",
)


class ReplayMiss(TransportFatal):
    """No recording exists for this request; replay never falls back to live.
    It fails the run like any other unrecoverable model error."""


class ReplayUnsafe(RuntimeError):
    """Replay was asked to start while live credentials are configured."""


class NetworkBlocked(RuntimeError):
    pass


class ReplayStore:
    """One JSON file per request hash under a directory (`replay/model/`)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, request_hash: str) -> Path:
        if len(request_hash) != 64 or not all(c in "0123456789abcdef" for c in request_hash):
            raise ValueError("not a request hash")
        return self.root / f"{request_hash}.json"

    def save(
        self,
        request_hash: str,
        payload: Mapping[str, Any],
        response: Mapping[str, Any],
        redactor: Redactor,
    ) -> None:
        record, _ = redactor.redact(
            {
                "request_hash": request_hash,
                "recorded_at": utcnow().isoformat(),
                "payload": dict(payload),
                "response": dict(response),
            }
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(request_hash).write_text(
            json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def load(self, request_hash: str) -> dict[str, Any]:
        path = self._path(request_hash)
        if not path.exists():
            raise ReplayMiss(f"no recording for request {request_hash[:12]}")
        record: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return record


class RecordingTransport:
    kind: Literal["live", "replay"] = "live"

    def __init__(self, inner: Transport, store: ReplayStore, redactor: Redactor) -> None:
        if inner.kind != "live":
            raise ValueError("only a live transport can be recorded")
        self.inner = inner
        self.store = store
        self.redactor = redactor

    def send(self, payload: Mapping[str, Any], request_hash: str) -> dict[str, Any]:
        response = self.inner.send(payload, request_hash)
        self.store.save(request_hash, payload, response, self.redactor)
        return response


class ReplayTransport:
    kind: Literal["live", "replay"] = "replay"

    def __init__(self, store: ReplayStore) -> None:
        self.store = store

    def send(self, payload: Mapping[str, Any], request_hash: str) -> dict[str, Any]:
        response: dict[str, Any] = self.store.load(request_hash)["response"]
        return response


def configured_credentials() -> list[str]:
    """Names (never values) of credential variables that are set."""
    return [name for name in CREDENTIAL_ENV_VARS if os.environ.get(name)]


def _blocked(*_: Any, **__: Any) -> Any:
    raise NetworkBlocked("network access is blocked during offline replay")


@contextmanager
def replay_guard() -> Iterator[None]:
    """Refuse if live credentials are configured; block the network while active."""
    present = configured_credentials()
    if present:
        raise ReplayUnsafe(
            "offline replay refuses to run while credentials are set: " + ", ".join(present)
        )
    targets = [
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
    ]
    saved = [(owner, name, getattr(owner, name)) for owner, name in targets]
    for owner, name in targets:
        setattr(owner, name, _blocked)
    try:
        yield
    finally:
        for owner, name, original in saved:
            setattr(owner, name, original)
