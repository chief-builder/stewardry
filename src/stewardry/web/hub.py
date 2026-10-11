"""OIDC client for the existing hub (ADR 0006): authorization code with PKCE.

Stewardry is a confidential client. It asks for `openid mcp-gateway`, so the
access token's audience is the broker's MCP gateway; that token is the user's
gateway-scoped credential (spec section 4.3.2). The ID token establishes the
Stewardry session and is verified here: issuer, audience, nonce, expiry, and
an allowed signature algorithm (PS256 or ES256, never RS256 or HMAC, matching
the broker's policy). Group claims are not read.

Errors are kept apart on purpose: an unreachable hub (`HubUnavailable`) is not
a refused sign-in (`HubRejected`, `TokenInvalid`).
"""

import base64
import datetime as dt
import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx2
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

ALGORITHMS = ("PS256", "ES256")
CLOCK_SKEW = 30  # seconds, as the broker allows


class HubError(Exception):
    pass


class HubUnavailable(HubError):
    """The hub could not be reached or answered 5xx. Retry; never 'refused'."""


class HubRejected(HubError):
    """The hub refused a request (4xx), for example an expired refresh token."""


class TokenInvalid(HubError):
    """An ID token failed verification."""


@dataclass(frozen=True)
class TokenSet:
    access_token: str = field(repr=False)
    expires_at: dt.datetime
    refresh_token: str | None = field(default=None, repr=False)
    id_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Pkce:
    verifier: str = field(repr=False)

    @classmethod
    def new(cls) -> Pkce:
        return cls(secrets.token_urlsafe(48))

    @property
    def challenge(self) -> str:
        digest = hashlib.sha256(self.verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class HubClient:
    def __init__(
        self,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        http: httpx2.Client,
        scope: str = "openid mcp-gateway",
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scope = scope
        self._http = http
        self._metadata: dict[str, Any] | None = None
        self._keys: KeySet | None = None

    def __repr__(self) -> str:
        return f"HubClient(issuer={self.issuer!r}, client_id={self.client_id!r})"

    # ----------------------------------------------------------- transport

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx2.Response:
        try:
            response = self._http.request(method, url, **kwargs)
        except httpx2.HTTPError as exc:
            raise HubUnavailable(f"hub unreachable ({type(exc).__name__})") from exc
        if response.status_code >= 500:
            raise HubUnavailable(f"hub answered {response.status_code}")
        return response

    def metadata(self) -> dict[str, Any]:
        if self._metadata is None:
            response = self._request("GET", f"{self.issuer}/.well-known/openid-configuration")
            if response.status_code != 200:
                raise HubRejected(f"discovery answered {response.status_code}")
            data = response.json()
            if data.get("issuer") != self.issuer:
                raise HubRejected("discovery issuer does not match the configured issuer")
            self._metadata = data
        return self._metadata

    def _key_set(self, refresh: bool = False) -> KeySet:
        if self._keys is None or refresh:
            response = self._request("GET", self.metadata()["jwks_uri"])
            if response.status_code != 200:
                raise HubRejected(f"JWKS answered {response.status_code}")
            self._keys = KeySet.import_key_set(response.json())
        return self._keys

    # --------------------------------------------------------------- flow

    def authorization_url(self, state: str, nonce: str, pkce: Pkce) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": self.scope,
                "state": state,
                "nonce": nonce,
                "code_challenge": pkce.challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.metadata()['authorization_endpoint']}?{query}"

    def _token_request(self, form: dict[str, str]) -> TokenSet:
        response = self._request(
            "POST",
            self.metadata()["token_endpoint"],
            data=form,
            auth=(self.client_id, self._client_secret),
        )
        if response.status_code != 200:
            error = response.json().get("error", "") if response.content else ""
            raise HubRejected(f"token endpoint answered {response.status_code} {error}".strip())
        body = response.json()
        expires_in = int(body.get("expires_in", 0))
        return TokenSet(
            access_token=body["access_token"],
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=expires_in),
            refresh_token=body.get("refresh_token"),
            id_token=body.get("id_token"),
        )

    def exchange_code(self, code: str, pkce: Pkce) -> TokenSet:
        return self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": pkce.verifier,
            }
        )

    def refresh(self, refresh_token: str) -> TokenSet:
        return self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def verify_id_token(self, id_token: str, nonce: str) -> dict[str, Any]:
        """Verify signature and claims; return the claims. Raises TokenInvalid."""
        try:
            try:
                token = jwt.decode(id_token, self._key_set(), algorithms=list(ALGORITHMS))
            except JoseError:
                # A key the hub rotated in: refetch the key set once, then decide.
                token = jwt.decode(
                    id_token, self._key_set(refresh=True), algorithms=list(ALGORITHMS)
                )
            jwt.JWTClaimsRegistry(
                leeway=CLOCK_SKEW,
                iss={"essential": True, "value": self.issuer},
                aud={"essential": True, "value": self.client_id},
                nonce={"essential": True, "value": nonce},
                sub={"essential": True},
                exp={"essential": True},
                iat={"essential": True},
            ).validate(token.claims)
        except (JoseError, ValueError) as exc:
            raise TokenInvalid(f"ID token rejected ({type(exc).__name__})") from exc
        claims = dict(token.claims)
        if claims.get("azp", self.client_id) != self.client_id:
            raise TokenInvalid("ID token was issued to another client")
        return claims
