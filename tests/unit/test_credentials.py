import datetime as dt

import pytest

from stewardry.platform.credentials import CredentialStore, GatewayCredential

NOW = dt.datetime(2026, 10, 10, 12, tzinfo=dt.UTC)
TOKEN = "gateway-token-value-123"


def cred(minutes: int, token: str = TOKEN) -> GatewayCredential:
    return GatewayCredential(access_token=token, expires_at=NOW + dt.timedelta(minutes=minutes))


def test_tokens_never_appear_in_repr() -> None:
    store = CredentialStore()
    store.put("s1", "sub-bob", cred(5))
    assert TOKEN not in repr(cred(5)) and TOKEN not in repr(store)


def test_returns_the_latest_unexpired_credential_for_that_user_only() -> None:
    store = CredentialStore()
    store.put("s1", "sub-bob", cred(5, "short"))
    store.put("s2", "sub-bob", cred(30, "long"))
    store.put("s3", "sub-alice", cred(60, "alice"))
    store.put("s4", "sub-bob", cred(-1, "expired"))
    found = store.get("sub-bob", NOW)
    assert found is not None and found.access_token == "long"
    assert store.get("sub-carol", NOW) is None


def test_signing_out_removes_the_credential() -> None:
    store = CredentialStore()
    store.put("s1", "sub-bob", cred(5))
    store.remove_session("s1")
    store.remove_session("s1")  # idempotent
    assert store.get("sub-bob", NOW) is None


def test_naive_expiry_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        GatewayCredential(access_token=TOKEN, expires_at=dt.datetime(2026, 10, 10))


def test_sessions_of_lists_a_users_sessions() -> None:
    store = CredentialStore()
    later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    store.put("s1", "sub-a", GatewayCredential("t1", later))
    store.put("s2", "sub-a", GatewayCredential("t2", later))
    store.put("s3", "sub-b", GatewayCredential("t3", later))
    assert sorted(store.sessions_of("sub-a")) == ["s1", "s2"]
    assert store.sessions_of("nobody") == []
