"""A minimal browser for the stack tier: follows redirects across the local
stack's ports and keeps cookies per host, sending them even when Keycloak
marks them Secure on http://localhost (a real browser treats localhost as a
secure context; HTTP client libraries do not)."""

import html
import re
from http.cookies import SimpleCookie

import httpx2


class Browser:
    def __init__(self) -> None:
        self.cookies: dict[str, dict[str, str]] = {}
        self.http = httpx2.Client(timeout=30)

    def close(self) -> None:
        self.http.close()

    def _store(self, response: httpx2.Response) -> None:
        jar = self.cookies.setdefault(response.url.host, {})
        for header in response.headers.get_list("set-cookie"):
            parsed = SimpleCookie()
            parsed.load(header)
            for name, morsel in parsed.items():
                jar[name] = morsel.value

    def request(
        self, method: str, url: str, data: dict[str, str] | None = None, follow: bool = True
    ) -> httpx2.Response:
        while True:
            host = httpx2.URL(url).host
            cookie = "; ".join(f"{k}={v}" for k, v in self.cookies.get(host, {}).items())
            response = self.http.request(
                method, url, data=data, headers={"Cookie": cookie} if cookie else {}
            )
            self._store(response)
            if not follow or response.status_code not in (301, 302, 303, 307, 308):
                return response
            url = str(response.url.join(response.headers["location"]))
            if response.status_code in (301, 302, 303):
                method, data = "GET", None

    def login(self, page: httpx2.Response, username: str, password: str) -> httpx2.Response:
        """Submit Keycloak's login form; returns the (unfollowed) response."""
        action = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', page.text)
        assert action, "no Keycloak login form"
        return self.request(
            "POST",
            html.unescape(action[1]),
            {"username": username, "password": password, "credentialId": ""},
            follow=False,
        )
