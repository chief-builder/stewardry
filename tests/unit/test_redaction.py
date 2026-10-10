import logging

import pytest

from stewardry.platform.redaction import (
    RedactingFilter,
    Redactor,
    install_log_redaction,
)

# Secret-shaped probes are built at runtime so no source file contains one.
JWT = "eyJ" + "a" * 20 + ".eyJ" + "b" * 20 + "." + "c" * 20
BEARER = "Bearer " + "t" * 30
API_KEY = "sk-" + "ant-" + "k" * 30
GITHUB = "ghp_" + "g" * 36
CONFIGURED = "hub-client-secret-value"


@pytest.mark.parametrize(
    ("probe", "kind"),
    [(JWT, "jwt"), (BEARER, "bearer"), (API_KEY, "anthropic-api-key"), (GITHUB, "github-token")],
)
def test_secret_shapes_are_replaced(probe: str, kind: str) -> None:
    text, found = Redactor().redact_text(f"before {probe} after")
    assert probe not in text
    assert f"[REDACTED:{kind}]" in text
    assert kind in found


def test_configured_secret_values_are_replaced() -> None:
    text, found = Redactor([CONFIGURED]).redact_text(f"secret={CONFIGURED}")
    assert text == "secret=[REDACTED:configured-secret]"
    assert found == {"configured-secret"}


def test_short_configured_values_are_ignored() -> None:
    text, found = Redactor(["abc"]).redact_text("abc is a normal word")
    assert text == "abc is a normal word"
    assert found == set()


def test_nested_values_and_keys_are_redacted() -> None:
    value, found = Redactor().redact({"outer": [{"token": JWT}, 3, None], JWT: True})
    assert JWT not in repr(value)
    assert value["outer"][1:] == [3, None]
    assert found == {"jwt"}


def test_clean_text_is_unchanged() -> None:
    assert Redactor().redact_text("policy SEC-7 v3") == ("policy SEC-7 v3", set())


def test_environment_secrets_are_picked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEWARDRY_HUB_CLIENT_SECRET", CONFIGURED)
    text, _ = Redactor.from_environment().redact_text(CONFIGURED)
    assert CONFIGURED not in text


def test_log_records_are_redacted(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("stewardry.test")
    caplog.handler.addFilter(RedactingFilter(Redactor()))
    with caplog.at_level(logging.INFO, logger="stewardry"):
        logger.info("calling gateway with %s", BEARER)
    assert BEARER not in caplog.text
    assert "[REDACTED:bearer]" in caplog.text


def test_installing_log_redaction_twice_does_not_stack_filters() -> None:
    install_log_redaction(Redactor())
    install_log_redaction(Redactor())
    for handler in logging.getLogger("stewardry").handlers:
        assert sum(isinstance(f, RedactingFilter) for f in handler.filters) == 1
