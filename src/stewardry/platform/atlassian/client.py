"""What the adapter needs from an MCP client of the broker's gateway.

The real client (MCP over streamable HTTP, always advertising URL elicitation)
lands in Milestone 1 step S14b, where it is tested against the broker's
stand-in stack. Until then the adapter is tested against a fake that behaves
like the pinned gateway (`tests/fakes/gateway.py`).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from stewardry.platform.credentials import GatewayCredential


@dataclass(frozen=True)
class ToolSuccess:
    """`isError: false`. `structured` is `structuredContent` when present;
    `text` is the concatenated text content."""

    structured: Any
    text: str


@dataclass(frozen=True)
class ToolFailure:
    """`isError: true`: a gateway `ToolError`, or the vendor tool's own error."""

    text: str


@dataclass(frozen=True)
class ConsentRequested:
    """The gateway sent a URL-mode elicitation (structured, never parsed from text)."""

    url: str


@dataclass(frozen=True)
class SignInRequired:
    """The gateway refused Stewardry's own token (HTTP 401 with WWW-Authenticate)."""


@dataclass(frozen=True)
class GatewayUnreachable:
    """The gateway itself could not be reached or did not answer in time."""

    reason: str


GatewayOutcome = ToolSuccess | ToolFailure | ConsentRequested | SignInRequired | GatewayUnreachable


class GatewayClient(Protocol):
    def call_tool(
        self,
        credential: GatewayCredential,
        name: str,
        arguments: Mapping[str, Any],
        *,
        interactive: bool,
    ) -> GatewayOutcome:
        """Call one gateway tool as the credential's user. `interactive` is True
        only for a user's own "Connect Atlassian" action (ADR 0010)."""
        ...
