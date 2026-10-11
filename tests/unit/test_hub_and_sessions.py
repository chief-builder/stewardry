"""Hub client error handling, session lifetimes, and web settings."""

import datetime as dt

import httpx2
import pytest

from stewardry.web.hub import HubClient, HubRejected, HubUnavailable, Pkce
from stewardry.web.sessions import ABSOLUTE_TIMEOUT, IDLE_TIMEOUT, LOGIN_TIMEOUT, SessionStore
from stewardry.web.settings import SettingsError, WebSettings
from tests.fakes.hub import CLIENT_ID, CLIENT_SECRET, ISSUER, REDIRECT_URI, FakeHub

NOW = dt.datetime(2026, 10, 10, 12, tzinfo=dt.UTC)


def client(handler: httpx2.MockTransport) -> HubClient:
    return HubClient(
        ISSUER, CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, httpx2.Client(transport=handler)
    )


# ---------------------------------------------------------------- hub client


def test_network_errors_are_outages_not_refusals() -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(HubUnavailable, match="unreachable"):
        client(httpx2.MockTransport(fail)).metadata()


def test_discovery_must_name_the_configured_issuer() -> None:
    def other_issuer(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"issuer": "http://evil.test/realms/mcp"})

    with pytest.raises(HubRejected, match="does not match"):
        client(httpx2.MockTransport(other_issuer)).metadata()


@pytest.mark.parametrize("status", [404, 401])
def test_discovery_and_jwks_refusals(status: int) -> None:
    with pytest.raises(HubRejected, match="discovery"):
        client(httpx2.MockTransport(lambda r: httpx2.Response(status))).metadata()

    hub = FakeHub()

    def no_keys(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/certs"):
            return httpx2.Response(status)
        return hub.handle(request)

    with pytest.raises(HubRejected, match="JWKS"):
        client(httpx2.MockTransport(no_keys)).verify_id_token("a.b.c", "nonce")


def test_refused_refresh_and_bad_client_are_rejections() -> None:
    hub = FakeHub()
    with pytest.raises(HubRejected, match="invalid_grant"):
        client(hub.transport()).refresh("unknown-refresh-token")
    wrong = HubClient(
        ISSUER, CLIENT_ID, "wrong-secret", REDIRECT_URI, httpx2.Client(transport=hub.transport())
    )
    with pytest.raises(HubRejected, match="401 invalid_client"):
        wrong.refresh("anything")


def test_wrong_pkce_verifier_is_refused() -> None:
    hub = FakeHub()
    hub_client = client(hub.transport())
    pkce = Pkce.new()
    code, _ = hub.approve(hub_client.authorization_url("s", "n", pkce), "sub-a", "a")
    with pytest.raises(HubRejected, match="invalid_grant"):
        hub_client.exchange_code(code, Pkce.new())


def test_pkce_challenge_is_s256_of_the_verifier() -> None:
    pkce = Pkce("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")  # RFC 7636 appendix B
    assert pkce.challenge == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert "dBjft" not in repr(pkce)


# ------------------------------------------------------------------ sessions


def test_sessions_expire_when_idle_or_too_old() -> None:
    store = SessionStore()
    session = store.create("sub-a", "a", frozenset({"sales"}), frozenset(), NOW)
    assert store.get(session.session_id, NOW + IDLE_TIMEOUT - dt.timedelta(minutes=1))
    assert store.get(session.session_id, NOW + IDLE_TIMEOUT * 3) is None

    active = store.create("sub-a", "a", frozenset({"sales"}), frozenset(), NOW)
    step = IDLE_TIMEOUT - dt.timedelta(minutes=1)
    moment = NOW
    while moment + step - NOW <= ABSOLUTE_TIMEOUT:
        moment += step
        assert store.get(active.session_id, moment) is not None
    assert store.get(active.session_id, moment + step) is None
    assert store.get(None, NOW) is None


def test_pending_logins_are_single_use_and_short_lived() -> None:
    store = SessionStore()
    login_id, _ = store.start_login(NOW)
    assert store.finish_login(login_id, NOW) is not None
    assert store.finish_login(login_id, NOW) is None
    late_id, _ = store.start_login(NOW)
    assert store.finish_login(late_id, NOW + LOGIN_TIMEOUT + dt.timedelta(seconds=1)) is None
    stale_id, _ = store.start_login(NOW)
    store.start_login(NOW + LOGIN_TIMEOUT * 2)  # sweeps stale pending logins
    assert store.finish_login(stale_id, NOW) is None
    assert "sessions=" in repr(store)


# ------------------------------------------------------------------ settings


def test_settings_name_missing_variables_without_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STEWARDRY_HUB_ISSUER", raising=False)
    monkeypatch.setenv("STEWARDRY_HUB_CLIENT_SECRET", "secret-value-123")
    with pytest.raises(SettingsError) as exc:
        WebSettings.from_environment()
    assert "STEWARDRY_HUB_ISSUER" in str(exc.value) and "secret-value" not in str(exc.value)


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEWARDRY_HUB_ISSUER", ISSUER + "/")
    monkeypatch.setenv("STEWARDRY_HUB_CLIENT_SECRET", "secret-value-123")
    monkeypatch.delenv("STEWARDRY_PUBLIC_URL", raising=False)
    settings = WebSettings.from_environment()
    assert settings.hub_issuer == ISSUER and settings.hub_client_id == "stewardry"
    assert settings.redirect_uri == "http://localhost:8700/auth/callback"
    assert settings.origin == "http://localhost:8700" and not settings.secure_cookies
    assert "secret-value" not in repr(settings)
    monkeypatch.setenv("STEWARDRY_PUBLIC_URL", "https://stewardry.test/")
    assert WebSettings.from_environment().secure_cookies
