"""The narrow Atlassian read adapter (docs/broker-contracts.md, ADR 0009, 0010).

Each operation builds every argument itself: the configured site (`cloudId`),
Jira field keys mapped from the policy's allow-listed names, CQL from a fixed
template, and fixed `detail` and format settings. Results are filtered to the
allow-listed fields before anyone else sees them. `executeRead`, `discover`,
and Jira search are never called (a contract test checks this source file).

Outcomes are classified by `gateway_errors.classify`, and each class maps to
one action:

- needs_consent, consent_declined, consent_timeout, needs_reconnect,
  needs_signin, disconnect_pending, vendor_unusable: pause the run;
- unavailable, unexpected: retry a bounded number of times, then pause;
- config_error: fail the run;
- upstream_tool_error: refuse this operation; the run continues.

The response shapes parsed here follow the pinned tool snapshot. The exact
shapes of real responses, page versions included, are verified in the
connected step S15 (gaps G7 and G8); until then an unrecognized shape fails
the run instead of being guessed at.
"""

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, NoReturn

from pydantic import BaseModel

from stewardry.platform.atlassian.client import GatewayClient, ToolSuccess
from stewardry.platform.atlassian.gateway_errors import Classification, classify
from stewardry.platform.credentials import GatewayCredential
from stewardry.platform.execution import (
    CatalogLookup,
    ConfluenceListPagesArgs,
    ConfluenceReadPageArgs,
    HandlerStop,
    JiraGetRequestArgs,
    OperationRefused,
    RunContext,
    ToolSpec,
)
from stewardry.platform.policy import EffectiveOperation

# Gateway tool names (the gateway prefixes the upstream name with "atlassian_").
GET_JIRA_ISSUE = "atlassian_getJiraIssue"
SEARCH_CONFLUENCE = "atlassian_searchConfluence"
GET_CONFLUENCE_CONTENT = "atlassian_getConfluenceContent"
USER_INFO = "atlassian_atlassianUserInfo"
ACCESSIBLE_RESOURCES = "atlassian_getAccessibleAtlassianResources"
CONNECT = "connect_atlassian"
TOOLS_USED = (
    GET_JIRA_ISSUE,
    SEARCH_CONFLUENCE,
    GET_CONFLUENCE_CONTENT,
    USER_INFO,
    ACCESSIBLE_RESOURCES,
)

CLOUD_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SPACE_KEY = re.compile(r"^[A-Z][A-Z0-9]{1,31}$")
LIST_LIMIT = 100
# Jira's own field IDs that a policy may name directly; anything else needs a mapping.
JIRA_STANDARD_FIELDS = frozenset({"summary", "reporter", "created", "updated", "status", "labels"})

PAUSE_REASONS = {
    "needs_consent": "Atlassian is not connected for the acting user; they must connect it",
    "consent_declined": "the acting user declined to connect Atlassian",
    "consent_timeout": "connecting Atlassian was not finished in time",
    "needs_reconnect": "Atlassian rejected the acting user's connection; they must reconnect",
    "needs_signin": "the gateway did not accept the acting user's sign-in; they must sign in",
    "disconnect_pending": "a disconnect of the acting user's Atlassian account is pending",
    "vendor_unusable": "Atlassian is not usable for the acting user right now",
}


@dataclass(frozen=True)
class AtlassianSettings:
    cloud_id: str
    consent_url_prefix: str  # for example http://localhost:8600/v1/authorize/atlassian?txn=
    jira_field_keys: Mapping[str, str] = field(default_factory=dict)
    attempts: int = 3
    backoff_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not CLOUD_ID.fullmatch(self.cloud_id):
            raise ValueError("cloud_id must be the site's UUID")
        # The vendor name in the path is the broker registry's (`atlassian` on the
        # real stack, `mockhub-atlassian` on the stand-in), so it is configured.
        if "/v1/authorize/" not in self.consent_url_prefix or not self.consent_url_prefix.endswith(
            "?txn="
        ):
            raise ValueError("consent_url_prefix must be a broker authorize path ending in ?txn=")


@dataclass(frozen=True)
class ConsentLink:
    """Shown only to the user who asked to connect (ADR 0010)."""

    url: str = field(repr=False)


class ConnectRefused(Exception):
    pass


def _unwrap(value: Any, key: str) -> Any:
    if isinstance(value, Mapping) and set(value) == {key}:
        return value[key]
    return value


def _field_value(value: Any) -> Any:
    """A Jira user (such as `reporter`) is reduced to its display name. The
    full object carries the person's email address and account ID, which no
    stage needs."""
    if isinstance(value, Mapping) and "displayName" in value:
        return value["displayName"]
    return value


def _parse(outcome: ToolSuccess) -> Any:
    """The result value. Two wrappers are removed:

    - `{"result": …}`: MCP structured content must be an object on
      2025-11-25, so servers wrap other values (the stand-in stack, S14b);
    - `{"data": …}`: Atlassian's Rovo MCP server returns every result as text
      JSON in this envelope (observed on the connected site, S15).
    """
    if outcome.structured is not None:
        return _unwrap(_unwrap(outcome.structured, "result"), "data")
    try:
        return _unwrap(json.loads(outcome.text), "data")
    except json.JSONDecodeError:
        return None


class AtlassianReads:
    def __init__(
        self,
        client: GatewayClient,
        settings: AtlassianSettings,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.settings = settings
        self.sleep = sleep

    # -------------------------------------------------------------- calls

    def _stop(self, result: Classification) -> NoReturn:
        if result.cls == "config_error":
            raise HandlerStop(
                "the gateway could not hand off the user's identity "
                "(configuration problem; tell the platform engineer)",
                "fail",
            )
        if result.cls == "upstream_tool_error":
            raise OperationRefused("Atlassian refused the request")
        reason = PAUSE_REASONS.get(result.cls, "Atlassian is unavailable; try again later")
        raise HandlerStop(reason, "pause")

    def _call(
        self, credential: GatewayCredential | None, tool: str, arguments: Mapping[str, Any]
    ) -> Any:
        if credential is None:
            raise HandlerStop("the acting user's credential is not available", "pause")
        result = Classification("unexpected")
        for attempt in range(1, self.settings.attempts + 1):
            outcome = self.client.call_tool(credential, tool, arguments, interactive=False)
            result = classify(outcome, self.settings.consent_url_prefix)
            if result.cls == "ok" and isinstance(outcome, ToolSuccess):
                payload = _parse(outcome)
                if payload is not None:
                    return payload
                result = Classification("unexpected", "response was not JSON")
            if result.cls not in ("unavailable", "unexpected"):
                self._stop(result)
            if attempt < self.settings.attempts:
                self.sleep(self.settings.backoff_seconds * attempt)
        self._stop(result)

    def _base(self) -> dict[str, Any]:
        return {"cloudId": self.settings.cloud_id}

    # --------------------------------------------------------- operations

    def jira_get_request(
        self, credential: GatewayCredential | None, issue_key: str, field_names: Iterable[str]
    ) -> dict[str, Any]:
        """Only the allow-listed fields, by their policy names. A policy name is
        sent as a Jira field only if it is a standard field or mapped to a
        custom field; an unmapped custom name (such as `customer` on a site
        without that field) is not requested and reads as None."""
        names = sorted(set(field_names))
        keys = {
            name: self.settings.jira_field_keys.get(name, name)
            for name in names
            if name in self.settings.jira_field_keys or name in JIRA_STANDARD_FIELDS
        }
        payload = self._call(
            credential,
            GET_JIRA_ISSUE,
            self._base()
            | {
                "issueIdOrKey": issue_key,
                "fields": sorted(set(keys.values())),
                "fieldsByKeys": True,
                # The default "compact" view returned only `summary` on the real
                # site (S15). `fields` still limits what Jira fetches.
                "view": "full",
                "responseContentFormat": "markdown",
            },
        )
        fields = payload.get("fields") if isinstance(payload, Mapping) else None
        if not isinstance(fields, Mapping):
            raise HandlerStop("unexpected Jira response shape (see gap G7)", "fail")
        return {
            "issue_key": issue_key,
            "fields": {
                name: _field_value(fields.get(keys[name])) if name in keys else None
                for name in names
            },
        }

    def confluence_list_pages(
        self, credential: GatewayCredential | None, space: str, allowed_page_ids: frozenset[str]
    ) -> dict[str, Any]:
        """Catalog pages in one space, with version metadata where returned."""
        if not SPACE_KEY.fullmatch(space):
            raise OperationRefused("invalid space key")
        payload = self._call(
            credential,
            SEARCH_CONFLUENCE,
            self._base()
            | {
                "cql": f'type = page AND space = "{space}"',
                "expand": "version",
                "limit": LIST_LIMIT,
            },
        )
        results = payload.get("results") if isinstance(payload, Mapping) else None
        if not isinstance(results, list):
            raise HandlerStop("unexpected Confluence search response shape (see gap G7)", "fail")
        pages = []
        for item in results:
            content = item.get("content", item) if isinstance(item, Mapping) else {}
            page_id = str(content.get("id", ""))
            if page_id not in allowed_page_ids:
                continue  # never let pages outside the catalog through
            version = content.get("version")
            pages.append(
                {
                    "page_id": page_id,
                    "title": content.get("title"),
                    "version": version.get("number") if isinstance(version, Mapping) else None,
                }
            )
        truncated = len(results) >= LIST_LIMIT
        return {"space": space, "pages": pages, "truncated": truncated}

    def confluence_read_page(
        self, credential: GatewayCredential | None, page_id: str
    ) -> dict[str, Any]:
        """The full published body, not a summary (spec 4.3.1)."""
        payload = self._call(
            credential,
            GET_CONFLUENCE_CONTENT,
            self._base()
            | {
                "content_id": page_id,
                "detail": "full",
                "content_format": "html",
            },
        )
        if not isinstance(payload, Mapping):
            raise HandlerStop("unexpected Confluence page response shape (see gap G7)", "fail")
        body = payload.get("body")
        if isinstance(body, Mapping):
            body = body.get("value")
        # Atlassian's server reports the version under metadata (S15, gap G7).
        metadata = payload.get("metadata")
        version = payload.get("version") or (
            metadata.get("version") if isinstance(metadata, Mapping) else None
        )
        return {
            "page_id": str(payload.get("id", page_id)),
            "title": payload.get("title"),
            "version": version.get("number") if isinstance(version, Mapping) else version,
            "body_html": body if isinstance(body, str) else None,
        }

    def accessible_site_ids(self, credential: GatewayCredential | None) -> frozenset[str]:
        payload = self._call(credential, ACCESSIBLE_RESOURCES, {})
        # A list of sites with `id` (the stand-in), or {"resources": [...]} with
        # `cloudId` (Atlassian's server, S15).
        if isinstance(payload, Mapping):
            payload = payload.get("resources")
        items = payload if isinstance(payload, list) else []
        return frozenset(
            str(i.get("cloudId") or i.get("id"))
            for i in items
            if isinstance(i, Mapping) and (i.get("cloudId") or i.get("id"))
        )

    def account_id(self, credential: GatewayCredential | None) -> str | None:
        payload = self._call(credential, USER_INFO, {})
        if not isinstance(payload, Mapping):
            return None
        # Atlassian's REST API spells it accountId; the stand-in uses account_id.
        # The real MCP server's spelling is confirmed in S15 (G11).
        value = payload.get("accountId") or payload.get("account_id")
        return str(value) if value else None

    # ------------------------------------------------------------- consent

    def connect(self, credential: GatewayCredential) -> ConsentLink | None:
        """The user's own "Connect Atlassian" action. Returns the consent link to
        show that user, or None if already connected. Never runs in a background run."""
        outcome = self.client.call_tool(credential, CONNECT, {}, interactive=True)
        result = classify(outcome, self.settings.consent_url_prefix)
        if result.cls == "ok":
            return None
        if result.cls == "needs_consent" and result.consent_url is not None:
            return ConsentLink(result.consent_url)
        raise ConnectRefused(
            PAUSE_REASONS.get(result.cls, "Atlassian is unavailable; try again later")
        )

    # ---------------------------------------------------- executor wiring

    def tool_specs(self, catalogs: CatalogLookup) -> dict[str, ToolSpec]:
        """Handlers for the executor's three Atlassian operations."""

        def fields_of(op: EffectiveOperation | None) -> frozenset[str]:
            return op.fields if op is not None else frozenset()

        def jira(
            run: RunContext,
            args: BaseModel,
            cred: GatewayCredential | None,
            op: EffectiveOperation | None,
        ) -> Any:
            assert isinstance(args, JiraGetRequestArgs)  # noqa: S101
            return self.jira_get_request(cred, args.issue_key, fields_of(op))

        def list_pages(
            run: RunContext,
            args: BaseModel,
            cred: GatewayCredential | None,
            op: EffectiveOperation | None,
        ) -> Any:
            assert isinstance(args, ConfluenceListPagesArgs)  # noqa: S101
            refs = op.catalogs if op is not None else frozenset()
            allowed = frozenset().union(*(catalogs(ref) for ref in refs))
            return self.confluence_list_pages(cred, args.space, allowed)

        def read_page(
            run: RunContext,
            args: BaseModel,
            cred: GatewayCredential | None,
            op: EffectiveOperation | None,
        ) -> Any:
            assert isinstance(args, ConfluenceReadPageArgs)  # noqa: S101
            return self.confluence_read_page(cred, args.page_id)

        return {
            "jira.get_request": ToolSpec("jira.get_request", "operation", JiraGetRequestArgs, jira),
            "confluence.list_pages": ToolSpec(
                "confluence.list_pages", "operation", ConfluenceListPagesArgs, list_pages
            ),
            "confluence.read_page": ToolSpec(
                "confluence.read_page", "operation", ConfluenceReadPageArgs, read_page
            ),
        }
