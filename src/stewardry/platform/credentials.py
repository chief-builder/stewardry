"""In-memory holder for users' gateway-scoped hub credentials (ADR 0006, G6).

Credentials live only in this process: never on disk, in logs, in the model's
context, or in the browser. A restart empties the store, so work that needs a
user's credential pauses until that user signs in again. There is no fallback
to another user's credential.
"""

import datetime as dt
import threading
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GatewayCredential:
    """A hub access token whose audience is the gateway. Never printed."""

    access_token: str = field(repr=False)
    expires_at: dt.datetime

    def __post_init__(self) -> None:
        if self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")


@dataclass(frozen=True)
class _Entry:
    sub: str
    credential: GatewayCredential
    refresh_token: str | None = field(default=None, repr=False)


class CredentialStore:
    """Per-session credentials. A session's refresh token renews its credential
    while the hub session lasts (G6); it is never written anywhere."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_session: dict[str, _Entry] = {}

    def put(
        self,
        session_id: str,
        sub: str,
        credential: GatewayCredential,
        refresh_token: str | None = None,
    ) -> None:
        with self._lock:
            self._by_session[session_id] = _Entry(sub, credential, refresh_token)

    def remove_session(self, session_id: str) -> None:
        with self._lock:
            self._by_session.pop(session_id, None)

    def get(self, sub: str, now: dt.datetime) -> GatewayCredential | None:
        """The latest-expiring unexpired credential of any live session of `sub`."""
        with self._lock:
            live = [
                e.credential
                for e in self._by_session.values()
                if e.sub == sub and e.credential.expires_at > now
            ]
        return max(live, key=lambda c: c.expires_at, default=None)

    def needs_refresh(self, session_id: str, now: dt.datetime, margin: dt.timedelta) -> str | None:
        """The session's refresh token if its credential expires within `margin`."""
        with self._lock:
            entry = self._by_session.get(session_id)
        if entry is None or entry.refresh_token is None:
            return None
        return entry.refresh_token if entry.credential.expires_at - now <= margin else None

    def __repr__(self) -> str:
        return f"CredentialStore(sessions={len(self._by_session)})"
