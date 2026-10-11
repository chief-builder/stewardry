"""An in-process OIDC hub for offline tests (Milestone 1, S4b).

It signs ID tokens with PS256 like the broker's Keycloak realm, checks client
authentication, single-use codes, the redirect URI, and PKCE, and supports
refresh. Switches let tests inject faults: outages, other signing keys,
other algorithms, wrong claims, and refused refreshes.
"""

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
from joserfc import jwt
from joserfc.jwk import ECKey, RSAKey

ISSUER = "http://hub.test/realms/mcp"
CLIENT_ID = "stewardry"
CLIENT_SECRET = "test-client-secret-for-fake-hub"
REDIRECT_URI = "http://localhost:8700/auth/callback"


@dataclass
class Grant:
    sub: str
    username: str
    nonce: str
    challenge: str
    redirect_uri: str
    groups: list[str]


@dataclass
class FakeHub:
    key: RSAKey = field(
        default_factory=lambda: RSAKey.generate_key(2048, parameters={"kid": "hub-1"})
    )
    codes: dict[str, Grant] = field(default_factory=dict)
    refresh_tokens: dict[str, Grant] = field(default_factory=dict)
    access_lifetime: int = 300
    # Fault injection
    down: bool = False
    refuse_refresh: bool = False
    sign_with: Any = None  # another key, to forge a signature
    algorithm: str = "PS256"
    claim_overrides: dict[str, Any] = field(default_factory=dict)
    token_requests: int = 0

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    # ------------------------------------------------- "the user signs in"

    def approve(
        self, authorize_url: str, sub: str, username: str, groups: list[str] | None = None
    ) -> tuple[str, str]:
        """Simulate the browser step; return (code, state) for the callback."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        assert query["client_id"] == CLIENT_ID and query["code_challenge_method"] == "S256"
        assert "openid" in query["scope"].split() and "mcp-gateway" in query["scope"].split()
        code = secrets.token_urlsafe(16)
        self.codes[code] = Grant(
            sub,
            username,
            query["nonce"],
            query["code_challenge"],
            query["redirect_uri"],
            groups or [],
        )
        return code, query["state"]

    # ----------------------------------------------------------- endpoints

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if self.down:
            return httpx2.Response(503)
        path = request.url.path
        if path == "/realms/mcp/.well-known/openid-configuration":
            return httpx2.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
                    "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
                    "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
                    "id_token_signing_alg_values_supported": ["PS256"],
                },
            )
        if path == "/realms/mcp/protocol/openid-connect/certs":
            public = self.key.as_dict(private=False) | {"alg": self.algorithm, "use": "sig"}
            return httpx2.Response(200, json={"keys": [public]})
        if path == "/realms/mcp/protocol/openid-connect/token":
            return self._token(request)
        return httpx2.Response(404)

    def _token(self, request: httpx2.Request) -> httpx2.Response:
        self.token_requests += 1
        expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if request.headers.get("authorization") != f"Basic {expected}":
            return httpx2.Response(401, json={"error": "invalid_client"})
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("grant_type") == "authorization_code":
            grant = self.codes.pop(form.get("code", ""), None)  # single use
            if grant is None or form.get("redirect_uri") != grant.redirect_uri:
                return httpx2.Response(400, json={"error": "invalid_grant"})
            digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != grant.challenge:
                return httpx2.Response(400, json={"error": "invalid_grant"})
            return self._issue(grant, with_id_token=True)
        if form.get("grant_type") == "refresh_token":
            grant = self.refresh_tokens.pop(form.get("refresh_token", ""), None)  # rotates
            if grant is None or self.refuse_refresh:
                return httpx2.Response(400, json={"error": "invalid_grant"})
            return self._issue(grant, with_id_token=False)
        return httpx2.Response(400, json={"error": "unsupported_grant_type"})

    def _issue(self, grant: Grant, with_id_token: bool) -> httpx2.Response:
        refresh = secrets.token_urlsafe(24)
        self.refresh_tokens[refresh] = grant
        body: dict[str, Any] = {
            "access_token": f"gateway-access-{secrets.token_urlsafe(16)}",
            "token_type": "Bearer",
            "expires_in": self.access_lifetime,
            "refresh_token": refresh,
        }
        if with_id_token:
            now = int(time.time())
            claims = {
                "iss": ISSUER,
                "aud": CLIENT_ID,
                "azp": CLIENT_ID,
                "sub": grant.sub,
                "preferred_username": grant.username,
                "nonce": grant.nonce,
                "iat": now,
                "exp": now + 300,
                "groups": grant.groups,
            } | self.claim_overrides
            claims = {k: v for k, v in claims.items() if v is not None}
            key = self.sign_with or self.key
            body["id_token"] = jwt.encode(
                {"alg": self.algorithm, "kid": "hub-1"}, claims, key, algorithms=[self.algorithm]
            )
        return httpx2.Response(
            200, content=json.dumps(body).encode(), headers={"content-type": "application/json"}
        )


def other_rsa_key() -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": "hub-1"})


def ec_key() -> ECKey:
    return ECKey.generate_key("P-256", parameters={"kid": "hub-1"})
