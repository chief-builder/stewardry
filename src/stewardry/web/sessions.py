"""Server-side sessions and pending sign-ins, held in memory (ADR 0005, G6).

The browser holds only an opaque random ID. A restart drops every session,
matching the credential store: people sign in again and paused work resumes
under their renewed authority.
"""

import datetime as dt
import secrets
import threading
from dataclasses import dataclass, field, replace

from stewardry.web.hub import Pkce

IDLE_TIMEOUT = dt.timedelta(minutes=30)
ABSOLUTE_TIMEOUT = dt.timedelta(hours=8)
LOGIN_TIMEOUT = dt.timedelta(minutes=10)


@dataclass(frozen=True)
class Session:
    session_id: str = field(repr=False)
    sub: str
    username: str
    roles: frozenset[str]
    customers: frozenset[str]
    csrf_token: str = field(repr=False)
    created_at: dt.datetime
    last_seen_at: dt.datetime


@dataclass(frozen=True)
class PendingLogin:
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    pkce: Pkce
    created_at: dt.datetime


class SessionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._logins: dict[str, PendingLogin] = {}

    def create(
        self,
        sub: str,
        username: str,
        roles: frozenset[str],
        customers: frozenset[str],
        now: dt.datetime,
    ) -> Session:
        session = Session(
            secrets.token_urlsafe(32),
            sub,
            username,
            roles,
            customers,
            secrets.token_urlsafe(32),
            now,
            now,
        )
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str | None, now: dt.datetime) -> Session | None:
        """The live session for this ID, refreshed for idle time; None if expired."""
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if (
                now - session.last_seen_at > IDLE_TIMEOUT
                or now - session.created_at > ABSOLUTE_TIMEOUT
            ):
                del self._sessions[session_id]
                return None
            session = replace(session, last_seen_at=now)
            self._sessions[session_id] = session
            return session

    def delete(self, session_id: str | None) -> None:
        with self._lock:
            self._sessions.pop(session_id or "", None)

    def start_login(self, now: dt.datetime) -> tuple[str, PendingLogin]:
        login = PendingLogin(secrets.token_urlsafe(32), secrets.token_urlsafe(32), Pkce.new(), now)
        login_id = secrets.token_urlsafe(32)
        with self._lock:
            self._logins = {
                k: v for k, v in self._logins.items() if now - v.created_at <= LOGIN_TIMEOUT
            }
            self._logins[login_id] = login
        return login_id, login

    def finish_login(self, login_id: str | None, now: dt.datetime) -> PendingLogin | None:
        """Single use: a replayed callback finds nothing."""
        with self._lock:
            login = self._logins.pop(login_id or "", None)
        if login is None or now - login.created_at > LOGIN_TIMEOUT:
            return None
        return login

    def __repr__(self) -> str:
        return f"SessionStore(sessions={len(self._sessions)}, pending={len(self._logins)})"
