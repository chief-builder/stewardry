"""Classifying gateway outcomes without guessing (ADR 0010).

Consent comes only from a structured URL elicitation whose URL is under the
configured broker authorize path. Every gateway `ToolError` text is matched
against the templates below, which are kept equal to the pinned gateway source
by a test (`tests/unit/test_atlassian_contract.py`). Anything unmatched fails
closed: an `isError` result becomes `upstream_tool_error` (the vendor's own
error), and anything malformed becomes `unexpected`, handled as `unavailable`.
Text never yields consent.
"""

import re
from dataclasses import dataclass
from typing import Literal

from stewardry.platform.atlassian.client import (
    ConsentRequested,
    GatewayOutcome,
    GatewayUnreachable,
    SignInRequired,
    ToolFailure,
    ToolSuccess,
)

ErrorClass = Literal[
    "ok",
    "needs_consent",
    "consent_declined",
    "consent_timeout",
    "needs_reconnect",
    "needs_signin",
    "disconnect_pending",
    "vendor_unusable",
    "config_error",
    "unavailable",
    "upstream_tool_error",
    "unexpected",
]

# Gateway ToolError templates at vendor-token-broker@20d1426, with each f-string
# hole written as "{}". The order does not matter; the contract test compares sets.
CATALOG: dict[str, ErrorClass] = {
    "the gateway could not establish your identity with the token broker; "
    "this is a gateway configuration problem": "config_error",
    "a dependency is unavailable ({}); retry shortly": "unavailable",
    "not authenticated": "needs_signin",
    # Resolved by the problem title: revoke-pending is a pending disconnect.
    "{} is not usable right now ({}); try again later": "vendor_unusable",
    # The manual consent text is sent only to clients that cannot elicit.
    # Stewardry always can, so this text is never consent (ADR 0010).
    "Connect {} first: open {} in your browser, then retry.": "unexpected",
    "{} was not connected; the request was cancelled": "consent_declined",
    "{} is not connected yet; finish connecting in the browser, then retry": "consent_timeout",
    "{} connection is not usable ({}); retry": "unavailable",
    "{} rejected the connection; reconnect {} and retry": "needs_reconnect",
    "{}'s MCP server is unavailable; retry shortly": "unavailable",
}


def _pattern(template: str) -> re.Pattern[str]:
    return re.compile("(.+?)".join(re.escape(part) for part in template.split("{}")), re.DOTALL)


_PATTERNS = [(template, _pattern(template), cls) for template, cls in CATALOG.items()]


@dataclass(frozen=True)
class Classification:
    cls: ErrorClass
    detail: str | None = None
    consent_url: str | None = None


def classify_text(text: str) -> Classification:
    """Match one `isError` text against the gateway catalog."""
    for template, pattern, cls in _PATTERNS:
        match = pattern.fullmatch(text.strip())
        if match is None:
            continue
        if template.startswith("{} is not usable right now"):
            problem = match.group(2)
            return Classification(
                "disconnect_pending" if problem == "revoke-pending" else "vendor_unusable",
                problem,
            )
        return Classification(cls, template)
    return Classification("upstream_tool_error", text[:200])


def classify(outcome: GatewayOutcome, consent_url_prefix: str) -> Classification:
    if isinstance(outcome, ToolSuccess):
        return Classification("ok")
    if isinstance(outcome, ConsentRequested):
        if outcome.url.startswith(consent_url_prefix):
            return Classification("needs_consent", consent_url=outcome.url)
        return Classification("unexpected", "consent link outside the broker authorize path")
    if isinstance(outcome, SignInRequired):
        return Classification("needs_signin")
    if isinstance(outcome, GatewayUnreachable):
        return Classification("unavailable", outcome.reason)
    if isinstance(outcome, ToolFailure):
        return classify_text(outcome.text)
    return Classification("unexpected", type(outcome).__name__)  # pragma: no cover
