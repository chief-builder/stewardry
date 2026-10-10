"""Secret-shaped strings: one list of patterns for redaction and repo scans.

Redaction replaces a match with ``[REDACTED:<kind>]``. It never drops the
surrounding record, because audit events must survive (spec section 4.5).
"""

import logging
import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

PATTERNS: dict[str, re.Pattern[str]] = {
    "anthropic-api-key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "private-key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "bearer": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    "aws-access-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}

# Environment variables whose values are always redacted when set.
SECRET_ENV_VARS = ("ANTHROPIC_API_KEY", "STEWARDRY_HUB_CLIENT_SECRET", "KEYCLOAK_ADMIN_PASSWORD")
MIN_SECRET_LENGTH = 8  # shorter configured values would redact ordinary words


class Redactor:
    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        self._values = sorted(
            {v for v in secret_values if len(v) >= MIN_SECRET_LENGTH}, key=len, reverse=True
        )

    @classmethod
    def from_environment(cls) -> Redactor:
        return cls(os.environ[name] for name in SECRET_ENV_VARS if os.environ.get(name))

    def redact_text(self, text: str) -> tuple[str, set[str]]:
        """Return the text with secrets replaced, and the kinds that were found."""
        found: set[str] = set()
        for value in self._values:
            if value in text:
                text = text.replace(value, "[REDACTED:configured-secret]")
                found.add("configured-secret")
        for kind, pattern in PATTERNS.items():
            text, count = pattern.subn(f"[REDACTED:{kind}]", text)
            if count:
                found.add(kind)
        return text, found

    def redact(self, value: Any) -> tuple[Any, set[str]]:
        """Redact every string inside a JSON-like value (dicts, lists, scalars)."""
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            found: set[str] = set()
            for key, item in value.items():
                clean_key, key_found = self.redact_text(str(key))
                out[clean_key], item_found = self.redact(item)
                found |= key_found | item_found
            return out, found
        if isinstance(value, list | tuple):
            items = [self.redact(item) for item in value]
            return [i for i, _ in items], set().union(*(f for _, f in items))
        return value, set()


class RedactingFilter(logging.Filter):
    """Redacts the formatted message of every log record that passes through."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        message, _ = self.redactor.redact_text(record.getMessage())
        record.msg, record.args = message, None
        return True


def install_log_redaction(redactor: Redactor | None = None) -> RedactingFilter:
    """Attach a redacting filter to every handler of the ``stewardry`` logger tree."""
    log_filter = RedactingFilter(redactor or Redactor.from_environment())
    logger = logging.getLogger("stewardry")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    for handler in logger.handlers:
        for existing in list(handler.filters):
            if isinstance(existing, RedactingFilter):
                handler.removeFilter(existing)  # replace, so repeated calls do not stack
        handler.addFilter(log_filter)
    return log_filter
