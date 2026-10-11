"""The real gateway client: MCP over streamable HTTP (ADR 0006, 0010).

- **One session per call,** with the acting user's own gateway credential as
  the bearer token. Nothing is shared between calls or users.
- **Always advertises URL-mode elicitation,** so consent arrives as a
  structured elicitation, never as text (ADR 0010).
- **On a URL elicitation, the link is captured and the elicitation is declined.**
  The call returns `ConsentRequested(url)`. A background run then pauses
  without keeping the link. A user's own "Connect Atlassian" action shows
  the link to that user, who finishes consent in their own browser and
  retries. The gateway never waits on a browser flow inside a Stewardry
  request. A test-only `consent_driver` can accept instead (stack tier).
- **Form-mode elicitations are always declined.** Stewardry never fills
  forms for a server.
- **Outcomes:**
  - HTTP 401 from the gateway becomes `SignInRequired`.
  - Connection failures and timeouts become `GatewayUnreachable`.
  - Tool results become `ToolSuccess` or `ToolFailure`.
"""

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal

import anyio
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp_types import (
    CallToolResult,
    ElicitRequestParams,
    ElicitRequestURLParams,
    ElicitResult,
    TextContent,
)

from stewardry.platform.atlassian.client import (
    ConsentRequested,
    GatewayOutcome,
    GatewayUnreachable,
    SignInRequired,
    ToolFailure,
    ToolSuccess,
)
from stewardry.platform.credentials import GatewayCredential

ConsentAction = Literal["accept", "decline", "cancel"]
# Test-only: drive consent for a URL in a browser-like client, then answer.
ConsentDriver = Callable[[str], ConsentAction]
# Opens the MCP streams over an HTTP client (tests substitute an in-memory server).
Connect = Callable[[httpx2.AsyncClient], AbstractAsyncContextManager[Any]]


@dataclass(frozen=True)
class RawCall:
    """What one call produced, before mapping to an outcome (tests use this)."""

    elicited_urls: tuple[str, ...]
    result: CallToolResult | None
    http_status: int | None = None
    error: str | None = None


def _text(result: CallToolResult) -> str:
    return "".join(block.text for block in result.content if isinstance(block, TextContent))


class McpGatewayClient:
    def __init__(
        self,
        url: str,
        timeout_s: float = 15.0,
        call_timeout_s: float = 60.0,
        consent_driver: ConsentDriver | None = None,
        connect: Connect | None = None,
    ) -> None:
        self.url = url
        self.connect = connect or (lambda http: streamable_http_client(url, http_client=http))
        self.timeout_s = timeout_s
        self.call_timeout_s = call_timeout_s
        self.consent_driver = consent_driver

    def call_tool(
        self,
        credential: GatewayCredential,
        name: str,
        arguments: Mapping[str, Any],
        *,
        interactive: bool,
    ) -> GatewayOutcome:
        raw = self.raw_call(credential, name, arguments, interactive=interactive)
        if raw.http_status == 401:
            return SignInRequired()
        if raw.error is not None:
            return GatewayUnreachable(raw.error)
        accepted = interactive and self.consent_driver is not None
        if raw.elicited_urls and not accepted:
            return ConsentRequested(raw.elicited_urls[0])
        assert raw.result is not None  # noqa: S101 - no error implies a result
        if raw.result.is_error:
            return ToolFailure(_text(raw.result))
        return ToolSuccess(raw.result.structured_content, _text(raw.result))

    def raw_call(
        self,
        credential: GatewayCredential,
        name: str,
        arguments: Mapping[str, Any],
        *,
        interactive: bool,
    ) -> RawCall:
        return anyio.run(self._call, credential, name, dict(arguments), interactive)

    async def _call(
        self,
        credential: GatewayCredential,
        name: str,
        arguments: dict[str, Any],
        interactive: bool,
    ) -> RawCall:
        elicited: list[str] = []

        async def on_elicit(context: Any, params: ElicitRequestParams) -> ElicitResult:
            if not isinstance(params, ElicitRequestURLParams):
                return ElicitResult(action="decline")  # never fill forms for a server
            elicited.append(params.url)
            if interactive and self.consent_driver is not None:
                action = await anyio.to_thread.run_sync(self.consent_driver, params.url)
                return ElicitResult(action=action)
            return ElicitResult(action="decline")

        headers = {"Authorization": f"Bearer {credential.access_token}"}
        timeout = httpx2.Timeout(self.timeout_s, read=self.call_timeout_s)
        try:
            async with (
                httpx2.AsyncClient(headers=headers, timeout=timeout) as http,
                self.connect(http) as (read, write),
                ClientSession(read, write, elicitation_callback=on_elicit) as session,
            ):
                await session.initialize()
                result = await session.call_tool(
                    name, arguments, read_timeout_seconds=self.call_timeout_s
                )
        except Exception as exc:  # the SDK raises exception groups from its task group
            cause = _leaf(exc)
            if isinstance(cause, httpx2.HTTPStatusError):
                status = cause.response.status_code
                return RawCall(tuple(elicited), None, status, f"gateway answered HTTP {status}")
            if isinstance(cause, httpx2.TransportError | TimeoutError | OSError):
                return RawCall(tuple(elicited), None, None, type(cause).__name__)
            raise
        return RawCall(tuple(elicited), result)


def _leaf(exc: BaseException) -> BaseException:
    """The first underlying error inside (possibly nested) exception groups."""
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc
