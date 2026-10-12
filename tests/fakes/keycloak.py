"""A small fake of the Keycloak endpoints `stewardry hub bootstrap` uses.

It starts with the broker's realm shape (clients, client scopes, alice and
bob with their fixed IDs; no credentials) and records every request.
`keep_ids=False` simulates a partial import that ignores supplied IDs.
"""

import copy
import json
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx2

BASE = "http://localhost:18180"
ISSUER = f"{BASE}/realms/mcp"
ADMIN_USER, ADMIN_PASSWORD = "admin", "fake-admin-password"
ADMIN_TOKEN = "fake-admin-token"

BROKER_CLIENTS = [
    {
        "id": "c1",
        "clientId": "mcp-demo-cli",
        "publicClient": True,
        "optionalClientScopes": ["mcp-gateway", "offline_access"],
    },
    {
        "id": "c2",
        "clientId": "mcp-gateway",
        "publicClient": False,
        "attributes": {"standard.token.exchange.enabled": "true"},
    },
    {
        "id": "c3",
        "clientId": "vtb-broker",
        "publicClient": False,
        "redirectUris": ["http://localhost:8600/v1/callback/_hub"],
    },
]
BROKER_SCOPES = [
    {"id": "s1", "name": "basic"},
    {"id": "s2", "name": "mcp-gateway"},
    {"id": "s3", "name": "hub-tier"},
]
BROKER_USERS = [
    {"id": "a11ce000-0000-4000-8000-000000000001", "username": "alice"},
    {"id": "b0b00000-0000-4000-8000-000000000002", "username": "bob"},
]


@dataclass
class FakeKeycloak:
    keep_ids: bool = True
    issuer: str = ISSUER
    clients: list[dict[str, Any]] = field(default_factory=lambda: copy.deepcopy(BROKER_CLIENTS))
    scopes: list[dict[str, Any]] = field(default_factory=lambda: copy.deepcopy(BROKER_SCOPES))
    users: list[dict[str, Any]] = field(default_factory=lambda: copy.deepcopy(BROKER_USERS))
    requests: list[tuple[str, str]] = field(default_factory=list)
    imported: list[dict[str, Any]] = field(default_factory=list)
    tamper_on_import: bool = False  # simulate a broker client changing mid-run

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if path == "/realms/master/protocol/openid-connect/token":
            form = parse_qs(request.content.decode())
            if form.get("password") != [ADMIN_PASSWORD] or form.get("username") != [ADMIN_USER]:
                return httpx2.Response(401, json={"error": "invalid_grant"})
            return httpx2.Response(200, json={"access_token": ADMIN_TOKEN})
        if path == "/realms/mcp/.well-known/openid-configuration":
            return httpx2.Response(200, json={"issuer": self.issuer})
        if request.headers.get("authorization") != f"Bearer {ADMIN_TOKEN}":
            return httpx2.Response(401)
        if path == "/admin/realms/mcp/clients" and request.method == "GET":
            return httpx2.Response(200, json=self.clients)
        if path == "/admin/realms/mcp/clients" and request.method == "POST":
            body = json.loads(request.content)
            self.clients.append(body | {"id": uuid.uuid4().hex})
            return httpx2.Response(201)
        if path == "/admin/realms/mcp/client-scopes" and request.method == "POST":
            self.scopes.append(json.loads(request.content) | {"id": uuid.uuid4().hex})
            return httpx2.Response(201)
        if path == "/admin/realms/mcp/client-scopes":
            return httpx2.Response(200, json=self.scopes)
        if path == "/admin/realms/mcp/users":
            name = request.url.params.get("username")
            return httpx2.Response(200, json=[u for u in self.users if u["username"] == name])
        if path == "/admin/realms/mcp/partialImport":
            body = json.loads(request.content)
            for user in body["users"]:
                self.imported.append(user)
                if any(u["username"] == user["username"] for u in self.users):
                    continue
                kept = user["id"] if self.keep_ids else str(uuid.uuid4())
                self.users.append({"id": kept, "username": user["username"]})
            if self.tamper_on_import:
                self.clients[0]["redirectUris"] = ["http://evil.test/callback"]
            return httpx2.Response(200, json={"added": len(body["users"])})
        return httpx2.Response(404)

    def writes(self) -> list[tuple[str, str]]:
        return [
            r
            for r in self.requests
            if r[0] in ("POST", "PUT", "DELETE", "PATCH") and "openid-connect/token" not in r[1]
        ]
