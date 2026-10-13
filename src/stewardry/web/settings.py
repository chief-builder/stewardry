"""Web settings from the environment. Missing names are listed; values never are."""

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

REQUIRED = ("STEWARDRY_HUB_ISSUER", "STEWARDRY_HUB_CLIENT_SECRET")


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class WebSettings:
    public_url: str
    hub_issuer: str
    hub_client_id: str
    hub_client_secret: str = field(repr=False)
    hub_scope: str = "openid mcp-gateway"
    # Atlassian through the broker's gateway. Without a cloud ID, Atlassian is off.
    gateway_url: str = "http://localhost:8500/mcp"
    atlassian_cloud_id: str = ""
    consent_url_prefix: str = "http://localhost:8600/v1/authorize/atlassian?txn="

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}/auth/callback"

    @property
    def origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @classmethod
    def from_environment(cls) -> WebSettings:
        missing = [name for name in REQUIRED if not os.environ.get(name)]
        if missing:
            raise SettingsError("missing required settings: " + ", ".join(missing))
        return cls(
            public_url=os.environ.get("STEWARDRY_PUBLIC_URL", "http://localhost:8700").rstrip("/"),
            hub_issuer=os.environ["STEWARDRY_HUB_ISSUER"].rstrip("/"),
            hub_client_id=os.environ.get("STEWARDRY_HUB_CLIENT_ID", "stewardry"),
            hub_client_secret=os.environ["STEWARDRY_HUB_CLIENT_SECRET"],
            hub_scope=os.environ.get("STEWARDRY_HUB_SCOPE", "openid mcp-gateway"),
            gateway_url=os.environ.get("STEWARDRY_GATEWAY_URL", cls.gateway_url),
            atlassian_cloud_id=os.environ.get("STEWARDRY_ATLASSIAN_CLOUD_ID", ""),
            consent_url_prefix=os.environ.get(
                "STEWARDRY_CONSENT_URL_PREFIX", cls.consent_url_prefix
            ),
        )
